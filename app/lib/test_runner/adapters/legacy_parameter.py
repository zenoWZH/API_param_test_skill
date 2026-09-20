"""Frozen existing text presets with counted transport and unchanged validators.

Preparation is credential free. The domain handler retains the existing
parameter/tool/token/identity evaluators, while its restricted client proxy
routes each initial, follow-up and enabled token-count call through dispatch.
"""
from __future__ import annotations

import contextlib
import ast
import copy
import dataclasses
import hashlib
import random
import re
import secrets
from pathlib import Path

import requests

from lib.client import ChatResult, OpenAICompatibleClient, _normalized_interfaces
from lib.config import get_timeout_sec
from lib.deepseek_params import BuiltRequest
from lib.parameter_job_controls import make_parameter_execution
from lib.parameter_output_limit import enforce_parameter_test_output_limit
from lib.test_runner import CancellationContext, DispatchStopped, HandlerRegistry, IntegrityError, PlanValidationError, compile_plan, digest_json, execute_plan, validate_plan
from lib.test_runner.common import json_copy
from lib.test_runner.cancellation import response_deadline

HANDLER_ID = "legacy.parameter_profile.v1"
IDENTITY_HANDLER_ID = "legacy.identity_probe.v1"
FACTORY_ID = "legacy_parameter"
FACTORY_VERSION = "1"
METHOD_TRANSPORTS = {
    "chat_completion": "chat_completions", "openai_responses": "openai_responses",
    "claude_messages": "claude_messages", "gemini_generate_content": "gemini_generate_content",
    "fim_completion": "fim_completions",
}


def _runner(module=None):
    if module is not None:
        return module
    from scripts import param_test
    return param_test


def _source_digest(module):
    root = Path(module.__file__).resolve().parents[1]
    paths = [Path(module.__file__), *[root / "lib" / name for name in (
        "client.py", "deepseek_params.py", "profile_validation.py", "token_audit.py",
        "model_identity.py", "parameter_output_limit.py", "parameter_reference_policy.py",
        "reference_specs.py", "param_outcome.py", "credential_security.py",
        "config.py", "model_profile_catalog.py", "token_counter.py", "metrics.py",
        "parameter_job_controls.py", "gemini_schema_validation.py",
        "gemini_interactions_validation.py", "gemini_interactions_stream.py",
        "anthropic_compat_rejections.py", "gemini_api_version.py",
        "image_validation.py", "image_token_expectations.py",
    )]]
    return digest_json({str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})


def source_digest():
    """Public definition identity, excluding deployment-specific CLI entry code."""
    root = Path(__file__).resolve().parents[3]
    names = {"prepare_identity_probe_request", "prepare_one_profile_request"}
    tree = ast.parse((root / "scripts/param_test.py").read_text())
    definitions = {node.name: ast.dump(node, include_attributes=False) for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names}
    return digest_json({"definitions": definitions, "adapter": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})


def _config_snapshot(config, *, runs, mode):
    snapshot = copy.deepcopy(config)
    snapshot["_parameter_execution_controls"] = make_parameter_execution(runs, mode)
    return snapshot


def _config_digest(config, *, provider, model):
    # JobSpec's v1/v2 envelope is separately frozen; only its resolved mode/runs
    # affect this domain adapter. Never place config or credentials in a plan.
    snapshot = copy.deepcopy(config)
    snapshot.pop("_parameter_execution_controls", None)
    snapshot.pop("_test_execution_plan", None)
    snapshot.pop("_parameter_identity_snapshot", None)
    snapshot.pop("_functional_parameter_target", None)
    snapshot.pop("_parameter_test_exact_input", None)
    # Normalize only aliases of the explicitly selected target. Provider route,
    # authentication, timeout and request-building configuration remain hashed.
    selected = snapshot["providers"][provider]
    snapshot["active_provider"] = provider
    snapshot.setdefault("models", {})["default"] = model
    selected.setdefault("models", {})["default"] = model
    if selected["models"].get("candidates"):
        snapshot["models"]["candidates"] = copy.deepcopy(selected["models"]["candidates"])
    if selected.get("base_url"):
        snapshot.setdefault("api", {})["base_url"] = str(selected["base_url"]).rstrip("/")
    return digest_json(snapshot)


