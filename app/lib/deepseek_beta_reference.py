"""Pure execution plan and adjudication for five approved Pro 0813 beta cases.

No files, environment, catalog singleton, credentials, or HTTP are accessed.
The caller supplies the immutable MPDB job snapshot and retains the original
response text. Transport code must dispatch each returned request once, in
order, using ``body_bytes`` without adding an identity probe or retries.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from .token_audit import (
    TOKEN_AUDIT_SCHEMA_VERSION, audit_exchange, combine_exchange_audits,
    summarize_token_audits,
)

SOURCE_ID = FAMILY_ID = "deepseek"
MODEL_SLUG = "deepseek-v4-pro-0813"
REQUEST_MODEL_ID = "deepseek-v4-pro"
PROFILE_ID = "text/deepseek/deepseek/" + MODEL_SLUG
INTERFACE_ID = PROFILE_ID + "#deepseek-beta-chat-prefix"
CONTRACT_ID = "deepseek_v4_pro_0813_chat_prefix_beta"
API_FORM = "deepseek_beta_chat_prefix"
TRANSPORT = "deepseek-beta-chat-prefix"
ENDPOINT = "https://api.deepseek.com/beta/chat/completions"
POLICY_BINDING_ID = "interface/" + INTERFACE_ID.replace("#", "/")
PARAMETER_BINDING_ID = "parameter/" + CONTRACT_ID
OBSERVATION_KEY = "bounded_prefix_observations_20260908"
SUMMARY_SHA256 = "99be7f4081944580efb285a6c07bd209b448a2072df0ce6b4c1f26365edc55b2"
FACT_SHA256 = "ea7ba5a30365451b52ca2a2d3b36c38dd797c5db61e0a43f79e3949e6b978351"
# Integrity pin for the installed Pro observation, not a second parameter DB.
# Request bodies and case expectations are read only from the supplied Binding.
OBSERVATION_SHA256 = "105be04c476011374fd36a8c5c1b4e890e39647aaa8f28e6e2a602d2767cf973"
EXECUTION_SCOPE = "official_documented_beta_prefix_bounded_parameter_execution_live_unverified"
CASE_IDS = (
    "deepseek_beta_prefix_matrix_false_control",
    "deepseek_beta_prefix_matrix_true_json",
    "deepseek_beta_prefix_matrix_invalid_type",
    "deepseek_beta_prefix_stop_followup_baseline",
    "deepseek_beta_prefix_stop_followup_enabled",
)
REQUEST_CAP = 5
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("Beta value is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _object(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(label + " must be an object")
    return value


def _require_values(record: dict, expected: dict, label: str) -> None:
    for key, value in expected.items():
        if canonical_bytes(record.get(key)) != canonical_bytes(value):
            raise ValueError(label + "." + key + " differs from the approved beta scope")


def decode_beta_response(raw: bytes | str) -> dict:
    """Decode original JSON without losing duplicate-member/type evidence."""
    if not isinstance(raw, (bytes, str)):
        raise ValueError("Original beta response must be bytes or text")
    try:
        data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        if not data or len(data) > MAX_RESPONSE_BYTES:
            raise ValueError("Beta response is empty or exceeds the bounded size")
        text = data.decode("utf-8")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate beta JSON member")
                result[key] = value
            return result
        def finite(value):
            raise ValueError("Nonfinite beta JSON number")
        return _object(json.loads(text, object_pairs_hook=unique, parse_constant=finite), "Beta JSON")
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Invalid beta JSON encoding or syntax") from exc


def _check_snapshot(snapshot: dict) -> tuple[dict, list[dict]]:
    _object(snapshot, "MPDB snapshot")
    _require_values(snapshot, {"snapshot_schema_version": 1, "source_id": SOURCE_ID, "modality": "text",
        "family_id": FAMILY_ID, "suite_family_id": FAMILY_ID, "model_slug": MODEL_SLUG, "profile_id": PROFILE_ID,
        "interface_id": INTERFACE_ID, "api_form": API_FORM, "catalog_resolved": True,
        "test_binding_id": POLICY_BINDING_ID, "reference_contract_id": CONTRACT_ID,
        "parameter_test_binding_id": PARAMETER_BINDING_ID}, "Snapshot")
    for name in ("catalog_digest", "test_extension_digest", "snapshot_digest"):
        if not isinstance(snapshot.get(name), str) or not re.fullmatch("[0-9a-f]{64}", snapshot[name]):
            raise ValueError("Missing or malformed MPDB " + name)
    for name in ("package_version", "catalog_version"):
        if not isinstance(snapshot.get(name), str) or not snapshot[name].strip():
            raise ValueError("Missing MPDB " + name)
    payload = {k: v for k, v in snapshot.items() if k != "snapshot_digest"}
    if _digest(payload) != snapshot["snapshot_digest"]:
        raise ValueError("Immutable MPDB snapshot digest mismatch")
    profile = _object(snapshot.get("profile"), "Profile")
    interface = _object(snapshot.get("interface"), "Interface")
    contract = _object(snapshot.get("reference_contract"), "Contract")
    policy = _object(snapshot.get("test_binding"), "Policy Binding")
    parameter = _object(snapshot.get("parameter_test_binding"), "Parameter Binding")
    common = {"source_id": SOURCE_ID, "modality": "text", "family_id": FAMILY_ID,
              "model_slug": MODEL_SLUG, "profile_id": PROFILE_ID}
    _require_values(profile, {**common, "canonical_model_id": "deepseek/" + MODEL_SLUG,
        "request_model_ids": [REQUEST_MODEL_ID], "lifecycle": "active", "profile_state": "executable"}, "Profile")
    interface_ids = profile.get("interface_ids")
    if (not isinstance(interface_ids, list) or not all(isinstance(iid, str) for iid in interface_ids)
            or len(interface_ids) != len(set(interface_ids)) or INTERFACE_ID not in interface_ids):
        raise ValueError("Profile does not own beta Interface")
    _require_values(interface, {**common, "interface_id": INTERFACE_ID, "api_form": API_FORM,
        "routing_mode": "vendor_direct", "request_model_ids": [REQUEST_MODEL_ID], "transport_adapter_id": TRANSPORT,
        "contract_ids": [CONTRACT_ID], "default_contract_id": CONTRACT_ID, "default_api_version": "beta",
        "api_versions": {"beta": {"stability": "beta", "path_template": "/beta/chat/completions"}}}, "Interface")
    _require_values(contract, {"contract_id": CONTRACT_ID, "source_id": SOURCE_ID, "source_ids": [SOURCE_ID],
        "family_id": FAMILY_ID, "api_form": API_FORM, "routing_mode": "vendor_direct",
        "test_binding_status": "required", "certification_scope": "official_documented_beta_prefix_live_unverified"}, "Contract")
    if contract.get("parent_contract_id") or contract.get("extends"):
        raise ValueError("The exact beta Contract must not inherit another Contract")
    for label, record in (("Profile", profile), ("Contract", contract)):
        if (record.get("disabled_reason") or any(flag in record and record[flag] is not True
                for flag in ("enabled", "executable", "runner_enabled", "parameter_test_enabled"))):
            raise ValueError(label + " explicitly disables the beta reference")
    _require_values(policy, {"test_binding_id": POLICY_BINDING_ID, "source_id": SOURCE_ID, "extension_type": "model_test_policy",
        "interface_id": INTERFACE_ID, "suite_family_id": FAMILY_ID, "canonical_family_id": FAMILY_ID,
        "reference_contract_ids": [CONTRACT_ID], "default_reference_contract_id": CONTRACT_ID, "api_version": "beta",
        "compatibility_family_alias": False}, "Policy Binding")
    _require_values(parameter, {"test_binding_id": PARAMETER_BINDING_ID, "source_id": SOURCE_ID,
        "extension_type": "parameter", "contract_id": CONTRACT_ID, "api_version": "beta"}, "Parameter Binding")
    for label, record in (("Interface", interface), ("Policy Binding", policy), ("Parameter Binding", parameter)):
        _require_values(record, {"enabled": True, "executable": True, "runner_enabled": True,
            "parameter_test_enabled": True, "pressure_test_enabled": False, "test_binding_status": "required",
            "certification_scope": EXECUTION_SCOPE}, label)
        if record.get("disabled_reason"):
            raise ValueError(label + " is disabled")
    if policy.get("pressure_profiles") != {}:
        raise ValueError("Beta policy must not carry pressure profiles")
    embedded = interface.get("test_bindings")
    if not isinstance(embedded, list):
        raise ValueError("Interface has no immutable policy Binding")
    matching = [r for r in embedded if isinstance(r, dict) and r.get("test_binding_id") == POLICY_BINDING_ID]
    if len(matching) != 1 or canonical_bytes(matching[0]) != canonical_bytes(policy):
        raise ValueError("Interface and snapshot policy Binding conflict")
    execution = _object(snapshot.get("execution_target"), "Execution target")
    _require_values(execution, {"request_model_id": REQUEST_MODEL_ID, "route_profile": "vendor_direct", "api_form": API_FORM}, "Execution target")
    if not isinstance(execution.get("provider_id"), str) or not execution["provider_id"].strip():
        raise ValueError("Beta execution target lacks provider identity")
    if execution.get("transport", TRANSPORT) != TRANSPORT:
        raise ValueError("Beta execution target transport conflict")
    metadata = _object(_object(interface.get("source_conflicts"), "Interface observations").get(OBSERVATION_KEY), "Beta observation")
    if _digest(metadata) != OBSERVATION_SHA256:
        raise ValueError("Installed Pro beta observation integrity pin mismatch")
    _require_values(metadata.get("summary_artifact", {}), {"path": "references/deepseek_prefix_observation_summary_20260908.json",
        "sha256": SUMMARY_SHA256, "json_pointer": "/targets/1"}, "Summary reference")
    _require_values(metadata.get("facts_artifact", {}), {"path": "references/approved_parameter_facts_deepseek_beta_20260907.json",
        "sha256": FACT_SHA256}, "Fact reference")
    candidate = metadata["pro_app_fixed_case_candidate"]
    _require_values(candidate, {"app_approval_leaf": "R7-APP-PARITY-DEEPSEEK-0813",
        "supported_case_ids": list(CASE_IDS), "eligible_for_item16_model_scope": True,
        "enabled_by_this_metadata": False}, "Pro fixed case candidate")
    manifest = metadata["binding_case_manifest"]
    expected_ids = [row["case_id"] for row in manifest]
    definitions = parameter.get("case_definitions")
    if (not isinstance(definitions, list) or not all(isinstance(row, dict) for row in definitions)
            or [row.get("case_id") for row in definitions] != expected_ids
            or len(set(expected_ids)) != len(expected_ids)
            or parameter.get("test_cases") != expected_ids or policy.get("test_cases") != expected_ids):
        raise ValueError("Beta Binding case list differs from its reviewed manifest")
    for definition, reference in zip(definitions, manifest):
        body = _object(definition.get("body"), "Case body")
        if (_digest(definition) != reference["case_definition_sha256"]
                or _digest(body) != reference["request_body_sha256"]):
            raise ValueError("Beta case definition or body digest mismatch")
    selected = [next(row for row in definitions if row["case_id"] == cid) for cid in CASE_IDS]
    for record in (policy, parameter):
        excluded = record.get("excluded_test_profiles", [])
        if (not isinstance(excluded, list) or not all(isinstance(cid, str) for cid in excluded)
                or any(cid in excluded for cid in CASE_IDS)):
            raise ValueError("An approved beta case is excluded or its exclusion policy is malformed")
        for name in ("expectations", "default_expectations"):
            overrides = _object(record.get(name, {}), "Beta case expectation overrides")
            if any(row["case_id"] in overrides and canonical_bytes(overrides[row["case_id"]]) != canonical_bytes(row["expectation"])
                   for row in selected):
                raise ValueError("Binding overrides a fixed beta case expectation")
        for name in ("model_expectations", "parameter_expectations", "default_parameter_expectations", "model_parameter_expectations"):
            if record.get(name, {}) != {}:
                raise ValueError("Additional beta expectation overrides require an explicit supported scope")
    for row in selected:
        _require_values(row["body"], {"model": REQUEST_MODEL_ID, "max_tokens": 512,
            "thinking": {"type": "disabled"}, "stream": False}, "Fixed beta body")
    contract_caps = _object(contract.get("parameter_capabilities"), "Contract capabilities")
    interface_caps = _object(interface.get("parameter_capabilities", {}), "Interface capabilities")
    for field_name in ("model", "messages", "messages[].prefix", "max_tokens", "thinking.type", "stop", "stream",
                       "response.model", "response.choices[].message.content", "response.choices[].finish_reason", "response.usage"):
        capability = _object(interface_caps.get(field_name, contract_caps.get(field_name)), "Effective beta capability")
        if capability.get("state") != "supported":
            raise ValueError("Effective beta parameter capability is unsupported")
    return metadata, selected


@dataclass(frozen=True)
class DeepSeekBetaRequest:
    case_id: str
    expectation: str
    target_parameter: str
    _body_bytes: bytes = field(repr=False)

    @property
    def body(self) -> dict:
        return decode_beta_response(self._body_bytes)

    @property
    def body_bytes(self) -> bytes:
        return self._body_bytes

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self._body_bytes).hexdigest()

    @property
    def endpoint(self) -> str:
        return ENDPOINT

    @property
    def method(self) -> str:
        return "POST"


@dataclass(frozen=True)
class DeepSeekBetaReferencePlan:
    _snapshot_bytes: bytes = field(repr=False)
    endpoint: str

    @property
    def snapshot(self) -> dict:
        return decode_beta_response(self._snapshot_bytes)

    @property
    def requests(self) -> tuple[DeepSeekBetaRequest, ...]:
        if self.endpoint != ENDPOINT:
            raise ValueError("Beta plan endpoint changed")
        _, cases = _check_snapshot(self.snapshot)
        return tuple(DeepSeekBetaRequest(row["case_id"], row["expectation"], row["target_parameter"],
                                        canonical_bytes(row["body"])) for row in cases)

    @property
    def request_cap(self) -> int:
        return REQUEST_CAP

    @property
    def retries(self) -> int:
        return 0

    @property
    def identity_probe_requests(self) -> int:
        return 0


def build_deepseek_beta_reference_plan(snapshot: dict, *, endpoint: str,
                                      runs: int = 1, job_type: str = "param_test") -> DeepSeekBetaReferencePlan:
    """Freeze only the Pro five-case parameter plan from this job's MPDB view."""
    if endpoint != ENDPOINT:
        raise ValueError("Beta reference requires its exact official beta endpoint")
    if type(runs) is not int or runs != 1 or job_type != "param_test":
        raise ValueError("Beta reference permits one five-request parameter run only")
    _check_snapshot(snapshot)
    return DeepSeekBetaReferencePlan(canonical_bytes(snapshot), endpoint)


