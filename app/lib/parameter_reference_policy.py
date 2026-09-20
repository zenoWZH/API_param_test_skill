"""Source-local controls for the existing non-thinking sampling probe.

This module contains probe intent, not model-specific sampling values. Values
and permitted modes come from the selected Interface's reviewed constraints.
"""
from __future__ import annotations

import copy
import json
import math
import re
from typing import Any

# Values of the shared test fixture, independent of any provider/model facts.
_SHARED_SAMPLING_FIXTURE = {"temperature": 0.7, "top_p": 0.9}


def _same_json(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def non_thinking_probe_policy(capability: dict[str, Any], profile: str) -> dict[str, Any]:
    if profile != "sampling_non_thinking":
        return {}
    snapshot = capability.get("model_profile_database") or {}
    source = capability.get("source_id") or snapshot.get("source_id")
    family = capability.get("canonical_family_id") or capability.get("family")
    if source != "moonshot" or family != "kimi" or capability.get("api_form") != "openai_chat_completions":
        return {}
    constraints = capability.get("parameter_constraints")
    if not isinstance(constraints, dict):
        constraints = (snapshot.get("interface") or {}).get("parameter_constraints") or {}
    thinking = constraints.get("thinking.type")
    if not isinstance(thinking, dict) or "allowed_values" not in thinking:
        return {}
    allowed = thinking["allowed_values"]
    if not isinstance(allowed, list) or not allowed or any(value not in {"enabled", "disabled"} for value in allowed):
        raise ValueError("source thinking.type allowed_values are malformed")
    capabilities = capability.get("parameter_capabilities") or {}
    required = {"thinking.type"} | ({"temperature", "top_p"} if "disabled" in allowed else set())
    if any(not isinstance(capabilities.get(field), dict) or capabilities[field].get("state") != "supported"
           for field in required):
        raise ValueError("source does not certify the required thinking/sampling request fields")
    policy = {"source_id": source, "interface_id": capability.get("interface_id") or capability.get("model_api_profile_id"),
              "reference_source": capability.get("reference_source"), "profile": profile,
              "requested_thinking_type": "disabled", "parameter_effect_verified": False,
              "parameter_constraints": copy.deepcopy(constraints)}
    if "disabled" in allowed:
        return {**policy, "expectation": "supported", "diagnostic_kind": "fixed_sampling_in_supported_non_thinking_mode",
                "target_parameter": "thinking.type, temperature, top_p"}
    if thinking.get("always_on") is True and "disabled" in thinking.get("documented_rejected_values", []):
        return {**policy, "expectation": "unsupported", "diagnostic_kind": "explicit_unsupported_thinking_mode",
                "target_parameter": "thinking.type", "rejection_parameter": "thinking.type"}
    raise ValueError("non-thinking probe is not supported and has no documented rejection contract")


def validate_probe_reference_identity(
    capability: dict[str, Any], *, model: str, family: str, api_form: str, reference_source: str,
    source_id: str,
) -> None:
    snapshot = capability.get("model_profile_database") or {}
    target = snapshot.get("execution_target") or capability.get("execution_target") or {}
    selected_model = target.get("request_model_id") or capability.get("model")
    profile_id = capability.get("profile_id")
    interface_id = capability.get("interface_id") or capability.get("model_api_profile_id")
    if (selected_model != model or capability.get("reference_source") != reference_source
            or (capability.get("source_id") or snapshot.get("source_id")) != source_id
            or capability.get("api_form") != api_form
            or (capability.get("canonical_family_id") or capability.get("family")) != family
            or not isinstance(profile_id, str) or not profile_id.startswith(f"text/{source_id}/{family}/")
            or not isinstance(interface_id, str) or not interface_id.startswith(profile_id + "#")
            or snapshot.get("profile_id", profile_id) != profile_id
            or snapshot.get("interface_id", interface_id) != interface_id):
        raise ValueError("parameter reference identity does not match this model/Interface/API request")


def apply_non_thinking_probe_policy(
    settings: dict[str, Any], policy: dict[str, Any], *,
    default_settings: dict[str, Any] | None = None,
    explicit_overrides: dict[str, Any] | None = None,
    omitted_parameters: list[str] | None = None,
) -> dict[str, Any]:
    if not policy:
        return {}
    thinking = settings.get("thinking")
    if not isinstance(thinking, dict) or thinking.get("type") != "disabled":
        raise ValueError("the non-thinking probe must retain its explicit thinking.disabled intent")
    defaults = default_settings if default_settings is not None else copy.deepcopy(settings)
    explicit = set(explicit_overrides or {})
    omitted = set(omitted_parameters or [])
    customized = {key for key, value in _SHARED_SAMPLING_FIXTURE.items()
                  if key in defaults and not _same_json(defaults[key], value)}
    result = {key: copy.deepcopy(value) for key, value in policy.items() if key != "parameter_constraints"}
    result["requested_sampling"] = {key: settings[key] for key in ("temperature", "top_p") if key in settings}
    result["preserved_explicit_parameters"] = sorted(explicit & {"thinking", "temperature", "top_p"})
    result["omitted_explicit_parameters"] = sorted(omitted & {"thinking", "temperature", "top_p"})
    result["customized_fixture_parameters"] = sorted(customized)
    result["effective_thinking"] = copy.deepcopy(thinking)
    result["sampling_effect_tested"] = False
    default_fields = {key for key, value in _SHARED_SAMPLING_FIXTURE.items()
                      if key in defaults and _same_json(defaults[key], value)
                      and key not in explicit and key not in omitted}
    if policy["expectation"] == "unsupported":
        # Remove only shared defaults. Explicit user controls remain on wire;
        # a rejection of one of those controls cannot pass as thinking proof.
        for key in default_fields:
            settings.pop(key, None)
        result["omitted_companion_parameters"] = sorted(default_fields)
    else:
        constraints = policy["parameter_constraints"]
        temperature = (constraints.get("temperature") or {}).get("const_by_thinking_type", {}).get("disabled")
        top_p = (constraints.get("top_p") or {}).get("const")
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in (temperature, top_p)):
            raise ValueError("non-thinking sampling requires reviewed source temperature/top_p constants")
        if not 0 <= temperature <= 2 or not 0 <= top_p <= 1:
            raise ValueError("source sampling constants are outside the request schema")
        required = {"temperature": temperature, "top_p": top_p}
        for key in default_fields:
            settings[key] = required[key]
        result["applied_default_sampling"] = {key: required[key] for key in sorted(default_fields)}
        result["required_sampling"] = required
        result["disabled_response_reasoning_content"] = constraints["thinking.type"].get("disabled_response_reasoning_content")
        invalid = [key for key, required_value in required.items()
                   if key in settings and (type(settings[key]) not in (int, float) or settings[key] != required_value)]
        if len(invalid) > 1:
            raise ValueError("isolate one explicit invalid sampling parameter per reference probe")
        if invalid:
            result.update(expectation="unsupported", diagnostic_kind="explicit_invalid_sampling_value",
                          target_parameter=invalid[0], rejection_parameter=invalid[0])
    result["effective_sampling"] = {key: copy.deepcopy(settings[key]) for key in ("temperature", "top_p") if key in settings}
    if result["expectation"] == "supported":
        result["target_parameter"] = ", ".join(["thinking.type", *result["effective_sampling"]])
    return result


