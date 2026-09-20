#!/usr/bin/env python3
"""Prepare or execute bounded official GPT Image 2.5 Responses image-tool proofs.

No network or credential access occurs during preparation. Newly prepared CLI
batches use the shared workflow engine and do not resume business sends. Existing
historical batches remain available for offline replay. Cleanup-only recovery
uses an exact original run ID; repeat experiments require a new batch.
"""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from lib.credential_security import ProviderCredential, credential_from_config, redact_secrets
from lib.gpt_image_25_responses import (
    DOCS, ENDPOINT, MAINLINE_MODEL, MAX_RESPONSE_BYTES, MODELS, SNAPSHOTS,
    build_request, digest, evaluate, fixture_png, public_payload, require_id,
    responses_cases, responses_image_cases, validate_case, validate_stream,
    CURRENT_EXPECTATION_POLICY, FROZEN_EXPECTATION_POLICY, effective_case_expectations,
    expand_responses_case_dependencies, select_responses_image_cases,
    responses_case_request_cap,
)
from lib.gpt_image_25_result_policy import aggregate_result_gates, case_result_gates, attach_semantic_review
from lib.image_matrix_reference import (
    auto_output_geometry_audit, build_image_matrix_candidate, canonical_digest, rejection_attribution,
    persist_image_matrix_candidate, select_image_matrix_cases,
)
from lib.image_reference_candidate import image_exchange_evidence
try:
    from lib.report_retention import initialize_report_retention, register_report_files
except ModuleNotFoundError as exc:
    if exc.name != "lib.report_retention":
        raise
    # The packaged App consumes the same audited retention kernel from MPDB.
    from lib import model_profile_catalog as _catalog_bootstrap
    from model_profile_db._report_retention import initialize_report_retention, register_report_files

REPORT_ROOT = ROOT / "reports" / "gpt_image_25_responses_reference_20260908"
FILES_ENDPOINT = "https://api.openai.com/v1/files"
REQUEST_TIMEOUT = (20, 600)
SCHEMA_VERSION = 1
MATRIX_PACKAGE_KIND = "gpt_resolution_quality"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def designated_credential() -> ProviderCredential:
    """Load only the designated OpenAI key, without shell evaluation or logs."""
    secret = None
    path = Path(os.environ.get("LLM_API_TEST_DOTENV") or ROOT / ".env").expanduser()
    if os.environ.get("LOADTEST_SKIP_DOTENV") != "1" and path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            matched = re.match(r"^\s*(?:export\s+)?OPENAI_API_KEY\s*=\s*(.*)$", line)
            if matched:
                values = shlex.split(matched.group(1), comments=True)
                if len(values) != 1:
                    raise ValueError("designated OPENAI_API_KEY must have one literal value")
                secret = values[0]
    secret = secret or os.environ.get("OPENAI_API_KEY")
    if not secret:
        raise ValueError("designated OPENAI_API_KEY is unavailable")
    return ProviderCredential.create(provider="openai_official", secret=secret, base_urls=[ENDPOINT])


def build_package(models: list[str] | None = None, names: list[str] | None = None) -> dict:
    selected = models or list(MODELS)
    if len(selected) != len(set(selected)) or any(model not in (*MODELS, *SNAPSHOTS.values()) for model in selected):
        raise ValueError("models must be distinct exact GPT Image 2.5 IDs")
    all_cases = [case for model in selected for case in responses_cases(model)]
    if names:
        unknown = set(names) - {case["name"] for case in all_cases} - {case["case_id"] for case in all_cases}
        if unknown:
            raise ValueError("unknown case selectors: " + ", ".join(sorted(unknown)))
        wanted = {case["case_id"] for case in all_cases if case["name"] in names or case["case_id"] in names}
        all_cases = expand_responses_case_dependencies(all_cases, list(wanted))
    else:
        all_cases = expand_responses_case_dependencies(all_cases)
    if not all_cases:
        raise ValueError("empty case selection")
    return {"schema_version": SCHEMA_VERSION, "source_id": "openai", "api_form": "openai_responses",
            "mainline_model": MAINLINE_MODEL, "models": selected, "case_selectors": names or [],
            "cases": all_cases, "official_references": list(DOCS),
            "request_cap": responses_case_request_cap(all_cases),
            "request_cap_includes": "one generation per case plus created-file upload and cleanup; no retries",
            "credential_source": "project OPENAI_API_KEY, official api.openai.com only",
            "input_fixtures": {"image_sha256": hashlib.sha256(fixture_png()).hexdigest(),
                               "mask_sha256": hashlib.sha256(fixture_png(mask=True)).hexdigest()}}


def validate_package(package: dict) -> None:
    if package.get("package_kind") == MATRIX_PACKAGE_KIND:
        expected = build_matrix_package(package.get("models", [None])[0], package.get("case_selectors"),
                                        package.get("reference_candidate"),
                                        max_generation_requests=package.get("generation_request_budget"))
        if package != expected:
            raise ValueError("frozen image reference package differs from its exact candidate factory")
        return
    if package != build_package(package.get("models"), package.get("case_selectors")):
        raise ValueError("frozen package no longer matches the executable source matrix")
    for case in package["cases"]:
        validate_case(case)


def build_matrix_package(model: str, names: list[str] | None, reference_candidate: dict,
                         *, max_generation_requests: int | None = None) -> dict:
    """Freeze the new matrix separately; schema 1 and its case digests stay intact."""
    from lib.gpt_image_resolution_quality import gpt_image_resolution_quality_responses_cases
    from lib.reference_specs import load_model_capability_profile

    route = reference_candidate.get("route_profile") if isinstance(reference_candidate, dict) else "openai_official"
    capability = load_model_capability_profile("image", "gpt-image-2", model,
                                               route_profile=route, api_form="openai_responses")
    expected = build_image_matrix_candidate(model=model, api_form="openai_responses", capability=capability)
    admitted = copy.deepcopy(reference_candidate) if isinstance(reference_candidate, dict) else {}
    manifest_hash = admitted.pop("sha256", None)
    if admitted != expected or not isinstance(manifest_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash):
        raise ValueError("Responses matrix package requires an admitted exact official candidate")
    rows = gpt_image_resolution_quality_responses_cases(model)
    selectors = names or []
    known = {case["name"] for case in rows} | {case["case_id"] for case in rows}
    if set(selectors) - known:
        raise ValueError("unknown image reference case selector")
    rows = [case for case in rows if not selectors or case["name"] in selectors or case["case_id"] in selectors]
    if not rows:
        raise ValueError("empty image reference case selection")
    budget = len(rows) if max_generation_requests is None else max_generation_requests
    if type(budget) is not int or budget <= 0:
        raise ValueError("image reference generation budget must be positive")
    return {"schema_version": 2, "package_kind": MATRIX_PACKAGE_KIND, "source_id": "openai",
            "api_form": "openai_responses", "mainline_model": MAINLINE_MODEL, "models": [model],
            "case_selectors": selectors, "cases": rows, "reference_candidate": copy.deepcopy(reference_candidate),
            "case_definitions_sha256": canonical_digest(rows), "request_cap": min(budget, len(rows)),
            "generation_request_budget": budget, "request_cap_includes": "one generation per selected case; no retries or file operations",
            "execution_mode": "reference_observation", "certified": False,
            "official_references": list(dict.fromkeys(url for case in rows for url in case["official_references"])),
            "credential_source": "project OPENAI_API_KEY, official api.openai.com only"}


def _validate_package_case(package: dict, case: dict) -> None:
    if package.get("package_kind") != MATRIX_PACKAGE_KIND:
        validate_case(case)
        return
    from lib.gpt_image_resolution_quality import gpt_image_resolution_quality_responses_cases

    expected = next((row for row in gpt_image_resolution_quality_responses_cases(package["models"][0])
                     if row["case_id"] == case["case_id"]), None)
    if expected != case:
        raise ValueError("image reference case changed before the outbound boundary")


