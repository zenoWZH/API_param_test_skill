"""Dynamic result gates for registered GPT Image 2.5 parameter tests.

Passing a test means its declared checks passed. Missing optional backend IDs
or an unavailable exact media-input counter are evidence limits, not an
unconditional failure. Explicit semantic failures remain visible and blocking
when a hash-bound review is attached to that particular output.
"""
from __future__ import annotations

from typing import Any
import copy
import hashlib
import json
from pathlib import Path


def case_result_gates(verdict: dict[str, Any]) -> dict[str, Any]:
    outcome = verdict.get("effective_expected_outcome", verdict.get("expected_outcome"))
    rejection = outcome == "rejection"
    compatibility = verdict.get("pass") is True
    failures: list[str] = []
    if not compatibility:
        failures.append("response_or_parameter_validation_failed")
    if rejection:
        if verdict.get("parameter_rejection_proven") is not True:
            failures.append("expected_rejection_not_proven")
        token_pass, identity_pass = None, None
    else:
        aggregate = verdict.get("aggregate_image_tool_usage")
        image_rows = verdict.get("image_tool_usage") or []
        image_usage_pass = (
            aggregate.get("pass") is True if isinstance(aggregate, dict)
            else bool(image_rows) and all(row.get("pass") is True for row in image_rows)
        )
        usage_pass = verdict.get("usage", {}).get("pass") is True and image_usage_pass
        estimate_pass = verdict.get("image_output_token_accuracy_pass") is True
        token_pass = usage_pass and estimate_pass
        identity_pass = verdict.get("mainline_identity_pass") is True
        if not usage_pass:
            failures.append("usage_missing_or_invalid")
        if not estimate_pass:
            failures.append("image_output_estimate_not_verified")
        if not identity_pass:
            failures.append("mainline_identity_not_verified")
    semantic = verdict.get("semantic_validation")
    semantic_pass = semantic.get("pass") if isinstance(semantic, dict) else None
    if semantic_pass is False:
        failures.append("semantic_validation_failed")
    cleanup = verdict.get("file_cleanup")
    cleanup_required = isinstance(cleanup, dict) and cleanup.get("required") is True
    cleanup_pass = cleanup.get("pass") is True if cleanup_required else None
    if cleanup_required and not cleanup_pass:
        failures.append("uploaded_file_cleanup_failed")
    return {"compatibility_pass": compatibility, "token_validation_pass": token_pass,
            "model_identity_pass": identity_pass, "semantic_validation_pass": semantic_pass,
            "file_cleanup_pass": cleanup_pass,
            "model_identity_scope": "mainline_model_and_exposed_tool_configuration",
            "overall_pass": not failures, "overall_failures": failures,
            "validation_scope": "declared_parameter_checks_and_attached_semantic_review",
            "token_validation_scope": "usage_arithmetic_and_official_image_output_estimate" if not rejection else "not_applicable_rejection",
            "evidence_limits": [] if rejection else [
                "Exact media-input quantity is not established by this estimate-based check.",
                "Returned mainline model and tool configuration do not establish physical image-backend identity.",
            ]}


def aggregate_result_gates(package: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    expected = [case["case_id"] for case in package.get("cases", [])]
    ids = [row.get("case_id") for row in rows]
    complete = bool(expected) and len(ids) == len(set(ids)) and set(ids) == set(expected)
    checks = [(row, case_result_gates(row.get("verdict", {}))) for row in rows]
    parameter_pass = complete and all(item["compatibility_pass"] for _, item in checks)
    passed = complete and all(item["overall_pass"] for _, item in checks)
    token_checks = [item["token_validation_pass"] for _, item in checks if item["token_validation_pass"] is not None]
    identity_checks = [item["model_identity_pass"] for _, item in checks if item["model_identity_pass"] is not None]
    cleanup_checks = [item["file_cleanup_pass"] for _, item in checks if item["file_cleanup_pass"] is not None]
    return {"pass": passed, "overall_pass": passed, "compatibility_pass": parameter_pass,
            "parameter_checks_pass": parameter_pass, "all_cases_recorded": complete,
            "overall_passed_cases": sum(item["overall_pass"] for _, item in checks),
            "overall_failed_cases": [row.get("case_id") for row, item in checks if not item["overall_pass"]],
            "missing_cases": sorted(set(expected) - set(ids)),
            "unexpected_or_duplicate_cases": len(ids) != len(set(ids)) or bool(set(ids) - set(expected)),
            "token_validation_pass": all(token_checks) if token_checks else None,
            "model_identity_pass": all(identity_checks) if identity_checks else None,
            "model_identity_scope": "mainline_model_and_exposed_tool_configuration",
            "semantic_reviewed_cases": sum(item["semantic_validation_pass"] is not None for _, item in checks),
            "semantic_failed_cases": [row.get("case_id") for row, item in checks if item["semantic_validation_pass"] is False],
            "file_cleanup_pass": all(cleanup_checks) if cleanup_checks else None,
            "file_cleanup_failed_cases": [row.get("case_id") for row, item in checks if item["file_cleanup_pass"] is False],
            "required_check_failures": {row.get("case_id"): item["overall_failures"] for row, item in checks if item["overall_failures"]},
            "validation_scope": "declared_parameter_checks_and_attached_semantic_review"}


def attach_semantic_review(path: Path, rows: list[dict], source_paths: dict,
                            sources: dict, batch: Path, *, root: Path) -> dict:
    """Apply human review only when source record and output hashes match."""
    path = path.resolve()
    raw = path.read_bytes()
    review = json.loads(raw)
    selected = {row["case_id"]: row for row in rows}
    seen, attached = set(), []
    for item in review.get("records", []):
        case_id = item.get("case_id")
        if not isinstance(case_id, str) or case_id in seen or type(item.get("pass")) is not bool:
            raise ValueError("semantic review requires unique case IDs and explicit boolean results")
        seen.add(case_id)
        if case_id not in sources:
            raise ValueError("semantic review belongs to a different source batch")
        record = item.get("case_record", {})
        if ((root / str(record.get("path", ""))).resolve() != Path(source_paths[case_id]["path"])
                or record.get("sha256") != source_paths[case_id]["sha256"]):
            raise ValueError("semantic review source record hash or path mismatch")
        output = item.get("output", {})
        output_path = (root / str(output.get("path", ""))).resolve()
        artifacts = sources[case_id].get("verdict", {}).get("artifacts", [])
        if (not output_path.is_relative_to(batch) or not output_path.is_file()
                or not any(Path(info.get("path", "")) == output_path
                           and info.get("sha256") == output.get("sha256") for info in artifacts)
                or hashlib.sha256(output_path.read_bytes()).hexdigest() != output.get("sha256")):
            raise ValueError("semantic review does not match the saved output image")
        if case_id in selected:
            selected[case_id]["verdict"]["semantic_validation"] = {
                "pass": item["pass"], "status": item.get("status"),
                "criteria": copy.deepcopy(item.get("criteria", {})), "notes": copy.deepcopy(item.get("notes", [])),
                "review_path": str(path), "review_sha256": hashlib.sha256(raw).hexdigest(),
                "output_sha256": output["sha256"], "scope": item.get("review_scope"),
            }
            attached.append(case_id)
    if not seen:
        raise ValueError("semantic review is empty")
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "attached_case_ids": attached}
