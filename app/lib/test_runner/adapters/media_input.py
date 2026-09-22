"""Source-bound media input matrices through the shared counted dispatcher.

Compatibility observations and complete token quantity proof are separate steps.
Missing independent media counts remain inconclusive, even when recognition works.
"""
from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

from lib import media_input_matrix as matrix
from lib.config import get_model_route_profile, get_provider_config, get_provider_interface
from lib.parameter_output_limit import enforce_parameter_test_output_limit
from lib.token_audit import audit_exchange

from .. import HandlerRegistry, IntegrityError, compile_plan, digest_json, validate_plan
from ..transport import HttpDispatcher

ROOT = Path(__file__).resolve().parents[3]
FACTORY_ID = "media_input"
FACTORY_VERSION = "1"
OBSERVE = "media_input.observe.v1"
ASSERT = "media_input.assert.v1"
TRANSPORTS = {"openai_chat_completions": "chat_completions", "openai_responses": "openai_responses",
              "anthropic_messages": "claude_messages", "gemini_generate_content": "gemini_generate_content"}
OFFICIAL_HOSTS = {"openai": {"api.openai.com"}, "anthropic": {"api.anthropic.com"},
                  "google_ai_studio": {"generativelanguage.googleapis.com"}, "xai": {"api.x.ai"},
                  "deepseek": {"api.deepseek.com"}, "moonshot": {"api.moonshot.ai", "api.moonshot.cn"},
                  "minimax": {"api.minimax.io", "api.minimaxi.com"},
                  "zhipu": {"api.z.ai", "open.bigmodel.cn"},
                  "aliyun_maas": {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"}}


def source_digest() -> str:
    return digest_json({"matrix": matrix.source_digest(), **{
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ("lib/test_runner/adapters/media_input.py", "lib/media_input_validation.py")}})


def execution_digest() -> str:
    return digest_json({"factory": source_digest(), **{
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
            "lib/token_audit.py", "lib/token_counter.py", "lib/test_runner/transport.py",
            "lib/credential_security.py", "lib/parameter_output_limit.py", "lib/config.py")}})


def case_ids_for(source_id: str, model: str, api_form: str) -> list[str]:
    return [case["id"] for case in matrix.cases_for(source_id, model, api_form)]


def _zhipu_endpoint(binding: dict, base_url: str, path: str) -> None:
    """Keep Coding and general PaaS on their distinct MPDB references."""
    from lib.model_profile_catalog import get_model_profile_catalog

    catalog = get_model_profile_catalog()
    try:
        reference = catalog.get_interface(binding["interface_id"])
        contract = catalog.get_contract(binding["contract_id"])
        profile = catalog.get_profile(binding["profile_id"])
    except KeyError as exc:
        raise ValueError("Zhipu media endpoint requires a registered interface and contract") from exc
    execution = binding["execution_target"]
    if (reference["source_id"] != binding["source_id"]
            or reference["profile_id"] != binding["profile_id"]
            or reference["default_contract_id"] != binding["contract_id"]
            or reference["api_form"] != execution["api_form"]
            or execution["request_model_id"] not in (reference.get("request_model_ids") or profile["request_model_ids"])):
        raise ValueError("Zhipu media endpoint differs from its registered interface and contract")
    slug = reference["interface_slug"]
    if slug in {"zai-coding-chat", "zai-general-chat"}:
        scope = reference.get("reference_scope") or {}
        if not scope or scope != contract.get("reference_scope") or scope.get("scheme") != "https":
            raise ValueError("Zhipu media endpoint requires a consistent registered reference scope")
        hosts, expected_path = {scope.get("host")}, scope.get("path")
    elif slug == "openai-chat-default" and not reference.get("reference_scope") and not contract.get("reference_scope"):
        hosts, expected_path = OFFICIAL_HOSTS["zhipu"], "/api/paas/v4/chat/completions"
    else:
        raise ValueError("Zhipu media endpoint has no reviewed interface scope")
    # Reject spellings that requests/proxies may normalize onto another route.
    # Resolve the remaining base/path split exactly as HttpDispatcher does.
    if any(char in "%\\?#" or char.isspace() or ord(char) < 32 or ord(char) == 127
           for char in base_url + path):
        raise ValueError("Zhipu media endpoint contains an ambiguous URL spelling")
    base = urlsplit(base_url.rstrip("/"))
    if any(part in {".", ".."} for value in (base.path, path) for part in value.split("/")):
        raise ValueError("Zhipu media endpoint contains a path traversal")
    prefix = base.path.rstrip("/")
    final_path = path if prefix and (path == prefix or path.startswith(prefix + "/")) else prefix + path
    if base.hostname not in hosts or final_path != expected_path:
        raise ValueError("Zhipu media endpoint differs from its exact registered interface scope")


def _official_interface(config: dict, binding: dict) -> tuple[dict, str]:
    execution = binding["execution_target"]
    source, model, form = binding["source_id"], execution["request_model_id"], execution["api_form"]
    transport = TRANSPORTS.get(form)
    if transport is None or execution["transport_adapter_id"] != transport:
        raise ValueError("Media workflow requires an exact documented API transport")
    provider = get_provider_config(config, execution["provider_id"])
    if provider.get("reference_source_id") not in (None, "", source):
        raise ValueError("Configured media provider has a different official source")
    per_model_source = ((provider.get("models") or {}).get("reference_source_ids") or {}).get(model)
    if per_model_source not in (None, "", source):
        raise ValueError("Configured media model has a different official source")
    if model not in (provider.get("models") or {}).get("candidates", []):
        raise ValueError("Media model is absent from the selected provider candidates")
    interface = get_provider_interface(config, transport, execution["provider_id"])
    origin = urlsplit(interface["base_url"])
    host = origin.hostname or ""
    official = host in OFFICIAL_HOSTS.get(source, set())
    if source == "aliyun_maas":
        official |= bool(re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+\.maas\.aliyuncs\.com", host))
    if (not official or origin.scheme != "https" or origin.username or origin.password
            or origin.query or origin.fragment or origin.port not in (None, 443)
            or interface.get("enabled") is False or interface.get("disabled_reason")):
        raise ValueError("Media workflow requires an enabled official HTTPS source endpoint")
    expected_auth = "google_api_key" if form == "gemini_generate_content" else "anthropic" if form == "anthropic_messages" else "bearer"
    if interface.get("auth") != expected_auth:
        raise ValueError("Configured media interface authentication differs from its API form")
    path = str(interface.get("path") or "").format(model=quote(model, safe=""))
    expected_suffix = {"openai_chat_completions": "/chat/completions", "openai_responses": "/responses",
                       "anthropic_messages": "/messages", "gemini_generate_content": "/models/" + quote(model, safe="") + ":generateContent"}[form]
    if (not path.startswith("/") or path.startswith("//") or not path.endswith(expected_suffix)
            or urlsplit(path).query or urlsplit(path).fragment or ".." in path.split("/")):
        raise ValueError("Configured media interface path differs from its exact generation API")
    if source == "zhipu":
        _zhipu_endpoint(binding, interface["base_url"], path)
    return interface, path


def _source_binding(resolved: dict) -> dict:
    result = {key: copy.deepcopy(resolved[key]) for key in (
        "source_id", "profile_id", "interface_id", "contract_id", "test_binding_id", "execution_target")}
    workflow = resolved.get("workflow") or {}
    result["workflow_id"] = str(resolved.get("workflow_id") or workflow.get("id") or workflow.get("workflow_id") or "")
    if not result["workflow_id"]:
        raise ValueError("Media workflow requires a registered workflow ID")
    route = resolved.get("route_profile") or ((resolved.get("reference") or {}).get("interface") or {}).get("routing_mode")
    if route:
        result["route_profile"] = route
    return result


def _count_descriptor(config: dict, binding: dict, generation_interface: dict) -> dict | None:
    """Use only the already configured native Google full-request counter."""
    execution = binding["execution_target"]
    if binding["source_id"] != "google_ai_studio" or execution["api_form"] != "gemini_generate_content":
        return None
    provider = get_provider_config(config, execution["provider_id"])
    count = (provider.get("api_interfaces") or {}).get("token_count")
    if not isinstance(count, dict) or count.get("enabled", True) is False or count.get("disabled_reason"):
        return None
    if "gemini_generate_content" not in count.get("transports", []):
        return None
    expected = {"auth": "google_api_key", "request_wrapper": "generateContentRequest",
                "request_model_field": "model", "response_field": "totalTokens"}
    if any(count.get(key) != value for key, value in expected.items()):
        raise ValueError("Media token counter must count the complete native Google request")
    if str(count.get("base_url") or generation_interface["base_url"]).rstrip("/") != str(generation_interface["base_url"]).rstrip("/"):
        raise ValueError("Media token counter must use the same official API origin")
    path = str(count.get("path") or "").format(model=quote(execution["request_model_id"], safe=""))
    generation_path = str(generation_interface["path"]).format(model=quote(execution["request_model_id"], safe=""))
    if path != generation_path.removesuffix(":generateContent") + ":countTokens":
        raise ValueError("Media token counter crosses its exact model or API version")
    return {"path": path, **expected}


def build_definition(config: dict, resolved: dict) -> dict:
    binding = _source_binding(resolved)
    execution = binding["execution_target"]
    source, model, form = binding["source_id"], execution["request_model_id"], execution["api_form"]
    interface, path = _official_interface(config, binding)
    count_descriptor = _count_descriptor(config, binding, interface)
    cases = matrix.cases_for(source, model, form)
    if not cases:
        raise ValueError("No executable documented media input cases for this exact model/API")
    route = get_model_route_profile(config, model, execution["provider_id"], route_profile=binding.get("route_profile"))
    binding["route_profile"] = route
    target = {**binding, "base_url": interface["base_url"], "path": path, "modality": "text", "route_profile": route}
    reference = matrix.find_reference(source, model, form)
    allowed_models = list(reference.get("allowed_response_models") or [])
    rows, steps = [], []
    for case in cases:
        name, observation = case["id"], case["id"] + ".observe"
        body = copy.deepcopy(case["body"])
        enforce_parameter_test_output_limit(body, execution["transport_adapter_id"])
        if body != case["body"]:
            raise ValueError("Media factory request violates minimum output allowance")
        rows.append({"id": name, "name": case["name"], "group": case["group"]})
        baseline_refs = [{"$result_ref": {"step": item + ".observe", "path": ["record"], "type": "object"}}
                         for item in case.get("depends_on", [])]
        if case["expectation"] == "rejected":
            # A rejected observation has accepted=False and cannot be an engine
            # prerequisite. Keep its observation and final assertion together.
            steps.append({"id": name, "case_id": name, "handler": OBSERVE,
                          "inputs": {"case": case, "baselines": baseline_refs},
                          "depends_on": [item + ".observe" for item in case.get("depends_on", [])],
                          "request_cap": 1, "cleanup_request_cap": 0})
            continue
        steps.append({"id": observation, "case_id": name, "handler": OBSERVE,
                      "inputs": {"case": case}, "depends_on": [item + ".observe" for item in case.get("depends_on", [])],
                      "request_cap": (2 if count_descriptor else 1) + 2 * len(case.get("external_media", [])), "cleanup_request_cap": 0})
        steps.append({"id": name, "case_id": name, "handler": ASSERT,
                      "inputs": {"record": {"$result_ref": {"step": observation, "path": ["record"], "type": "object"}},
                                 "baselines": baseline_refs},
                      "request_cap": 0, "cleanup_request_cap": 0})
    return {"workflow_schema_version": 1, "id": binding["workflow_id"], "target": target,
            "factory": {"factory_id": FACTORY_ID, "version": FACTORY_VERSION, "source_sha256": source_digest()},
            "factory_arguments": {"source_id": source, "model": model, "api_form": form},
            "source_binding": binding, "allowed_response_models": allowed_models, "token_count": count_descriptor,
            "public_media_urls": sorted({f["external_url"] for case in cases for f in case.get("external_media", [])}),
            "cases": rows, "steps": steps,
            "limits": {"max_requests": sum(step["request_cap"] for step in steps), "request_timeout_seconds": 120,
                       "deadline_seconds": sum(step["request_cap"] for step in steps) * 120, "cleanup_max_requests": 0, "cleanup_deadline_seconds": 15},
            "policy": {"fatal_status_codes": [*range(300, 400), 401, 402, 403, 404, 408, 429, *range(500, 600)]},
            "proof_scope": "official_media_input_compatibility_and_semantics",
            "full_parameter_certification": False, "physical_identity_attestation": False}


def register_handlers(registry: HandlerRegistry) -> HandlerRegistry:
    registry.register(OBSERVE, _observe, version=FACTORY_VERSION, digest=execution_digest)
    registry.register(ASSERT, _assert, version=FACTORY_VERSION, digest=execution_digest)
    return registry


def prepare_media_input_plan(config: dict, resolved: dict, *, selected_cases=None, runs=1, timeout_sec=120):
    definition = build_definition(config, resolved)
    if type(timeout_sec) is not int or not 1 <= timeout_sec <= 3600:
        raise ValueError("Media response deadline must be an integer from 1 to 3600")
    definition["limits"]["request_timeout_seconds"] = timeout_sec
    registry = register_handlers(HandlerRegistry())
    selected = compile_plan(definition, registry, selected_cases=selected_cases, run_count=runs)
    count = max(1, sum(step["request_cap"] for step in selected["ordered_steps"]))
    definition["limits"].update(max_requests=count, deadline_seconds=count * timeout_sec)
    return compile_plan(definition, registry, selected_cases=selected_cases, run_count=runs), registry


def registry_for_media_input_plan(config: dict, plan: dict) -> HandlerRegistry:
    """Reject edited and rehashed factory bodies before credential construction."""
    frozen = validate_plan(plan)
    original = frozen["definition"]
    timeout = original["limits"]["request_timeout_seconds"]
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise IntegrityError("Invalid frozen media response deadline")
    current, registry = prepare_media_input_plan(config, original["source_binding"],
        selected_cases=frozen["requested_cases"], runs=frozen["run_count"], timeout_sec=timeout)
    if current != frozen:
        raise IntegrityError("Media workflow differs from its exact current source definition")
    return registry


def make_dispatcher(config: dict, plan: dict, **kwargs) -> HttpDispatcher:
    registry_for_media_input_plan(config, plan)
    execution = plan["target"]["execution_target"]
    return HttpDispatcher(config, execution["provider_id"], execution["transport_adapter_id"],
                          public_urls=tuple(plan["definition"]["public_media_urls"]), **kwargs)


def _exact_case(context, case: dict) -> None:
    target = context.target
    execution = target["execution_target"]
    candidates = matrix.cases_for(target["source_id"], execution["request_model_id"], execution["api_form"])
    matches = [row for row in candidates if row["id"] == case.get("id")]
    if len(matches) != 1 or matches[0] != case:
        raise IntegrityError("Media request differs from its exact documented fixture and case")
    expected_path = context._plan["definition"]["target"]["path"]
    if target["path"] != expected_path:
        raise IntegrityError("Media request changed its frozen endpoint")


def _token_audit(case: dict, receipt: dict, target: dict, verdict: dict, independent_input_count=None) -> dict:
    response = receipt.get("response") or {}
    transport = target["execution_target"]["transport_adapter_id"]
    usage = response.get("usageMetadata", {}) if transport == "gemini_generate_content" else response.get("usage", {})
    result = SimpleNamespace(response_json=response, usage=usage, text=verdict.get("text", ""),
                             success=True, finish_reason=None, error_type=receipt.get("error_type"),
                             status_code=receipt.get("http_status", receipt.get("status_code")))
    # A visible-text tokenizer cannot count images/audio. No credential-bearing
    # runtime configuration is serialized, and no uncounted provider call occurs.
    return audit_exchange(case["body"], result, transport, {}, case["id"],
                          provider=target["execution_target"]["provider_id"],
                          model=target["execution_target"]["request_model_id"],
                          accounting_source_id=target["source_id"], accounting_contract_id=target["contract_id"],
                          independent_input_count=independent_input_count)


def _observe(context, inputs: dict) -> dict:
    from lib.media_input_validation import validate_response
    case = inputs["case"]
    _exact_case(context, case)
    target, execution = context.target, context.target["execution_target"]
    external = case.get("external_media", [])
    before = _verify_remote_media(context, external, "before")
    if any(row["verified"] is not True for row in before):
        return {"status": "inconclusive", "accepted": False, "reason": "public_media_bytes_unverified",
                "record": {"case_id": case["id"], "request": copy.deepcopy(case["body"]),
                           "external_media_checks": before, "compatibility_observation_pass": False,
                           "model_request_sent": False}}
    receipt = context.dispatch({"method": "POST", "path": target["path"], "body": copy.deepcopy(case["body"]), "capture_raw": True})
    after = _verify_remote_media(context, external, "after")
    verdict = validate_response(case, receipt, model=execution["request_model_id"],
                                transport=execution["transport_adapter_id"],
                                allowed_response_models=context._plan["definition"]["allowed_response_models"])
    negative = case["expectation"] == "rejected"
    valid = verdict.get("compatibility_pass") is True if negative else all(
        verdict.get(field) is True for field in ("compatibility_pass", "semantic_pass", "usage_pass", "identity_pass"))
    remote_verified = all(row["verified"] is True for row in [*before, *after])
    count_receipt = independent = None
    counter = context._plan["definition"].get("token_count")
    if valid and not negative and counter:
        full_request = {**copy.deepcopy(case["body"]), "model": "models/" + execution["request_model_id"]}
        request = {"method": "POST", "path": counter["path"], "body": {"generateContentRequest": full_request}, "capture_raw": True}
        count_request_digest = digest_json(request)
        count_receipt = context.dispatch(request, kind="count")
        count_payload = count_receipt.get("response")
        tokens = count_payload.get("totalTokens") if isinstance(count_payload, dict) else None
        counted = (digest_json(request) == count_request_digest and count_receipt.get("http_status", count_receipt.get("status_code")) == 200
                   and count_receipt.get("response_complete") is True and not count_receipt.get("failure")
                   and not count_receipt.get("error_type") and isinstance(count_payload, dict) and not count_payload.get("error")
                   and type(tokens) is int and tokens > 0)
        independent = {"tokens": tokens if counted else None, "evidence_level": "official_count" if counted else "unavailable",
                       "covers_full_input": counted, "kind": "provider_count", "source": "token_count:totalTokens",
                       "request_integrity": "pass" if digest_json(request) == count_request_digest else "fail",
                       "note": "Complete generateContentRequest, including original media bytes, sent to the configured official countTokens endpoint."}
    audit = _token_audit(case, receipt, target, verdict, independent) if valid and not negative else None
    record = {**receipt, "case_id": case["id"], "request": copy.deepcopy(case["body"]),
              "request_sha256": digest_json(case["body"]), "verdict": verdict, "token_audit": audit,
              "expectation": case["expectation"], "compatibility_observation_pass": valid and remote_verified,
              "count_receipt": count_receipt, "external_media_checks": [*before, *after], "model_request_sent": True}
    if not remote_verified:
        return {"status": "inconclusive", "accepted": False, "reason": "public_media_changed_during_request", "record": record}
    if negative:
        return {**_assert(context, {"record": record, "baselines": inputs.get("baselines", [])}), "record": record}
    return {"status": "passed" if valid else "failed", "accepted": bool(valid and not negative),
            "record": record, "semantic_effect": {"pass": verdict.get("semantic_pass")},
            "proof_scope": verdict.get("proof_scope")}


def _verify_remote_media(context, fixtures: list[dict], phase: str) -> list[dict]:
    rows = []
    for fixture in fixtures:
        receipt = context.dispatch({"method": "GET", "url": fixture["external_url"], "public": True}, kind="fixture")
        verified = (receipt.get("http_status", receipt.get("status_code")) == 200
                    and receipt.get("response_complete") is True and not receipt.get("error_type")
                    and not receipt.get("failure") and receipt.get("response_bytes_sha256") == fixture["sha256"]
                    and receipt.get("response_byte_length") == fixture["byte_length"])
        rows.append({"phase": phase, "url": fixture["external_url"], "expected_sha256": fixture["sha256"],
                     "observed_sha256": receipt.get("response_bytes_sha256"), "verified": verified,
                     "http_status": receipt.get("http_status", receipt.get("status_code"))})
    return rows


def _assert(context, inputs: dict) -> dict:
    record = inputs["record"]
    verdict = record["verdict"]
    valid = record["compatibility_observation_pass"]
    if not valid:
        return {"status": "failed", "accepted": False, "verdict": verdict}
    if record["expectation"] == "rejected":
        baselines = inputs.get("baselines", [])
        statuses = [_audit_status(baseline["token_audit"]) for baseline in baselines]
        status = "failed" if "failed" in statuses else "passed" if statuses and all(value == "passed" for value in statuses) else "inconclusive"
        return {"status": status, "accepted": False, "verdict": {**verdict, "rejection_pass": True,
                "baseline_token_validation_pass": status == "passed", "overall_pass": status == "passed"},
                "semantic_effect": {"status": "attributed_rejection_with_accepted_baseline"},
                "reason": None if status == "passed" else "baseline_media_token_quantity_unverified"}
    audit = record["token_audit"]
    status = _audit_status(audit)
    passed = status == "passed"
    return {"status": status, "accepted": passed, "verdict": {**verdict,
            "token_validation_pass": passed, "overall_pass": passed},
            "identity_token_audit": audit, "semantic_effect": {"pass": verdict["semantic_pass"]},
            "reason": None if passed else "token_audit_failed" if status == "failed" else "complete_media_token_quantity_unverified"}


def _audit_status(audit: dict) -> str:
    checks = [audit.get("usage_presence", {}), audit.get("usage_arithmetic", {}), audit.get("output_completion", {}),
              audit.get("input_accuracy", {}), audit.get("output_accuracy", {})]
    # Missing quantity evidence is inconclusive; a contradictory arithmetic,
    # completion, output allowance or exact count is a genuine failure.
    checks.extend((audit.get("gross_plausibility") or {}).get(name, {}) for name in ("input", "output"))
    failed = audit.get("count_request_integrity") == "fail" or any(check.get("status") == "fail" for check in checks)
    complete = bool((audit.get("independent_count", {}).get("input") or {}).get("covers_full_input"))
    passed = complete and audit.get("validation_pass") is True
    return "failed" if failed else "passed" if passed else "inconclusive"