def _evaluate_package_case(package: dict, case: dict, status, payload, artifacts,
                           *, expectation_policy=CURRENT_EXPECTATION_POLICY, **kwargs) -> dict:
    if package.get("package_kind") != MATRIX_PACKAGE_KIND:
        return evaluate(case, status, payload, artifacts, expectation_policy=expectation_policy, **kwargs)
    from lib.image_validation import ImageTestCase

    attribution = rejection_attribution(
        ImageTestCase(name=case["name"], parameters=case["parameters"], metadata=case.get("metadata", {})),
        payload.get("error") if isinstance(payload, dict) else None,
    )
    checked_case = {**case, "expected_outcome": "success", "expected_count": 1}
    verdict = evaluate(checked_case, status, payload, artifacts,
                       expectation_policy=CURRENT_EXPECTATION_POLICY, **kwargs)
    if case["parameters"].get("size") == "auto":
        verdict.update(geometry_check=auto_output_geometry_audit(verdict.get("artifacts", [])),
                       auto_output_geometry_certified=False,
                       auto_output_size_validation="completed_decoded_image",
                       auto_output_dimension_scope="observed_pixels_without_requested_custom_size_guarantee")
    accounting_errors = {"separate_image_tool_usage_invalid", "aggregate_image_tool_usage_invalid",
                         "aggregate_and_individual_image_usage_disagree"}
    parameter_errors = [error for error in verdict.get("image_metadata_errors", []) if error not in accounting_errors]
    accepted = (verdict.get("envelope_pass") is True and verdict.get("mainline_identity_pass") is True
                and verdict.get("image_count") == 1 and len(verdict.get("artifacts", [])) == 1
                and all(item.get("pass") is True for item in verdict.get("artifacts", [])) and not parameter_errors)
    if case["parameters"].get("size") == "auto":
        accepted = accepted and verdict["geometry_check"]["pass"]
    rejected = status in {400, 422} and attribution["matched"] is True
    diagnostic = accepted or rejected
    verdict.update({"pass": False, "overall_pass": False, "compatibility_pass": False,
                    "certified_route_contract_pass": False, "expected_outcome": case["expected_outcome"],
                    "diagnostic_pass": diagnostic, "parameter_rejection_attribution": attribution,
                    "usage_validation_pass": verdict.get("usage", {}).get("pass"),
                    "quantity_audit_pass": verdict.get("image_output_token_accuracy_pass"),
                    "diagnostic_scope": "request_parameter_acceptance_and_decoded_output_constraints",
                    "assessment": "observed_acceptance" if accepted else "observed_parameter_rejection" if rejected else "unresolved",
                    "documented_outcome_match": accepted if case["expected_outcome"] == "success" else rejected if case["expected_outcome"] == "rejection" else None})
    return verdict


def write_json(batch: Path, name: str, value: object) -> Path:
    path = batch / name
    if path.parent != batch or path.is_symlink():
        raise ValueError("report name must be a direct file in its batch")
    with path.open("x", encoding="utf-8") as stream:
        json.dump(redact_secrets(value), stream, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    path.chmod(0o600)
    register_report_files(batch, [path.name], root=REPORT_ROOT)
    return path


def prepare(models: list[str] | None = None, names: list[str] | None = None, *, package: dict | None = None) -> Path:
    package = build_package(models, names) if package is None else package
    validate_package(package)
    batch = REPORT_ROOT / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8])
    initialize_report_retention(batch, root=REPORT_ROOT)
    write_json(batch, "package.json", package)
    write_json(batch, "package_digest.json", {"sha256": digest(package)})
    return batch


def require_batch(batch: Path) -> Path:
    batch = batch.absolute()
    if batch.parent != REPORT_ROOT or batch.is_symlink() or batch.resolve() != batch:
        raise ValueError("batch must be a real direct child of the dedicated report root")
    initialize_report_retention(batch, root=REPORT_ROOT)
    return batch


def read_package(batch: Path) -> dict:
    package = json.loads((batch / "package.json").read_text())
    if digest(package) != json.loads((batch / "package_digest.json").read_text())["sha256"]:
        raise ValueError("package digest changed")
    validate_package(package)
    return package


def read_json_response(response: requests.Response) -> dict:
    content = bytearray()
    for chunk in response.iter_content(chunk_size=65536):
        content.extend(chunk)
        if len(content) > MAX_RESPONSE_BYTES:
            raise ValueError("response exceeds bounded byte limit")
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return {"parse_error": "non_json_response"}
    return payload if isinstance(payload, dict) else {"parse_error": "non_object_response"}


def read_sse(response: requests.Response) -> list[dict]:
    events, data_lines = [], []
    size = 0
    # Streaming from the socket bounds memory before a server-controlled line is
    # buffered, and handles event separators split across HTTP chunks.
    buffer = b""
    for chunk in response.iter_content(chunk_size=65536):
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ValueError("stream exceeds bounded byte limit")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip())
            elif not line and data_lines:
                raw = b"\n".join(data_lines)
                data_lines = []
                if raw == b"[DONE]":
                    continue
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError("SSE data must be a JSON object")
                events.append(event)
                if len(events) > 10000:
                    raise ValueError("stream exceeds event limit")
    if buffer.strip() or data_lines:
        raise ValueError("truncated SSE event framing")
    return events


def upload_fixture(session, credential, batch: Path, ordinal: int, label: str, *, timeout=REQUEST_TIMEOUT) -> str:
    headers = credential.auth_headers(url=FILES_ENDPOINT, auth_mode="bearer")
    headers.pop("content-type", None)
    write_json(batch, f"wire_{ordinal:03d}_upload_{label}.json", {"created_at": now(), "endpoint": FILES_ENDPOINT,
               "method": "POST", "purpose": "vision", "fixture_sha256": hashlib.sha256(fixture_png(mask=label == "mask")).hexdigest()})
    with session.post(FILES_ENDPOINT, headers=headers, data={"purpose": "vision"},
                      files={"file": (f"public_fixture_{label}.png", fixture_png(mask=label == "mask"), "image/png")},
                      timeout=timeout, allow_redirects=False, stream=True) as response:
        payload = read_json_response(response)
        write_json(batch, f"upload_{ordinal:03d}_{label}.json", {"status_code": response.status_code,
                   "response": credential.redact(payload), "request_id": response.headers.get("x-request-id")})
        if response.status_code != 200:
            raise ValueError("official fixture upload failed; inspect upload record")
        return require_id(payload.get("id"), "file")


def cleanup_file(session, credential, batch: Path, ordinal: int, label: str, file_id: str, *, timeout=REQUEST_TIMEOUT) -> dict:
    endpoint = FILES_ENDPOINT + "/" + require_id(file_id, "file")
    write_json(batch, f"wire_{ordinal:03d}_delete_{label}.json", {"created_at": now(), "endpoint": endpoint, "method": "DELETE"})
    try:
        with session.delete(endpoint, headers=credential.auth_headers(url=endpoint, auth_mode="bearer"),
                            timeout=timeout, allow_redirects=False, stream=True) as response:
            payload = read_json_response(response)
            result = {"status_code": response.status_code, "file_id": file_id,
                       "request_id": response.headers.get("x-request-id"),
                       "response": credential.redact(payload), "cleanup_pass": response.status_code == 200
                       and payload.get("deleted") is True and payload.get("id") == file_id}
    except (requests.RequestException, ValueError) as exc:
        result = {"file_id": file_id, "cleanup_pass": False, "error_type": type(exc).__name__}
    write_json(batch, f"delete_{ordinal:03d}_{label}.json", result)
    return result


def _registered_bytes(batch: Path, path: Path, owned: dict) -> bytes:
    if (path.is_symlink() or path.resolve() != path or not path.is_file()
            or path.stat().st_size > MAX_RESPONSE_BYTES or not path.is_relative_to(batch)):
        raise ValueError("unsafe registered source evidence path")
    raw = path.read_bytes()
    entry = owned.get(str(path.relative_to(batch)))
    if not isinstance(entry, dict) or entry.get("sha256") != hashlib.sha256(raw).hexdigest() or entry.get("size") != len(raw):
        raise ValueError("source evidence differs from its original registered hash")
    return raw


def _cleanup_evidence(case: dict, ordinal: int, batch: Path, owned: dict) -> dict:
    records = []
    if case.get("input_kind") == "file_id":
        labels = ["image"] + (["mask"] if case["parameters"].get("input_image_mask", {}).get("file_id") else [])
        for label in labels:
            upload_path = batch / f"upload_{ordinal:03d}_{label}.json"
            if not upload_path.exists():
                continue
            upload = json.loads(_registered_bytes(batch, upload_path, owned))
            if upload.get("status_code") != 200:
                continue
            file_id = require_id(upload.get("response", {}).get("id"), "file")
            delete_path = batch / f"delete_{ordinal:03d}_{label}.json"
            deletion = json.loads(_registered_bytes(batch, delete_path, owned)) if delete_path.exists() else {}
            if deletion:
                delete_wire = json.loads(_registered_bytes(batch, batch / f"wire_{ordinal:03d}_delete_{label}.json", owned))
                if delete_wire.get("method") != "DELETE" or delete_wire.get("endpoint") != FILES_ENDPOINT + "/" + file_id:
                    raise ValueError("cleanup response is not bound to the uploaded file deletion")
            ok = (deletion.get("cleanup_pass") is True and deletion.get("status_code") == 200
                  and deletion.get("response", {}).get("id") == file_id and deletion.get("response", {}).get("deleted") is True)
            records.append({"label": label, "file_id": file_id, "cleanup_pass": ok,
                            "upload_record": str(upload_path), "delete_record": str(delete_path) if deletion else None,
                            "status_code": deletion.get("status_code"), "request_id": deletion.get("request_id"),
                            "error_type": deletion.get("error_type")})
    return {"required": bool(records), "pass": all(row["cleanup_pass"] for row in records), "files": records}


