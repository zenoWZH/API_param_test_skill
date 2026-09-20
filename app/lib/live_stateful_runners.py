"""Official resource gates and validators with shared-workflow entry points.

The caller supplies an exact executable MPDB target and a reviewed execution
plan. No provider overlay or credential discovery occurs here. These senders
enforce request/time/output limits, not a currency spend limit; callers must
price the fixed input and limits before invoking them.
Public lifecycle functions compile create/use/poll steps into the common engine.
The shared dispatcher applies complete response deadlines and durable ownership;
the legacy private _Sequence remains only for compatibility with version checks.
"""
from __future__ import annotations

from .gemini_api_version import require_ai_studio_v1beta_url

import copy
from dataclasses import dataclass
from datetime import datetime
import json
import re
import time
from typing import Any, Callable

from .offline_stateful_runners import OfflineModelTarget
from .parameter_output_limit import enforce_parameter_test_output_limit
from . import ai_studio_interactions_scope as interaction_scope


@dataclass(frozen=True)
class LifecycleLimits:
    max_requests: int
    timeout_sec: float
    max_run_seconds: float
    max_output_tokens: int
    max_input_chars: int
    retention: str
    max_polls: int = 3
    poll_interval_sec: float = 1
    cleanup_timeout_sec: float = 15
    max_response_bytes: int = 2 * 1024 * 1024

    def validate(self, minimum_requests: int) -> None:
        for name, low, high in (("max_requests", minimum_requests, 20),
                                ("max_output_tokens", 256, 4096),
                                ("max_input_chars", 1, 100000), ("max_polls", 0, 10),
                                ("max_response_bytes", 1024, 2 * 1024 * 1024)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer in [{low}, {high}]")
        for name, low, high in (("timeout_sec", 1, 120), ("max_run_seconds", 1, 600),
                                ("poll_interval_sec", 0.1, 10), ("cleanup_timeout_sec", 1, 30)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise ValueError(f"invalid {name}")
        if self.retention != "delete_created_resources_finally":
            raise ValueError("this bounded runner requires finally deletion of created resources")


class LiveLifecycleError(RuntimeError):
    def __init__(self, result: dict[str, Any]):
        super().__init__("Official lifecycle did not complete; inspect the payload-free result")
        self.result = result


def _target(catalog: Any, interface_id: str, model: str, source: str, form: str,
            required_parameters: tuple[str, ...], api_version: str,
            path_template: str) -> OfflineModelTarget:
    target = OfflineModelTarget.from_catalog(catalog, interface_id=interface_id,
                                             request_model_id=model, expected_api_form=form)
    if (target.source_id, target.modality, target.family_id) != (source, "text", "gemini"):
        raise ValueError("lifecycle requires the exact official Gemini source and text Interface")
    interface = catalog.get_interface(interface_id)
    profile = catalog.get_profile(target.profile_id)
    if (interface.get("enabled") is not True or interface.get("executable") is not True
            or profile.get("profile_state") != "executable"
            or profile.get("lifecycle") != "active"
            or interface.get("lifecycle", "active") != "active"
            or interface.get("test_binding_status") != "required"):
        raise ValueError("lifecycle target is not executable in the current MPDB")
    if (interface.get("routing_mode") != source
            or interface_id not in (profile.get("interface_ids") or [])
            or interface.get("interface_id") != interface_id):
        raise ValueError("lifecycle Interface must have exact official routing and Profile linkage")
    for row in (interface, profile):
        ids = row.get("request_model_ids")
        if not isinstance(ids, list) or model not in ids:
            raise ValueError("lifecycle requires a model ID declared by both Interface and Profile")
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", model):
        raise ValueError("model must be an exact Gemini model ID")
    # Resolve the exact current model policy as well as the Interface. A
    # permissive offline identity fixture must never unlock real dispatch.
    policies = [b for b in (interface.get("test_bindings") or [])
                if isinstance(b, dict) and b.get("extension_type") == "model_test_policy"
                and b.get("parameter_test_enabled") is True]
    if (len(policies) != 1 or not isinstance(policies[0].get("test_binding_id"), str)
            or not policies[0]["test_binding_id"] or policies[0].get("interface_id") != interface_id):
        raise ValueError("lifecycle requires one exact executable model policy")
    versions = interface.get("api_versions")
    version = versions.get(api_version) if isinstance(versions, dict) else None
    if (not isinstance(version, dict) or version.get("path_template") != path_template
            or policies[0].get("api_version") != api_version):
        raise ValueError("lifecycle API version/path is not bound by the exact Interface and policy")
    resolved = catalog.resolve_parameter_config(
        source_id=source, modality="text", family_id="gemini", model_slug=target.model_slug,
        interface_id=interface_id, api_form=form, test_binding_id=policies[0].get("test_binding_id"),
    )
    expected = {"source_id": source, "modality": "text", "family_id": "gemini",
                "model_slug": target.model_slug, "profile_id": target.profile_id,
                "interface_id": interface_id, "api_form": form,
                "test_binding_id": policies[0]["test_binding_id"]}
    if not isinstance(resolved, dict) or any(resolved.get(k) != v for k, v in expected.items()):
        raise ValueError("lifecycle parameter resolution did not return the exact bound target")
    contract = resolved.get("contract")
    if (not isinstance(contract, dict) or contract.get("source_ids") != [source]
            or contract.get("api_form") != form or contract.get("family_id") != "gemini"
            or not resolved.get("test_cases")):
        raise ValueError("lifecycle needs an executable source-local parameter Contract")
    capabilities = contract.get("parameter_capabilities")
    for parameter in required_parameters:
        capability = capabilities.get(parameter) if isinstance(capabilities, dict) else None
        if not isinstance(capability, dict) or capability.get("state") != "supported":
            raise ValueError("lifecycle state operation is not supported by the exact parameter Contract")
    return target


def _text(value: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("lifecycle input must be non-empty and within max_input_chars")
    return value


class _Sequence:
    """Retired sender retained only for the historical version-policy guard.

    Real lifecycle execution belongs to the compiled shared engine. Keeping the
    strict URL check first preserves diagnostic compatibility without an alternate
    uncounted network boundary.
    """
    def __init__(self, limits: LifecycleLimits, headers: dict[str, str], request: Callable[..., Any] | None):
        self.limits = limits
        self.count = 0
        self.events: list[dict[str, Any]] = []

    def call(self, operation: str, method: str, url: str, body: dict[str, Any] | None = None,
             *, reserve: int = 0, cleanup: bool = False) -> dict[str, Any]:
        require_ai_studio_v1beta_url(url)
        raise RuntimeError("Legacy lifecycle sends are retired; use the shared workflow entry points")

    def close(self) -> None:
        pass


def _credential(value: str) -> str:
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError("an in-memory official credential is required")
    return value


def _response_json(data: bytes) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate response member")
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError("nonfinite response value")

    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)
    except (ValueError, UnicodeError):
        raise ValueError("official lifecycle response is not valid JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("official lifecycle response must be an object")
    return payload


def _usage(usage: Any, *, interactions: bool, output_limit: int) -> dict[str, int]:
    if not isinstance(usage, dict):
        raise ValueError("official lifecycle response lacks usage")
    keys = ("total_input_tokens", "total_output_tokens", "total_tokens",
            "total_thought_tokens", "total_cached_tokens", "total_tool_use_tokens") if interactions else (
            "promptTokenCount", "candidatesTokenCount", "totalTokenCount",
            "thoughtsTokenCount", "cachedContentTokenCount", "toolUsePromptTokenCount")
    counts = {}
    for i, key in enumerate(keys):
        value = usage.get(key, 0 if i >= 3 else None)
        if type(value) is not int or value < 0:
            raise ValueError("official lifecycle usage must contain nonnegative integer counts")
        counts[key] = value
    input_count, output, total, thought, cached, tool = (counts[key] for key in keys)
    if input_count <= 0 or output <= 0 or cached > input_count:
        raise ValueError("official lifecycle usage does not match nonempty text input/output")
    if tool:
        raise ValueError("tool usage is outside this text-only lifecycle")
    if total != input_count + output + thought or output + thought > output_limit:
        raise ValueError("official lifecycle token arithmetic or output allowance mismatch")
    if not interactions and cached <= 0:
        raise ValueError("Vertex use lacks positive explicit-cache telemetry")
    return counts


def _cache_expiry(created: dict, ttl_seconds: int) -> None:
    try:
        start = datetime.fromisoformat(created["createTime"].replace("Z", "+00:00"))
        expiry = datetime.fromisoformat(created["expireTime"].replace("Z", "+00:00"))
        if start.tzinfo is None or expiry.tzinfo is None or not 0 < (expiry - start).total_seconds() <= ttl_seconds + 1:
            raise ValueError("invalid cache lifetime")
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError("Vertex cache response does not confirm the bounded requested TTL") from None


def _interaction_text(response: dict, previous: str | None) -> str:
    # Current official v1beta resources use steps, not legacy outputs:
    # https://ai.google.dev/api/interactions-api
    if (response.get("object") != "interaction" or
            "previous_interaction_id" in response and response["previous_interaction_id"] != previous):
        raise ValueError("Interaction resource or previous-interaction linkage mismatch")
    if "errors" in response and response["errors"] != []:
        raise ValueError("Interaction resource contains diagnostic faults or invalid errors")
    steps = response.get("steps")
    if not isinstance(steps, list):
        raise ValueError("Interaction response has no official steps envelope")
    text_parts = []
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Malformed Interaction step")
        if step.get("type") == "model_output":
            content = step.get("content")
            if not isinstance(content, list):
                raise ValueError("Malformed Interaction model output")
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str):
                    raise ValueError("Unexpected non-text Interaction output")
                text_parts.append(part["text"])
        elif step.get("type") not in {"user_input", "thought"}:
            raise ValueError("Interaction lifecycle observed an unrequested tool or step")
    text = "".join(text_parts).strip()
    if not text:
        raise ValueError("Interaction completed without model text output")
    return text


def resolve_ai_studio_lifecycle_target(catalog: Any) -> OfflineModelTarget:
    """Resolve only approval #4's dedicated lifecycle gate, never generic Param.

    The normal executable/parameter gates remain closed on this Interface and
    both Test Bindings. A separately named scope authorizes exactly one chain.
    """
    s = interaction_scope
    target = OfflineModelTarget.from_catalog(catalog, interface_id=s.INTERFACE_ID,
                                             request_model_id=s.MODEL, expected_api_form=s.FORM)
    profile = catalog.get_profile(s.PROFILE_ID)
    interface = catalog.get_interface(s.INTERFACE_ID)
    parameter, policy = catalog.get_test_binding(s.PARAMETER_ID), catalog.get_test_binding(s.POLICY_ID)
    contract = interface.get("contracts", {}).get(s.CONTRACT_ID)
    if not isinstance(contract, dict):
        raise ValueError("dedicated lifecycle Contract is absent")
    if (target.source_id != s.SOURCE or target.modality != "text" or target.family_id != "gemini"
            or target.model_slug != s.MODEL or target.profile_id != s.PROFILE_ID
            or profile.get("profile_state") != "executable" or profile.get("lifecycle") != "active"
            or profile.get("request_model_ids") != [s.MODEL] or s.INTERFACE_ID not in profile.get("interface_ids", [])
            or interface.get("request_model_ids") != [s.MODEL] or interface.get("routing_mode") != s.SOURCE
            or interface.get("transport_adapter_id") != "gemini_interactions"
            or interface.get("contract_ids") != [s.CONTRACT_ID] or interface.get("default_contract_id") != s.CONTRACT_ID
            or interface.get("default_api_version") != s.API_VERSION
            or interface.get("api_versions") != {s.API_VERSION: {"stability": "beta", "path_template": s.PATH}}):
        raise ValueError("dedicated lifecycle source/model/version/route differs")
    for obj in (interface, contract, parameter, policy):
        if (obj.get("source_id") != s.SOURCE or obj.get("enabled") is not True
                or any(obj.get(k) is not False for k in ("executable", "runner_enabled", "parameter_test_enabled", "pressure_test_enabled"))
                or obj.get("lifecycle_test_enabled") is not True or obj.get("lifecycle_scope") != s.SCOPE
                or type(obj.get("lifecycle_request_cap")) is not int or obj["lifecycle_request_cap"] != 4
                or obj.get("lifecycle_runner_id") != s.CASE_ID or obj.get("test_binding_status") != "required"):
            raise ValueError("dedicated lifecycle gate or generic execution boundary changed")
    encoded = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if any(encoded(obj.get("lifecycle_limits")) != encoded(s.LIMITS) for obj in (interface, parameter, policy)):
        raise ValueError("dedicated lifecycle limits differ from the four-request fixture")
    if (contract.get("source_ids") != [s.SOURCE] or contract.get("source_id") != s.SOURCE
            or contract.get("contract_id") != s.CONTRACT_ID or contract.get("api_form") != s.FORM
            or contract.get("family_id") != "gemini" or contract.get("routing_mode") != s.SOURCE
            or parameter.get("extension_type") != "parameter" or parameter.get("contract_id") != s.CONTRACT_ID
            or parameter.get("test_binding_id") != s.PARAMETER_ID or parameter.get("test_cases") != [s.CASE_ID]
            or policy.get("extension_type") != "model_test_policy" or policy.get("interface_id") != s.INTERFACE_ID
            or policy.get("test_binding_id") != s.POLICY_ID or policy.get("test_cases") != [s.CASE_ID]
            or policy.get("reference_contract_ids") != [s.CONTRACT_ID] or policy.get("default_reference_contract_id") != s.CONTRACT_ID
            or any(obj.get("api_version") != s.API_VERSION for obj in (parameter, policy))):
        raise ValueError("dedicated lifecycle Contract or exact bindings changed")
    capabilities = {**contract.get("parameter_capabilities", {}), **interface.get("parameter_capabilities", {})}
    for field in ("model", "input", "store", "stream", "background", "previous_interaction_id", "generation_config.max_output_tokens", "response.id", "response.model", "response.status", "response.steps", "response.usage"):
        if not isinstance(capabilities.get(field), dict) or capabilities[field].get("state") != "supported":
            raise ValueError("dedicated lifecycle field is not supported")
    definitions = parameter.get("case_definitions")
    if not isinstance(definitions, list) or len(definitions) != 1 or definitions[0].get("case_id") != s.CASE_ID:
        raise ValueError("dedicated lifecycle case definition is missing or expanded")
    definition = definitions[0]
    operations = definition.get("operations")
    if (definition.get("runner") != "lib.live_stateful_runners.run_ai_studio_interactions"
            or definition.get("dedicated_resolver") != "lib.live_stateful_runners.resolve_ai_studio_lifecycle_target"
            or definition.get("generic_parameter_runner_supported") is not False
            or definition.get("lifecycle_scope") != s.SCOPE or encoded(definition.get("limits")) != encoded(s.LIMITS)
            or not isinstance(operations, list) or len(operations) != 4
            or encoded([op.get("ordinal") for op in operations]) != "[1,2,3,4]"):
        raise ValueError("dedicated lifecycle runner, scope or operations changed")
    for index, op in enumerate(operations):
        if index < 2:
            expected_body = s.body_for(None if index == 0 else "{owned_parent_id}")
            if (op.get("method") != "POST" or op.get("url") != s.URL
                    or encoded(op.get("body")) != encoded(expected_body)
                    or op.get("expected_model_text") != (s.ACK if index == 0 else s.NONCE_TEMPLATE)):
                raise ValueError("dedicated lifecycle nonce request or response fixture changed")
        elif (op.get("method") != "DELETE" or op.get("finally") is not True
              or op.get("only_if_created_id_known") is not True
              or op.get("url_template") != s.URL + ("/{owned_child_id}" if index == 2 else "/{owned_parent_id}")):
            raise ValueError("dedicated lifecycle cleanup order or ownership changed")
    return target


def validate_vertex_cached_response(response: dict[str, Any], *, model: str, output_limit: int) -> dict[str, int]:
    """Pure cached GenerateContent envelope, signature, identity and usage checks."""
    if response.get("modelVersion") != model:
        raise ValueError("Vertex use returned model mismatch")
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], dict):
        raise ValueError("Vertex use has no candidate")
    candidate = candidates[0]
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    feedback = response.get("promptFeedback")
    if (candidate.get("finishReason") != "STOP" or not isinstance(parts, list)
            or content.get("role") != "model"
            or isinstance(feedback, dict) and feedback.get("blockReason")
            or any(not isinstance(part, dict) or not isinstance(part.get("text"), str)
                   or set(part) - {"text", "thought", "thoughtSignature"}
                   or "thought" in part and type(part["thought"]) is not bool
                   or "thoughtSignature" in part and not isinstance(part["thoughtSignature"], str)
                   for part in parts)
            or not any(isinstance(part, dict) and isinstance(part.get("text"), str)
                       and part["text"].strip() and part.get("thought") is not True for part in parts)):
        raise ValueError("Vertex use did not finish with text")
    return _usage(response.get("usageMetadata"), interactions=False, output_limit=output_limit)