def _token_count_enabled(interfaces, transport):
    interface = interfaces.get("token_count") if isinstance(interfaces, dict) else None
    if not isinstance(interface, dict) or interface.get("enabled", True) is False:
        return False
    supported = interface.get("transports")
    return not isinstance(supported, list) or transport in supported


class _ClientProxy:
    def __init__(self, context, *, built, model, counter_enabled):
        self._context = context
        self._built = built
        self._model = model
        self._counter_enabled = counter_enabled
        self._generation_calls = 0

    def __getattr__(self, name):
        if name in METHOD_TRANSPORTS:
            def call(*args, **kwargs):
                if name == "gemini_generate_content":
                    if len(args) != 2 or args[0] != self._model:
                        raise IntegrityError("Native request model differs from the frozen target")
                    body = args[1]
                else:
                    if len(args) != 1:
                        raise PlanValidationError("Unexpected legacy client call arguments")
                    body = args[0]
                if set(kwargs) - {"headers"}:
                    raise PlanValidationError("Unsupported client request option")
                transport = METHOD_TRANSPORTS[name]
                if transport != self._built.metadata.get("transport", "chat_completions"):
                    raise IntegrityError("Legacy transport differs from the frozen request")
                if self._generation_calls == 0 and digest_json(body) != digest_json(self._built.body):
                    raise IntegrityError("Initial parameter body differs from its compiled request")
                if self._generation_calls > 0 and self._built.metadata.get("multi_turn") is not True:
                    raise IntegrityError("Unplanned follow-up request")
                if self._generation_calls >= 2:
                    raise IntegrityError("Legacy parameter handler exceeded initial/follow-up scope")
                outgoing = {"operation": name, "transport": transport, "model": self._model, "body": json_copy(body)}
                if kwargs.get("headers") is not None:
                    outgoing["headers"] = json_copy(kwargs["headers"])
                if self._generation_calls == 0:
                    frozen_headers = self._built.metadata.get("request_headers")
                    if (outgoing.get("headers") or {}) != (frozen_headers if isinstance(frozen_headers, dict) else {}):
                        raise IntegrityError("Request headers differ from frozen metadata")
                self._generation_calls += 1
                receipt = self._context.dispatch(outgoing, kind="api")
                # Preserve the prior mutation audit even though transport gets
                # its own JSON copy at the shared boundary.
                if receipt.get("observed_body") is not None:
                    body.clear()
                    body.update(json_copy(receipt["observed_body"]))
                return ChatResult(**receipt["chat_result"])
            return call
        if name in {"_gemini_native_url", "_transport_url"}:
            dispatcher = self._context._dispatcher
            client = getattr(dispatcher, "client", None)
            method = getattr(client, name, None)
            if callable(method):
                return method  # Existing trusted pure URL construction, no IO.
        raise AttributeError(name)

    def count_tokens(self, transport, model, body):
        if not self._counter_enabled:
            return None
        if transport != self._built.metadata.get("transport", "chat_completions") or model != self._model:
            raise IntegrityError("Token count target differs from the frozen request")
        receipt = self._context.dispatch({"operation": "count_tokens", "transport": transport, "model": model, "body": json_copy(body)}, kind="count_tokens")
        if receipt.get("observed_body") is not None:
            body.clear()
            body.update(json_copy(receipt["observed_body"]))
        return receipt.get("count_result")


class _SingleSendSession:
    """Allow exactly one HTTP send inside each already-counted client operation."""
    def __init__(self, session, timeout):
        self._session = session
        self._timeout = timeout
        self.sends = 0
        self.status_code = None

    def __getattr__(self, name):
        if name in {"post", "get", "put", "delete", "patch", "head"}:
            return lambda url, **kwargs: self.request(name.upper(), url, **kwargs)
        if name in {"headers", "cookies"}:
            return getattr(self._session, name)
        raise AttributeError(name)

    def request(self, method, url, **kwargs):
        if self.sends:
            raise IntegrityError("A legacy client operation attempted an uncounted additional HTTP send")
        self.sends += 1
        kwargs["allow_redirects"] = False
        kwargs["timeout"] = self._timeout
        response = self._session.request(method, url, **kwargs)
        self.status_code = response.status_code
        return response


