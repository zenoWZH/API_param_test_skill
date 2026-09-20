"""One bound DeepSeek beta prefix case; no route/body discovery or retries.

The exact MPDB Interface and both bindings gate the shared counted dispatch.
An accepted prefix request is deliberately not a completed semantic proof.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import time
from typing import Any, Callable

from .deepseek_beta_prefix import (
    DEEPSEEK_BETA_PREFIX_API_FORM, DEEPSEEK_BETA_PREFIX_URL,
    build_prefix_request, validate_prefix_response,
)
from .live_stateful_runners import _response_json
from .model_profile_catalog import Catalog


APPROVAL_CODE = "R7-ALL-MODELS-PREFIX-COMPLETION-LIVE"
TARGETS = {
    "text/deepseek/deepseek/deepseek-v4-flash-0731#deepseek-beta-chat-prefix":
        ("deepseek-v4-flash", "deepseek_v4_flash_0731_chat_prefix_beta"),
    "text/deepseek/deepseek/deepseek-v4-pro-0813#deepseek-beta-chat-prefix":
        ("deepseek-v4-pro", "deepseek_v4_pro_0813_chat_prefix_beta"),
}
POSITIVE_CASE = "deepseek_beta_prefix_json"
NEGATIVE_CASE = "deepseek_beta_prefix_reject_type"
EXECUTION_FLAGS = ("enabled", "executable", "runner_enabled", "parameter_test_enabled")
MAX_INPUT_BYTES = 32 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class PrefixLiveConditions:
    """Caller records satisfied conditions; this object does not grant approval."""

    authentication_verified: bool = False
    request_and_binding_reviewed: bool = False
    request_cost_approved: bool = False
    retention_approved: bool = False

    def validate(self) -> None:
        if any(getattr(self, field) is not True for field in self.__dataclass_fields__):
            raise ValueError("all four prefix live execution conditions must be explicitly satisfied")


def _enabled(record: dict) -> bool:
    return (all(record.get(flag) is True for flag in EXECUTION_FLAGS) and
            not record.get("disabled_reason") and record.get("test_binding_status") == "required")


def _bound_case(catalog: Catalog, interface_id: str, model_id: str, case_id: str) -> tuple[dict, dict]:
    if not isinstance(catalog, Catalog):
        raise ValueError("prefix dispatch requires the real MPDB Catalog resolver")
    if not isinstance(interface_id, str) or interface_id not in TARGETS:
        raise ValueError("prefix dispatch requires one of the two exact documented beta Interfaces")
    expected_model, contract_id = TARGETS[interface_id]
    if model_id != expected_model or case_id not in {POSITIVE_CASE, NEGATIVE_CASE}:
        raise ValueError("prefix model or case is outside the exact approved selection")
    profile_id = interface_id.split("#")[0]
    model_slug = profile_id.rsplit("/", 1)[-1]
    policy_id = "interface/" + interface_id.replace("#", "/")
    parameter_id = "parameter/" + contract_id
    expected = {"source_id": "deepseek", "modality": "text", "family_id": "deepseek",
                "model_slug": model_slug, "profile_id": profile_id, "interface_id": interface_id,
                "api_form": DEEPSEEK_BETA_PREFIX_API_FORM, "contract_id": contract_id,
                "test_binding_id": policy_id, "parameter_test_binding_id": parameter_id}
    # Call the actual resolver, including both explicit Test Binding selectors.
    # Its disabled-Interface/policy rejection must never be replaced by a shim.
    resolved = Catalog.resolve_parameter_config(catalog, **{k: v for k, v in expected.items() if k != "profile_id"})
    if not isinstance(resolved, dict) or any(resolved.get(k) != v for k, v in expected.items()):
        raise ValueError("prefix resolver did not return the exact source/model/Interface/Contract bindings")
    profile, interface = resolved.get("profile"), resolved.get("interface")
    policy, parameter, contract = (resolved.get("test_binding"), resolved.get("parameter_test_binding"),
                                   resolved.get("contract"))
    if not all(isinstance(record, dict) for record in (profile, interface, policy, parameter, contract)):
        raise ValueError("prefix resolver returned incomplete target records")
    for record in (profile, interface):
        identity = {key: value for key, value in expected.items()
                    if key in {"source_id", "modality", "family_id", "model_slug", "profile_id"}}
        if (any(record.get(key) != value for key, value in identity.items()) or
                record.get("request_model_ids") != [model_id] or record.get("lifecycle", "active") != "active"):
            raise ValueError("prefix Profile/Interface identity or active model linkage differs")
    if (profile.get("profile_state") != "executable" or interface_id not in profile.get("interface_ids", []) or
            interface.get("interface_id") != interface_id or interface.get("api_form") != DEEPSEEK_BETA_PREFIX_API_FORM or
            interface.get("routing_mode") != "vendor_direct" or interface.get("transport_adapter_id") != "deepseek-beta-chat-prefix" or
            interface.get("default_contract_id") != contract_id or interface.get("contract_ids") != [contract_id] or
            interface.get("default_api_version") != "beta" or
            interface.get("api_versions", {}).get("beta", {}).get("path_template") != "/beta/chat/completions"):
        raise ValueError("prefix target does not have the exact beta transport and Contract")
    if not all(_enabled(record) for record in (interface, policy, parameter)):
        raise ValueError("prefix Interface and both Test Bindings must have all execution flags enabled")
    if (any(record.get("disabled_reason") for record in (profile, contract)) or
            any(flag in record and record[flag] is not True for record in (profile, contract) for flag in EXECUTION_FLAGS)):
        raise ValueError("prefix Profile or Contract has an explicit disabled execution flag or reason")
    if any("api_version" in record and record["api_version"] != "beta" for record in (policy, parameter)):
        raise ValueError("prefix Test Binding API version differs from the selected beta Interface")
    if (policy.get("source_id") != "deepseek" or policy.get("interface_id") != interface_id or
            policy.get("test_binding_id") != policy_id or policy.get("extension_type") != "model_test_policy" or
            policy.get("reference_contract_ids") != [contract_id] or policy.get("default_reference_contract_id") != contract_id or
            policy.get("suite_family_id") != "deepseek" or policy.get("canonical_family_id") != "deepseek" or
            policy.get("compatibility_family_alias") is not False or
            parameter.get("source_id") != "deepseek" or parameter.get("test_binding_id") != parameter_id or
            parameter.get("extension_type") != "parameter" or parameter.get("contract_id") != contract_id or
            contract.get("source_id") != "deepseek" or contract.get("source_ids") != ["deepseek"] or
            contract.get("contract_id") != contract_id or contract.get("api_form") != DEEPSEEK_BETA_PREFIX_API_FORM or
            contract.get("family_id") != "deepseek" or contract.get("test_binding_status") != "required"):
        raise ValueError("prefix Contract or Test Binding crosses the exact source-local selection")
    capabilities = {**contract.get("parameter_capabilities", {}), **interface.get("parameter_capabilities", {})}
    if any(capabilities.get(field, {}).get("state") != "supported" for field in
           ("model", "messages", "messages[].prefix", "max_tokens", "thinking.type", "stream",
            "response.model", "response.choices[].message.content", "response.choices[].finish_reason", "response.usage")):
        raise ValueError("prefix target lacks a supported request/response parameter contract")
    for record in (policy, parameter, resolved):
        if case_id not in record.get("test_cases", []) or case_id in record.get("excluded_test_profiles", []):
            raise ValueError("selected prefix case is absent or excluded by its exact bindings")
    definitions = parameter.get("case_definitions")
    if not isinstance(definitions, list) or any(not isinstance(case, dict) for case in definitions):
        raise ValueError("prefix parameter binding has no explicit case definitions")
    cases = [case for case in definitions if case.get("case_id") == case_id]
    if len(cases) != 1:
        raise ValueError("prefix case definition is absent or ambiguous")
    case = copy.deepcopy(cases[0])
    expectation = "supported" if case_id == POSITIVE_CASE else "unsupported"
    for field in ("expectations", "default_expectations"):
        overrides = policy.get(field, {})
        if not isinstance(overrides, dict) or case_id in overrides and overrides[case_id] != expectation:
            raise ValueError("prefix model policy overrides the exact selected case expectation")
    if (case.get("expectation") != expectation or
            case_id == NEGATIVE_CASE and case.get("target_parameter") != "messages[].prefix"):
        raise ValueError("prefix case expectation or negative field attribution was changed")
    return resolved, case


def _prepare(case: dict, model_id: str) -> tuple[bytes, Any]:
    body = copy.deepcopy(case.get("body"))
    if not isinstance(body, dict) or set(body) != {"model", "messages", "max_tokens", "stream", "thinking"}:
        raise ValueError("prefix case contains unapproved body fields")
    cap = body["max_tokens"]
    if type(cap) is not int or not 256 <= cap <= 4096:
        raise ValueError("prefix case output limit must be an integer in [256, 4096]")
    if body["model"] != model_id or body["stream"] is not False or body["thinking"] != {"type": "disabled"}:
        raise ValueError("prefix case must retain its exact model, nonstreaming, non-thinking settings")
    messages = body["messages"]
    if (not isinstance(messages, list) or len(messages) != 2 or not all(isinstance(m, dict) for m in messages) or
            messages[0].get("role") != "user" or messages[-1].get("role") != "assistant" or
            messages[-1].get("content") != '{"answer":'):
        raise ValueError("prefix case must retain the bound two-message JSON prefix design")
    expected_prefix = True if case["case_id"] == POSITIVE_CASE else "not-a-boolean"
    if messages[-1].get("prefix") != expected_prefix or type(messages[-1].get("prefix")) is not type(expected_prefix):
        raise ValueError("prefix case changed its sole selected positive/negative field")
    # The existing builder validates every legal field, even for the deliberate
    # negative. Only that negative's exact prefix type differs on the wire.
    valid_messages = copy.deepcopy(messages)
    valid_messages[-1]["prefix"] = True
    request = build_prefix_request(source_id="deepseek", family_id="deepseek", api_form=DEEPSEEK_BETA_PREFIX_API_FORM,
                                   model=model_id, messages=valid_messages, max_tokens=cap, thinking_enabled=False)
    expected_body = request.body
    expected_body["messages"][-1]["prefix"] = expected_prefix
    if body != expected_body:
        raise ValueError("prefix case contains fields outside the validated request contract")
    try:
        serialized = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("prefix case is not finite UTF-8 JSON") from None
    if len(serialized) > MAX_INPUT_BYTES:
        raise ValueError("prefix case input exceeds 32768 bytes")
    return serialized, request


def _positive_observation(request: Any, envelope: dict, cap: int) -> dict:
    if "error" in envelope:
        raise ValueError("successful prefix response contains an error")
    parsed = validate_prefix_response(request, status_code=200, payload=envelope)
    message = envelope["choices"][0]["message"]
    usage = parsed["usage"]
    if (parsed["finish_reason"] != "stop" or message.get("tool_calls") or message.get("refusal") or
            message.get("reasoning_content") or
            any(type(usage.get(k)) is not int for k in ("prompt_tokens", "completion_tokens", "total_tokens")) or
            usage["prompt_tokens"] <= 0 or not 0 < usage["completion_tokens"] <= cap or
            usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]):
        raise ValueError("prefix response has invalid termination, usage, refusal or unexpected thinking/tools")
    # Preserve optional cache telemetry only if it is internally consistent.
    cache = {k: usage[k] for k in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens") if k in usage}
    if (any(type(v) is not int or not 0 <= v <= usage["prompt_tokens"] for v in cache.values()) or
            len(cache) == 2 and sum(cache.values()) != usage["prompt_tokens"]):
        raise ValueError("prefix response cache usage is inconsistent")
    prefix, content = request.body["messages"][-1]["content"], parsed["continuation"]

    def matches_target(text: str) -> bool:
        try:
            value = _response_json(text.encode("utf-8"))
        except (ValueError, UnicodeError):
            return False
        return set(value) == {"answer"} and type(value["answer"]) is int and value["answer"] == 2

    return {"response_valid": True, "returned_model_id": parsed["model"], "returned_model_identity_status": "exact",
            "finish_reason": parsed["finish_reason"],
            "usage": {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")} | cache,
            "syntax_observation": {"prefix_plus_response_matches_target": matches_target(prefix + content),
                                   "response_alone_matches_target": matches_target(content)},
            "semantic_proof": "not_established",
            "semantic_limit": "response prefix inclusion contract and a field-ignored control are not established",
            "case_observation": "accepted_response_envelope_observed", "failure": "prefix_semantics_not_established"}


def send_deepseek_prefix_probe(*, catalog: Catalog, interface_id: str, model_id: str, case_id: str,
                              api_key: str, conditions: PrefixLiveConditions, timeout_sec: float = 60,
                              request: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Send one exact bound case, only after all MPDB and execution gates pass.

    ``request`` is an offline-test transport seam. The default Session disables
    environment credentials/proxies, redirects and retries. The return value is
    a payload-free summary. The shared private ledger retains sanitized request
    and response evidence; the credential is neither discovered nor persisted.
    """
    if not isinstance(conditions, PrefixLiveConditions):
        raise ValueError("explicit prefix live conditions are required")
    conditions.validate()
    if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, (int, float)) or not 1 <= timeout_sec <= 120:
        raise ValueError("timeout_sec must be in [1, 120]")
    if (not isinstance(api_key, str) or not api_key or
            any(ord(c) < 33 or ord(c) > 126 for c in api_key)):
        raise ValueError("an in-memory API key without whitespace or control characters is required")
    resolved, case = _bound_case(catalog, interface_id, model_id, case_id)
    body, _ = _prepare(case, model_id)
    from .test_runner.adapters.deepseek_beta import _binding, execute_bound_observation
    item = {'case_id': case_id, 'model_id': model_id, 'target_binding': _binding(resolved),
            'body': case['body'], 'request_sha256': hashlib.sha256(body).hexdigest()}
    record = execute_bound_observation(item, group='probe', api_key=api_key, catalog=catalog,
                                       request=request, timeout_sec=timeout_sec)
    return record['public_result']
