"""Pure request plans for Fable platform features; never performs network I/O.

Only ``status == 'ready'`` plans have a sendable ``request``. Resource plans
have a separate template; the caller must bind real uploads or verified public
bytes before freezing the final request. HTTP counts include file cleanup.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import re
import struct
import zlib
from typing import Any

from referencing import Registry

from lib.fable_5_5_1_validation import native_evidence, parameter_rejection

MODELS = ("claude-fable-5", "claude-fable-5-1")
PROVIDERS = ("anthropic_official", "inferenceai_awsb")
DOC_URL = "https://platform.claude.com/docs/en/api/messages/create"
PUBLIC_FIXTURES = {
    "url_image": "https://platform.claude.com/docs/images/vision-example.jpg",
    "url_document": "https://assets.anthropic.com/m/1cd9d098ac3e6467/original/Claude-3-Model-Card-October-Addendum.pdf",
}
PUBLIC_PDF_CANONICAL_URL = "https://www-cdn.anthropic.com/c7822cdc35ad788ec87e14b3a9d45010f1f86c38.pdf"
VERIFIED_PDF_REDIRECT = {
    "method": "GET", "url": PUBLIC_FIXTURES["url_document"], "http_status": 301,
    "location": PUBLIC_PDF_CANONICAL_URL, "authenticated": False, "allow_redirects": False,
    "response_bytes_sha256": "446a6087825fa73eadb045e5a2e9e2adf7df241b571228187728191d961dda1f",
}
MCP_PUBLIC_SERVER = "https://mcp.deepwiki.com/mcp"
MCP_PUBLIC_REPO = "anthropics/anthropic-sdk-python"
FILE_BINDING = {"$resource": "upload_fixture.response.id"}


def _request(body: dict, headers: dict) -> dict:
    return {"method": "POST", "path": "/v1/messages", "headers": headers, "body": body}


def _has_placeholder(value: Any) -> bool:
    if isinstance(value, dict):
        return bool({"__fixture__", "$resource"} & value.keys()) or any(_has_placeholder(v) for v in value.values())
    return isinstance(value, list) and any(_has_placeholder(v) for v in value)


def _set_beta(headers: dict, beta: str) -> None:
    values = [x.strip() for x in headers.get("anthropic-beta", "").split(",") if x.strip()]
    headers["anthropic-beta"] = ",".join(dict.fromkeys([*values, beta]))


def _fixture_bytes(media: str) -> tuple[bytes, str, str]:
    if media == "image":
        def chunk(name: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF)
        raw = (b"\0" + b"\0\0\xff" * 128) * 128
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 128, 128, 8, 2, 0, 0, 0))
        return png + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""), "image/png", "fable-matrix-blue.png"
    if media != "document":
        raise ValueError("File media must be image or document")
    content = b"BT /F1 18 Tf 40 120 Td (Verification code: FABLE_MATRIX_323) Tj ET\n"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 450 180] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream"]
    pdf, offsets = b"%PDF-1.4\n", [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 6\n0000000000 65535 f \n"
    pdf += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    pdf += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return pdf, "application/pdf", "fable-matrix-note.pdf"


def materialize_platform_case(case: dict) -> dict:
    """Construct bounded plans without resolving provider URLs or credentials."""
    case = copy.deepcopy(case)
    fixture = case.get("validation", {}).get("fixture", {})
    if fixture.get("kind") != "platform_feature":
        raise ValueError("A platform_feature descriptor is required")
    model, provider = case.get("model"), case.get("provider")
    if model not in MODELS or provider not in PROVIDERS:
        raise ValueError("Only designated Fable model/provider groups are allowed")
    source = "anthropic" if provider == "anthropic_official" else "aws_bedrock"
    if case.get("source_id") != source or case.get("api_form") != "anthropic_messages":
        raise ValueError("Source/API form must match the designated provider")
    body = copy.deepcopy(case.get("body", {}))
    if body.get("model") != model or type(body.get("max_tokens")) is not int or not 256 <= body["max_tokens"] <= 128000:
        raise ValueError("Platform request requires matching model and 256..128000 output cap")
    headers = copy.deepcopy(case.get("headers", {}))
    if not isinstance(headers, dict) or set(headers) - {"anthropic-beta"} or any(not isinstance(x, str) for x in headers.values()):
        raise ValueError("Credentials and endpoint headers belong to the sender")
    feature = fixture.get("feature")
    variant = fixture.get("variant", "positive")
    if variant not in {"positive", "invalid_schema"}:
        raise ValueError("Unknown platform variant")
    negative = variant == "invalid_schema"
    if negative and feature not in {"advisor", "browser", "computer", "mcp"}:
        raise ValueError("No documented schema-negative materializer for this feature")
    plan = {"schema_version": 1, "case_id": case.get("case_id"), "provider": provider,
            "source_id": source, "model": model, "feature": feature, "variant": variant,
            "expected_outcome": case.get("expected_outcome"), "status": "ready", "request": None,
            "validator": {"kind": feature, "baseline_response_cannot_prove_feature": True},
            "resources": {}, "extra_api_steps": [], "preconditions": [],
            "max_api_requests": 1, "max_public_http_requests": 0,
            "max_model_invocations": None, "maximum_primary_output_tokens": body["max_tokens"],
            "client_tool_execution_allowed": False,
            "official_urls": case.get("official_urls", [])}
    v = plan["validator"]
    if feature == "code_execution":
        version = fixture.get("tool_version", "20260521")
        if version not in {"20250825", "20260120", "20260521"}:
            raise ValueError("Unverified code execution version")
        body["tools"] = [{"type": "code_execution_" + version, "name": "code_execution"}]
        body["messages"] = [{"role": "user", "content": "Use code execution to run print(17 * 19). Do not read files or access a network. Then reply only with 323."}]
        v.update(kind="server_code_execution", expected_text="323", require_successful_result=True,
                 allowed_call_names=["code_execution", "bash_code_execution", "text_editor_code_execution"])
    elif feature in {"web_search", "web_fetch"}:
        tool = {"type": feature + "_20260318", "name": feature, "max_uses": 1,
                "allowed_domains": ["platform.claude.com"],
                "allowed_callers": ["direct"], "response_inclusion": "full"}
        if feature == "web_fetch":
            tool.update(max_content_tokens=2048, citations={"enabled": True})
        body["tools"] = [tool]
        body["messages"] = [{"role": "user", "content": f"Use {feature} exactly once for {DOC_URL}. Read the returned source and state its Messages creation endpoint as /v1/messages. Do not use other sites."}]
        v.update(kind="server_web_tool", tool_name=feature, required_url=DOC_URL,
                 expected_text_contains="/v1/messages", maximum_tool_uses=1)
    elif feature == "tool_search":
        search = fixture.get("search")
        if search not in {"regex", "bm25"}:
            raise ValueError("Unverified tool-search variant")
        name = "tool_search_tool_" + search
        body["tools"] = [{"type": name + "_20251119", "name": name}, {
            "name": "record_product", "description": "Record the integer product of 17 and 19.", "defer_loading": True,
            "input_schema": {"type": "object", "properties": {"product": {"type": "integer", "enum": [323]}},
                             "required": ["product"], "additionalProperties": False}}]
        body["messages"] = [{"role": "user", "content": "First use tool search to find record_product, then call record_product with product 323."}]
        v.update(kind="server_tool_search", search_name=name, tool_name="record_product", expected_arguments={"product": 323},
                 proof_scope="tool_discovery_and_call_schema_only")
    elif feature in PUBLIC_FIXTURES:
        if fixture.get("url") != PUBLIC_FIXTURES[feature]:
            raise ValueError("URL fixture must be the exact documented public fixture")
        media = "image" if feature == "url_image" else "document"
        body["messages"] = [{"role": "user", "content": [{"type": media, "source": {"type": "url", "url": fixture["url"]}},
                                                               {"type": "text", "text": {"__fixture__": "verified_url_fixture_question"}}]}]
        plan.update(status="resource_preparation_required", request_template=_request(body, headers), max_public_http_requests=1)
        plan["preconditions"] = ["Download this exact URL and freeze bytes, media type, hash and independently reviewed question/answer before sending."]
        plan["extra_api_steps"] = [{"id": "fetch_public_fixture", "phase": "before_primary", "kind": "public_http",
                                     "request": {"method": "GET", "url": fixture["url"], "headers": {}},
                                     "max_requests": 1, "follow_redirects": False, "maximum_download_bytes": 32 * 1024 * 1024}]
        v.update(kind="verified_public_media", media=media, required_url=fixture["url"])
        return plan
    elif feature == "files_api":
        media = fixture.get("media")
        data, mime, filename = _fixture_bytes(media)
        plan["resources"]["fixture"] = {"media_type": mime, "filename": filename,
            "data_base64": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest(), "synthetic": True}
        expected = "blue" if media == "image" else "FABLE_MATRIX_323"
        question = "Name the square color using one lowercase word." if media == "image" else "Return only the document verification code."
        body["messages"] = [{"role": "user", "content": [{"type": media, "source": {"type": "file", "file_id": copy.deepcopy(FILE_BINDING)}},
                                                               {"type": "text", "text": question}]}]
        plan.update(status="resource_preparation_required", request_template=_request(body, headers), max_api_requests=3)
        plan["extra_api_steps"] = [
            {"id": "upload_fixture", "phase": "before_primary", "request": {"method": "POST", "path": "/v1/files",
                "headers": {}, "multipart": {"file": {"resource_id": "fixture"}}}, "max_requests": 1,
                "bind_response_id_to": "/body/messages/0/content/0/source/file_id"},
            {"id": "delete_fixture", "phase": "finally", "request": {"method": "DELETE",
                "path_template": "/v1/files/{upload_fixture.response.id}", "headers": {}}, "max_requests": 1,
                "only_if_upload_created_id": True, "run_after_primary_error": True,
                "cleanup_failure_blocks_resource_completion": True}]
        v.update(kind="uploaded_media", media=media, expected_text=expected, require_cleanup=True)
        return plan
    elif feature == "fallbacks":
        body.pop("fallbacks", None)
        _set_beta(headers, "server-side-fallback-2026-07-01")
        plan.update(status="resource_preparation_required", request_template=_request(body, headers), max_api_requests=2)
        plan["extra_api_steps"] = [{"id": "discover_allowed_fallbacks", "phase": "before_primary",
            "request": {"method": "GET", "path": "/v1/models/" + model, "headers": copy.deepcopy(headers)}, "max_requests": 1}]
        plan["preconditions"] = ["Discover a permitted fallback among the two approved exact Fable IDs, excluding the primary."]
        v.update(kind="fallback", allowed_model_scope=list(MODELS), expected_text="323", require_fallback_iteration=True)
        return plan
    elif feature == "advisor":
        _set_beta(headers, "advisor-tool-2026-03-01")
        body["tools"] = [{"type": "advisor_20260301", "name": "advisor", "model": model,
                          "max_uses": 1, "max_tokens": 1023 if negative else 1024, "caching": None}]
        body["messages"] = [{"role": "user", "content": "Consult the advisor once to verify 17 * 19, then reply with only 323."}]
        plan.update(maximum_advisor_calls=1, maximum_advisor_output_tokens=1024)
        v.update(kind="advisor", expected_text="323", advisor_model=model, maximum_tool_uses=1)
        if negative:
            v.update(kind="field_rejection", error_parameter_paths=["tools.max_tokens"])
    elif feature in {"browser", "computer"}:
        tool = {"type": feature + "_toolset_20260801"}
        if negative:
            tool["name"] = feature
        body["tools"] = [tool]
        body["messages"] = [{"role": "user", "content": "Request one screenshot with the provided toolset. Do not click, type, navigate, read files, or perform any other action."}]
        body["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
        v.update(kind="client_toolset_schema", toolset_name=feature, member_name="screenshot",
                 proof_scope="toolset_declaration_and_call_schema_only", client_execution_verified=False)
        if negative:
            v.update(kind="field_rejection", error_parameter_paths=["tools.name"])
    elif feature == "mcp":
        _set_beta(headers, "mcp-client-2025-11-20")
        if not negative:
            plan.update(status="external_fixture_required")
            plan["preconditions"] = ["An approved public HTTPS MCP server with a deterministic read-only tool, expected arguments and result is required."]
            v.update(kind="mcp", feature_execution_verified=False)
            plan["max_api_requests"] = 0
            return plan
        body["mcp_servers"] = []
        body["tools"] = [{"type": "mcp_toolset", "mcp_server_name": "missing_server"}]
        body["messages"] = [{"role": "user", "content": "List tools from the declared MCP toolset."}]
        v.update(kind="field_rejection", error_parameter_paths=["mcp_server_name", "mcp_servers"],
                 proof_scope="schema_only_not_execution")
    else:
        raise ValueError("Unimplemented platform feature: " + str(feature))
    if _has_placeholder(body):
        raise ValueError("A ready platform request must not contain fixture placeholders")
    plan["request"] = _request(body, headers)
    return plan


def bind_uploaded_file(plan: dict, response: dict) -> dict:
    """Bind a real Files response ID and prepare its guaranteed cleanup path."""
    plan = copy.deepcopy(plan)
    if plan.get("feature") != "files_api" or plan.get("status") != "resource_preparation_required":
        raise ValueError("Only a pending upload plan can receive a file ID")
    file_id = response.get("id")
    if response.get("type") != "file" or not isinstance(file_id, str) or not re.fullmatch(r"file_[A-Za-z0-9_-]+", file_id):
        raise ValueError("Upload must return a real-shaped file resource")
    request = plan.pop("request_template")
    request["body"]["messages"][0]["content"][0]["source"]["file_id"] = file_id
    plan["extra_api_steps"][1]["request"] = {"method": "DELETE", "path": "/v1/files/" + file_id, "headers": {}}
    plan["resources"]["uploaded_file"] = {"id": file_id, "cleanup_pending": True}
    plan.update(status="ready", request=request)
    return plan


def bind_verified_url_fixture(plan: dict, *, content: bytes, media_type: str,
                              question: str, expected_text: str) -> dict:
    """Freeze caller-downloaded bytes and independently reviewed exact semantics."""
    plan = copy.deepcopy(plan)
    if plan.get("feature") not in PUBLIC_FIXTURES or plan.get("status") != "resource_preparation_required":
        raise ValueError("Expected a pending documented URL fixture")
    if not isinstance(content, bytes) or not content or not question.strip() or not expected_text.strip():
        raise ValueError("Verified fixture bytes and nonempty question/answer are required")
    if len(content) > 32 * 1024 * 1024:
        raise ValueError("Public fixture exceeds the bounded download size")
    expected_mime = "image/jpeg" if plan["feature"] == "url_image" else "application/pdf"
    magic = b"\xff\xd8\xff" if plan["feature"] == "url_image" else b"%PDF-"
    if media_type.split(";", 1)[0].strip() != expected_mime or not content.startswith(magic):
        raise ValueError("Public resource must have the documented binary format, not an HTML error page")
    if plan["feature"] == "url_image":
        from PIL import Image
        try:
            with Image.open(io.BytesIO(content)) as image:
                image.load()
                if image.format != "JPEG" or max(image.size) > 8000:
                    raise ValueError("JPEG fixture dimensions or format are invalid")
        except (OSError, ValueError) as exc:
            raise ValueError("Public JPEG fixture could not be decoded") from exc
    elif b"%%EOF" not in content[-1024:]:
        raise ValueError("Public PDF fixture is incomplete")
    request = plan.pop("request_template")
    request["body"]["messages"][0]["content"][1]["text"] = question
    effective_url = request["body"]["messages"][0]["content"][0]["source"]["url"]
    plan["resources"]["verified_public_fixture"] = {"url": effective_url, "original_url": PUBLIC_FIXTURES[plan["feature"]],
        "media_type": expected_mime, "sha256": hashlib.sha256(content).hexdigest(), "byte_length": len(content),
        "question": question, "expected_text": expected_text, "semantics_review": "supplied_by_caller"}
    plan["validator"]["expected_text"] = expected_text
    plan.update(status="ready", request=request, preconditions=[])
    return plan


def resolve_public_fixture_url(evidence: dict) -> str:
    """Permit only the independently observed, hash-bound official PDF redirect."""
    original = evidence.get("url")
    if original not in PUBLIC_FIXTURES.values():
        raise ValueError("Public fixture evidence must retain an original documented URL")
    keys = {"original_url", "canonical_url", "redirect_receipt", "redirect_receipt_sha256"}
    present = keys & evidence.keys()
    if not present:
        return original
    if present != keys or original != PUBLIC_FIXTURES["url_document"]:
        raise ValueError("Only the verified PDF fixture has an explicit canonical redirect")
    if (evidence["original_url"] != original or evidence["canonical_url"] != PUBLIC_PDF_CANONICAL_URL
            or evidence["redirect_receipt"] != VERIFIED_PDF_REDIRECT):
        raise ValueError("Public redirect differs from the observed unauthenticated official receipt")
    encoded = json.dumps(VERIFIED_PDF_REDIRECT, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    actual_encoded = json.dumps(evidence["redirect_receipt"], sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    if actual_encoded != encoded or evidence["redirect_receipt_sha256"] != hashlib.sha256(actual_encoded).hexdigest():
        raise ValueError("Public redirect receipt hash changed")
    return PUBLIC_PDF_CANONICAL_URL


def bind_public_url_redirect(plan: dict, evidence: dict) -> dict:
    """Keep the original case descriptor and explicitly bind its verified URL."""
    plan = copy.deepcopy(plan)
    if plan.get("feature") not in PUBLIC_FIXTURES or plan.get("status") != "resource_preparation_required":
        raise ValueError("Expected an unprepared public URL case")
    if evidence.get("url") != PUBLIC_FIXTURES[plan["feature"]]:
        raise ValueError("URL evidence belongs to a different fixture")
    effective = resolve_public_fixture_url(evidence)
    if effective != evidence["url"]:
        plan["resources"]["verified_redirect"] = {k: copy.deepcopy(evidence[k]) for k in
            ("original_url", "canonical_url", "redirect_receipt", "redirect_receipt_sha256")}
        plan["validator"].update(original_url=evidence["url"], required_url=effective)
        plan["request_template"]["body"]["messages"][0]["content"][0]["source"]["url"] = effective
        plan["extra_api_steps"][0]["request"]["url"] = effective
    return plan


def bind_fallback_discovery(plan: dict, response: dict) -> dict:
    """Select only a discovered allowed secondary model within the user's scope."""
    plan = copy.deepcopy(plan)
    if plan.get("feature") != "fallbacks" or plan.get("status") != "resource_preparation_required":
        raise ValueError("Expected pending fallback discovery")
    if (not isinstance(response, dict) or response.get("id") != plan["model"]
            or not isinstance(response.get("allowed_fallback_models"), list)):
        raise ValueError("Discovery must identify the primary model and its allowed fallback list")
    allowed = [x if isinstance(x, str) else x.get("id") if isinstance(x, dict) else None
               for x in response["allowed_fallback_models"]]
    targets = [m for m in MODELS if m != plan["model"] and m in allowed]
    if not targets:
        plan.update(status="external_fixture_required", preconditions=["No discovered permitted secondary model lies inside the two-model scope."])
        return plan
    request = plan.pop("request_template")
    request["body"]["fallbacks"] = [{"model": targets[0], "max_tokens": request["body"]["max_tokens"], "output_config": {"effort": "low"}}]
    plan["validator"]["allowed_fallbacks"] = targets[:1]
    plan.update(status="ready", request=request, preconditions=[])
    return plan


