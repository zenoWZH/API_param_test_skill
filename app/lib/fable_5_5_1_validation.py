"""Native response and parameter-attribution evidence for the Fable matrices.

These checks establish the declared response contract. They do not infer a
physical AWS backend from a gateway response or infer hidden reasoning effort.
"""
from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator


def native_evidence(payload: Any, *, model: str, max_tokens: int) -> dict:
    result = {"pass": False, "envelope_pass": False, "identity_pass": False, "usage_pass": False,
              "text": "", "tool_calls": [], "failures": []}
    if not isinstance(payload, dict):
        result["failures"].append("response_not_object")
        return result
    result.update(returned_model=payload.get("model"), stop_reason=payload.get("stop_reason"),
                  stop_sequence=payload.get("stop_sequence"), usage=payload.get("usage"))
    result["identity_pass"] = payload.get("model") == model
    blocks, usage = payload.get("content"), payload.get("usage")
    result["envelope_pass"] = bool(
        payload.get("type") == "message" and payload.get("role") == "assistant"
        and isinstance(payload.get("id"), str) and payload["id"]
        and isinstance(blocks, list) and not payload.get("error")
        and all(isinstance(block, dict) and isinstance(block.get("type"), str) for block in blocks)
        and isinstance(payload.get("stop_reason"), str)
        and payload["stop_reason"] in {"end_turn", "stop_sequence", "tool_use", "max_tokens", "pause_turn", "refusal", "compaction"})
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                if isinstance(block.get("text"), str):
                    result["text"] += block["text"]
                else:
                    result["envelope_pass"] = False
            if block.get("type") == "thinking":
                if not isinstance(block.get("thinking"), str) or not isinstance(block.get("signature"), str) or not block.get("signature"):
                    result["envelope_pass"] = False
            if block.get("type") == "tool_use":
                result["tool_calls"].append(block)
                if (not isinstance(block.get("id"), str) or not block.get("id")
                        or not isinstance(block.get("name"), str) or not block.get("name")
                        or not isinstance(block.get("input"), dict)):
                    result["envelope_pass"] = False
        result["content_types"] = [block.get("type") for block in blocks if isinstance(block, dict)]
        ids = [call.get("id") for call in result["tool_calls"]]
        if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
            result["envelope_pass"] = False
    if isinstance(usage, dict):
        counters = [usage.get("input_tokens"), usage.get("output_tokens"),
                    usage.get("cache_read_input_tokens", 0), usage.get("cache_creation_input_tokens", 0)]
        valid = all(type(value) is int and value >= 0 for value in counters)
        result["usage_pass"] = bool(valid and counters[1] <= max_tokens and counters[0] + counters[2] + counters[3] > 0)
        cache = usage.get("cache_creation")
        if isinstance(cache, dict):
            parts = [cache.get("ephemeral_5m_input_tokens", 0), cache.get("ephemeral_1h_input_tokens", 0)]
            result["usage_pass"] &= all(type(value) is int and value >= 0 for value in parts)
            if result["usage_pass"]:
                result["usage_pass"] &= sum(parts) == usage.get("cache_creation_input_tokens", 0)
        thinking = usage.get("output_tokens_details", {}).get("thinking_tokens") if isinstance(usage.get("output_tokens_details"), dict) else None
        if thinking is not None:
            result["usage_pass"] &= type(thinking) is int and 0 <= thinking <= usage.get("output_tokens", -1)
    for name in ("envelope", "identity", "usage"):
        if not result[name + "_pass"]:
            result["failures"].append(name + "_invalid")
    result["pass"] = not result["failures"]
    return result


def parameter_rejection(status: int | None, payload: Any, parameters: list[str]) -> dict:
    result = {"attributed": False, "category": "unresolved", "matched_parameters": []}
    if not isinstance(payload, dict):
        return result
    error = payload.get("error")
    if not isinstance(error, dict):
        return result
    message = str(error.get("message") or "")
    error_type = str(error.get("type") or error.get("code") or "")
    result.update(error_type=error_type, message=message)
    if re.search(r"permission|authentication|authorization|rate_limit|overload|billing|quota", error_type, re.I):
        result["category"] = "access_quota_or_service"
        return result
    if status in {401, 403, 429} or status is not None and status >= 500:
        result["category"] = "access_quota_or_service"
        return result
    if re.search(r"retention|billing|credit balance|organization.*verif|workspace.*retain", message, re.I):
        result["category"] = "account_prerequisite"
        return result
    if re.search(r"quota|rate[ _-]limit|not authorized|permission denied|access denied|not available for (?:this |your |the )?(?:account|organization|workspace)", message, re.I):
        result["category"] = "access_quota_or_service"
        return result
    if status not in {400, 422} or payload.get("content"):
        return result
    if not re.search(r"invalid|not|must|only|unsupported|deprecated|cannot|required|should|expected|extra|unknown|allowed|greater|less|missing|exceed", message, re.I):
        return result
    for parameter in parameters:
        parts = [part for part in re.split(r"[.\[\]]+", parameter) if part and not part.isdigit()]
        normalized = re.sub(r"\[(?:\d+)?\]|\.\d+(?=\.|:|\s|$)", "", message)
        exact = ".".join(parts)
        names = {exact}
        if len(parts) == 1 or parts[-1] not in {"type", "name", "content", "value", "format", "total", "enabled", "role", "schema", "ttl"}:
            names.add(parts[-1])
        specific = any(re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", normalized, re.I) for name in names)
        if not specific and len(parts) > 1:
            specific = bool(re.search(r"\b" + re.escape(parts[0]) + r"\b[^.;\n]{0,80}\b" + re.escape(parts[-1]) + r"\b", message, re.I))
        if specific:
            result["matched_parameters"].append(parameter)
    result["attributed"] = bool(result["matched_parameters"])
    if result["attributed"]:
        result["category"] = "parameter_rejected"
    return result


def valid_tool_calls(evidence: dict, *, tools: list[dict], minimum: int = 1, maximum: int | None = None) -> bool:
    calls = evidence.get("tool_calls", [])
    schemas = {tool.get("name"): tool.get("input_schema") for tool in tools}
    ids = [call.get("id") for call in calls]
    if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
        return False
    if len(calls) < minimum or maximum is not None and len(calls) > maximum:
        return False
    if calls and evidence.get("stop_reason") != "tool_use":
        return False
    return all(call.get("name") in schemas and isinstance(schemas[call["name"]], dict)
               and Draft202012Validator(schemas[call["name"]]).is_valid(call.get("input")) for call in calls)