def _records(plan: DeepSeekBetaReferencePlan, records: list[dict]) -> tuple[tuple[DeepSeekBetaRequest, ...], list[dict]]:
    if not isinstance(plan, DeepSeekBetaReferencePlan) or not isinstance(records, list):
        raise ValueError("Beta result collection requires a fixed plan and record list")
    requests = plan.requests
    if len(records) > REQUEST_CAP:
        raise ValueError("Beta result collection exceeds five requests")
    for request, row in zip(requests, records):
        _object(row, "Beta exchange record")
        if row.get("case_id") != request.case_id or row.get("request_body_sha256") != request.body_sha256:
            raise ValueError("Beta records must match the next unique fixed request and body digest")
        status = row.get("status_code")
        if "status_code" not in row or (status is not None and (type(status) is not int or not 100 <= status <= 599)):
            raise ValueError("Beta HTTP status must be an integer or None")
        if "response_raw" not in row or not isinstance(row["response_raw"], (bytes, str)):
            raise ValueError("Beta records must retain original response text")
    return requests, records


def next_deepseek_beta_request(plan: DeepSeekBetaReferencePlan, records: list[dict]) -> DeepSeekBetaRequest | None:
    """Return the next fixed request; callers persist attempts before dispatch.

    This pure function cannot own an HTTP attempt counter. The transport must
    append one record even for transport failures and must never retry a case.
    """
    requests, checked = _records(plan, records)
    return requests[len(checked)] if len(checked) < REQUEST_CAP else None