def _read_registered_sources(package: dict, batch: Path, *, expectation_policy: str | None = None) -> tuple[dict, dict]:
    """Read immutable exchanges before trusting a verdict or stateful ID.

    Resume may only reuse the policy recorded with each result. Offline replay
    is the explicit route for applying a different policy to historical data.
    """
    manifest = json.loads((batch / ".report-retention.json").read_text())
    owned = {item["path"]: item for item in manifest["owned_files"]}
    saved_package = json.loads(_registered_bytes(batch, batch / "package.json", owned))
    saved_digest = json.loads(_registered_bytes(batch, batch / "package_digest.json", owned))
    if saved_package != package or saved_digest.get("sha256") != digest(package):
        raise ValueError("registered source package differs from the selected frozen package")
    if (batch / "shared_workflow_result.json").exists():
        return _read_shared_sources(package, batch, owned, expectation_policy=expectation_policy)
    sources, source_paths = {}, {}
    for ordinal, case in enumerate(package["cases"], 1):
        path = batch / f"case_{ordinal:03d}.json"
        if not path.exists():
            continue
        raw = _registered_bytes(batch, path, owned)
        row = json.loads(raw)
        if (row.get("package_sha256") != digest(package) or row.get("case_id") != case["case_id"]
                or row.get("endpoint") != ENDPOINT or row.get("tool_model") != case["tool_model"]
                or row.get("mainline_model") != MAINLINE_MODEL):
            raise ValueError("saved exchange does not match the frozen case identity")
        actual_policy = row.get("verdict", {}).get("expectation_policy", FROZEN_EXPECTATION_POLICY)
        if expectation_policy is not None and actual_policy != expectation_policy:
            raise ValueError("existing result uses a different expectation policy; use explicit offline --replay")
        sources[case["case_id"]] = row
        source_paths[case["case_id"]] = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
    for ordinal, case in enumerate(package["cases"], 1):
        row = sources.get(case["case_id"])
        if row is None:
            continue
        request = row.get("request")
        if request is not None:
            wire = json.loads(_registered_bytes(batch, batch / f"wire_{ordinal:03d}.json", owned))
            if (wire.get("case_id") != case["case_id"] or wire.get("endpoint") != ENDPOINT
                    or wire.get("request") != request or wire.get("request_sha256") != row.get("request_sha256")):
                raise ValueError("registered wire and case requests disagree")
            files = {}
            if case["input_kind"] == "file_id":
                files["image"] = require_id(request["input"][0]["content"][1].get("file_id"), "file")
                if case["parameters"].get("input_image_mask", {}).get("file_id"):
                    files["mask"] = require_id(request["tools"][0]["input_image_mask"].get("file_id"), "file")
                for label, file_id in files.items():
                    uploaded = json.loads(_registered_bytes(batch, batch / f"upload_{ordinal:03d}_{label}.json", owned))
                    if uploaded.get("status_code") != 200 or uploaded.get("response", {}).get("id") != file_id:
                        raise ValueError("file input does not match its registered upload")
                    upload_wire = json.loads(_registered_bytes(batch, batch / f"wire_{ordinal:03d}_upload_{label}.json", owned))
                    if (upload_wire.get("endpoint") != FILES_ENDPOINT or upload_wire.get("method") != "POST"
                            or upload_wire.get("purpose") != "vision"
                            or upload_wire.get("fixture_sha256") != hashlib.sha256(fixture_png(mask=label == "mask")).hexdigest()):
                        raise ValueError("file upload differs from the public frozen input fixture")
            rebuilt = build_request(case, dependency=sources.get(case.get("depends_on")), file_ids=files)
            if public_payload(rebuilt) != request or digest(rebuilt) != row.get("request_sha256"):
                raise ValueError("saved outbound request differs from frozen executable fixture")
        elif row.get("status_code") is not None:
            raise ValueError("saved HTTP response has no matching outbound request")
        response_output = row.get("response", {}).get("output")
        calls = [item for item in (response_output if isinstance(response_output, list) else [])
                 if isinstance(item, dict) and item.get("type") == "image_generation_call"]
        for index, artifact in enumerate(row.get("verdict", {}).get("artifacts", [])):
            if not artifact.get("path"):
                if artifact.get("decoded") is True:
                    raise ValueError("decoded image has no registered artifact")
                continue
            expected = batch / f"artifacts_{ordinal:03d}" / (f"image_{index:02d}." + case["parameters"]["output_format"])
            if artifact["path"] != str(expected) or index >= len(calls):
                raise ValueError("image artifact is not bound to its exact output call")
            raw_image = _registered_bytes(batch, expected, owned)
            encoded = base64.b64encode(raw_image)
            if (hashlib.sha256(raw_image).hexdigest() != artifact.get("sha256") or len(raw_image) != artifact.get("byte_length")
                    or hashlib.sha256(encoded).hexdigest() != calls[index].get("result_sha256")
                    or len(encoded) != calls[index].get("result_encoded_length")):
                raise ValueError("saved image differs from original decoded response evidence")
        partial_events = [event for event in row.get("stream_events", []) if event.get("type") == "response.image_generation_call.partial_image"]
        for artifact in row.get("verdict", {}).get("stream", {}).get("partial_images", []):
            path = Path(artifact.get("path", ""))
            match = re.fullmatch(r"partial_(\d+)\.png", path.name)
            if path.parent != batch / f"artifacts_{ordinal:03d}" or not match or int(match[1]) >= len(partial_events):
                raise ValueError("partial image is not bound to its source stream")
            raw_image = _registered_bytes(batch, path, owned)
            if (hashlib.sha256(raw_image).hexdigest() != artifact.get("sha256")
                    or hashlib.sha256(base64.b64encode(raw_image)).hexdigest() != partial_events[int(match[1])].get("partial_image_b64_sha256")):
                raise ValueError("partial image differs from original stream evidence")
        if case.get("input_kind") == "file_id":
            row["verdict"]["file_cleanup"] = _cleanup_evidence(case, ordinal, batch, owned)
    return sources, source_paths