def validate_reference_request(body: dict[str, Any], policy: dict[str, Any]) -> None:
    if not policy:
        return
    if not _same_json(body.get("thinking"), policy["effective_thinking"]):
        raise ValueError("source-required thinking.disabled was removed or changed during construction")
    actual = {key: body[key] for key in ("temperature", "top_p") if key in body}
    if not _same_json(actual, policy["effective_sampling"]):
        raise ValueError("effective sampling controls changed during construction")


def validate_reference_response(policy: dict[str, Any], response: dict[str, Any], status_code: int | None) -> str | None:
    if not policy:
        return None
    if policy["expectation"] == "supported":
        if status_code is None or not 200 <= status_code < 300 or policy.get("disabled_response_reasoning_content") != "absent_or_empty":
            return None
        for choice in response.get("choices") or []:
            value = (choice.get("message") or {}).get("reasoning_content")
            if value is not None and (not isinstance(value, str) or value.strip()):
                return "thinking_content_unexpected"
        return None
    if status_code not in {400, 422}:
        return None
    field = policy["rejection_parameter"]
    failure = "thinking_rejection_not_attributed" if field == "thinking.type" else "parameter_rejection_not_attributed"
    error = response.get("error")
    if not isinstance(error, dict) or error.get("type") not in {"invalid_request_error", "validation_error"}:
        return failure
    if response.get("choices"):
        return failure
    param = error.get("param")
    message = str(error.get("message") or "")
    if field == "thinking.type" and any(
        subfield != "type" for subfield in re.findall(r"\bthinking\.([A-Za-z0-9_]+)", message)
    ):
        return failure
    if param is not None:
        accepted_fields = {"thinking", "thinking.type"} if field == "thinking.type" else {field}
        if (field == "thinking.type" and param == "thinking"
                and set(policy.get("effective_thinking") or {}) != {"type"}
                and not re.search(r"thinking\.type|only\s+type\s*[=:]\s*enabled", message, re.I)):
            return failure
        return None if param in accepted_fields else failure
    if field == "thinking.type":
        path = r"thinking(?:\.type)?(?![A-Za-z0-9_.])"
        matched = re.search(r"\b(?:invalid|unsupported)\s+" + path, message, re.I)
        if not matched:
            matched = re.search(r"\b" + path + r"[^\n]*(?:only\s+(?:type\s*[=:]\s*)?enabled|cannot\s+be\s+disabled|does\s+not\s+support\s+disabled)", message, re.I)
    else:
        path = re.escape(field) + r"(?![A-Za-z0-9_.])"
        matched = re.search(r"\b(?:invalid|unsupported)\s+" + path, message, re.I)
        if not matched:
            matched = re.search(r"\b" + path + r"[^\n]*(?:invalid|must|only|not\s+supported)", message, re.I)
    return None if matched else failure


def parameter_coverage_for_profiles(
    rows: list[dict[str, Any]], profiles: list[str], *,
    capability: dict[str, Any] | None = None,
    results: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """Use the same model/control scope for parameter rows and run summaries."""
    selected = set(profiles)
    observed: dict[str, list[dict[str, Any]]] = {}
    for result in results or []:
        policy = result.get("parameter_reference_policy")
        if isinstance(policy, dict) and policy:
            observed.setdefault(str(result.get("profile") or ""), []).append(policy)
    tested, untested = [], []
    for row in rows:
        parameter = str(row["parameter"])
        candidates = selected.intersection(row.get("test_profiles") or [])
        if capability is not None and parameter in {"temperature", "top_p"}:
            eligible = set()
            for profile in candidates:
                policies = observed.get(profile) or [non_thinking_probe_policy(capability, profile)]
                if any(not policy or (
                    policy.get("rejection_parameter") == parameter if policy.get("expectation") == "unsupported"
                    else parameter in policy.get("effective_sampling", {"temperature": None, "top_p": None})
                ) for policy in policies):
                    eligible.add(profile)
            candidates = eligible
        covered = row.get("coverage_mode") == "selection" or bool(candidates)
        (tested if covered else untested).append(parameter)
    return tested, untested