class LegacyClientDispatcher:
    """Inject the existing protected client; no credentials are copied into JSON."""
    def __init__(self, client):
        self.client = client

    def __call__(self, request, *, timeout, context):
        operation = request.get("operation")
        if operation not in {*METHOD_TRANSPORTS, "count_tokens"}:
            raise PlanValidationError("Unregistered legacy client operation")
        target = context.target
        if request.get("model") != target["model"]:
            raise IntegrityError("Client request model differs from frozen target")
        transport = request.get("transport")
        if operation != "count_tokens" and METHOD_TRANSPORTS[operation] != transport:
            raise IntegrityError("Client operation and transport differ")
        if operation == "count_tokens" and not _token_count_enabled(getattr(self.client, "api_interfaces", {}), transport):
            raise PlanValidationError("Token count interface is not enabled")
        body = json_copy(request["body"])
        if operation != "count_tokens":
            checked = copy.deepcopy(body)
            enforce_parameter_test_output_limit(checked, transport)
            if digest_json(checked) != digest_json(body):
                raise IntegrityError("Frozen parameter request violates its explicit output allowance")
        real_client = isinstance(self.client, OpenAICompatibleClient)
        old_timeout = getattr(self.client, "timeout_sec", None)
        old_session = getattr(self.client, "session", None)
        guarded = None
        changed_adapters = []
        old_trust = None
        deadline = contextlib.nullcontext()
        if real_client:
            deadline = response_deadline(timeout)
            if not isinstance(old_session, requests.Session):
                raise PlanValidationError("Production legacy transport requires a bounded requests.Session")
            if self.client.provider != target["provider"]:
                raise IntegrityError("Runtime credential provider differs from the frozen target")
            if digest_json(self.client.api_interfaces) != context._plan["definition"]["client_interfaces_digest"] or digest_json(self.client.base_url) != context._plan["definition"]["client_base_digest"]:
                raise IntegrityError("Runtime client routes differ from the frozen endpoint configuration")
            old_trust = old_session.trust_env
            old_session.trust_env = False
            for adapter in old_session.adapters.values():
                changed_adapters.append((adapter, adapter.max_retries))
                adapter.max_retries = requests.adapters.HTTPAdapter(max_retries=0).max_retries
            guarded = _SingleSendSession(old_session, timeout)
            self.client.session = guarded
            self.client.timeout_sec = timeout
        try:
            with deadline:
                if operation == "count_tokens":
                    result = self.client.count_tokens(transport, request["model"], body)
                    receipt = {"count_result": json_copy(result), "status_code": guarded.status_code if guarded else None}
                else:
                    method = getattr(self.client, operation)
                    if operation == "gemini_generate_content":
                        result = method(request["model"], body, headers=request.get("headers"))
                    else:
                        result = method(body)
                    if not isinstance(result, ChatResult):
                        raise PlanValidationError("Legacy client must return ChatResult")
                    receipt = {"chat_result": json_copy(dataclasses.asdict(result)), "status_code": result.status_code}
                receipt["observed_body"] = json_copy(body)
                return receipt
        finally:
            if real_client:
                self.client.session = old_session
                self.client.timeout_sec = old_timeout
                old_session.trust_env = old_trust
                for adapter, retries in changed_adapters:
                    adapter.max_retries = retries