def _read_shared_sources(package: dict, batch: Path, owned: dict, *, expectation_policy=None) -> tuple[dict, dict]:
    """Bind shared-run projections to their immutable plans, ledgers and media.

    Replay deliberately does not recompile against today's handlers: the saved
    package, request reconstruction and registered hashes define its source.
    Semantic review binds the complete result file plus the exact case ID.
    """
    from lib.test_runner.common import digest_json
    from lib.test_runner.transport import canonical_bytes
    from lib.test_runner.adapters.image import _Response

    result_path = batch / "shared_workflow_result.json"
    raw = _registered_bytes(batch, result_path, owned)
    result = json.loads(raw)
    execution = json.loads(_registered_bytes(batch, batch / "shared_execution_plans.json", owned))
    frozen = json.loads(_registered_bytes(batch, batch / "shared_workflow_plan.json", owned))
    if (result.get("execution_engine") != "shared_workflow_v1" or result.get("cleanup_only") is not False
            or execution.get("package_sha256") != digest(package) or frozen.get("package_sha256") != digest(package)):
        raise ValueError("shared source evidence does not match the frozen package")
    plans = {plan["plan_digest"]: plan for plan in execution["plans"]}
    frozen_definitions = [plan["definition"] for plan in frozen["plans"]]
    if any(plan["definition"] not in frozen_definitions for plan in plans.values()):
        raise ValueError("shared execution definition differs from the frozen plan")
    cases = {case["case_id"]: case for case in package["cases"]}
    sources, source_paths, ledgers = {}, {}, {}
    records = []
    for report in result["reports"]:
        plan = plans.get(report.get("plan_digest"))
        if plan is None or not set(plan["selected_cases"]) <= set(cases):
            raise ValueError("shared report differs from its registered execution plan")
        for run in report["runs"]:
            run_id = run["run_id"]
            if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", run_id):
                raise ValueError("shared source run ID is unsafe")
            model = plan["target"]["execution_target"]["request_model_id"]
            ledger_path = batch / "shared_workflow" / hashlib.sha256(model.encode()).hexdigest()[:16] / run_id / "ledger.json"
            if run.get("ledger_path") != str(ledger_path):
                raise ValueError("shared source ledger is outside its original run")
            ledger = json.loads(_registered_bytes(batch, ledger_path, owned))
            checksum = ledger.pop("ledger_digest", None)
            if (checksum != digest_json(ledger) or ledger.get("plan_digest") != plan["plan_digest"]
                    or ledger.get("run_id") != run_id or ledger.get("target") != plan["target"]
                    or ledger.get("steps") != run.get("steps") or ledger.get("attempts") != run.get("attempts")):
                raise ValueError("shared report differs from its original registered ledger")
            for definition in plan["ordered_steps"]:
                step = run["steps"].get(definition["id"], {})
                if "record" not in step:
                    continue
                row = step["record"]
                case_id = row.get("case_id")
                if case_id not in plan["selected_cases"] or case_id in sources:
                    raise ValueError("shared source contains an unexpected or duplicate case")
                case = cases[case_id]
                if (row.get("endpoint") != ENDPOINT or row.get("tool_model") != case["tool_model"]
                        or row.get("mainline_model") != MAINLINE_MODEL):
                    raise ValueError("shared exchange does not match the frozen case identity")
                if expectation_policy is not None and row.get("verdict", {}).get("expectation_policy") != expectation_policy:
                    raise ValueError("existing result uses a different expectation policy; use explicit offline --replay")
                sources[case_id] = row
                records.append(row)
                ledgers[case_id] = ledger
                source_paths[case_id] = {"path": str(result_path), "sha256": hashlib.sha256(raw).hexdigest(),
                                         "run_id": run_id,
                                         "workflow_cleanup_pass": all(item["status"] == "deleted" for item in ledger["resources"])
                                            and all(item["status"] not in {"pending", "unknown"} for item in ledger["creations"]),
                                         "artifact_directory": str(batch / "shared" / run_id / hashlib.sha256(case_id.encode()).hexdigest()[:16])}
    if records != result.get("records"):
        raise ValueError("shared result records differ from their original run steps")
    for case_id, row in sources.items():
        case, ledger = cases[case_id], ledgers[case_id]
        attempts = [item for item in ledger["attempts"] if item["step_id"] == case_id
                    and item["phase"] == "business" and item["request"].get("path") == "/v1/responses"]
        if len(attempts) != 1:
            raise ValueError("shared image case lacks one original response request")
        attempt = attempts[0]
        request, receipt = attempt["request"], attempt.get("receipt", {})
        source_paths[case_id]["response_complete"] = receipt.get("response_complete") is True
        body = request.get("body")
        files = {}
        if case["input_kind"] == "file_id":
            files["image"] = require_id(body["input"][0]["content"][1].get("file_id"), "file")
            if case["parameters"].get("input_image_mask", {}).get("file_id"):
                files["mask"] = require_id(body["tools"][0]["input_image_mask"].get("file_id"), "file")
            for label, file_id in files.items():
                resource = next((item for item in ledger["resources"] if item["resource_id"] == file_id), None)
                upload = next((item for item in ledger["attempts"] if resource and item["id"] == resource["creation_attempt_id"]), {})
                upload_request, upload_receipt = upload.get("request", {}), upload.get("receipt", {})
                wire = base64.b64decode(upload_request.get("wire_base64", ""), validate=True)
                if (upload_request.get("method") != "POST" or upload_request.get("path") != "/v1/files"
                        or upload_receipt.get("http_status") != 200 or upload_receipt.get("response", {}).get("id") != file_id
                        or fixture_png(mask=label == "mask") not in wire):
                    raise ValueError("shared file input does not match its registered fixture upload")
        rebuilt = build_request(case, dependency=sources.get(case.get("depends_on")), file_ids=files)
        if (request.get("method") != "POST" or body != rebuilt or public_payload(body) != row.get("request")
                or digest(rebuilt) != row.get("request_sha256")
                or receipt.get("http_status") != row.get("status_code")
                or (receipt.get("request_bytes_sha256") is not None
                    and receipt["request_bytes_sha256"] != hashlib.sha256(canonical_bytes(body)).hexdigest())):
            raise ValueError("shared outbound request differs from its frozen fixture or receipt")
        payload, events = row.get("response", {}), row.get("stream_events", [])
        if "transport_or_setup_error_type" not in row.get("response", {}):
            if case["stream"] and receipt.get("http_status") == 200:
                events = read_sse(_Response(receipt))
                completed = [event["response"] for event in events if event.get("type") == "response.completed"]
                if public_payload(events) != row.get("stream_events"):
                    raise ValueError("shared stream differs from its original receipt")
                payload = completed[0] if len(completed) == 1 else {}
            else:
                payload = receipt.get("response") or {}
            if public_payload(payload) != row.get("response"):
                raise ValueError("shared response differs from its original receipt")
        directory = Path(source_paths[case_id]["artifact_directory"])
        output = row.get("response", {}).get("output")
        calls = [item for item in (output if isinstance(output, list) else [])
                 if isinstance(item, dict) and item.get("type") == "image_generation_call"]
        artifacts = row.get("verdict", {}).get("artifacts", [])
        if len(calls) != len(artifacts):
            raise ValueError("shared image calls lack one-to-one artifact evidence")
        media = [(artifact, calls[index], "result", directory / (f"image_{index:02d}." + case["parameters"]["output_format"]))
                 for index, artifact in enumerate(artifacts)]
        partials = [event for event in row.get("stream_events", []) if event.get("type") == "response.image_generation_call.partial_image"]
        partial_artifacts = row.get("verdict", {}).get("stream", {}).get("partial_images", [])
        for artifact in partial_artifacts:
            match = re.fullmatch(r"partial_(\d+)\.png", Path(artifact.get("path", "")).name)
            if not match or int(match[1]) >= len(partials):
                raise ValueError("shared partial image is not bound to its source event")
            index = int(match[1])
            media.append((artifact, partials[index], "partial_image_b64", directory / f"partial_{index:02d}.png"))
        for artifact, item, key, path in media:
            if not artifact.get("path") and artifact.get("decoded") is False:
                continue  # Failed decoding is replayed from the registered raw receipt.
            if artifact.get("path") != str(path):
                raise ValueError("shared image is outside its exact source case")
            image_bytes = _registered_bytes(batch, path, owned)
            encoded = base64.b64encode(image_bytes)
            if (hashlib.sha256(image_bytes).hexdigest() != artifact.get("sha256")
                    or len(image_bytes) != artifact.get("byte_length")
                    or hashlib.sha256(encoded).hexdigest() != item.get(key + "_sha256")
                    or len(encoded) != item.get(key + "_encoded_length")):
                raise ValueError("shared image differs from original response evidence")
        row["_replay_response"] = copy.deepcopy(payload)
        row["_replay_stream_events"] = copy.deepcopy(events)
    return sources, source_paths


def summarize(package: dict, rows: list[dict], *, expectation_policy=FROZEN_EXPECTATION_POLICY) -> dict:
    by_id = {row["case_id"]: row for row in rows}
    observations = [row["case_id"] for row in rows if row["verdict"].get("expected_outcome") == "observation"]
    required = [case for case in package["cases"]
                if (case if package.get("package_kind") == MATRIX_PACKAGE_KIND else effective_case_expectations(
                    case, expectation_policy=expectation_policy))["expected_outcome"] != "observation"]
    image_rows = [row for row in rows if row.get("status_code") == 200 and row["verdict"].get("image_count", 0) > 0]
    quantity_flags = [row["verdict"].get("image_output_token_accuracy_pass") for row in image_rows]
    quantity_pass = all(quantity_flags) if quantity_flags and all(flag is not None for flag in quantity_flags) else None
    result = {"created_at": now(), "expectation_policy": expectation_policy,
            "planned_cases": len(package["cases"]), "recorded_cases": len(rows),
            "passed_cases": sum(row["verdict"].get("pass") is True for row in rows), "observation_cases": observations,
            "all_cases_recorded": len(by_id) == len(package["cases"]),
            "all_required_cases_pass": all(by_id.get(case["case_id"], {}).get("verdict", {}).get("pass") is True for case in required),
            "missing_cases": [case["case_id"] for case in package["cases"] if case["case_id"] not in by_id],
            "image_tool_identity_scope": "official endpoint plus exact requested tools.model; returned tool model checked when present",
            "image_tool_token_accuracy": "output_verified_input_unverified" if quantity_pass is True else
                "image_output_token_mismatch" if quantity_pass is False else "unverified_without_separate_image_tool_usage",
            "image_output_token_accuracy_pass": quantity_pass,
            "image_output_token_verified_cases": sum(flag is True for flag in quantity_flags),
            "semantic_edit_quality": "requires_visual_comparison",
            "cases": [{"case_id": row["case_id"], "status_code": row["status_code"], "verdict": row["verdict"]} for row in rows]}
    if package.get("package_kind") != MATRIX_PACKAGE_KIND:
        result.update(aggregate_result_gates(package, rows))
    else:
        completed = sum(row["verdict"].get("diagnostic_pass") is True for row in rows)
        result.update({"pass": False, "overall_pass": False, "compatibility_pass": False,
                       "all_required_cases_pass": False, "certified": False, "certified_route_contract_pass": False,
                       "passed_cases": 0, "execution_mode": "reference_observation",
                       "diagnostic_pass": len(by_id) == len(package["cases"]) and completed == len(package["cases"]),
                       "completed_observation_count": completed, "unresolved_count": len(rows) - completed,
                       "generation_requests": len(rows), "max_generation_requests": package["generation_request_budget"],
                       "budget_exhausted": len(rows) >= package["generation_request_budget"] and len(rows) < len(package["cases"])})
    return result