def bind_mcp_fixture(plan: dict, fixture: dict) -> dict:
    """Bind independently verified public DeepWiki evidence, with one read tool."""
    from jsonschema import Draft202012Validator
    plan, fixture = copy.deepcopy(plan), copy.deepcopy(fixture)
    if plan.get("feature") != "mcp" or plan.get("variant") != "positive" or plan.get("status") != "external_fixture_required":
        raise ValueError("Expected an unbound positive MCP plan")
    fields = {"server_url", "server_name", "tool_name", "arguments", "input_schema",
              "expected_result_sha256", "result_hash_mode", "verification"}
    metadata = {"schema_version", "authentication_required", "evidence_batch", "official_fixture_documentation", "scope", "reference_result"}
    if not isinstance(fixture, dict) or not fields <= fixture.keys() or set(fixture) - fields - metadata:
        raise ValueError("MCP fixture requires the exact public verification fields")
    if fixture.get("authentication_required", False) is not False:
        raise ValueError("Only the verified unauthenticated public MCP fixture is permitted")
    if fixture.get("official_fixture_documentation", "https://docs.devin.ai/work-with-devin/deepwiki-mcp") != "https://docs.devin.ai/work-with-devin/deepwiki-mcp":
        raise ValueError("MCP fixture documentation is outside the verified provider source")
    if (fixture["server_url"] != MCP_PUBLIC_SERVER or fixture["server_name"] != "deepwiki_public_sdk"
            or fixture["tool_name"] != "read_wiki_structure"
            or fixture["arguments"] != {"repoName": MCP_PUBLIC_REPO}
            or fixture["result_hash_mode"] != "text_blocks_join_newline"):
        raise ValueError("MCP fixture must target only the approved public SDK repository and read tool")
    verification = fixture["verification"]
    if not isinstance(verification, dict) or set(verification) != {"tools_list_response_sha256", "tool_call_response_sha256"}:
        raise ValueError("MCP discovery and direct-call receipts are required")
    hashes = [fixture["expected_result_sha256"], *verification.values()]
    if any(not isinstance(x, str) or not re.fullmatch(r"[0-9a-f]{64}", x) for x in hashes):
        raise ValueError("MCP fixture proof hashes must be exact SHA256 values")
    schema = fixture["input_schema"]
    if not isinstance(schema, dict):
        raise ValueError("MCP fixture must preserve the discovered input schema")
    if any(key in item and (not isinstance(item[key], str) or not item[key].startswith("#"))
           for item in _objects(schema) for key in ("$ref", "$dynamicRef", "$recursiveRef")):
        raise ValueError("External schema references cannot introduce preparation network requests")
    Draft202012Validator.check_schema(schema)
    # An explicit registry has no retrieval callback, including for references
    # affected by nested schema IDs. Preparation and replay stay offline.
    if not Draft202012Validator(schema, registry=Registry()).is_valid(fixture["arguments"]):
        raise ValueError("Public SDK arguments do not satisfy the discovered MCP schema")
    reference = fixture.get("reference_result")
    if reference is not None:
        if not isinstance(reference, dict) or reference.get("isError", False) is not False:
            raise ValueError("MCP reference tool call did not succeed")
        contents = reference.get("content")
        if not isinstance(contents, list) or not contents or not all(isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str) for b in contents):
            raise ValueError("MCP reference result requires deterministic text blocks")
        if hashlib.sha256("\n".join(b["text"] for b in contents).encode()).hexdigest() != fixture["expected_result_sha256"]:
            raise ValueError("MCP reference result does not match its frozen hash")
    body = {"model": plan["model"], "max_tokens": plan["maximum_primary_output_tokens"], "stream": False,
        "output_config": {"effort": "low"}, "messages": [{"role": "user", "content":
            "Use read_wiki_structure exactly once for the public repository " + MCP_PUBLIC_REPO
            + ". Read its returned structure, then name the repository and briefly identify the listed topics. Do not call other tools or query other repositories."}],
        "mcp_servers": [{"type": "url", "name": fixture["server_name"], "url": MCP_PUBLIC_SERVER}],
        "tools": [{"type": "mcp_toolset", "mcp_server_name": fixture["server_name"],
                   "default_config": {"enabled": False}, "configs": {"read_wiki_structure": {"enabled": True}}}]}
    if type(body["max_tokens"]) is not int or body["max_tokens"] < 256:
        raise ValueError("MCP request must preserve the output floor")
    plan["request"] = _request(body, {"anthropic-beta": "mcp-client-2025-11-20"})
    plan["resources"]["mcp_fixture"] = fixture
    plan["validator"] = {"kind": "mcp_fixture", "server_name": fixture["server_name"],
        "tool_name": fixture["tool_name"], "expected_arguments": fixture["arguments"], "input_schema": schema,
        "expected_result_sha256": fixture["expected_result_sha256"], "result_hash_mode": fixture["result_hash_mode"],
        "expected_text_contains": MCP_PUBLIC_REPO, "maximum_tool_uses": 1,
        "baseline_response_cannot_prove_feature": True, "proof_scope": "mcp_read_only_tool_result_verified"}
    plan.update(status="ready", max_api_requests=1, preconditions=[])
    return plan


