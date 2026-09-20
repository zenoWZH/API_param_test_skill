"""Compile the existing Fable cases into shared, auditable execution steps.

Only pure fixtures, request transforms and domain validators are reused from the
historical runner. HTTP and cleanup are exclusively owned by RunContext.
"""
from __future__ import annotations

import base64
import copy
import hashlib
from pathlib import Path
import re
from typing import Any

from lib.fable_5_5_1_platform import (
    PUBLIC_FIXTURES, bind_fallback_discovery, bind_mcp_fixture,
    bind_public_url_redirect, bind_uploaded_file, bind_verified_url_fixture,
    materialize_platform_case, validate_platform_response,
)
from lib import fable_workflow_definition as definitions

from ..transport import canonical_bytes

FACTORY_ID = "fable_research"
FACTORY_VERSION = "1"
_NONCE = "__workflow_run_nonce__"
_HANDLERS = {
    "fable.case": "run_case",
    "fable.platform.prepare": "prepare_platform",
    "fable.platform.request": "run_platform",
    "fable.files.delete": "delete_file",
}


def source_digest() -> str:
    # Factory identity covers shared domain definitions, not deployment-specific
    # configuration modules. The dispatcher separately binds the runtime target.
    root = Path(__file__).resolve().parents[3]
    names = ("lib/fable_5_5_1_matrix.py", "lib/fable_5_5_1_validation.py", "lib/fable_5_5_1_platform.py",
             "lib/fable_workflow_definition.py")
    hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}
    hashes["workflow_fable_adapter"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return hashlib.sha256(canonical_bytes(hashes)).hexdigest()


def register_handlers(registry: Any) -> None:
    for name, function in _HANDLERS.items():
        registry.register(name, globals()[function], version=FACTORY_VERSION, digest=runtime_digest)


def http_operation_auth(provider: str) -> dict[str, str]:
    """Preserve reviewed gateway helper auth independently of Messages auth."""
    if provider in {"inferenceai_awsb", "sanqiaoapi"}:
        return {"/v1/files": "bearer", "/v1/models": "bearer"}
    if provider == "anthropic_official":
        return {}
    raise ValueError("Unregistered Fable workflow provider")


def runtime_digest() -> str:
    root = Path(__file__).resolve().parents[3]
    files = ("lib/test_runner/transport.py", "lib/credential_security.py", "lib/parameter_output_limit.py",
             "lib/anthropic_message_stream.py", "lib/config.py", "scripts/workflow_test.py")
    return hashlib.sha256(canonical_bytes({"factory": source_digest(), **{
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}})).hexdigest()


def _multipart(plan: dict) -> tuple[bytes, str]:
    resource = plan["resources"]["fixture"]
    raw = base64.b64decode(resource["data_base64"], validate=True)
    if hashlib.sha256(raw).hexdigest() != resource["sha256"]:
        raise ValueError("Synthetic upload resource hash changed")
    boundary = "fable-matrix-" + hashlib.sha256(canonical_bytes({"case_id": plan["case_id"], "sha256": resource["sha256"]})).hexdigest()[:24]
    filename, mime = resource["filename"], resource["media_type"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", filename) or mime not in {"image/png", "application/pdf"}:
        raise ValueError("Invalid synthetic multipart metadata")
    wire = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n").encode() + raw + f"\r\n--{boundary}--\r\n".encode()
    return wire, "multipart/form-data; boundary=" + boundary


def _ref(step: str, path: list[str]) -> dict:
    return {"$ref": {"step": step, "path": path, "type": "object"}}


def _replace_nonce(value: Any, nonce: str) -> Any:
    if isinstance(value, str):
        return value.replace(_NONCE, nonce)
    if isinstance(value, list):
        return [_replace_nonce(item, nonce) for item in value]
    if isinstance(value, dict):
        return {key: _replace_nonce(item, nonce) for key, item in value.items()}
    return value


def build_workflow(provider: str, model: str, *, fixture_evidence: list[dict] | None = None,
                   mcp_fixture: dict | None = None) -> dict:
    """Pure, offline full-suite compiler. No provider configuration is read."""
    rows = definitions.cases_for_provider(provider, model)
    source = "aws_bedrock" if provider == "inferenceai_awsb" else "anthropic"
    profile = f"text/{source}/claude_fable/{model}"
    native_contract = "claude_fable_5_1_native_messages" if model.endswith("5-1") else "claude_fable_native_messages"
    aws_contract = "claude_fable_5_1_aws_bedrock_runtime_messages" if model.endswith("5-1") else "claude_fable_aws_bedrock_runtime_messages"
    interface = profile + ("#bedrock-runtime-messages-default" if source == "aws_bedrock" else "#anthropic-messages-default")
    target = {"modality": "text", "source_id": source, "profile_id": profile, "interface_id": interface,
              "contract_id": aws_contract if source == "aws_bedrock" else native_contract,
              "base_url": definitions.BASES[provider],
              "execution_target": {"provider_id": provider, "request_model_id": model,
                                   "api_form": "anthropic_messages", "transport_adapter_id": "claude_messages"}}
    baseline = rows[0]["case_id"]
    evidence = {row["url"]: copy.deepcopy(row) for row in fixture_evidence or []}
    cases, steps, maximum_requests, cleanup_requests = [], [], 0, 0
    for original in rows:
        case = copy.deepcopy(original)
        case_id = case["case_id"]
        deps = list(dict.fromkeys([*case.get("depends_on", []),
                                  *([case["validation"]["positive_control"]] if case["validation"].get("positive_control") else []),
                                  *([baseline] if case_id != baseline else [])]))
        cases.append({"id": case_id, "name": case["name"], "phase": case.get("phase", "core")})
        requires = [{"step": item, "path": ["accepted"], "equals": True} for item in deps]
        if case.get("phase") != "platform":
            if case["validation"].get("requires_specialized_sender"):
                raise ValueError("Missing registered specialized Fable step handler")
            if case["validation"].get("dependency", {}).get("operation") != "reuse_exact_request_body":
                case["body"] = definitions.replace_fixtures(case["body"], definitions.fixture_values(case, _NONCE))
            step = {"id": case_id, "case_id": case_id, "handler": "fable.case", "depends_on": deps,
                    "requires": requires, "inputs": {"case": case, "rows": {key: _ref(key, ["record"]) for key in deps}},
                    "request_cap": 1}
            steps.append(step)
            maximum_requests += 1
            continue
        # Sanqiao reuses the source's pure plans; only execution scope is retargeted.
        materializer_case = copy.deepcopy(case)
        if provider in definitions.CASE_SPECS:
            materializer_case = definitions.retarget_provider(materializer_case, provider, definitions.CASE_SPECS[provider])
        plan = materialize_platform_case(materializer_case)
        fixture = evidence.get(PUBLIC_FIXTURES.get(plan["feature"]))
        if fixture is not None:
            plan = bind_public_url_redirect(plan, fixture)
        if plan["feature"] == "mcp" and plan.get("status") == "external_fixture_required" and mcp_fixture is not None:
            plan = bind_mcp_fixture(plan, mcp_fixture)
        if provider in definitions.CASE_SPECS:
            plan = definitions.retarget_provider(plan, definitions.CASE_SPECS[provider], provider)
        preparation = case_id + "/prepare"
        steps.append({"id": preparation, "case_id": case_id, "handler": "fable.platform.prepare",
                      "depends_on": deps, "requires": requires,
                      "inputs": {"plan": plan, "fixture_evidence": fixture}, "request_cap": 1 if plan["extra_api_steps"] else 0,
                      "cleanup_request_cap": 1 if plan["feature"] == "files_api" else 0})
        steps.append({"id": case_id, "case_id": case_id, "handler": "fable.platform.request", "depends_on": [preparation],
                      "requires": [{"step": preparation, "path": ["accepted"], "equals": True}],
                      "inputs": {"prepared": _ref(preparation, ["prepared"]), "case": case}, "request_cap": 1})
        cases[-1]["preconditions"] = list(plan.get("preconditions", []))
        cases[-1]["preparation_status"] = plan["status"]
        maximum_requests += plan.get("max_api_requests", 1) + plan.get("max_public_http_requests", 0)
        if plan["feature"] == "files_api":
            cleanup_requests += 1
            maximum_requests -= 1  # Deletion has its own reserved budget.
    return {"workflow_schema_version": 1, "id": f"fable/{provider}/{model}/full", "target": target,
            "cases": cases, "steps": steps,
            "limits": {"max_requests": maximum_requests, "request_timeout_seconds": definitions.TIMEOUT,
                       "deadline_seconds": maximum_requests * definitions.TIMEOUT,
                       "cleanup_max_requests": cleanup_requests, "cleanup_deadline_seconds": max(15, cleanup_requests * 15)},
            "policy": {"fatal_status_codes": [401, 403, 429]}, "native_aws_certification": False,
            "factory": {"factory_id": FACTORY_ID, "version": FACTORY_VERSION, "source_sha256": source_digest()},
            "factory_arguments": {"fixture_evidence": copy.deepcopy(fixture_evidence), "mcp_fixture": copy.deepcopy(mcp_fixture)}}


def run_case(context: Any, inputs: dict) -> dict:
    case = _replace_nonce(inputs["case"], context.run_id)
    rows = inputs.get("rows", {})
    body = definitions.materialize_request(case, rows)
    receipt = context.dispatch({"method": "POST", "path": "/v1/messages", "body": body, "headers": case["headers"]})
    verdict = definitions.evaluate(case, body, receipt.get("http_status"), receipt.get("response"),
                                   complete=receipt.get("response_complete", False), stream_error=receipt.get("stream_error"),
                                   stream_observed=receipt.get("stream_observed", False), rows=rows)
    record = {**receipt, "case_id": case["case_id"], "request": body, "verdict": verdict}
    return {"status": "passed" if verdict["pass"] else "failed", "accepted": verdict["actual_outcome"] == "accepted",
            "record": record, "verdict": verdict}


def _blocked(reason: str, **extra: Any) -> dict:
    return {"status": "blocked", "accepted": False, "reason": reason, **extra}


def prepare_platform(context: Any, inputs: dict) -> dict:
    plan = copy.deepcopy(inputs["plan"])
    feature = plan["feature"]
    if plan["status"] == "external_fixture_required":
        return _blocked("external_fixture_required", preconditions=plan["preconditions"])
    if feature in PUBLIC_FIXTURES:
        evidence = inputs.get("fixture_evidence")
        if not evidence:
            return _blocked("verified_public_fixture_required", preconditions=plan["preconditions"])
        request = {**plan["extra_api_steps"][0]["request"], "public": True}
        receipt = context.dispatch(request, kind="public")
        if receipt.get("http_status") != 200 or not receipt.get("response_complete"):
            return {"status": "failed", "accepted": False, "receipt": receipt}
        raw = base64.b64decode(receipt["raw_base64"], validate=True)
        mime = receipt.get("content_type", "").split(";", 1)[0].strip()
        if hashlib.sha256(raw).hexdigest() != evidence["sha256"] or mime != evidence["media_type"]:
            return {"status": "failed", "accepted": False, "reason": "public_fixture_evidence_mismatch"}
        plan = bind_verified_url_fixture(plan, content=raw, media_type=mime,
                                        question=evidence["question"], expected_text=evidence["expected_text"])
    elif feature == "files_api":
        wire, content_type = _multipart(plan)
        request = {**plan["extra_api_steps"][0]["request"], "wire_base64": base64.b64encode(wire).decode(), "content_type": content_type}
        receipt = context.dispatch(request, creates_resource=True)
        payload = receipt.get("response")
        file_id = payload.get("id") if isinstance(payload, dict) else None
        if receipt.get("http_status") in {200, 201} and isinstance(file_id, str) and re.fullmatch(r"file_[A-Za-z0-9_-]+", file_id):
            context.register_resource(file_id, cleanup_handler="fable.files.delete", cleanup_inputs={"file_id": file_id})
        elif type(receipt.get("http_status")) is int and 400 <= receipt["http_status"] < 500 and receipt["http_status"] != 408:
            context.creation_failed(context.last_creation_attempt_id, definitely_not_created=True)
        if receipt.get("http_status") not in {200, 201} or not receipt.get("response_complete"):
            return {"status": "failed", "accepted": False, "receipt": receipt}
        plan = bind_uploaded_file(plan, payload)
    elif feature == "fallbacks":
        receipt = context.dispatch(plan["extra_api_steps"][0]["request"])
        if receipt.get("http_status") != 200 or not receipt.get("response_complete"):
            return _blocked("fallback_discovery_unavailable", receipt=receipt)
        plan = bind_fallback_discovery(plan, receipt["response"])
    if plan["status"] != "ready":
        return _blocked(plan["status"], preconditions=plan.get("preconditions", []))
    return {"status": "passed", "accepted": True, "prepared": plan}


def delete_file(context: Any, inputs: dict) -> dict:
    file_id = inputs["file_id"]
    if not isinstance(file_id, str) or not re.fullmatch(r"file_[A-Za-z0-9_-]+", file_id):
        raise ValueError("Invalid owned file ID")
    receipt = context.dispatch({"method": "DELETE", "path": "/v1/files/" + file_id, "headers": {}})
    payload = receipt.get("response")
    deleted = bool(receipt.get("response_complete") and receipt.get("http_status") == 200 and isinstance(payload, dict)
                   and payload.get("id") == file_id and payload.get("type", "file_deleted") == "file_deleted" and not payload.get("error"))
    return {"status": "passed" if deleted else "failed", "accepted": deleted, "deleted": deleted, "receipt": receipt}


def run_platform(context: Any, inputs: dict) -> dict:
    plan, case = inputs["prepared"], inputs["case"]
    file_id = plan.get("resources", {}).get("uploaded_file", {}).get("id")
    cleanup = None
    try:
        receipt = context.dispatch(plan["request"])
    finally:
        if file_id:
            resource = context.cleanup_resource(file_id)
            cleanup = {"file_id": file_id, "deleted": resource.get("status") == "deleted"}
    verdict = validate_platform_response(plan, receipt.get("http_status"), receipt.get("response"), cleanup=cleanup)
    record = {**receipt, "case_id": case["case_id"], "request": plan["request"].get("body"), "verdict": verdict, "cleanup": cleanup}
    return {"status": "passed" if verdict.get("pass") else "failed",
            "accepted": bool(receipt.get("response_complete") and receipt.get("http_status") == 200),
            "record": record, "verdict": verdict}