def replay(batch: Path, *, names: list[str] | None = None,
           expectation_policy=CURRENT_EXPECTATION_POLICY, semantic_review: Path | None = None) -> dict:
    """Derive fresh verdicts from hash-checked saved exchanges, without network.

    The original case files and image bytes are never modified. Public response
    image hashes must match the exact saved image bytes before reconstructing
    the payload for the current validator. Replay outputs get new unique paths.
    """
    batch = require_batch(batch)
    package = read_package(batch)
    by_id, source_paths = _read_registered_sources(package, batch)
    selectors = names or []
    known = {case["name"] for case in package["cases"]} | {case["case_id"] for case in package["cases"]}
    if set(selectors) - known:
        raise ValueError("replay selector is outside the frozen package")
    replay_id = "replay_" + uuid.uuid4().hex[:12]
    rows = []

    def restore_image(item: dict, key: str, evidence: dict, expected_path: Path) -> str:
        if (evidence.get("path") != str(expected_path) or expected_path.is_symlink()
                or expected_path.resolve() != expected_path or not expected_path.is_file()
                or expected_path.stat().st_size > MAX_RESPONSE_BYTES):
            raise ValueError("replay artifact path is not the exact source case artifact")
        raw = expected_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != evidence.get("sha256") or len(raw) != evidence.get("byte_length"):
            raise ValueError("saved image bytes changed since source record")
        encoded = base64.b64encode(raw).decode("ascii")
        public = public_payload(item)
        if (hashlib.sha256(encoded.encode()).hexdigest() != public.get(key + "_sha256")
                or len(encoded) != public.get(key + "_encoded_length")):
            raise ValueError("saved image bytes do not match original response image digest")
        item[key] = encoded
        return encoded

    for ordinal, case in enumerate(package["cases"], 1):
        source = by_id.get(case["case_id"])
        if source is None or selectors and case["name"] not in selectors and case["case_id"] not in selectors:
            continue
        request = source.get("request")
        if request is not None:
            files = {}
            if case["input_kind"] == "file_id":
                files["image"] = require_id(request["input"][0]["content"][1].get("file_id"), "file")
                if case["parameters"].get("input_image_mask", {}).get("file_id"):
                    files["mask"] = require_id(request["tools"][0]["input_image_mask"].get("file_id"), "file")
            rebuilt = build_request(case, dependency=by_id.get(case.get("depends_on")), file_ids=files)
            if public_payload(rebuilt) != request or digest(rebuilt) != source.get("request_sha256"):
                raise ValueError("saved outbound request differs from frozen executable fixture")
        elif source.get("status_code") is not None:
            raise ValueError("saved HTTP response has no matching outbound request")
        payload = copy.deepcopy(source.get("_replay_response", source.get("response", {})))
        output = payload.get("output")
        calls = [item for item in (output if isinstance(output, list) else [])
                 if isinstance(item, dict) and item.get("type") == "image_generation_call"]
        previous_artifacts = source.get("verdict", {}).get("artifacts", [])
        source_dir = Path(source_paths[case["case_id"]].get("artifact_directory", batch / f"artifacts_{ordinal:03d}"))
        if len(calls) != len(previous_artifacts):
            raise ValueError("saved image calls lack one-to-one artifact evidence")
        for index, item in enumerate(calls):
            if ("_replay_response" in source and not previous_artifacts[index].get("path")
                    and previous_artifacts[index].get("decoded") is False):
                continue
            restore_image(item, "result", previous_artifacts[index],
                          source_dir / (f"image_{index:02d}." + case["parameters"]["output_format"]))
        derived_dir = batch / replay_id / f"artifacts_{ordinal:03d}"
        events = copy.deepcopy(source.get("_replay_stream_events", source.get("stream_events", [])))
        stream_verdict = None
        if source.get("status_code") == 200 and case["stream"]:
            previous_partials = source.get("verdict", {}).get("stream", {}).get("partial_images", [])
            partials = [event for event in events if event.get("type") == "response.image_generation_call.partial_image"]
            if "_replay_stream_events" not in source and len(previous_partials) != len(partials):
                raise ValueError("saved stream lacks complete partial image evidence")
            for index, event in enumerate(partials):
                expected = source_dir / f"partial_{index:02d}.png"
                artifact = next((item for item in previous_partials if item.get("path") == str(expected)), None)
                if artifact is None and "_replay_stream_events" in source:
                    continue  # Preserve an original undecodable partial, without inventing media.
                restore_image(event, "partial_image_b64", artifact or {}, expected)
            for event in events:
                if event.get("type") == "response.completed":
                    if len([item for item in events if item.get("type") == "response.completed"]) != 1 and "_replay_stream_events" in source:
                        continue
                    if public_payload(event.get("response")) != source["response"]:
                        raise ValueError("saved stream terminal response differs from source response")
                    event["response"] = copy.deepcopy(payload)
            payload, stream_verdict = validate_stream(events, case, derived_dir,
                expectation_policy=expectation_policy if package.get("package_kind") != MATRIX_PACKAGE_KIND else FROZEN_EXPECTATION_POLICY)
        verdict = _evaluate_package_case(package, case, source.get("status_code"), payload, derived_dir,
                           partial_count=(stream_verdict or {}).get("partial_count", 0),
                           partial_counts_by_item_id=(stream_verdict or {}).get("partial_counts_by_item_id"),
                           expectation_policy=expectation_policy)
        if stream_verdict is not None:
            verdict["stream"] = stream_verdict
            verdict["pass"] = verdict["pass"] and stream_verdict["pass"]
        if "file_cleanup" in source.get("verdict", {}):
            verdict["file_cleanup"] = copy.deepcopy(source["verdict"]["file_cleanup"])
        if source_paths[case["case_id"]].get("response_complete") is False:
            verdict.update({"pass": False, "envelope_pass": False})
        rows.append({"case_id": case["case_id"], "status_code": source.get("status_code"), "verdict": verdict,
                     "source_record": source_paths[case["case_id"]], "source_request_id": source.get("request_id")})
    review_evidence = attach_semantic_review(semantic_review, rows, source_paths, by_id, batch, root=ROOT) if semantic_review else None
    result = {"replay_id": replay_id, "created_at": now(), "network_requests": 0, "package_sha256": digest(package),
              "expectation_policy": expectation_policy, "semantic_review": review_evidence,
              "source_batch": str(batch), "validator_sha256": hashlib.sha256((ROOT / "lib" / "gpt_image_25_responses.py").read_bytes()).hexdigest(),
              "original_records_modified": False, "records": rows,
              "summary": summarize(package, rows, expectation_policy=expectation_policy)}
    cleanup_sources = [value for value in source_paths.values() if "workflow_cleanup_pass" in value]
    if cleanup_sources:
        cleanup_pass = all(value["workflow_cleanup_pass"] for value in cleanup_sources)
        result["summary"].update(workflow_cleanup_pass=cleanup_pass,
            workflow_cleanup_failed_runs=sorted({value["run_id"] for value in cleanup_sources if not value["workflow_cleanup_pass"]}))
        if not cleanup_pass:
            result["summary"].update({"pass": False, "overall_pass": False})
    if (batch / replay_id).exists():
        register_report_files(batch, [path.relative_to(batch) for path in (batch / replay_id).rglob("*") if path.is_file()], root=REPORT_ROOT)
    path = write_json(batch, replay_id + ".json", result)
    return {**result, "replay_record": str(path)}