def legacy_parameter_registry(config, *, runner_module=None, on_result=None):
    module = _runner(runner_module)
    runtime_config = copy.deepcopy(config)
    registered = HandlerRegistry()
    def execute_profile(context, inputs, *, identity_probe=False):
        definition = context._plan["definition"]
        target = context.target
        if definition.get("runtime_config_digest") != _config_digest(runtime_config, provider=target["provider"], model=target["model"]):
            raise IntegrityError("Legacy runtime configuration differs from the compiled plan")
        if module.get_model_route_profile(runtime_config, target["model"], target["provider"], route_profile=target["route_profile"]) != target["route_profile"]:
            raise IntegrityError("Runtime model route differs from the frozen target")
        if target["reference_source"] != inputs["reference_source"]:
            raise IntegrityError("Legacy reference identity differs from the compiled target")
        if len(inputs["variants"]) != context._plan["run_count"]:
            raise IntegrityError("Frozen parameter input variants do not match the run count")
        variant = inputs["variants"][context.run_index - 1]
        built = BuiltRequest(**json_copy(variant["prepared_request"]))
        config_for_run = _config_snapshot(runtime_config, runs=context._plan["run_count"], mode=definition["tool_validation_mode"])
        identity_snapshot = inputs["capability_profile"].get("model_profile_database")
        if isinstance(identity_snapshot, dict):
            config_for_run["_parameter_identity_snapshot"] = json_copy(identity_snapshot)
        proxy = _ClientProxy(context, built=copy.deepcopy(built), model=target["model"], counter_enabled=inputs["count_tokens"])
        if identity_probe:
            payload = module.run_identity_probe(config_for_run, proxy, target["provider"], target["model"], inputs["family"],
                                                inputs["reference_source"], inputs["reference_family"], prepared_request=built)
            payload["workflow_run_index"] = context.run_index
        else:
            payload = module.run_one_profile(
                config_for_run, proxy, target["provider"], target["model"], inputs["family"],
                inputs["reference_source"], inputs["reference_family"], inputs["profile"],
                context.run_index, json_copy(variant["input_sample"]), expectation=inputs["expectation"],
                capability_profile=json_copy(inputs["capability_profile"]), prepared_request=built,
                route_profile_override=target["route_profile"],
            )
        payload = json_copy(payload)
        if on_result is not None:
            on_result(json_copy(payload))
        passing = payload.get("overall_pass") is True
        identity = payload.get("model_identity_audit") or {}
        if identity.get("status") in {"mismatch", "suspicious"}:
            passing = False
        code = payload.get("status_code")
        accepted = passing and type(code) is int and 200 <= code <= 299
        # Ask the existing reviewed aggregation helper whether this exact failure
        # could be covered by a valid same-context pass; never alter the raw row.
        candidate = copy.deepcopy(payload)
        control = {**copy.deepcopy(payload), "status": "pass", "token_validation_pass": True}
        control.pop("satisfied_by_sibling_run", None)
        module.apply_any_run_success([candidate, control], profile=payload["profile"], mode="any")
        eligible = (not identity_probe and candidate.get("satisfied_by_sibling_run") is True
                    and candidate.get("overall_pass") is True and identity.get("status") not in {"mismatch", "suspicious"})
        from lib.param_outcome import _RUN_CONTEXT_FIELDS
        aggregation_key = digest_json([payload.get(field) for field in _RUN_CONTEXT_FIELDS])
        return {"status": "passed" if passing else "failed", "accepted": accepted,
                "aggregate_eligible": eligible, "aggregation_key": aggregation_key,
                "parameter_outcome": {"status": payload.get("compatibility_status"), "pass": payload.get("compatibility_pass")},
                "semantic_effect": {"status": payload.get("status"), "failure_classification": payload.get("failure_classification")},
                "token_audit": payload.get("token_audit"), "identity_audit": identity,
                "legacy_result": payload}
    def handle(context, inputs):
        return execute_profile(context, inputs)
    def identity(context, inputs):
        return execute_profile(context, inputs, identity_probe=True)
    registered.register(HANDLER_ID, handle, version="1", digest=lambda: _source_digest(module))
    registered.register(IDENTITY_HANDLER_ID, identity, version="1", digest=lambda: _source_digest(module))
    return registered