def run_vertex_cached_content(*, catalog: Any, interface_id: str, model: str,
                              project: str, project_number: str, location: str,
                              access_token: str, cache_text: str, prompt: str,
                              ttl_seconds: int, limits: LifecycleLimits,
                              request: Callable[..., Any] | None = None,
                              evidence_dir=None) -> dict[str, Any]:
    """Run the compiled create/use/delete workflow for one exact Vertex cache."""
    from .test_runner.adapters.gemini_resources import prepare_vertex_cached_content_plan, execute_gemini_resource_plan
    plan, registry = prepare_vertex_cached_content_plan(
        catalog=catalog, interface_id=interface_id, model=model, project=project,
        project_number=project_number, location=location, cache_text=cache_text,
        prompt=prompt, ttl_seconds=ttl_seconds, limits=limits,
    )
    result, _ = execute_gemini_resource_plan(plan, registry, credential=access_token, request=request, evidence_dir=evidence_dir)
    if not result["success"]:
        raise LiveLifecycleError(result)
    return result


def run_ai_studio_interactions(*, catalog: Any, interface_id: str, model: str,
                               api_key: str, first_input: str, next_input: str,
                               state_nonce: str, background: bool, limits: LifecycleLimits,
                               request: Callable[..., Any] | None = None,
                               evidence_dir=None) -> dict[str, Any]:
    """Execute the existing exact gated state-chain through shared ownership/cleanup."""
    from .test_runner.adapters.gemini_resources import prepare_ai_studio_interactions_plan, execute_gemini_resource_plan
    plan, registry = prepare_ai_studio_interactions_plan(
        catalog=catalog, interface_id=interface_id, model=model, first_input=first_input,
        next_input=next_input, state_nonce=state_nonce, background=background, limits=limits,
        dedicated_only=False,
    )
    result, _ = execute_gemini_resource_plan(plan, registry, credential=api_key, request=request, evidence_dir=evidence_dir)
    if not result["success"]:
        raise LiveLifecycleError(result)
    return result