def execute(package: dict, batch: Path, *, names: list[str] | None = None, credential=None, on_row=None,
            timeout=REQUEST_TIMEOUT, expectation_policy=CURRENT_EXPECTATION_POLICY) -> dict:
    validate_package(package)
    effective_case_expectations(package["cases"][0], expectation_policy=expectation_policy)
    selectors = names or []
    known = {case["name"] for case in package["cases"]} | {case["case_id"] for case in package["cases"]}
    if set(selectors) - known:
        raise ValueError("execute case selector is outside the frozen package")
    selected = {case["case_id"] for case in package["cases"] if not selectors or case["name"] in selectors or case["case_id"] in selectors}
    selected.update(case["depends_on"] for case in package["cases"] if case["case_id"] in selected and case.get("depends_on"))
    sources, _ = _read_registered_sources(package, batch,
        expectation_policy=expectation_policy if package.get("package_kind") != MATRIX_PACKAGE_KIND else None)
    rows = list(sources.values())
    for ordinal, case in enumerate(package["cases"], 1):
        path = batch / f"case_{ordinal:03d}.json"
        if not path.exists() and case["case_id"] in selected and (batch / f"started_{ordinal:03d}.json").exists():
            raise ValueError("interrupted case has unresolved outbound state; inspect existing records before an explicit new batch")
    done = {row["case_id"] for row in rows}
    access_blocked_models = {row["case_id"].split("/responses/", 1)[0] for row in rows if row.get("status_code") == 403}
    pending = [case for case in package["cases"] if case["case_id"] in selected and case["case_id"] not in done
               and case["case_id"].split("/responses/", 1)[0] not in access_blocked_models]
    if not pending:
        return summarize(package, rows, expectation_policy=expectation_policy)
    credential = credential or designated_credential()
    with requests.Session() as session:
        session.trust_env = False
        for ordinal, case in enumerate(package["cases"], 1):
            if case["case_id"] in done or case["case_id"] not in selected:
                continue
            if case["case_id"].split("/responses/", 1)[0] in access_blocked_models:
                continue
            if package.get("package_kind") == MATRIX_PACKAGE_KIND and len(rows) >= package["generation_request_budget"]:
                break
            _validate_package_case(package, case)
            dependency = next((row for row in rows if row["case_id"] == case.get("depends_on")), None)
            if case.get("depends_on") and (not dependency or not dependency.get("verdict", {}).get("pass")):
                # No misleading failure row for a request that never happened.
                print(json.dumps({"case": case["case_id"], "state": "blocked_by_unverified_seed"}), flush=True)
                continue
            write_json(batch, f"started_{ordinal:03d}.json", {"created_at": now(), "case_id": case["case_id"], "package_sha256": digest(package)})
            files, events, stream_verdict, cleanup_records = {}, [], None, []
            started = time.monotonic()
            request_started_at = now()
            status, payload, request, request_id = None, {}, None, None
            artifacts = batch / f"artifacts_{ordinal:03d}"
            try:
                if case["input_kind"] == "file_id":
                    files["image"] = upload_fixture(session, credential, batch, ordinal, "image", timeout=timeout)
                    if case["parameters"].get("input_image_mask", {}).get("file_id"):
                        files["mask"] = upload_fixture(session, credential, batch, ordinal, "mask", timeout=timeout)
                request = build_request(case, dependency=dependency, file_ids=files)
                # Rebuild immediately at the outbound boundary; no mutable body
                # supplied by a report or runtime provider override is trusted.
                if request != build_request(case, dependency=dependency, file_ids=files):
                    raise ValueError("request changed before dispatch")
                write_json(batch, f"wire_{ordinal:03d}.json", {"created_at": now(), "endpoint": ENDPOINT,
                           "case_id": case["case_id"], "request_sha256": digest(request), "request": public_payload(request)})
                with session.post(ENDPOINT, json=request, headers=credential.auth_headers(url=ENDPOINT, auth_mode="bearer"),
                                  timeout=timeout, allow_redirects=False, stream=True) as response:
                    status, request_id = response.status_code, response.headers.get("x-request-id")
                    if status == 200 and case["stream"]:
                        if "text/event-stream" not in response.headers.get("content-type", ""):
                            raise ValueError("streaming request did not return text/event-stream")
                        events = read_sse(response)
                        payload, stream_verdict = validate_stream(events, case, artifacts,
                            expectation_policy=expectation_policy if package.get("package_kind") != MATRIX_PACKAGE_KIND else FROZEN_EXPECTATION_POLICY)
                    else:
                        payload = read_json_response(response)
            except (requests.RequestException, ValueError, OSError) as exc:
                payload = {"transport_or_setup_error_type": type(exc).__name__, "message": credential.redact(str(exc))[:300]}
            finally:
                for label, file_id in files.items():
                    cleanup_records.append(cleanup_file(session, credential, batch, ordinal, label, file_id, timeout=timeout))
            verdict = _evaluate_package_case(package, case, status, payload, artifacts, partial_count=(stream_verdict or {}).get("partial_count", 0),
                               partial_counts_by_item_id=(stream_verdict or {}).get("partial_counts_by_item_id"),
                               expectation_policy=expectation_policy)
            if stream_verdict is not None:
                verdict["stream"] = stream_verdict
                verdict["pass"] = verdict["pass"] and stream_verdict["pass"]
            if case.get("input_kind") == "file_id":
                verdict["file_cleanup"] = {"required": bool(files), "pass": all(row.get("cleanup_pass") is True for row in cleanup_records),
                                           "files": cleanup_records}
            row = {"case_id": case["case_id"], "package_sha256": digest(package), "endpoint": ENDPOINT,
                   "mainline_model": MAINLINE_MODEL, "tool_model": case["tool_model"], "status_code": status,
                   "request_id": request_id, "request_sha256": digest(request) if request else None,
                   "request": public_payload(request), "response": public_payload(payload),
                   "stream_events": public_payload(events), "elapsed_seconds": round(time.monotonic() - started, 3),
                   "verdict": verdict}
            if package.get("package_kind") == MATRIX_PACKAGE_KIND:
                row.update(image_exchange_evidence(endpoint=ENDPOINT, request_body=request or {}, response_payload=payload,
                                                   request_started_at=request_started_at))
            write_json(batch, f"case_{ordinal:03d}.json", credential.redact(row))
            if artifacts.exists():
                register_report_files(batch, [path.relative_to(batch) for path in artifacts.iterdir() if path.is_file()], root=REPORT_ROOT)
            rows.append(row)
            if on_row is not None:
                on_row(rows)
            print(json.dumps({"case": case["case_id"], "http": status, "pass": verdict["pass"], "assessment": verdict["assessment"]}), flush=True)
            if status == 403:
                access_blocked_models.add(case["case_id"].split("/responses/", 1)[0])
            if status is None or status in {401, 429}:
                break
            if package.get("package_kind") == MATRIX_PACKAGE_KIND and (
                (len(rows) == 1 and verdict.get("assessment") != "observed_acceptance")
                or status in {403, 404}
                or (len(rows) >= 3 and all(row["verdict"].get("diagnostic_pass") is not True for row in rows[-3:]))
            ):
                break
    result = summarize(package, rows, expectation_policy=expectation_policy)
    write_json(batch, "summary_" + uuid.uuid4().hex[:10] + ".json", result)
    return result