def prepare_legacy_parameter_plan(config, provider, model, family, reference_source, reference_family, *, runs=1, profiles=None, capability_profile=None, runner_module=None, sampling_seed=None, route_profile=None):
    """Compile actual finalized request bodies without clients or credentials."""
    module = _runner(runner_module)
    make_parameter_execution(runs, module._tool_validation_mode(config))
    if sampling_seed is None:
        sampling_seed = secrets.token_hex(16)
    if not isinstance(sampling_seed, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", sampling_seed) is None:
        raise PlanValidationError("sampling_seed must be a bounded visible identifier")
    available = module.reference_test_profiles(reference_source)
    if profiles is None:
        profiles = module._selected_param_profiles(available)
    elif not isinstance(profiles, list) or not profiles or any(profile not in available for profile in profiles) or len(set(profiles)) != len(profiles):
        raise PlanValidationError("Parameter selection is outside the enabled reference suite")
    else:
        profiles = [profile for profile in available if profile in profiles]
    modes = module._profile_run_success_modes(config, profiles)
    capability = capability_profile or module.capability_profile_snapshot("text", family, model, profiles, reference_source=reference_source)
    module._require_parameter_matrix_enabled(capability, family, model)
    reference = module.get_reference_source(reference_source)
    route = module.get_model_route_profile(config, model, provider, route_profile=route_profile)
    api_form = module.get_model_api_form(config, model, provider, route_profile=route, api_form=reference.get("api_form") or "openai_chat_completions")
    if api_form == "gemini_interactions":
        raise PlanValidationError("Ordinary Gemini Interactions remains disabled")
    provider_config = module.get_provider_config(config, provider)
    interfaces = provider_config.get("api_interfaces") or {}
    rng = random.Random(sampling_seed)
    capability = copy.deepcopy(capability)
    snapshot = capability.get("model_profile_database")
    if not isinstance(snapshot, dict) or not snapshot:
        binding = module.resolve_runtime_profile_binding(config, provider, model, family, route, api_form, modality="text")
        snapshot = module.database_snapshot(binding)
        capability["model_profile_database"] = json_copy(snapshot)
    identity = module.prepare_identity_probe_request(config, provider, model, family, reference_source, reference_family, route_profile_override=route)
    identity_transport = identity.metadata["transport"]
    identity_inputs = {"profile": "identity_probe", "family": family, "reference_source": reference_source,
                       "reference_family": reference_family, "capability_profile": json_copy(capability),
                       "variants": [{"prepared_request": json_copy(dataclasses.asdict(identity))} for _ in range(runs)],
                       "count_tokens": _token_count_enabled(interfaces, identity_transport)}
    steps = [{"id": "identity_probe", "case_id": "identity_probe", "handler": IDENTITY_HANDLER_ID, "inputs": identity_inputs,
              "request_cap": 2 if identity_inputs["count_tokens"] else 1, "cleanup_request_cap": 0}]
    for profile in profiles:
        expectation = module.resolve_profile_expectation("text", family, model, profile, capability_profile=capability, reference_source=reference_source)
        samples = module._sample_inputs_for_profile(config, profile, runs, rng)
        if len(samples) != runs:
            raise PlanValidationError("Every independent run requires one frozen input sample")
        variants = []
        for sample in samples:
            built, _ = module.prepare_one_profile_request(config, model, family, reference_source, profile, sample, expectation, capability_profile=capability,
                                                         api_form_override=api_form, provider_override=provider, execution_route_override=route)
            if built.metadata.get("provider") != provider or built.body.get("model", model) != model:
                raise IntegrityError("Prepared request provider/model differs from its explicit workflow target")
            variants.append({"input_sample": json_copy(sample), "prepared_request": json_copy(dataclasses.asdict(built))})
        transport = variants[0]["prepared_request"]["metadata"].get("transport", "chat_completions")
        if transport not in METHOD_TRANSPORTS.values():
            raise PlanValidationError(f"Unsupported legacy parameter transport: {transport}")
        steps.append({"id": profile, "case_id": profile, "handler": HANDLER_ID,
                      "request_cap": (2 if any(variant["prepared_request"]["metadata"].get("multi_turn") for variant in variants) else 1)
                                     * (2 if _token_count_enabled(interfaces, transport) else 1),
                      "cleanup_request_cap": 0,
                      "inputs": {"profile": profile, "family": family, "reference_source": reference_source,
                                 "reference_family": reference_family, "expectation": expectation,
                                 "capability_profile": json_copy(capability), "variants": variants,
                                 "count_tokens": _token_count_enabled(interfaces, transport)}})
    registry = legacy_parameter_registry(config, runner_module=module)
    definition = {"workflow_schema_version": 1, "id": f"legacy-parameter:{provider}:{model}:{reference_source}",
                  "factory": {"factory_id": FACTORY_ID, "version": FACTORY_VERSION, "source_sha256": source_digest()},
                  "target": {"provider": provider, "model": model, "route_profile": route, "api_form": api_form, "reference_source": reference_source,
                             "source_id": snapshot["source_id"], "profile_id": snapshot["profile_id"], "interface_id": snapshot["interface_id"], "contract_id": reference_source},
                  "model_profile_database": json_copy(snapshot),
                  "factory_arguments": {"profiles": profiles, "family": family, "reference_source": reference_source, "reference_family": reference_family,
                                        "sampling_seed": sampling_seed, "route_profile": route},
                  "cases": [{"id": "identity_probe", "success_policy": "all"}, *[{"id": profile, "success_policy": modes[profile]} for profile in profiles]], "steps": steps,
                  "runtime_config_digest": _config_digest(config, provider=provider, model=model), "tool_validation_mode": module._tool_validation_mode(config),
                  "client_interfaces_digest": digest_json(_normalized_interfaces(provider_config)),
                  "client_base_digest": digest_json(str(provider_config.get("base_url") or "").rstrip("/")),
                  "run_success_modes": modes,
                  "limits": {"max_requests": sum(step["request_cap"] for step in steps), "request_timeout_seconds": get_timeout_sec(config),
                             "deadline_seconds": max(3600, get_timeout_sec(config) * sum(step["request_cap"] for step in steps)), "cleanup_max_requests": 0}}
    return compile_plan(definition, registry, run_count=runs), registry


def execute_legacy_parameter_plan(plan, config, client, *, output_dir=None, runner_module=None, cancellation=None):
    module = _runner(runner_module)
    results = []
    identities = []
    total = len(plan["ordered_steps"]) * plan["run_count"]
    def completed(payload):
        profile = payload["profile"]
        if payload.get("identity_probe") is True:
            identities.append(payload)
        else:
            results.append(payload)
            module.apply_any_run_success(results, profile=profile, mode=plan["definition"]["run_success_modes"][profile])
        print(f"[{len(results) + len(identities)}/{total}] completed {plan['target']['reference_source']}:{profile} run={payload['run_index']}", flush=True)
        if output_dir is not None:
            module.write_json(Path(output_dir) / "param_results.json", results)
            module.write_json(Path(output_dir) / "token_audit.json", module.flatten_token_audits([*identities, *results]))
            module._write_failed_cases(Path(output_dir), results)
    registered = legacy_parameter_registry(config, runner_module=module, on_result=completed)
    frozen = validate_plan(plan, registered)
    if frozen["definition"].get("runtime_config_digest") != _config_digest(config, provider=frozen["target"]["provider"], model=frozen["target"]["model"]):
        raise IntegrityError("Legacy runtime configuration differs from the compiled plan")
    if output_dir is not None:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        module.write_json(Path(output_dir) / "test_workflow_plan.json", frozen)
    cancel = cancellation or CancellationContext()
    with cancel.install_signal_handlers():
        report = execute_plan(frozen, registered, LegacyClientDispatcher(client), cancellation=cancel,
                              evidence_dir=Path(output_dir) / "workflow_runs" if output_dir is not None else None)
    report["identity_probes"] = json_copy(identities)
    report["identity_probe"] = json_copy(identities[0]) if identities else None
    report["legacy_results"] = json_copy(results)
    if output_dir is not None:
        module.write_json(Path(output_dir) / "test_workflow_report.json", report)
    # Keep the legacy interruption contract after the shared core has completed cleanup.
    if report["status"] == "cancelled":
        raise KeyboardInterrupt(cancel.reason or "parameter workflow cancelled")
    return results, report


def run_legacy_parameter_tests(config, client, provider, model, family, reference_source, reference_family, *, runs=1, output_dir=None, capability_profile=None, runner_module=None, execution_plan=None, workflow_report=None):
    plan = execution_plan if execution_plan is not None else config.get("_test_execution_plan")
    controls = config.get("_parameter_execution_controls") or {}
    if plan is None:
        if controls.get("schema_version") == 2:
            raise PlanValidationError("Frozen workflow job requires its original execution plan")
        plan, _ = prepare_legacy_parameter_plan(config, provider, model, family, reference_source, reference_family,
                                               runs=runs, capability_profile=capability_profile, runner_module=runner_module)
    else:
        plan = validate_plan(plan)
        if any(plan["target"].get(key) != value for key, value in {"provider": provider, "model": model, "reference_source": reference_source}.items()) or plan["run_count"] != runs:
            raise IntegrityError("Legacy entrypoint target or run count differs from its frozen plan")
        if controls.get("plan_digest") not in (None, plan["plan_digest"]):
            raise IntegrityError("Legacy entrypoint received a different frozen plan")
    results, report = execute_legacy_parameter_plan(plan, config, client, output_dir=output_dir, runner_module=runner_module)
    if workflow_report is not None:
        workflow_report.update(json_copy(report))
    expected_profiles = sum(step["handler"] == HANDLER_ID for step in plan["ordered_steps"]) * plan["run_count"]
    if len(results) != expected_profiles or any(run.get("fatal_reason") for run in report["runs"]):
        raise DispatchStopped("Parameter workflow stopped before completing its frozen suite; inspect test_workflow_report.json")
    return results