def _objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def validate_platform_response(plan: dict, status: int, payload: Any, *, cleanup: dict | None = None) -> dict:
    """Check declared feature evidence, separate from physical-backend identity."""
    result = {"pass": False, "parameter_pass": False, "effect_pass": False,
              "proof_scope": plan.get("validator", {}).get("proof_scope", "feature_response"), "failures": []}
    if plan.get("status") != "ready" or not plan.get("request"):
        result["failures"].append("unprepared_platform_case")
        return result
    v, body = plan["validator"], plan["request"]["body"]
    if v["kind"] == "field_rejection":
        rejection = parameter_rejection(status, payload, v["error_parameter_paths"])
        result.update(rejection=rejection, proof_scope="attributed_schema_rejection", effect_pass=None)
        result["pass"] = result["parameter_pass"] = rejection["attributed"]
        if not result["pass"]:
            result["failures"].append("missing_attributed_rejection")
        return result
    if status != 200 or not isinstance(payload, dict):
        result["failures"].append("missing_successful_message")
        return result
    allowed_models = [plan["model"], *v.get("allowed_fallbacks", [])]
    model = payload.get("model") if payload.get("model") in allowed_models else plan["model"]
    evidence = native_evidence(payload, model=model, max_tokens=body["max_tokens"] + plan.get("maximum_advisor_output_tokens", 0))
    result["native_evidence"] = evidence
    if not evidence["pass"]:
        result["failures"].extend(evidence["failures"])
        return result
    result["parameter_pass"] = True
    blocks = payload["content"]
    objects = list(_objects(blocks))
    if payload.get("stop_reason") in {"refusal", "max_tokens", "pause_turn"}:
        result["failures"].append("feature_not_completed")
    if any(str(obj.get("type", "")).endswith("_error") or obj.get("error_code")
           or "return_code" in obj and (type(obj["return_code"]) is not int or obj["return_code"] != 0) for obj in objects):
        result["failures"].append("tool_error_inside_response")
    text = evidence["text"].strip()
    if v.get("expected_text") is not None and text != v["expected_text"]:
        result["failures"].append("unexpected_text")
    if v.get("expected_text_contains") and v["expected_text_contains"] not in text:
        result["failures"].append("missing_expected_text")
    calls = [b for b in blocks if b.get("type") == "server_tool_use"]
    ids = [b.get("id") for b in calls]
    if (any(not isinstance(value, str) or not value for value in ids)
            or len(ids) != len(set(ids))
            or any(not isinstance(b.get("name"), str) or not b["name"] or not isinstance(b.get("input"), dict) for b in calls)):
        result["failures"].append("malformed_server_tool_calls")
        return result
    by_id = {b["id"]: b for b in calls}
    results = [b for b in blocks if b["type"].endswith("_tool_result") and b["type"] != "mcp_tool_result"]
    result_ids = [b.get("tool_use_id") for b in results]
    if (any(not isinstance(value, str) or not value for value in result_ids)
            or len(result_ids) != len(set(result_ids)) or set(result_ids) != set(ids)):
        result["failures"].append("unmatched_server_tool_results")
        return result
    kind = v["kind"]
    if kind == "server_code_execution":
        valid = []
        for item in results:
            call = by_id[item["tool_use_id"]]
            inputs = json.dumps(call.get("input", {}))
            succeeded = [o for o in _objects(item) if o.get("type") in {"code_execution_result", "bash_code_execution_result"}
                         and type(o.get("return_code")) is int and o["return_code"] == 0]
            stdout = " ".join(str(o.get("stdout", "")) for o in succeeded)
            if (item.get("type") in {"code_execution_tool_result", "bash_code_execution_tool_result"}
                    and call.get("name") in v["allowed_call_names"] and re.search(r"\b17\b", inputs)
                    and re.search(r"\b19\b", inputs) and re.search(r"\b323\b", stdout)):
                valid.append(item)
        if not valid:
            result["failures"].append("missing_executed_arithmetic_result")
    elif kind == "server_web_tool":
        name, url = v["tool_name"], v["required_url"]
        matches = [r for r in results if r.get("type") == name + "_tool_result" and by_id[r["tool_use_id"]].get("name") == name]
        if not matches or not any(o.get("url") == url for r in matches for o in _objects(r)):
            result["failures"].append("missing_matching_official_web_result")
        usage = payload.get("usage", {}).get("server_tool_use")
        used = usage.get(name + "_requests") if isinstance(usage, dict) else None
        if type(used) is not int or used != 1:
            result["failures"].append("missing_bounded_web_usage")
    elif kind == "server_tool_search":
        matches = [r for r in results if r.get("type") == "tool_search_tool_result" and by_id[r["tool_use_id"]].get("name") == v["search_name"]]
        if not any(o.get("type") == "tool_reference" and o.get("tool_name") == v["tool_name"] for r in matches for o in _objects(r)):
            result["failures"].append("missing_discovered_tool_reference")
        if not any(c.get("name") == v["tool_name"] and c.get("input") == v["expected_arguments"] for c in evidence["tool_calls"]):
            result["failures"].append("missing_discovered_tool_call")
        if evidence["stop_reason"] != "tool_use":
            result["failures"].append("missing_client_tool_stop")
    elif kind == "advisor":
        matches = [r for r in results if r.get("type") == "advisor_tool_result" and by_id[r["tool_use_id"]].get("name") == "advisor"]
        if len(matches) != 1 or not any(o.get("type") == "advisor_redacted_result" and isinstance(o.get("encrypted_content"), str)
                                      and o["encrypted_content"] for r in matches for o in _objects(r)):
            result["failures"].append("missing_advisor_result")
        iterations = payload.get("usage", {}).get("iterations") or []
        if not any(i.get("type") == "advisor_message" and i.get("model") == v["advisor_model"] for i in iterations if isinstance(i, dict)):
            result["failures"].append("missing_advisor_iteration_usage")
    elif kind == "client_toolset_schema":
        calls = evidence["tool_calls"]
        if len(calls) != 1 or calls[0].get("toolset_name") != v["toolset_name"] or calls[0].get("name") != "screenshot" or calls[0].get("input") != {}:
            result["failures"].append("missing_screenshot_toolset_call")
        if evidence["stop_reason"] != "tool_use":
            result["failures"].append("missing_client_tool_stop")
        result["effect_pass"] = None
        result["client_execution_verified"] = False
    elif kind == "uploaded_media":
        file_id = plan["resources"]["uploaded_file"]["id"]
        if not cleanup or cleanup.get("file_id") != file_id or cleanup.get("deleted") is not True:
            result["failures"].append("uploaded_file_cleanup_unverified")
    elif kind == "verified_public_media":
        if not plan["resources"].get("verified_public_fixture"):
            result["failures"].append("public_fixture_unverified")
    elif kind == "fallback":
        iterations = payload.get("usage", {}).get("iterations") or []
        fallback = payload.get("model") in v.get("allowed_fallbacks", [])
        if not fallback or not any(i.get("type") == "fallback_message" and i.get("model") == payload.get("model") for i in iterations if isinstance(i, dict)):
            result["failures"].append("fallback_not_triggered")
    elif kind == "mcp_fixture":
        from jsonschema import Draft202012Validator
        mcp_calls = [b for b in blocks if b.get("type") == "mcp_tool_use"]
        mcp_results = [b for b in blocks if b.get("type") == "mcp_tool_result"]
        matched = (len(mcp_calls) == 1 and len(mcp_results) == 1
            and isinstance(mcp_calls[0].get("id"), str) and bool(mcp_calls[0]["id"])
            and mcp_calls[0].get("server_name") == v["server_name"]
            and mcp_calls[0].get("name") == v["tool_name"]
            and mcp_calls[0].get("input") == v["expected_arguments"]
            and Draft202012Validator(v["input_schema"], registry=Registry()).is_valid(mcp_calls[0].get("input"))
            and mcp_results[0].get("tool_use_id") == mcp_calls[0]["id"]
            and mcp_results[0].get("is_error", False) is False)
        text_parts = None
        if matched:
            content = mcp_results[0].get("content")
            if isinstance(content, str):
                text_parts = [content]
            elif isinstance(content, list) and content and all(isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str) for b in content):
                text_parts = [b["text"] for b in content]
        if text_parts is None or hashlib.sha256("\n".join(text_parts).encode()).hexdigest() != v["expected_result_sha256"]:
            result["failures"].append("mcp_tool_result_not_bound_to_verified_public_fixture")
    else:
        result["failures"].append("unimplemented_platform_validator")
    result["pass"] = not result["failures"]
    if result["effect_pass"] is not None:
        result["effect_pass"] = result["pass"]
    return result
