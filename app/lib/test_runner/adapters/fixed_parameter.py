"""Source-owned fixed parameter suites through individual counted workflow steps.

The reviewed MPDB snapshots and domain adjudicators remain authoritative. A
native observation is separate from its semantic assertion, so a documented
failed effect does not erase an otherwise usable control response. Every wire
request is one dispatch; negative rejections never become accepted responses.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import time
import uuid

from lib import deepseek_beta_reference as beta
from lib import deepseek_fim_reference as fim
from lib import anthropic_prefill_reference as prefill
from lib import anthropic_cache_reference as cache
from lib.parameter_output_limit import enforce_parameter_test_output_limit

from .. import CancellationContext, HandlerRegistry, IntegrityError, compile_plan, execute_plan, validate_plan
from ..cancellation import response_deadline
from ..common import digest_json

FACTORY_ID = "source_fixed_parameter"
FACTORY_VERSION = "1"
TIMEOUT_SECONDS = 150
_KIND_TYPES = {"beta": beta.DeepSeekBetaReferencePlan, "fim_causal": fim.FimPlan,
               "anthropic_prefill": prefill.PrefillPlan, "anthropic_cache": cache.CachePlan}
_DOMAINS = {"beta": beta, "fim_causal": fim, "anthropic_prefill": prefill, "anthropic_cache": cache}
_EVALUATE = {"beta": beta.evaluate_deepseek_beta_reference, "fim_causal": fim.evaluate_fim_plan,
             "anthropic_prefill": prefill.evaluate_prefill_plan, "anthropic_cache": cache.evaluate_cache_plan}
_NEXT = {"beta": beta.next_deepseek_beta_request, "fim_causal": fim.next_fim_request,
         "anthropic_prefill": prefill.next_prefill_request, "anthropic_cache": cache.next_cache_request}
_PURE_FILES = ("deepseek_beta_reference.py", "deepseek_fim_reference.py", "anthropic_prefill_reference.py",
               "anthropic_cache_reference.py", "fixed_parameter_specs.py")
_FATAL_CODES = [*range(300, 400), 401, 402, 403, 404, 408, 429, *range(500, 600)]


def source_digest():
    root = Path(__file__).resolve().parents[2]
    return digest_json({"adapter": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       **{name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in _PURE_FILES}})


def execution_digest():
    root = Path(__file__).resolve().parents[2]
    names = (*_PURE_FILES, "token_audit.py", "cache_acceptance.py", "parameter_output_limit.py",
             "credential_security.py", "model_profile_catalog.py", "token_counter.py",
             "client.py", "model_identity.py", "deepseek_params.py", "test_runner/fixed_results.py", "test_runner/fixed_entry.py", "test_runner/fixed_service.py", "config.py")
    return digest_json({"factory": source_digest(),
                        "parameter_cli": hashlib.sha256((root.parent / "scripts/param_test.py").read_bytes()).hexdigest(),
                        **{name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}})


def _kind(plan):
    for kind, cls in _KIND_TYPES.items():
        if type(plan) is cls:
            return kind
    raise ValueError("Fixed execution requires an exact immutable source plan, not a preview or subclass")


def _build_domain(snapshot, *, suite_id=None, endpoint=None, frozen_plan=None,
                  create_runtime_nonce=False, nonce_factory=None):
    if suite_id in cache.SUITE_IDS:
        return cache.build_cache_plan(snapshot, suite_id=suite_id, endpoint=endpoint or cache.ENDPOINT,
            frozen_plan=frozen_plan, create_runtime_nonce=create_runtime_nonce, nonce_factory=nonce_factory)
    if frozen_plan is not None or create_runtime_nonce or nonce_factory is not None:
        raise ValueError("Only a cache job may supply or create frozen nonces")
    if suite_id == fim.SUITE_ID:
        return fim.build_fim_plan(snapshot, suite_id=suite_id, endpoint=endpoint or fim.ENDPOINT)
    if suite_id in prefill.SUITE_IDS:
        return prefill.build_prefill_plan(snapshot, suite_id=suite_id, endpoint=endpoint or prefill.ENDPOINT)
    if suite_id is None and snapshot.get("api_form") == beta.API_FORM:
        return beta.build_deepseek_beta_reference_plan(snapshot, endpoint=endpoint or beta.ENDPOINT)
    raise ValueError("No executable fixed suite matches the exact source selection")


def _suite_id(plan):
    return fim.SUITE_ID if type(plan) is fim.FimPlan else getattr(plan, "suite_id", None)


def _descriptor(plan):
    kind = _kind(plan)
    return {"kind": kind, "snapshot": plan.snapshot, "endpoint": plan.endpoint,
            "suite_id": _suite_id(plan),
            "frozen_cache_plan": plan.frozen_payload if kind == "anthropic_cache" else None}


def _restore(value):
    if not isinstance(value, dict) or set(value) != {"kind", "snapshot", "endpoint", "suite_id", "frozen_cache_plan"}:
        raise ValueError("Incomplete fixed source descriptor")
    plan = _build_domain(value["snapshot"], suite_id=value["suite_id"], endpoint=value["endpoint"],
                         frozen_plan=value["frozen_cache_plan"])
    if _kind(plan) != value["kind"]:
        raise ValueError("Fixed source descriptor crosses suite kinds")
    return plan


def _boundary(plan, request):
    kind = _kind(plan)
    wire_kind = getattr(request, "kind", "generation")
    endpoint = getattr(request, "endpoint", plan.endpoint)
    expected = cache.COUNT_ENDPOINT if wire_kind == "count" and kind == "anthropic_cache" else plan.endpoint
    if wire_kind not in {"count", "generation"} or endpoint != expected or wire_kind == "count" and kind != "anthropic_cache":
        raise ValueError("Fixed request crosses its source endpoint or count boundary")
    return wire_kind, endpoint


def _negative(request):
    expectation = request.expectation
    return expectation == "unsupported" or isinstance(expectation, dict) and expectation.get("kind") == "attributed_rejection"


def _observe_id(index):
    return f"fixed.observe.{index + 1:02d}"


def _records(context):
    results = context.results
    records = []
    for index in range(context._plan["limits"]["max_requests"]):
        result = results.get(_observe_id(index))
        if not isinstance(result, dict) or not isinstance(result.get("record"), dict):
            break
        records.append(copy.deepcopy(result["record"]))
    return records


def _observe(context, inputs):
    if context._plan["run_count"] != 1:
        raise IntegrityError("Fixed source approval permits one complete experiment only")
    plan = _restore(context._plan["definition"]["fixed_parameter"])
    kind = _kind(plan)
    index = inputs["ordinal"]
    requests = plan.requests
    if type(index) is not int or not 0 <= index < len(requests) or context.step_id != _observe_id(index):
        raise IntegrityError("Fixed request ordinal differs from its frozen step")
    previous = _records(context)
    if len(previous) != index:
        return {"status": "blocked", "accepted": False, "reason": "source_sequence_missing_observation"}
    candidate = _NEXT[kind](plan, previous)
    if candidate is None:
        return {"status": "blocked", "accepted": False, "reason": "source_control_did_not_qualify"}
    item = requests[index]
    if candidate.case_id != item.case_id or candidate.body_bytes != item.body_bytes:
        raise IntegrityError("Fixed source next-request decision changed")
    wire_kind, endpoint = _boundary(plan, item)
    receipt = context.dispatch({"method": "POST", "url": endpoint, "case_id": item.case_id,
        "ordinal": index, "kind": wire_kind, "body": item.body, "body_sha256": item.body_sha256},
        kind="count_tokens" if wire_kind == "count" else "api")
    record = receipt["record"]
    evaluated = _EVALUATE[kind](plan, [*previous, record])
    row = evaluated["case_results"][-1]
    complete = record.get("response_complete") is True and not record.get("failure_type") and not record.get("interrupted")
    if record.get("interrupted"):
        context.cancel.request("fixed_source_interrupted")
    elif record.get("failure_type") or not complete:
        context._fatal_reason = "fixed_source_transport_incomplete"
    negative = _negative(item)
    valid = row.get("pass") is True if negative else row.get("response_valid") is True
    return {"status": "passed" if valid and complete else "failed", "accepted": bool(not negative and valid and complete),
            "record": record, "parameter_result": copy.deepcopy(row), "observation_valid": bool(valid and complete)}


def _assert_semantics(context, inputs):
    plan = _restore(context._plan["definition"]["fixed_parameter"])
    evaluated = _EVALUATE[_kind(plan)](plan, _records(context))
    row = next((row for row in evaluated["case_results"] if row["case_id"] == inputs["case_id"]), None)
    if row is None:
        return {"status": "blocked", "accepted": False, "reason": "source_observation_missing"}
    return {"status": "passed" if row.get("pass") is True else "failed", "accepted": row.get("response_valid") is True,
            "parameter_result": copy.deepcopy(row)}


def _assert_suite(context, inputs):
    domain = _restore(context._plan["definition"]["fixed_parameter"])
    evaluated = _EVALUATE[_kind(domain)](domain, _records(context))
    return {"status": "passed" if evaluated.get("pass") is True else "failed",
            "accepted": evaluated.get("complete") is True, "parameter_summary": evaluated}


def _registry():
    registry = HandlerRegistry()
    registry.register("source_fixed.observe", _observe, version=FACTORY_VERSION, digest=execution_digest)
    registry.register("source_fixed.assert", _assert_semantics, version=FACTORY_VERSION, digest=execution_digest)
    registry.register("source_fixed.suite", _assert_suite, version=FACTORY_VERSION, digest=execution_digest)
    return registry


def prepare_fixed_parameter_plan(snapshot, *, suite_id=None, endpoint=None, frozen_plan=None,
                                 create_runtime_nonce=False, nonce_factory=None, minimum_output_tokens=256):
    """Pure source preflight. Cache callers must supply a frozen job nonce plan or create one explicitly."""
    domain = snapshot if type(snapshot) in _KIND_TYPES.values() else _build_domain(snapshot, suite_id=suite_id,
        endpoint=endpoint, frozen_plan=frozen_plan, create_runtime_nonce=create_runtime_nonce, nonce_factory=nonce_factory)
    kind = _kind(domain)
    if not isinstance(minimum_output_tokens, int) or isinstance(minimum_output_tokens, bool) or minimum_output_tokens < 256:
        raise ValueError("Fixed source output floor must be at least 256")
    snapshot = domain.snapshot
    execution = snapshot["execution_target"]
    provider = "anthropic_official" if kind.startswith("anthropic_") else "deepseek_official"
    if execution.get("provider_id") != provider:
        raise ValueError("Fixed source requires the exact official execution provider")
    domain_module = _DOMAINS[kind]
    requests = domain.requests
    steps = []
    positions = {item.case_id: index for index, item in enumerate(requests)}
    previous_positive = []
    for index, item in enumerate(requests):
        wire_kind, _ = _boundary(domain, item)
        if wire_kind == "generation":
            limited = item.body
            enforce_parameter_test_output_limit(limited, domain_module.TRANSPORT, minimum=minimum_output_tokens)
            if beta.canonical_bytes(limited) != item.body_bytes:
                raise ValueError("Configured output floor changes a fixed source request")
        dependencies = []
        expectation = item.expectation
        control = expectation.get("positive_control") if isinstance(expectation, dict) else None
        if control in positions:
            dependencies.append(positions[control])
        if kind == "anthropic_prefill" and index:
            dependencies.append(0)
        if kind == "beta" and index in (1, 2, 4):
            dependencies.append({1: 0, 2: 1, 4: 3}[index])
        if kind == "anthropic_cache":
            dependencies.extend(previous_positive)
        refs = [{"$ref": {"step": _observe_id(prior), "path": ["record"], "type": "object"}}
                for prior in dict.fromkeys(dependencies)]
        steps.append({"id": _observe_id(index), "case_id": item.case_id, "handler": "source_fixed.observe",
                      "inputs": {"ordinal": index, "source_prerequisites": refs}})
        if not _negative(item):
            previous_positive.append(index)
            steps.append({"id": f"fixed.assert.{index + 1:02d}", "case_id": item.case_id,
                          "handler": "source_fixed.assert", "depends_on": [_observe_id(index)],
                          "inputs": {"case_id": item.case_id}})
    steps.append({"id": "fixed.suite.assert", "case_id": requests[-1].case_id,
                  "handler": "source_fixed.suite", "inputs": {}})
    target = {"provider": provider, "model": execution["request_model_id"], "route_profile": execution["route_profile"],
              "api_form": execution["api_form"], "source_id": snapshot["source_id"], "profile_id": snapshot["profile_id"],
              "interface_id": snapshot["interface_id"], "reference_contract_id": snapshot["reference_contract_id"],
              "test_binding_id": snapshot["test_binding_id"], "endpoint": domain.endpoint}
    workflow = {"workflow_schema_version": 1, "id": "source-fixed/" + (_suite_id(domain) or beta.CONTRACT_ID),
                "target": target, "cases": [{"id": item.case_id} for item in requests], "steps": steps,
                "fixed_parameter": _descriptor(domain), "minimum_output_tokens": minimum_output_tokens,
                "factory": {"factory_id": FACTORY_ID, "version": FACTORY_VERSION, "source_sha256": source_digest()},
                "limits": {"max_requests": len(requests), "request_timeout_seconds": TIMEOUT_SECONDS,
                           "deadline_seconds": len(requests) * TIMEOUT_SECONDS, "cleanup_max_requests": 0,
                           "cleanup_deadline_seconds": 1},
                "policy": {"fatal_status_codes": _FATAL_CODES}}
    registry = _registry()
    return compile_plan(workflow, registry), registry


def registry_for_fixed_parameter_plan(plan):
    registry = _registry()
    validate_plan(plan, registry)
    expected, _ = prepare_fixed_parameter_plan(_restore(plan["definition"]["fixed_parameter"]),
        minimum_output_tokens=plan["definition"]["minimum_output_tokens"])
    if expected != plan:
        raise IntegrityError("Fixed workflow differs from its reviewed source factory")
    return registry


def validate_fixed_client(client, domain, *, minimum_output_tokens=256):
    """Enforce credential ownership and exact official route on every counted wire."""
    kind = _kind(domain)
    module = _DOMAINS[kind]
    anthropic = kind.startswith("anthropic_")
    provider = "anthropic_official" if anthropic else "deepseek_official"
    origin = "https://api.anthropic.com" if anthropic else "https://api.deepseek.com"
    credential = getattr(client, "_credential", None)
    interface = client.api_interfaces.get(module.TRANSPORT) or {}
    if (client.provider != provider or client.base_url not in (origin, origin + "/v1")
            or getattr(credential, "provider", None) != provider
            or getattr(credential, "allowed_origins", None) != frozenset({origin})
            or client._transport_url(module.TRANSPORT) != module.ENDPOINT or domain.endpoint != module.ENDPOINT
            or domain.snapshot["execution_target"]["provider_id"] != provider
            or interface.get("auth") != ("anthropic" if anthropic else "bearer")
            or anthropic and interface.get("anthropic_version", "2023-06-01") != "2023-06-01"):
        raise ValueError("Fixed transport requires its exact official credential and endpoint")
    for item in domain.requests:
        wire_kind, _ = _boundary(domain, item)
        if wire_kind == "count":
            if "max_tokens" in item.body or "stream" in item.body:
                raise ValueError("Count wire cannot carry generation controls")
            continue
        body = item.body
        enforce_parameter_test_output_limit(body, module.TRANSPORT, minimum=minimum_output_tokens)
        if beta.canonical_bytes(body) != item.body_bytes:
            raise ValueError("Configured output floor changes a fixed source request")


def fixed_domain_from_plan(plan):
    """Restore a reviewed immutable source plan before creating its client."""
    registry_for_fixed_parameter_plan(plan)
    return _restore(plan["definition"]["fixed_parameter"])


class FixedParameterDispatcher:
    """One exact body per bounded call. The client credential never enters the plan or receipt."""
    def __init__(self, client, domain, request, *, validate_client=validate_fixed_client, minimum_output_tokens=256,
                 artifact_writer=None, on_record=None):
        validate_fixed_client(client, domain, minimum_output_tokens=minimum_output_tokens)
        validate_client(client, domain, minimum_output_tokens=minimum_output_tokens)
        self.client, self.domain, self.request = client, domain, request
        self.validate_client, self.minimum_output_tokens = validate_client, minimum_output_tokens
        self.artifact_writer, self.on_record = artifact_writer, on_record
        self.records = []

    def _write(self, name, value):
        if self.artifact_writer:
            self.artifact_writer(name, value)

    def __call__(self, outgoing, *, timeout, context):
        kind = _kind(self.domain)
        validate_fixed_client(self.client, self.domain, minimum_output_tokens=self.minimum_output_tokens)
        self.validate_client(self.client, self.domain, minimum_output_tokens=self.minimum_output_tokens)
        index = outgoing.get("ordinal")
        if type(index) is not int or index != len(self.records) or index >= self.domain.request_cap:
            raise IntegrityError("Fixed dispatch cannot repeat or reorder requests")
        item = self.domain.requests[index]
        wire_kind, endpoint = _boundary(self.domain, item)
        expected = {"method": "POST", "url": endpoint, "case_id": item.case_id, "ordinal": index,
                    "kind": wire_kind, "body": item.body, "body_sha256": item.body_sha256}
        if outgoing != expected or beta.canonical_bytes(outgoing["body"]) != item.body_bytes:
            raise IntegrityError("Fixed dispatch changed its source body or endpoint")
        row = {"case_id": item.case_id, "request_body_sha256": item.body_sha256,
               "status_code": None, "response_raw": "", "response_complete": False,
               "client_entered": False, "timestamp": time.time()}
        extra = {"request_kind": wire_kind, "request_endpoint": endpoint, "fixed_plan_digest": self.domain.plan_digest} if kind == "anthropic_cache" else {}
        row.update(extra)
        self._write(f"{kind}_{index + 1:02d}_attempt.json", {"case_id": item.case_id, "body": item.body,
            "body_sha256": item.body_sha256, "endpoint": endpoint, "snapshot_digest": self.domain.snapshot["snapshot_digest"],
            **({"request_kind": wire_kind, "fixed_plan_digest": self.domain.plan_digest} if kind == "anthropic_cache" else {})})
        raw, response, started = bytearray(), None, time.monotonic()
        try:
            with response_deadline(timeout):
                auth_mode = "anthropic" if kind.startswith("anthropic_") else "bearer"
                headers = self.client._credential.auth_headers(url=endpoint, auth_mode=auth_mode)
                row["client_entered"] = True
                response = self.request("POST", endpoint, data=item.body_bytes, headers=headers,
                                        timeout=(min(15, timeout), timeout), stream=True, allow_redirects=False)
                row["status_code"] = response.status_code
                for chunk in response.iter_content(chunk_size=256):
                    if type(chunk) is not bytes or len(raw) + len(chunk) > beta.MAX_RESPONSE_BYTES:
                        raise ValueError("Fixed response exceeds bounded byte limit")
                    raw.extend(chunk)
                raw.decode("utf-8")
                row["response_complete"] = True
        except (Exception, KeyboardInterrupt) as exc:
            row["failure_type"] = type(exc).__name__
            row["interrupted"] = isinstance(exc, KeyboardInterrupt)
        finally:
            if response is not None:
                try:
                    response.close()
                except (Exception, KeyboardInterrupt) as exc:
                    row["failure_type"] = type(exc).__name__
                    row["interrupted"] = isinstance(exc, KeyboardInterrupt)
            row.update(response_raw=raw.decode("utf-8", errors="replace"),
                       response_sha256_before_redaction=hashlib.sha256(raw).hexdigest(),
                       latency_ms=(time.monotonic() - started) * 1000)
            row = self.client._credential.redact(row)
        self.records.append(row)
        self._write(f"{kind}_{index + 1:02d}_observation.json", row)
        if self.on_record:
            self.on_record(self.records, _EVALUATE[kind](self.domain, self.records))
        return {"status_code": row["status_code"], "record": row}


def execute_fixed_parameter_plan(client, domain, directory, request, *, validate_client=validate_fixed_client,
                                 minimum_output_tokens=256, on_record=None, artifact_writer=None, execution_plan=None):
    """Compatibility facade; delegates each actual request to the shared engine."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("A real report directory is required")
    kind = _kind(domain)
    plan, registry = prepare_fixed_parameter_plan(domain, minimum_output_tokens=minimum_output_tokens)
    if execution_plan is not None:
        registry_for_fixed_parameter_plan(execution_plan)
        if plan != execution_plan:
            raise IntegrityError("Execution plan differs from the fixed job source, nonces, or output floor")
        plan = copy.deepcopy(execution_plan)
    validate_fixed_client(client, domain, minimum_output_tokens=minimum_output_tokens)
    validate_client(client, domain, minimum_output_tokens=minimum_output_tokens)
    if any(directory.glob(kind + "_*.json")) or (directory / "workflow_runs").exists():
        raise ValueError("The fixed report directory already contains an execution ledger")
    if artifact_writer is None:
        def artifact_writer(name, value):
            with (directory / name).open("xb") as stream:
                stream.write(beta.canonical_bytes(value) + b"\n")
    dispatcher = FixedParameterDispatcher(client, domain, request, validate_client=validate_client,
        minimum_output_tokens=minimum_output_tokens, artifact_writer=artifact_writer, on_record=on_record)
    boundaries = [_boundary(domain, item) for item in domain.requests]
    run_id = uuid.uuid4().hex
    artifact_writer(kind + "_dispatch_started.json", {"snapshot_digest": domain.snapshot["snapshot_digest"],
        "workflow_run_id": run_id, "workflow_plan_digest": plan["plan_digest"],
        "request_cap": domain.request_cap, "requests": [{"case_id": item.case_id, "body_sha256": item.body_sha256,
            **({"endpoint": endpoint, "kind": wire_kind} if kind == "anthropic_cache" else {})}
            for item, (wire_kind, endpoint) in zip(domain.requests, boundaries)],
        "retries": 0, "identity_probe_requests": 0,
        **({"fixed_plan_digest": domain.plan_digest} if kind == "anthropic_cache" else {})})
    cancel = CancellationContext()
    report = None
    try:
        with cancel.install_signal_handlers():
            report = execute_plan(plan, registry, dispatcher, cancellation=cancel, evidence_dir=directory / "workflow_runs", run_id=run_id)
    finally:
        artifact_writer(kind + "_dispatch_finished.json", {
            "requests_sent": sum(row["client_entered"] is True for row in dispatcher.records),
            "requests_recorded": len(dispatcher.records), "complete": len(dispatcher.records) == domain.request_cap,
            **({"workflow_result": report} if report is not None else {})})
    evaluated = _EVALUATE[kind](domain, dispatcher.records)
    evaluated["domain_validation_pass"] = evaluated.get("pass") is True
    evaluated["workflow_execution_pass"] = report is not None and report.get("status") == "passed"
    evaluated["pass"] = evaluated["domain_validation_pass"] and evaluated["workflow_execution_pass"]
    evaluated["workflow_result"] = report
    return dispatcher.records, evaluated
