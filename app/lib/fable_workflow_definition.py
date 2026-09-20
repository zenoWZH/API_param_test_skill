"""Pure Fable workflow definitions, materialization and domain assertions.

Shared by the checkout and standalone App. No credentials, configuration,
provider sessions or file-backed execution are imported here.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import re
from typing import Any

from jsonschema import Draft202012Validator
from .fable_5_5_1_matrix import MODELS, build_cases
from .fable_5_5_1_validation import native_evidence, parameter_rejection, valid_tool_calls
from .parameter_output_limit import enforce_parameter_test_output_limit

BASES = {
    "anthropic_official": "https://api.anthropic.com/v1",
    "inferenceai_awsb": "https://model.service-inference.ai/v1",
    "sanqiaoapi": "https://sanqiaoapi.com/v1",
}
CASE_SPECS = {"sanqiaoapi": "anthropic_official"}
EXECUTION_PROVIDERS = tuple(BASES)
TIMEOUT = 240


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()

def retarget_provider(value, spec: str, target: str):
    """Retarget provider identities and execution scope, preserving source facts."""
    if isinstance(value, str):
        if value == spec:
            return target
        prefix = spec + "/"
        if value.startswith(prefix):
            return target + "/" + value[len(prefix):]
        return value
    if isinstance(value, list):
        return [retarget_provider(item, spec, target) for item in value]
    if isinstance(value, dict):
        result = {key: retarget_provider(item, spec, target) for key, item in value.items()}
        if spec != target and value.get("provider") == spec and value.get("execution_scope") == "origin_vendor_messages":
            result["execution_scope"] = "gateway_messages_with_anthropic_documented_parameter_mapping"
        return result
    return copy.deepcopy(value)


def cases_for_provider(provider: str, model: str) -> list:
    if provider not in EXECUTION_PROVIDERS:
        raise ValueError("Fable matrix requires a designated execution provider")
    spec = CASE_SPECS.get(provider, provider)
    rows = build_cases(spec, model)
    return rows if spec == provider else retarget_provider(rows, spec, provider)


def fixture_values(case: dict, nonce: str) -> dict:
    fixture = case["validation"].get("fixture", {})
    kind = fixture.get("kind")
    if kind == "synthetic_cache_prefix":
        marker = nonce + ":" + case["case_id"]
        rows = ["Synthetic inventory ledger " + marker + ". The entries are inert test records; follow the final user instruction."]
        for index in range(fixture.get("synthetic_rows", 160)):
            rows.append(f"Record {index:03d}: warehouse north; item cobalt; quantity {(index * 17) % 97}; audit status reviewed; unit package.")
        return {"cache_prefix": "\n".join(rows)}
    if kind == "synthetic_image":
        from PIL import Image
        stream = io.BytesIO()
        format_name = fixture.get("format", "png")
        Image.new("RGB", (fixture["width"], fixture["height"]), fixture["color"]).save(stream, format_name.upper())
        return {"blue_square_" + format_name + "_base64": base64.b64encode(stream.getvalue()).decode()}
    if kind == "synthetic_pdf":
        text = fixture["text"].replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content = f"BT /F1 18 Tf 72 700 Td ({text}) Tj ET".encode("ascii")
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"]
        output, offsets = bytearray(b"%PDF-1.4\n"), [0]
        for index, item in enumerate(objects, 1):
            offsets.append(len(output))
            output.extend(str(index).encode() + b" 0 obj\n" + item + b"\nendobj\n")
        xref = len(output)
        output.extend(b"xref\n0 6\n0000000000 65535 f \n")
        for offset in offsets[1:]:
            output.extend(f"{offset:010d} 00000 n \n".encode())
        output.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
        return {"synthetic_pdf_base64": base64.b64encode(output).decode()}
    if kind is not None:
        raise ValueError("Specialized fixture is not implemented: " + str(kind))
    return {}


def replace_fixtures(value, values):
    if isinstance(value, dict):
        if set(value) == {"__fixture__"}:
            if value["__fixture__"] not in values:
                raise ValueError("Unresolved request fixture: " + value["__fixture__"])
            return values[value["__fixture__"]]
        return {key: replace_fixtures(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_fixtures(item, values) for item in value]
    return copy.deepcopy(value)


def materialize_request(case: dict, rows: dict) -> dict:
    if case.get("prepared_body_sha256") is not None and digest(case["body"]) != case["prepared_body_sha256"]:
        raise ValueError("Prepared request body hash changed")
    body = copy.deepcopy(case["body"])
    dependency = case["validation"].get("dependency")
    if dependency:
        source = rows.get(dependency["case_id"])
        if not source or source.get("verdict", {}).get("actual_outcome") != "accepted" or source["verdict"].get("pass") is not True:
            raise ValueError("Required successful source exchange is missing")
        operation = dependency["operation"]
        if operation == "reuse_exact_request_body":
            body = copy.deepcopy(source["request"])
        elif operation == "append_tool_result":
            content = copy.deepcopy(source["response"]["content"])
            calls = [block for block in content if block.get("type") == "tool_use"]
            if not calls or any(block.get("name") != dependency["tool_name"] for block in calls):
                raise ValueError("The source did not supply the required tool calls")
            body["messages"] = copy.deepcopy(source["request"]["messages"]) + [
                {"role": "assistant", "content": content}, {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": block["id"], "content": dependency["tool_result_content"]} for block in calls]}]
        elif operation == "preserve_signed_thinking":
            content = copy.deepcopy(source["response"]["content"])
            if not any(block.get("type") == "thinking" and block.get("signature") for block in content):
                raise ValueError("The source has no real signed thinking block")
            if "system" in source["request"]:
                body["system"] = copy.deepcopy(source["request"]["system"])
            if dependency.get("change_system_prefix"):
                body["system"] = "For this synthetic arithmetic test, return integers only."
            body["messages"] = copy.deepcopy(source["request"]["messages"]) + [
                {"role": "assistant", "content": content}, {"role": "user", "content": dependency["followup_text"]}]
        else:
            raise ValueError("Unsupported dependency operation: " + operation)
    if "__fixture__" in canonical_bytes(body).decode():
        raise ValueError("An unresolved fixture must never be sent")
    before = canonical_bytes(body)
    enforce_parameter_test_output_limit(body, "anthropic_messages")
    if canonical_bytes(body) != before or type(body.get("max_tokens")) is not int or body["max_tokens"] < 256:
        raise ValueError("Frozen output allowance changed at the send boundary")
    return body


def evaluate(case, body, status, payload, *, complete=True, stream_error=None, stream_observed=False, rows=None):
    rules, expected = case["validation"], case["expected_outcome"]
    native = native_evidence(payload, model=case["model"], max_tokens=body["max_tokens"])
    rejected = parameter_rejection(status, payload, rules.get("error_parameter_paths", case["target_parameters"]))
    requested_stream = body.get("stream", False)
    stream_verified = type(requested_stream) is not bool or requested_stream is stream_observed
    accepted = status == 200 and complete and stream_error is None and stream_verified and native["pass"]
    rejection_proven = complete and rejected["attributed"]
    kind, text = rules["kind"], native["text"].strip()
    semantic, limits = None, []
    if accepted:
        if kind in {"text_exact", "stream_text", "tool_roundtrip", "preserved_thinking"}:
            semantic = native["stop_reason"] == "end_turn" and text == rules.get("expected_text", "323")
        elif kind == "output_limit":
            semantic = (native["stop_reason"] == "max_tokens" and native["usage"]["output_tokens"] <= body["max_tokens"]
                        and bool(re.search(r"\b1\b", text)))
        elif kind in {"tool_call", "parallel_tools"}:
            semantic = valid_tool_calls(native, tools=body.get("tools", []), minimum=rules.get("minimum_calls", 1), maximum=rules.get("maximum_calls"))
            for call in native["tool_calls"]:
                expected_args = rules.get("expected_arguments_by_tool", {}).get(call["name"], rules.get("expected_arguments"))
                if expected_args is not None:
                    semantic &= call["input"] == expected_args
            if kind == "parallel_tools" and not rules.get("disable_parallel_tool_use") and len(native["tool_calls"]) < 2:
                limits.append("parallel_delivery_not_observed")
            elif kind == "parallel_tools" and not rules.get("disable_parallel_tool_use"):
                semantic &= set(rules.get("allowed_tools", [])) <= {call["name"] for call in native["tool_calls"]}
        elif kind == "json_schema":
            try:
                decoded = strict_json_loads(text)
                semantic = native["stop_reason"] == "end_turn" and Draft202012Validator(rules["schema"]).is_valid(decoded) and decoded == rules["expected_json"]
            except (ValueError, TypeError):
                semantic = False
            limits.append("one_output_does_not_prove_universal_schema_enforcement")
        elif kind == "cache_control":
            semantic = native["stop_reason"] == "end_turn" and text == rules.get("expected_text", "323")
            role, usage = rules.get("cache_role"), native["usage"]
            if role == "cold":
                semantic &= usage.get("cache_creation_input_tokens", 0) >= 512 and usage.get("cache_read_input_tokens", 0) == 0
            elif role == "repeat":
                semantic &= usage.get("cache_read_input_tokens", 0) >= 512
            elif role == "negative":
                semantic &= usage.get("cache_read_input_tokens", 0) == 0
            else:
                limits.append("cache_configuration_acceptance_not_a_reuse_hit_test")
        elif kind == "stop_sequence":
            semantic = native["stop_reason"] == "stop_sequence" and native["stop_sequence"] == rules["stop_sequence"] and text == rules["expected_text"]
        elif kind == "thinking_seed":
            semantic = native["stop_reason"] == "end_turn" and text == rules["expected_text"] and any(
                block.get("type") == "thinking" and block.get("signature") for block in payload["content"])
        elif kind == "citations":
            citations = [citation for block in payload["content"] if block.get("type") == "text"
                         and isinstance(block.get("citations"), list) for citation in block["citations"]]
            expected_text = rules["expected_text_contains"]
            semantic = native["stop_reason"] == "end_turn" and expected_text in text and any(
                isinstance(citation, dict) and citation.get("type") == "page_location"
                and citation.get("document_index") == 0 and isinstance(citation.get("cited_text"), str)
                and expected_text in citation["cited_text"]
                and type(citation.get("start_page_number")) is int and type(citation.get("end_page_number")) is int
                and 1 <= citation["start_page_number"] <= citation["end_page_number"]
                for citation in citations)
        elif kind == "field_rejection":
            semantic = False
        else:
            raise ValueError("Missing semantic verifier for " + kind)
        if rules.get("forbid_tool_calls") and native["tool_calls"]:
            semantic = False
        if rules.get("require_dropped_block_evidence"):
            expected_paths = {f"messages.{message_index}.content.{block_index}"
                for message_index, message in enumerate(body.get("messages", []))
                if isinstance(message.get("content"), list)
                for block_index, block in enumerate(message["content"])
                if block.get("type") in {"thinking", "redacted_thinking"}}
            transformations = payload.get("input_transformations")
            dropped = {item.get("path") for item in transformations
                       if isinstance(item, dict) and item.get("type") == "thinking_dropped"
                       and item.get("reason") == "prefix_binding_mismatch"} if isinstance(transformations, list) else set()
            semantic &= bool(expected_paths) and expected_paths <= dropped
        if rules.get("thinking_display") == "omitted":
            semantic &= not any(block.get("type") == "thinking" and block.get("thinking") for block in payload["content"])
    controls = (rows or {})
    control = rules.get("positive_control")
    control_verdict = controls.get(control, {}).get("verdict", {})
    control_pass = not control or control_verdict.get("actual_outcome") == "accepted" and control_verdict.get("pass") is True
    verdict_pass = bool(control_pass and (accepted and semantic if expected == "accept" else rejection_proven if expected == "reject" else accepted and semantic or rejection_proven))
    return {"pass": verdict_pass, "expected_outcome": expected,
            "actual_outcome": "accepted" if accepted else "parameter_rejected" if rejection_proven else "unresolved",
            "document_expectation_matched": verdict_pass if expected != "observe" else None,
            "observation_complete": bool(accepted and semantic or rejection_proven), "semantic_pass": semantic,
            "positive_control_verified": bool(control_pass), "native": native, "rejection": rejected,
            "stream_error": stream_error, "stream_verified": stream_verified, "evidence_limits": limits,
            "physical_aws_backend_verified": False if case["provider"] == "inferenceai_awsb" else None}