def main_image_cli(args, *, model: str, endpoint: str, capability: dict,
                   model_database_binding: dict | None = None, job_spec: dict | None = None,
                   config: dict | None = None, reference_candidate: dict | None = None) -> int:
    """Use the dedicated executor after the image CLI's MPDB route checks.

    This transport is source-scoped to OpenAI. Each selected case retains its
    authored parameters, rather than replacing xhigh/max controls with a CLI
    default. Exact token accuracy and qualitative editing remain separate gates.
    """
    from lib.model_profile_catalog import (
        binding_from_database_snapshot, database_snapshot, require_official_reference_binding, resolve_runtime_test_policy,
    )

    if endpoint != ENDPOINT or model not in (*MODELS, *SNAPSHOTS.values()):
        raise ValueError("Responses image reference requires an exact GPT Image 2.5 model on api.openai.com/v1/responses")
    if getattr(args, "auth_mode", None) not in {None, "bearer"}:
        raise ValueError("official Responses image authentication must be bearer")
    if getattr(args, "reference_candidate", None) and not reference_candidate:
        raise ValueError("Responses image runs require the registered MPDB binding")
    if reference_candidate and (job_spec or getattr(args, "transient_retries", 0)
                                or getattr(args, "retry_initial_delay", 15) != 15):
        raise ValueError("Responses reference candidates are CLI-only and do not support transient retry options")
    if getattr(args, "quality", "low") != "low" or getattr(args, "output_format", None) not in {None, "png"}:
        raise ValueError("Responses matrix parameters are pinned; select quality_max or a named format case with --case")
    binding = model_database_binding
    stored = (job_spec or {}).get("model_profile_database") or capability.get("model_profile_database")
    if stored:
        restored = binding_from_database_snapshot(stored)
        if binding and any(restored.get(key) != binding.get(key) for key in ("source_id", "profile_id", "interface_id", "runtime_model_id")):
            raise ValueError("immutable Responses image binding conflicts with runtime identity")
        binding = restored
    if not binding and reference_candidate:
        from lib.model_profile_catalog import resolve_runtime_profile_binding
        # Standalone candidates still resolve the registered official identity;
        # a manifest cannot manufacture a missing model/interface/policy.
        binding = resolve_runtime_profile_binding(
            {"providers": {"openai_official": {"name": "openai_official", "backend": "openai", "models": {}}}},
            "openai_official", model, "gpt-image-2", reference_candidate["route_profile"], "openai_responses", modality="image",
        )
    if not binding:
        raise ValueError("Responses image execution requires a complete MPDB binding or immutable snapshot")
    require_official_reference_binding(binding)
    resolve_runtime_test_policy(binding)
    interface = binding.get("interface", {})
    reference_model = binding.get("reference_model_id") or model
    alias = next((key for key, dated in SNAPSHOTS.items() if dated == model), model)
    if ((binding.get("reference_source_id") or binding.get("source_id")) != "openai"
            or interface.get("api_form") != "openai_responses"
            or binding.get("runtime_model_id") not in {None, model}
            or reference_model not in {alias, model}
            or model not in binding.get("profile", {}).get("request_model_ids", [])):
        raise ValueError("Responses image source/model/API binding mismatch")
    route_profile = binding.get("runtime_route_profile") or capability.get("route_profile") or getattr(args, "route_profile", None)
    if job_spec:
        for key, value in (("model", model), ("api_form", "openai_responses"), ("provider", getattr(args, "provider", None)),
                           ("route_profile", route_profile)):
            if job_spec.get(key) not in {None, "", value}:
                raise ValueError("immutable Responses image job conflicts with " + key)
    cases = select_image_matrix_cases(model, "openai_responses", suite=getattr(args, "suite", "smoke"),
                                     include_4k=getattr(args, "include_4k", False),
                                     include_negative=not getattr(args, "no_negative", False)) if reference_candidate else responses_image_cases(model, getattr(args, "suite", "smoke"),
                                  include_4k=getattr(args, "include_4k", False),
                                  include_negative=not getattr(args, "no_negative", False),
                                  expectation_policy=CURRENT_EXPECTATION_POLICY)
    selected = getattr(args, "case", []) or []
    if selected:
        if not reference_candidate:
            selected = [name if name.startswith("gpt_image_25_responses_") else "gpt_image_25_responses_" + name for name in selected]
        unknown = set(selected) - {case.name for case in cases}
        if unknown:
            raise ValueError("case not in selected Responses image suite: " + ", ".join(sorted(unknown)))
        if reference_candidate:
            cases = [case for case in cases if case.name in selected]
    if not reference_candidate:
        cases = select_responses_image_cases(model, cases, selected or None)
    raw_names = [case.name if reference_candidate else case.name.removeprefix("gpt_image_25_responses_") for case in cases]
    frozen_snapshot = stored or database_snapshot(binding)
    parameter_binding = frozen_snapshot.get("parameter_test_binding")
    frozen_definitions = parameter_binding.get("case_definitions") if isinstance(parameter_binding, dict) else None
    if not reference_candidate and frozen_definitions != [case.public() for case in responses_image_cases(reference_model)]:
        raise ValueError("immutable Responses image case definitions differ from the executable factory")
    package = build_matrix_package(model, raw_names, reference_candidate,
                                   max_generation_requests=getattr(args, "max_generation_requests", None)) if reference_candidate else build_package([model], raw_names)
    if job_spec and not reference_candidate:
        frozen_plan = job_spec.get("image_plan")
        expected_names = [case.name for case in cases]
        expected_profiles = [case.metadata["test_profile"] for case in cases]
        if (not isinstance(frozen_plan, dict)
                or frozen_plan.get("cases") != expected_names
                or frozen_plan.get("test_profiles") != expected_profiles):
            raise ValueError("immutable Responses image plan case selection differs from dependency-complete execution")
        for field, expected in (("estimated_case_count", len(cases)), ("request_cap", package["request_cap"])):
            if field in frozen_plan and (type(frozen_plan[field]) is not int or frozen_plan[field] != expected):
                raise ValueError("immutable Responses image plan " + field + " differs from execution")
    plan = {"source_id": "openai", "provider": getattr(args, "provider", None), "model": model,
            "mainline_model": MAINLINE_MODEL, "api_form": "openai_responses", "transport": "openai-responses-image",
            "endpoint": ENDPOINT, "family": "gpt-image-2", "route_profile": route_profile,
            "suite": getattr(args, "suite", "smoke"), "cases": [case.public() for case in cases],
            "test_profiles": [case.metadata["test_profile"] for case in cases],
            "model_profile_database": frozen_snapshot, "model_capability_profile": capability,
            "request_cap": package["request_cap"], "execution_mode": "reference_observation" if reference_candidate else "parameter_test",
            "expectation_policy": FROZEN_EXPECTATION_POLICY if reference_candidate else CURRENT_EXPECTATION_POLICY,
            "image_tool_token_accuracy": "unverified_without_separate_image_tool_usage"}
    if reference_candidate:
        plan.update({key: reference_candidate[key] for key in
                     ("model", "api_form", "source_id", "endpoint", "route_profile", "execution_mode", "profile_id", "interface_id")})
        plan.update(reference_candidate=reference_candidate, certified=False,
                    max_generation_requests=package["generation_request_budget"], transient_retries=0,
                    package_sha256=digest(package), package_kind=MATRIX_PACKAGE_KIND)
    if getattr(args, "dry_run", False):
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    credential_provider = getattr(args, "credential_provider", None)
    if credential_provider:
        if config is None:
            from lib.config import load_config
            config = load_config()
        credential = credential_from_config(config, credential_provider)
    elif getattr(args, "api_key_stdin", False):
        import getpass
        secret = getpass.getpass("OpenAI API key: ") if sys.stdin.isatty() else sys.stdin.readline().strip()
        credential = ProviderCredential.create(provider="openai_image_reference", secret=secret, base_urls=[ENDPOINT])
    elif os.environ.get(getattr(args, "api_key_env", "IMAGE_TEST_API_KEY")):
        credential = ProviderCredential.create(provider="openai_image_reference", secret=os.environ[getattr(args, "api_key_env", "IMAGE_TEST_API_KEY")], base_urls=[ENDPOINT])
    else:
        credential = designated_credential()
    # Validate the selected credential's origin before creating execution state.
    credential.auth_headers(url=ENDPOINT, auth_mode="bearer")
    batch = prepare(package=package) if reference_candidate else prepare([model], raw_names)
    output_dir = Path(getattr(args, "output_dir", None) or batch).absolute()
    if output_dir.resolve() != output_dir:
        raise ValueError("output directory must not traverse a symlink")
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("plan.json", "case_results.json", "summary.json"):
        if (output_dir / filename).exists():
            raise ValueError("output directory already contains image reports")
    if reference_candidate:
        persist_image_matrix_candidate(args.reference_candidate, batch / "reference_candidate.json", reference_candidate)
        register_report_files(batch, ["reference_candidate.json"], root=REPORT_ROOT)
        if output_dir != batch:
            persist_image_matrix_candidate(args.reference_candidate, output_dir / "reference_candidate.json", reference_candidate)
    plan["reference_batch"] = str(batch)

    def write_compat(name: str, value) -> None:
        destination = output_dir / name
        temporary = output_dir / ("." + name + "." + uuid.uuid4().hex)
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(credential.redact(value), stream, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        temporary.chmod(0o600)
        temporary.replace(destination)
        if output_dir == batch and name != "case_results.json":
            register_report_files(batch, [name], root=REPORT_ROOT)

    def compat_rows(rows: list[dict]) -> list[dict]:
        results = []
        if not reference_candidate:
            from lib.gpt_image_25_web_audit import build_case_audit, POLICY_NAME
            # Reuse the retained-evidence verifier: it compares every wire
            # request to the frozen factory, validates uploaded file IDs and
            # binds decoded image bytes to their original response hashes.
            verified_sources, _ = _read_registered_sources(
                package, batch, expectation_policy=CURRENT_EXPECTATION_POLICY,
            )
            if set(verified_sources) != {row["case_id"] for row in rows}:
                raise ValueError("Web image results differ from their registered exchanges")
            rows = [verified_sources[row["case_id"]] for row in rows]
        for row in rows:
            verdict = row["verdict"]
            rejection = verdict.get("expected_outcome") == "rejection"
            gates = case_result_gates(verdict)
            compatibility = verdict.get("pass") is True and not reference_candidate
            status = "expected_rejection" if gates["overall_pass"] and rejection else "pass" if gates["overall_pass"] else (
                "access_blocked" if verdict.get("assessment") == "account_or_model_access_unavailable" else "failed")
            ordinal = next(index for index, case in enumerate(package["cases"], 1) if case["case_id"] == row["case_id"])
            published_images, published_paths = [], []
            for index, artifact in enumerate(verdict.get("artifacts", [])):
                if artifact.get("decoded") is not True or not artifact.get("path"):
                    published_images.append(artifact)
                    continue
                source_path = Path(artifact["path"])
                source_dir = batch / f"artifacts_{ordinal:03d}"
                if (source_path.parent != source_dir or source_path.is_symlink() or source_path.resolve() != source_path
                        or not source_path.is_file() or source_path.stat().st_size > MAX_RESPONSE_BYTES):
                    raise ValueError("published image is outside its verified case artifact directory")
                image_bytes = source_path.read_bytes()
                if len(image_bytes) != artifact["byte_length"] or hashlib.sha256(image_bytes).hexdigest() != artifact["sha256"]:
                    raise ValueError("image artifact changed before publishing into image report")
                relative = f"images/{ordinal:03d}_{index:02d}.{artifact['format'].lower()}"
                destination = output_dir / relative
                destination.parent.mkdir(exist_ok=True)
                if destination.is_symlink() or destination.resolve() != destination:
                    raise ValueError("image report path must not traverse a symlink")
                if destination.exists():
                    if hashlib.sha256(destination.read_bytes()).hexdigest() != artifact["sha256"]:
                        raise ValueError("image report already contains different artifact bytes")
                else:
                    with destination.open("xb") as stream:
                        stream.write(image_bytes)
                    destination.chmod(0o600)
                    if output_dir == batch:
                        register_report_files(batch, [relative], root=REPORT_ROOT)
                published_paths.append(relative)
                published_images.append({**artifact, "path": relative})
            results.append({"case": "gpt_image_25_responses_" + row["case_id"].rsplit("/", 1)[-1], "case_id": row["case_id"],
                            "pass": compatibility, "compatibility_pass": compatibility,
                            "overall_pass": gates["overall_pass"] and not reference_candidate, "status": status,
                            "model": model, "mainline_model": MAINLINE_MODEL, "http_status": row["status_code"], "status_code": row["status_code"],
                            "token_validation_pass": gates["token_validation_pass"],
                            "token_accuracy_pass": verdict.get("image_output_token_accuracy_pass") if not rejection else None,
                            "model_identity_pass": gates["model_identity_pass"],
                            "model_identity_scope": gates["model_identity_scope"],
                            "overall_failures": gates["overall_failures"],
                            "validation_scope": gates["validation_scope"],
                            "token_validation_scope": gates["token_validation_scope"],
                            "semantic_validation": copy.deepcopy(verdict.get("semantic_validation")),
                            "file_cleanup": copy.deepcopy(verdict.get("file_cleanup")),
                            "image_tool_token_accuracy": verdict["image_tool_token_accuracy"],
                            "image_output_token_accuracy_pass": verdict.get("image_output_token_accuracy_pass"),
                            "completion_pass": verdict.get("envelope_pass"), "images": published_images, "artifacts": published_paths,
                            "verdict": verdict, "response_usage": row.get("response", {}).get("usage"),
                            "raw_record": str(batch), "request_id": row.get("request_id")})
            if not reference_candidate:
                case = package["cases"][ordinal - 1]
                # The four digests below have already been independently
                # checked by _read_registered_sources against the saved wire,
                # registered record, and reconstructed authored request.
                request_sha = row.get("request_sha256")
                integrity = {
                    "status": "pass" if row.get("request") is not None else "fail",
                    "expected_sha256": request_sha,
                    "actual_sha256": request_sha,
                    "wire_sha256": request_sha,
                    "public_request_sha256": digest(row.get("request")),
                    "verification_source": "registered_wire_and_frozen_request_reconstruction",
                }
                audit = build_case_audit(case, row, integrity)
                results[-1].update(
                    token_audit=audit,
                    token_validation_pass=audit["validation_pass"],
                    request_input_integrity=integrity,
                    expected_outcome=verdict.get("effective_expected_outcome", verdict.get("expected_outcome")),
                    validation_policy=POLICY_NAME,
                )
                if audit["validation_pass"] is not True:
                    results[-1]["overall_pass"] = False
                    results[-1]["status"] = "failed"
                    results[-1]["overall_failures"] = [
                        *results[-1]["overall_failures"], "image_web_audit_failed",
                    ]
            if reference_candidate:
                case = package["cases"][ordinal - 1]
                results[-1].update(case=case["name"], api_form="openai_responses", metadata=copy.deepcopy(case["metadata"]),
                                   status=verdict["assessment"], diagnostic_pass=verdict.get("diagnostic_pass") is True,
                                   certified_route_contract_pass=False, requested=build_request(case),
                                   effective_request_parameters=copy.deepcopy(case["parameters"]),
                                   request=copy.deepcopy(row.get("request")), actual_images=published_images,
                                   request_sha256=row.get("request_sha256"), response_sha256=row.get("response_sha256"),
                                   request_started_at=row.get("request_started_at"), response_evidence=row.get("response_evidence"),
                                   response=copy.deepcopy(row.get("response")),
                                   actual_request_endpoint=ENDPOINT)
        return results

    write_compat("plan.json", plan)
    requested_timeout = getattr(args, "timeout", 600)
    if type(requested_timeout) is not int or requested_timeout <= 0:
        raise ValueError("timeout must be a positive number of seconds")
    result = execute(package, batch, credential=credential, on_row=lambda rows: write_compat("case_results.json", compat_rows(rows)),
                     timeout=(min(20, requested_timeout), requested_timeout))
    records = [json.loads(path.read_text()) for path in sorted(batch.glob("case_[0-9][0-9][0-9].json"))]
    results = compat_rows(records)
    write_compat("case_results.json", results)
    if output_dir == batch:
        register_report_files(batch, ["case_results.json"], root=REPORT_ROOT)
    summary = {**result, "token_accuracy_pass": result.get("image_output_token_accuracy_pass"),
               "family": "gpt-image-2", "model": model, "api_form": "openai_responses", "transport": "openai-responses-image",
               "case_count": len(results), "planned_case_count": len(package["cases"]),
               "pass_count": sum(row["overall_pass"] for row in results),
               "failure_count": sum(not row["overall_pass"] for row in results),
               "unverified_gates": sorted({failure for row in results for failure in row.get("overall_failures", [])}),
               "evidence_limits": ["Exact media-input count and physical image-backend identity are not established by this run."],
               "report_dir": str(output_dir), "reference_batch": str(batch), "model_profile_database": plan["model_profile_database"]}
    if not reference_candidate:
        from lib.gpt_image_25_web_audit import summarize_case_audits, POLICY_NAME
        audit_summary = summarize_case_audits(results, package["cases"])
        summary.update(
            token_audit_summary=audit_summary,
            token_validation_pass=audit_summary["pass"],
            validation_policy=POLICY_NAME,
        )
        summary["pass"] = summary["pass"] is True and audit_summary["pass"] is True
        summary["overall_pass"] = summary["overall_pass"] is True and audit_summary["pass"] is True
    if reference_candidate:
        summary.update({key: reference_candidate[key] for key in
                        ("model", "api_form", "source_id", "endpoint", "route_profile", "execution_mode", "profile_id", "interface_id")})
        summary.update(reference_candidate=reference_candidate, certified=False, certified_route_contract_pass=False,
                       compatibility_pass=False, pass_count=0,
                       failure_count=sum(row.get("diagnostic_pass") is not True for row in results))
        # Reference-only candidates keep a separate diagnostic result.
        summary.update({"pass": False, "overall_pass": False})
    write_compat("summary.json", summary)
    print(json.dumps({"report_dir": str(output_dir), "compatibility_pass": summary["compatibility_pass"],
                      "token_accuracy": summary.get("token_accuracy_pass"), "overall_pass": summary.get("overall_pass")}))
    return 0 if (summary.get("diagnostic_pass") if reference_candidate else summary["pass"]) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--prepare", action="store_true")
    group.add_argument("--execute", action="store_true")
    group.add_argument("--replay", action="store_true", help="derive updated verdicts from saved hash-checked artifacts without network")
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--model", action="append", choices=(*MODELS, *SNAPSHOTS.values()))
    parser.add_argument("--case", action="append", dest="cases", help="exact case name or case ID; repeatable")
    parser.add_argument("--expectation-policy", choices=(FROZEN_EXPECTATION_POLICY, CURRENT_EXPECTATION_POLICY),
                        default=CURRENT_EXPECTATION_POLICY, help="evaluation rules; preserves original package and request definitions")
    parser.add_argument("--semantic-review", type=Path, help="hash-bound offline review to include during replay")
    parser.add_argument("--cleanup-only", action="store_true", help="recover known owned resources of one original shared run")
    parser.add_argument("--run-id", help="exact original shared run ID for --cleanup-only")
    args = parser.parse_args()
    if (args.cleanup_only or args.run_id) and not (args.execute and args.cleanup_only and args.run_id):
        parser.error("--cleanup-only and --run-id require --execute together")
    if args.semantic_review and not args.replay:
        parser.error("--semantic-review applies only to offline --replay")
    if args.replay:
        if not args.batch or args.model:
            parser.error("--replay requires --batch and uses its frozen models")
        result = replay(args.batch, names=args.cases, expectation_policy=args.expectation_policy,
                        semantic_review=args.semantic_review)
        print(json.dumps({"replay_record": result["replay_record"], "records": len(result["records"]), "network_requests": 0}))
        return 0
    if not args.execute:
        if args.batch:
            parser.error("--batch applies only to execution of an already prepared package")
        batch = prepare(args.model, args.cases)
        package = read_package(batch)
        from lib.test_runner.adapters.image import prepare_responses_batch
        prepare_responses_batch(package, batch)
        print(json.dumps({"batch": str(batch), "cases": len(package["cases"]), "request_cap": package["request_cap"], "network_requests": 0}))
        return 0
    if not args.batch or args.model:
        parser.error("--execute requires --batch and uses its frozen models")
    batch = require_batch(args.batch)
    lock_path = batch / ".execution.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        package = read_package(batch)
        from lib.test_runner.adapters.image import execute_responses_batch
        result = execute_responses_batch(package, batch, names=args.cases, expectation_policy=args.expectation_policy,
                                        cleanup_only=args.cleanup_only, run_id=args.run_id)
    print(json.dumps({"batch": str(batch), "recorded_cases": result["recorded_cases"], "passed_cases": result["passed_cases"],
                      "all_cases_recorded": result["all_cases_recorded"]}))
    return 0 if (result.get("diagnostic_pass") if package.get("package_kind") == MATRIX_PACKAGE_KIND else result.get("pass")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
