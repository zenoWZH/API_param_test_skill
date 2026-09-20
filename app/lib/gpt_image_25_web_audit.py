"""Explicit Web result policy for the dedicated official Responses image runner.

This policy checks request integrity, response identity/completion, usage
arithmetic and official image-output estimates. It does not certify exact
media-input quantities or infer a per-image allocation from aggregate usage.
The generic text token-audit schema is intentionally not reused.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from . import gpt_image_25_responses as matrix
from .gpt_image_25_result_policy import case_result_gates
from .image_token_expectations import image_output_token_expectation
from .image_validation import validate_gpt_image_2_size

POLICY_NAME = "openai_responses_image_usage_and_estimate_v1"
AUDIT_SCHEMA_VERSION = 1
RUN_SUMMARY_SCHEMA_VERSION = 2
_HEX = re.compile(r"[0-9a-f]{64}")
LIMITS = [
    "Exact media-input quantity and server-side revised-prompt quantity are unverified.",
    "Image-output quantities use official estimates, not exact measured counts.",
    "Aggregate image usage does not establish individual-call allocation.",
    "Mainline identity and exposed tool configuration do not establish physical backend identity.",
]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def eligible_image_plan(plan: Any) -> bool:
    if not isinstance(plan, dict):
        return False
    if (plan.get("transport") != "openai-responses-image"
            or plan.get("api_form") != "openai_responses"
            or plan.get("endpoint") != matrix.ENDPOINT
            or plan.get("model") not in (*matrix.MODELS, *matrix.SNAPSHOTS.values())
            or plan.get("reference_candidate")
            or plan.get("execution_mode") == "reference_observation"):
        return False
    from .model_profile_catalog import binding_from_database_snapshot
    try:
        binding = binding_from_database_snapshot(plan.get("model_profile_database") or {})
    except (KeyError, TypeError, ValueError, RuntimeError):
        return False
    target = binding.get("execution_target") or {}
    interface = binding.get("interface") or {}
    return (binding.get("source_id") == "openai"
            and target.get("api_form") == "openai_responses"
            and target.get("request_model_id") == plan["model"]
            and (target.get("transport") or interface.get("transport_adapter_id")) == "openai-responses-image")


def _image_checks(case: dict, row: dict, failures: list[str]) -> dict:
    response = row.get("response") if isinstance(row.get("response"), dict) else {}
    verdict = row.get("verdict") if isinstance(row.get("verdict"), dict) else {}
    output = response.get("output")
    output = output if isinstance(output, list) else []
    calls = [item for item in output if isinstance(item, dict) and item.get("type") == "image_generation_call"]
    try:
        matrix.require_id(response.get("id"), "resp")
        for item in calls:
            matrix.require_id(item.get("id"), "ig")
    except ValueError:
        failures.append("response_or_image_call_id_invalid")
        # Invalid IDs must produce a failed report, not become unhashable
        # keys in the call/partial-image attribution maps below.
        return {"mainline_usage": {"source_path": "usage", **matrix.audit_usage(response.get("usage"))},
                "aggregate_image_usage": None, "individual_image_usage": [],
                "image_output_expectations": [],
                "exact_input_count_verified": False, "exact_output_count_verified": False}
    if (response.get("object") != "response" or response.get("status") != "completed"
            or response.get("error") or response.get("incomplete_details")
            or response.get("model") != matrix.MAINLINE_MODEL
            or len(calls) != case["expected_count"]
            or len({item.get("id") for item in calls}) != len(calls)
            or any(not isinstance(item, dict) or item.get("type") not in {"message", "reasoning", "image_generation_call"} for item in output)
            or any(item.get("status") != "completed" for item in calls)
            or any(item.get("status") != "completed" or any(part.get("type") == "refusal" for part in item.get("content", []) if isinstance(part, dict))
                   for item in output if isinstance(item, dict) and item.get("type") == "message")):
        failures.append("response_envelope_identity_or_completion_invalid")
    tool = case["parameters"]
    allowed = {tool["model"], matrix.SNAPSHOTS.get(tool["model"], tool["model"])}
    if "tools" in response:
        echoed = [item for item in response["tools"] if isinstance(item, dict) and item.get("type") == "image_generation"] if isinstance(response["tools"], list) else []
        if len(echoed) != 1 or echoed[0].get("model") not in allowed:
            failures.append("tool_model_echo_mismatch")
    for item in calls:
        if item.get("model") is not None and item["model"] not in allowed:
            failures.append("returned_tool_model_mismatch")
        for key in ("quality", "size", "output_format", "background", "action"):
            if key in item and tool.get(key) not in {None, "auto"} and item[key] != tool[key]:
                failures.append("returned_tool_parameter_mismatch_" + key)
    usage = matrix.audit_usage(response.get("usage"))
    if usage["pass"] is not True:
        failures.append("mainline_usage_invalid")
    output_budget = (row.get("request") or {}).get("max_output_tokens")
    reported_output = usage["reported_usage"].get("output_tokens")
    if (type(output_budget) is not int or output_budget < 256
            or type(reported_output) is not int or reported_output > output_budget):
        failures.append("mainline_output_budget_invalid_or_exceeded")
    artifacts = verdict.get("artifacts") or []
    if len(artifacts) != len(calls):
        failures.append("decoded_image_count_mismatch")
    stream = verdict.get("stream") or {}
    partials = row.get("stream_events") or []
    if case.get("stream"):
        completed = [event for event in partials if event.get("type") == "response.completed"]
        sequence = [event.get("sequence_number") for event in partials]
        if (stream.get("pass") is not True or not partials or partials[-1].get("type") != "response.completed"
                or len(completed) != 1 or completed[0].get("response") != response
                or any(type(number) is not int for number in sequence)
                or any(a >= b for a, b in zip(sequence, sequence[1:]) if type(a) is int and type(b) is int)
                or any(event.get("type") in {"error", "response.failed", "response.incomplete"} for event in partials)):
            failures.append("stream_completion_invalid")
    partial_events = [event for event in partials if event.get("type") == "response.image_generation_call.partial_image"]
    by_item = {item.get("id"): sum(event.get("item_id") == item.get("id") for event in partial_events) for item in calls}
    if (sum(by_item.values()) != len(partial_events)
            or [event.get("partial_image_index") for event in partial_events] != list(range(len(partial_events)))
            or len(partial_events) > tool.get("partial_images", 0)
            or (not case.get("stream") and partial_events)):
        failures.append("partial_image_attribution_invalid")
    expectations, individual = [], []
    for index, (item, artifact) in enumerate(zip(calls, artifacts)):
        width, height = artifact.get("width"), artifact.get("height")
        size = f"{width}x{height}"
        if (artifact.get("decoded") is not True or artifact.get("pass") is not True or artifact.get("errors")
                or not isinstance(artifact.get("sha256"), str) or not _HEX.fullmatch(artifact["sha256"])
                or type(artifact.get("byte_length")) is not int or artifact["byte_length"] <= 0
                or type(width) is not int or type(height) is not int or min(width, height) <= 0
                or artifact.get("format") != tool.get("output_format", "png").upper()
                or (tool.get("size") not in {None, "auto"} and size != tool["size"])
                or (tool.get("size") != "auto" and validate_gpt_image_2_size(size))):
            failures.append("decoded_image_geometry_invalid")
        alpha = artifact.get("alpha_extrema")
        if ((tool.get("background") == "transparent" and (not isinstance(alpha, list) or len(alpha) != 2 or alpha[0] == 255 or alpha[1] == 0))
                or (tool.get("background") == "opaque" and alpha is not None and (not isinstance(alpha, list) or len(alpha) != 2 or alpha[0] != 255))):
            failures.append("decoded_image_background_invalid")
        # The existing estimator supplies the partial-image overhead as well.
        expectation = image_output_token_expectation(tool["model"],
            {**tool, "n": 1, "stream": bool(case.get("stream")),
             "quality": item.get("quality") if tool.get("quality") == "auto" else tool.get("quality")},
            [artifact], reference_source="openai", observed_partial_images=by_item.get(item.get("id"), 0))
        expectations.append(expectation)
        expected = (expectation or {}).get("tokens")
        check = matrix.audit_image_tool_usage(item.get("usage"), expected)
        check.update(source_path=f"output[{output.index(item)}].usage", scope="individual_image_tool_call", item_id=item.get("id"))
        individual.append(check)
        if item.get("usage") is not None and (check.get("pass") is not True or check.get("image_output_token_accuracy_pass") is not True):
            failures.append("individual_image_usage_invalid")
        if expectation is None or expected is None:
            failures.append("official_image_output_expectation_missing")
    aggregate = None
    tool_usage = response.get("tool_usage")
    if isinstance(tool_usage, dict) and "image_gen" in tool_usage:
        expected = sum(item["tokens"] for item in expectations) if expectations and all(item and type(item.get("tokens")) is int for item in expectations) else None
        aggregate = matrix.audit_image_tool_usage(tool_usage["image_gen"], expected, require_output_details=True)
        aggregate.update(source_path="tool_usage.image_gen", scope="aggregate_of_all_image_generation_calls",
                         per_call_allocation="not_inferred_from_aggregate", image_count=len(calls))
        if aggregate.get("pass") is not True or aggregate.get("image_output_token_accuracy_pass") is not True:
            failures.append("aggregate_image_usage_invalid")
        if individual and all(item.get("pass") is True for item in individual):
            if sum(item["reported_usage"]["output_tokens"] for item in individual) != aggregate.get("reported_usage", {}).get("output_tokens"):
                failures.append("aggregate_and_individual_image_usage_disagree")
    elif not individual or any(item.get("pass") is not True or item.get("image_output_token_accuracy_pass") is not True for item in individual):
        failures.append("image_usage_missing")
    if tool_usage is not None and not isinstance(tool_usage, dict):
        failures.append("tool_usage_invalid")
    return {"mainline_usage": {"source_path": "usage", **usage},
            "aggregate_image_usage": aggregate, "individual_image_usage": individual,
            "image_output_expectations": expectations,
            "exact_input_count_verified": False, "exact_output_count_verified": False}


def _rebuild_request(case: dict, request: dict) -> dict:
    """Rebuild authored inputs, admitting only the recorded lifecycle IDs."""
    files, dependency = {}, None
    if case["input_kind"] == "file_id":
        files["image"] = request["input"][0]["content"][1]["file_id"]
        if case["parameters"].get("input_image_mask", {}).get("file_id"):
            files["mask"] = request["tools"][0]["input_image_mask"]["file_id"]
    if case.get("depends_on"):
        dependency = {"case_id": case["depends_on"], "verdict": {"pass": True}, "response": {}}
        if case["input_kind"] == "previous_response":
            dependency["response"]["id"] = request.get("previous_response_id")
        else:
            dependency["response"]["output"] = [{"type": "image_generation_call", "id": request["input"][1]["id"]}]
    return matrix.build_request(case, file_ids=files, dependency=dependency)


def _exchange(case: dict, row: dict, integrity: dict) -> dict:
    failures: list[str] = []
    matrix.validate_case(case)
    effective = matrix.effective_case_expectations(case)
    if (row.get("case_id") != case["case_id"] or row.get("endpoint") != matrix.ENDPOINT
            or row.get("mainline_model") != matrix.MAINLINE_MODEL or row.get("tool_model") != case["tool_model"]):
        failures.append("source_or_case_identity_mismatch")
    hashes = [integrity.get(key) for key in ("expected_sha256", "actual_sha256", "wire_sha256")]
    if (integrity.get("status") != "pass" or any(not isinstance(value, str) or not _HEX.fullmatch(value) for value in hashes)
            or len(set(hashes)) != 1 or row.get("request_sha256") != hashes[0]
            or integrity.get("public_request_sha256") != _digest(row.get("request"))):
        failures.append("request_integrity_not_verified")
    request = row.get("request") or {}
    try:
        rebuilt_request = _rebuild_request(case, request)
        if matrix.public_payload(rebuilt_request) != request or matrix.digest(rebuilt_request) != row.get("request_sha256"):
            failures.append("authored_request_mismatch")
    except (KeyError, TypeError, ValueError, IndexError):
        failures.append("authored_request_invalid")
    if (request.get("model") != matrix.MAINLINE_MODEL or not isinstance(request.get("tools"), list)
            or len(request["tools"]) != 1 or request["tools"][0].get("model") != case["tool_model"]):
        failures.append("request_model_identity_mismatch")
    response = row.get("response") or {}
    verdict = row.get("verdict") or {}
    gates = case_result_gates(verdict)
    if gates["overall_pass"] is not True:
        failures.extend(gates["overall_failures"])
    if verdict.get("effective_expected_outcome", verdict.get("expected_outcome")) != effective["expected_outcome"]:
        failures.append("case_expectation_mismatch")
    rejection = effective["expected_outcome"] == "rejection"
    checks: dict[str, Any] = {}
    if rejection and row.get("status_code") != 200:
        proof = matrix.evaluate(case, row.get("status_code"), response, Path("."), expectation_policy=matrix.CURRENT_EXPECTATION_POLICY)
        if proof.get("pass") is not True or proof.get("parameter_rejection_proven") is not True:
            failures.append("expected_rejection_not_proven")
        checks["rejection"] = {key: proof.get(key) for key in ("parameter_rejection_proven", "error_param", "error_code", "http_status")}
    else:
        if row.get("status_code") != 200:
            failures.append("successful_generation_missing")
        checks.update(_image_checks(case, row, failures))
    return {"schema_version": AUDIT_SCHEMA_VERSION, "policy": POLICY_NAME,
            "case_id": case["case_id"], "case": "gpt_image_25_responses_" + case["name"],
            "usage_required": not rejection or row.get("status_code") == 200,
            "validation_status": "fail" if failures else "not_applicable" if rejection else "pass",
            "validation_pass": not failures, "validation_failures": sorted(set(failures)),
            "request_integrity": copy.deepcopy(integrity), "checks": checks,
            "evidence": {"case_definition": copy.deepcopy(case), "row": copy.deepcopy(row)},
            "evidence_limits": list(LIMITS)}


def build_case_audit(case: dict, row: dict, request_integrity: dict) -> dict:
    """Adapt a decoded source record; no credential, network or file reads occur.

    request_integrity must come from rebuilding the frozen request and checking
    the saved wire record. Its expected_sha256/actual_sha256/wire_sha256 must
    agree with row.request_sha256; public_request_sha256 binds the redacted body.
    """
    fields = ("case_id", "endpoint", "mainline_model", "tool_model", "status_code", "request_sha256", "request", "response", "stream_events", "verdict")
    evidence_row = matrix.public_payload({key: row.get(key) for key in fields})
    # File locations are not needed by the result classifier; hashes and decoded
    # metadata retain the evidence relationship without leaking local paths.
    for artifact in (evidence_row.get("verdict") or {}).get("artifacts") or []:
        artifact.pop("path", None)
    exchange = _exchange(case, evidence_row, request_integrity)
    return {"schema_version": AUDIT_SCHEMA_VERSION, "policy": POLICY_NAME,
            "validation_pass": exchange["validation_pass"], "validation_status": exchange["validation_status"],
            "validation_failures": exchange["validation_failures"], "exchanges": [exchange]}


def _summarize(audits: list[dict], planned_ids: list[str]) -> dict:
    exchanges, failures = [], []
    for audit in audits:
        if (not isinstance(audit, dict) or audit.get("policy") != POLICY_NAME
                or type(audit.get("schema_version")) is not int or audit.get("schema_version") != AUDIT_SCHEMA_VERSION
                or not isinstance(audit.get("exchanges"), list) or len(audit["exchanges"]) != 1):
            failures.append("invalid_case_audit")
            continue
        original = audit["exchanges"][0]
        try:
            rebuilt = _exchange(original["evidence"]["case_definition"], original["evidence"]["row"], original["request_integrity"])
            if original != rebuilt or audit.get("validation_pass") != rebuilt["validation_pass"] or audit.get("validation_status") != rebuilt["validation_status"] or audit.get("validation_failures") != rebuilt["validation_failures"]:
                failures.append("case_audit_evidence_mismatch")
            exchanges.append(rebuilt)
        except (KeyError, TypeError, ValueError, AttributeError):
            failures.append("invalid_case_audit_evidence")
    ids = [exchange["case_id"] for exchange in exchanges]
    if not planned_ids or len(planned_ids) != len(set(planned_ids)) or ids != planned_ids:
        failures.append("frozen_case_selection_mismatch")
    by_id = {exchange["case_id"]: exchange for exchange in exchanges}
    for exchange in exchanges:
        case = exchange["evidence"]["case_definition"]
        if case.get("depends_on"):
            dependency = by_id.get(case["depends_on"])
            request = exchange["evidence"]["row"].get("request") or {}
            if not dependency or dependency["validation_pass"] is not True or ids.index(case["depends_on"]) >= ids.index(case["case_id"]):
                failures.append("stateful_dependency_not_verified")
                continue
            response = dependency["evidence"]["row"].get("response") or {}
            if case["input_kind"] == "previous_response":
                if request.get("previous_response_id") != response.get("id"):
                    failures.append("stateful_dependency_response_id_mismatch")
            else:
                calls = [item for item in response.get("output") or [] if item.get("type") == "image_generation_call"]
                if len(calls) != 1 or request.get("input", [{}, {}])[1].get("id") != calls[0].get("id"):
                    failures.append("stateful_dependency_image_id_mismatch")
    failures.extend(failure for exchange in exchanges for failure in exchange["validation_failures"])
    required = sum(exchange["usage_required"] for exchange in exchanges)
    return {"schema_version": AUDIT_SCHEMA_VERSION, "policy": POLICY_NAME,
            "pass": not failures, "validation_status": "fail" if failures else "pass" if required else "not_applicable",
            "exchange_count": len(exchanges), "required_exchange_count": required,
            "validated_exchange_count": sum(exchange["usage_required"] and exchange["validation_pass"] for exchange in exchanges),
            "expected_rejection_count": sum(not exchange["usage_required"] and exchange["validation_pass"] for exchange in exchanges),
            "validation_failure_count": len(set(failures)), "validation_failures": sorted(set(failures)),
            "planned_case_ids": planned_ids, "case_names": [exchange["case"] for exchange in exchanges],
            "case_audits": copy.deepcopy(audits), "evidence_limits": list(LIMITS)}


def summarize_case_audits(case_results: list[dict], planned_cases: list[dict]) -> dict:
    return _summarize([row.get("token_audit") for row in case_results], [case["case_id"] for case in planned_cases])


def _summarize_runs(run_audits: list[dict], planned_ids: list[str], expected_runs: int) -> dict:
    """Retain run boundaries so dependencies can never cross repetitions."""
    failures, rebuilt_runs, seen = [], [], set()
    if type(expected_runs) is not int or expected_runs < 1 or len(run_audits) != expected_runs:
        failures.append("frozen_run_selection_mismatch")
    for index, run in enumerate(run_audits, 1):
        run_id = run.get("run_id")
        if (not isinstance(run_id, str) or not run_id or run_id in seen
                or type(run.get("run_index")) is not int or run["run_index"] != index):
            failures.append("frozen_run_selection_mismatch")
        if isinstance(run_id, str):
            seen.add(run_id)
        summary = run["summary"]
        rebuilt = _summarize(summary["case_audits"], planned_ids)
        if rebuilt != summary:
            failures.append("run_audit_evidence_mismatch")
        failures.extend(rebuilt["validation_failures"])
        rebuilt_runs.append({"run_id": run_id, "run_index": run.get("run_index"), "summary": rebuilt})
    counts = {key: sum(run["summary"][key] for run in rebuilt_runs) for key in (
        "exchange_count", "required_exchange_count", "validated_exchange_count", "expected_rejection_count")}
    return {"schema_version": RUN_SUMMARY_SCHEMA_VERSION, "policy": POLICY_NAME, "pass": not failures,
            "validation_status": "fail" if failures else "pass" if counts["required_exchange_count"] else "not_applicable",
            **counts, "validation_failure_count": len(set(failures)), "validation_failures": sorted(set(failures)),
            "planned_case_ids": planned_ids, "expected_run_count": expected_runs,
            "completed_run_count": len(rebuilt_runs), "run_audits": rebuilt_runs, "evidence_limits": list(LIMITS)}


def summarize_run_audits(runs: list[dict], planned_cases: list[dict], expected_runs: int) -> dict:
    return _summarize_runs([
        {"run_id": run["run_id"], "run_index": run["run_index"],
         "summary": summarize_case_audits([step["record"] for step in run["steps"].values() if "record" in step], planned_cases)}
        for run in runs
    ], [case["case_id"] for case in planned_cases], expected_runs)


def validate_policy_summary(job_spec: dict, result: dict) -> tuple[bool, list[str]]:
    reasons = []
    plan = job_spec.get("image_plan") or {}
    if job_spec.get("type") != "image_param_test" or not eligible_image_plan(plan):
        reasons.append("dedicated_image_policy_not_applicable")
    summary = result.get("token_audit_summary") or {}
    try:
        expected_runs = (job_spec.get("execution_plan") or {}).get("run_count", 1)
        multiple = summary.get("schema_version") == RUN_SUMMARY_SCHEMA_VERSION
        if (type(summary.get("schema_version")) is not int or summary.get("schema_version") not in (AUDIT_SCHEMA_VERSION, RUN_SUMMARY_SCHEMA_VERSION)
                or summary.get("policy") != POLICY_NAME or type(summary.get("pass")) is not bool
                or any(type(summary.get(key)) is not int or summary[key] < 0 for key in (
                    "exchange_count", "required_exchange_count", "validated_exchange_count", "expected_rejection_count", "validation_failure_count"))
                or any(type(result.get(key)) is not int or result[key] < 0 for key in (
                    "case_count", "planned_case_count", "pass_count", "failure_count"))):
            reasons.append("dedicated_image_summary_schema_invalid")
        rebuilt = (_summarize_runs(summary["run_audits"], summary["planned_case_ids"], expected_runs) if multiple
                   else _summarize(summary["case_audits"], summary["planned_case_ids"]))
        if type(expected_runs) is not int or expected_runs < 1 or not multiple and expected_runs != 1:
            reasons.append("frozen_run_selection_mismatch")
        if rebuilt != summary:
            reasons.append("dedicated_image_summary_evidence_mismatch")
        if rebuilt["pass"] is not True:
            reasons.extend(rebuilt["validation_failures"])
        expected_names = plan.get("cases")
        canonical = {"gpt_image_25_responses_" + case["name"]: case["case_id"] for case in matrix.responses_cases(plan.get("model"))}
        if rebuilt["planned_case_ids"] != [canonical.get(name) for name in expected_names or []]:
            reasons.append("frozen_case_model_or_definition_mismatch")
        if job_spec.get("model_profile_database") is not None and plan.get("model_profile_database") != job_spec["model_profile_database"]:
            reasons.append("image_plan_mpdb_snapshot_mismatch")
        run_summaries = [run["summary"] for run in rebuilt["run_audits"]] if multiple else [rebuilt]
        if (not isinstance(expected_names, list) or not expected_names
                or any(run["case_names"] != expected_names for run in run_summaries)
                or plan.get("test_profiles") != expected_names or plan.get("estimated_case_count") != len(expected_names)
                or result.get("case_count") != len(expected_names) * expected_runs
                or result.get("planned_case_count") != len(expected_names) * expected_runs):
            reasons.append("frozen_case_selection_mismatch")
        if result.get("token_validation_pass") is not rebuilt["pass"]:
            reasons.append("dedicated_image_result_validation_mismatch")
        if result.get("pass") is True and (rebuilt["pass"] is not True or result.get("failure_count") != 0 or result.get("pass_count") != len(expected_names) * expected_runs):
            reasons.append("dedicated_image_result_pass_mismatch")
    except (KeyError, TypeError, ValueError, AttributeError):
        reasons.append("dedicated_image_summary_missing_or_invalid")
    return not reasons, sorted(set(reasons))