def _native(body: dict, payload: dict) -> dict:
    _require_values(payload, {"model": REQUEST_MODEL_ID, "object": "chat.completion"}, "Native response")
    response_id = payload.get("id")
    if not isinstance(response_id, str) or not response_id.strip() or response_id != response_id.strip():
        raise ValueError("Native response lacks a valid id")
    if type(payload.get("created")) is not int or payload["created"] < 0 or "error" in payload:
        raise ValueError("Native response has invalid timestamp or mixed error")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("Native response requires one choice")
    choice = _object(choices[0], "Native choice")
    _require_values(choice, {"index": 0, "finish_reason": "stop"}, "Native choice")
    message = _object(choice.get("message"), "Native assistant message")
    if (message.get("role") != "assistant" or not isinstance(message.get("content"), str) or not message["content"]
            or message.get("tool_calls") not in (None, []) or message.get("reasoning_content") not in (None, "")
            or message.get("refusal") not in (None, "") or message.get("function_call") is not None):
        raise ValueError("Native response lacks plain nonthinking assistant content")
    usage = _object(payload.get("usage"), "Native usage")
    if (any(type(usage.get(key)) is not int for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
            or usage["prompt_tokens"] <= 0 or not 0 < usage["completion_tokens"] <= body["max_tokens"]
            or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]):
        raise ValueError("Native usage is incomplete or inconsistent")
    cache = {key: usage[key] for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens") if key in usage}
    if (any(type(value) is not int or not 0 <= value <= usage["prompt_tokens"] for value in cache.values())
            or len(cache) == 2 and sum(cache.values()) != usage["prompt_tokens"]):
        raise ValueError("Native cache usage is inconsistent")
    return {"content": message["content"], "returned_model_id": payload["model"],
            "finish_reason": choice["finish_reason"], "usage": copy.deepcopy(usage)}


def _prompt_target(body: dict) -> dict:
    # This approved fixture's target is owned by its pinned Binding body.
    prompt = body["messages"][0]["content"]
    start = prompt.find("{")
    if start < 0:
        raise ValueError("Fixed beta prompt lacks its JSON target")
    _, length = json.JSONDecoder().raw_decode(prompt[start:])
    return decode_beta_response(prompt[start:start + length])


def _same_json(left: dict, right: dict) -> bool:
    return canonical_bytes(left) == canonical_bytes(right)


def _reconstruct(prefix: str, content: str) -> tuple[dict, str]:
    if content.startswith(prefix):
        try:
            return decode_beta_response(content), "full_prefix_and_continuation"
        except ValueError:
            pass
    return decode_beta_response(prefix + content), "continuation_only"


def _attribute_type(positive: dict, negative: dict, good_body: dict, bad_body: dict) -> dict:
    result = {"attributed": False, "method": None}
    if not positive.get("response_valid") or negative.get("status_code") not in (400, 422):
        return result
    good, bad = copy.deepcopy(good_body), copy.deepcopy(bad_body)
    good_value = good["messages"][-1].pop("prefix")
    bad_value = bad["messages"][-1].pop("prefix")
    if not _same_json(good, bad) or good_value is not True or bad_value != "not-a-boolean":
        return result
    payload = negative.get("payload") or {}
    error = payload.get("error")
    if (not isinstance(error, dict) or error.get("type") not in ("invalid_request_error", "validation_error")
            or any(key in payload for key in ("choices", "model", "usage"))):
        return result
    param = error.get("param")
    if param in ("messages[1].prefix", "messages.1.prefix", "messages[].prefix"):
        return {"attributed": True, "method": "structured_prefix_field_and_single_mutation_control"}
    message = error.get("message")
    if not isinstance(message, str):
        return result
    if (param is None and re.search(r"(?<![A-Za-z0-9_.])messages(?:\[1\]|\.1)\.prefix(?![A-Za-z0-9_.])", message)
            and bad_value in message and re.search(r"(?:expected (?:a )?bool(?:ean)?|must be (?:a )?bool(?:ean)?)\b", message, re.I)):
        return {"attributed": True, "method": "exact_prefix_path_unique_invalid_string_boolean_error_and_single_mutation_control"}
    pattern = (r'Failed to deserialize the JSON body into the target type: messages\[1\]: '
               r'invalid type: string "not-a-boolean", expected a boolean at line 1 column [1-9][0-9]*')
    if (param is None and re.fullmatch(pattern, message)
            and canonical_bytes(bad_body).count(b'"not-a-boolean"') == 1):
        return {"attributed": True, "method": "native_message_object_boolean_error_unique_literal_and_single_prefix_mutation_control"}
    return result


def evaluate_deepseek_beta_reference(plan: DeepSeekBetaReferencePlan, records: list[dict]) -> dict:
    """Keep protocol validity, bounded behavior and strict target verdicts apart.

    ``pass`` requires the current strict target too. A historical-style framing
    success with a wrong tail is only ``bounded_observations_pass``; its case
    and overall strict pass stay false. No result certifies the full matrix.
    """
    requests, records = _records(plan, records)
    native, results = {}, []
    for request, record in zip(requests, records):
        row = {"case_id": request.case_id, "expectation": request.expectation,
               "target_parameter": request.target_parameter, "status_code": record["status_code"],
               "response_valid": False, "behavior_pass": False, "pass": False}
        parsed = {"status_code": record["status_code"], "response_valid": False}
        payload = {}
        try:
            if record.get("failure_type") or record.get("interrupted") or record.get("response_complete") is False:
                raise ValueError("Beta transport did not complete successfully")
            payload = decode_beta_response(record["response_raw"])
            if "response_json" in record and canonical_bytes(record["response_json"]) != canonical_bytes(payload):
                raise ValueError("Parsed response differs from original beta response")
            parsed["payload"] = payload
            if record["status_code"] == 200 and request.expectation == "supported":
                parsed.update(_native(request.body, payload), response_valid=True)
                row.update(response_valid=True, returned_model_id=parsed["returned_model_id"],
                           usage=copy.deepcopy(parsed["usage"]), finish_reason=parsed["finish_reason"])
        except ValueError as exc:
            row["validation_error"] = str(exc)
            parsed.pop("payload", None)
        usage_required = (
            record["status_code"] is not None and 200 <= record["status_code"] <= 299
            or bool(payload.get("usage")) or bool(payload.get("choices"))
        )
        try:
            exchange_audit = audit_exchange(
                request.body,
                SimpleNamespace(
                    success=record["status_code"] == 200,
                    status_code=record["status_code"], response_json=payload,
                    usage=payload.get("usage") or {}, finish_reason=None,
                    error_type=(
                        "incomplete_transport" if record.get("failure_type")
                        or record.get("interrupted") or record.get("response_complete") is False
                        else None
                    ),
                ),
                "chat_completions", {}, request.case_id,
                model=REQUEST_MODEL_ID, usage_required=usage_required,
                accounting_source_id=SOURCE_ID, accounting_contract_id=CONTRACT_ID,
            )
        except Exception as exc:
            exchange_audit = {
                "schema_version": TOKEN_AUDIT_SCHEMA_VERSION, "exchange": request.case_id,
                "usage_required": usage_required, "validation_status": "fail",
                "validation_pass": False, "status": "not_available",
                "validation_failures": ["fixed beta token audit error: " + type(exc).__name__],
            }
        row["token_audit"] = combine_exchange_audits([exchange_audit])
        row["token_validation_pass"] = row["token_audit"]["validation_pass"]
        row["token_validation_status"] = row["token_audit"]["validation_status"]
        native[request.case_id] = parsed
        results.append(row)
    by_id = {row["case_id"]: row for row in results}
    bodies = {r.case_id: r.body for r in requests}
    control, positive, invalid, baseline, stopped = [native.get(cid, {}) for cid in CASE_IDS]
    target = _prompt_target(bodies[CASE_IDS[0]])
    if control.get("response_valid"):
        try:
            passed = _same_json(decode_beta_response(control["content"]), target)
            by_id[CASE_IDS[0]].update(behavior_pass=passed, **{"pass": passed})
        except ValueError:
            by_id[CASE_IDS[0]]["semantic_error"] = "control_json_target_not_met"
    if CASE_IDS[1] in by_id:
        row = by_id[CASE_IDS[1]]
        row.update(strict_target_pass=False, historical_strict_tail_target_met=False,
                   verdict_scope="prefix framing and strict tail are separate")
        if positive.get("response_valid"):
            try:
                prefix = bodies[CASE_IDS[1]]["messages"][-1]["content"]
                reconstructed, mode = _reconstruct(prefix, positive["content"])
                seed = decode_beta_response(prefix + '"}')
                strict_target = {**target, "tail": seed["tail"] + target["tail"]}
                framing = (set(reconstructed) == set(target) and type(reconstructed.get("answer")) is int
                           and reconstructed["answer"] == target["answer"] and isinstance(reconstructed.get("tail"), str)
                           and reconstructed["tail"].startswith(seed["tail"]) and len(reconstructed["tail"]) > len(seed["tail"])
                           and by_id.get(CASE_IDS[0], {}).get("behavior_pass") is True)
                strict = _same_json(reconstructed, strict_target)
                # The relaxed framing evidence was continuation-only. A full
                # prefixed response must satisfy the original strict target.
                framing = framing and (mode == "continuation_only" or strict)
                row.update(returned_content_mode=mode, behavior_pass=framing, strict_target_pass=strict,
                           **{"pass": framing and strict})
            except (ValueError, KeyError, TypeError):
                row["semantic_error"] = "prefix_reconstruction_not_met"
    if CASE_IDS[2] in by_id:
        attribution = _attribute_type(positive, invalid, bodies[CASE_IDS[1]], bodies[CASE_IDS[2]])
        by_id[CASE_IDS[2]].update(type_attribution=attribution, behavior_pass=attribution["attributed"],
                                 **{"pass": attribution["attributed"]})
    stop_literal = bodies[CASE_IDS[4]]["stop"][0]
    if CASE_IDS[3] in by_id and baseline.get("response_valid"):
        try:
            reconstructed, mode = _reconstruct(bodies[CASE_IDS[3]]["messages"][-1]["content"], baseline["content"])
            exact = _same_json(reconstructed, _prompt_target(bodies[CASE_IDS[3]]))
            contains = stop_literal in baseline["content"]
            by_id[CASE_IDS[3]].update(exact_json_target_met=exact, actual_stop_literal_present=contains,
                                     returned_content_mode=mode, behavior_pass=exact and contains, **{"pass": exact and contains})
        except ValueError:
            by_id[CASE_IDS[3]]["semantic_error"] = "baseline_json_target_not_met"
    if CASE_IDS[4] in by_id:
        row = by_id[CASE_IDS[4]]
        row["stop_status"] = "insufficient_evidence"
        row.update(
            output_semantics="intentional_stop_segment",
            complete_json_output=False,
            token_cap_truncation_accepted=False,
        )
        if by_id.get(CASE_IDS[3], {}).get("behavior_pass") and stopped.get("response_valid"):
            control_body, stop_body = copy.deepcopy(bodies[CASE_IDS[3]]), copy.deepcopy(bodies[CASE_IDS[4]])
            stop_sequences = stop_body.pop("stop")
            paired = stop_sequences == [stop_literal] and _same_json(control_body, stop_body)
            prefix = bodies[CASE_IDS[4]]["messages"][-1]["content"]
            baseline_full = (
                baseline["content"] if baseline["content"].startswith(prefix)
                else prefix + baseline["content"]
            )
            expected_full_segment = baseline_full.split(stop_literal, 1)[0]
            stopped_full = (
                stopped["content"] if stopped["content"].startswith(prefix)
                else prefix + stopped["content"]
            )
            expected_visible_segment = expected_full_segment[len(prefix):]
            passed = bool(
                paired and expected_full_segment.startswith(prefix)
                and expected_visible_segment and stopped_full == expected_full_segment
                and stop_literal not in stopped["content"]
            )
            row.update(
                behavior_pass=passed, stop_status="observed" if passed else "not_observed",
                stop_boundary={
                    "declared_prefill": prefix, "stop_literal": stop_literal,
                    "expected_visible_segment": expected_visible_segment,
                    "expected_prefill_and_segment": expected_full_segment,
                    "actual_prefill_and_segment": stopped_full,
                    "exact_boundary_match": passed,
                    "comparison_scope": "declared prefill plus intentional stop segment",
                },
                **{"pass": passed},
            )
    for row in results:
        row["pass"] = row["pass"] and row["token_validation_pass"]
    complete = len(records) == REQUEST_CAP
    snapshot = plan.snapshot
    token_summary = summarize_token_audits(results)
    return {"source_id": SOURCE_ID, "profile_id": PROFILE_ID, "interface_id": INTERFACE_ID,
            "reference_contract_id": CONTRACT_ID, "api_form": API_FORM, "api_version": "beta",
            "snapshot_digest": snapshot["snapshot_digest"], "catalog_digest": snapshot["catalog_digest"],
            "test_extension_digest": snapshot["test_extension_digest"], "request_cap": REQUEST_CAP,
            "requests_recorded": len(records), "identity_probe_requests": 0, "automatic_retries": 0,
            "complete": complete, "case_results": results,
            "bounded_observations_pass": complete and all(row["behavior_pass"] for row in results),
            "pass": complete and all(row["pass"] for row in results),
            "historical_strict_tail_target_met": False, "original_stop_status": "insufficient_evidence",
            "full_parameter_matrix_verified": False, "token_exact_proof": False,
            "token_audit_schema_version": TOKEN_AUDIT_SCHEMA_VERSION,
            "token_audit_summary": token_summary,
            "token_validation_pass": token_summary["pass"],
            "scope": "five fixed Pro 0813 beta cases; framing does not establish strict JSON target success"}
