from __future__ import annotations

import copy
import base64
import io
from pathlib import Path

import pytest

from lib import gpt_image_25_responses as matrix
from lib.config import load_config
from lib.gpt_image_25_web_audit import (
    POLICY_NAME, build_case_audit, summarize_case_audits, validate_policy_summary,
)
from lib.job_spec import build_result_validation_contract, resolve_image_plan
from PIL import Image


def response():
    image = Image.new("RGB", (1024, 1024), (0, 80, 220))
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    return {"object": "response", "id": "resp_test", "model": matrix.MAINLINE_MODEL,
            "status": "completed", "error": None, "incomplete_details": None,
            "output": [{"type": "image_generation_call", "id": "ig_test", "status": "completed",
                        "quality": "low", "size": "1024x1024", "result": base64.b64encode(encoded.getvalue()).decode()}],
            "usage": {"input_tokens": 100, "output_tokens": 35, "total_tokens": 135,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 10}}}


def bound_plan(config):
    plan = resolve_image_plan(config, {"image_plan": {"suite": "smoke", "api_form": "openai_responses"}},
                              "openai_official", matrix.MODELS[0], 120)
    if "model_profile_database" not in plan:
        # The portable App attaches its immutable binding in _create_image_job
        # after the presentation planner, before declaring the result contract.
        from lib.model_profile_catalog import resolve_runtime_parameter_config
        bound = resolve_runtime_parameter_config(config, "openai_official", matrix.MODELS[0],
                    plan["family"], plan["route_profile"], plan["api_form"], modality="image")
        plan["model_profile_database"] = copy.deepcopy(bound["model_profile_database"])
        plan["model_capability_profile"]["model_profile_database"] = copy.deepcopy(bound["model_profile_database"])
    return plan


def make_row(tmp_path, name="baseline_generate", *, payload=None, status=200):
    case = next(case for case in matrix.responses_cases(matrix.MODELS[0]) if case["name"] == name)
    request = matrix.build_request(case)
    payload = response() if payload is None else payload
    if status == 200:
        payload.setdefault("tool_usage", {"image_gen": {
            "input_tokens": 12, "output_tokens": 196, "total_tokens": 208,
            "input_tokens_details": {"text_tokens": 12, "image_tokens": 0},
            "output_tokens_details": {"text_tokens": 0, "image_tokens": 196},
        }})
    row = {"case_id": case["case_id"], "endpoint": matrix.ENDPOINT,
           "mainline_model": matrix.MAINLINE_MODEL, "tool_model": case["tool_model"],
           "status_code": status, "request_sha256": matrix.digest(request),
           "request": matrix.public_payload(request), "response": matrix.public_payload(payload),
           "verdict": matrix.evaluate(case, status, payload, tmp_path / name,
                                      expectation_policy=matrix.CURRENT_EXPECTATION_POLICY)}
    integrity = {"status": "pass", "expected_sha256": row["request_sha256"],
                 "actual_sha256": row["request_sha256"], "wire_sha256": row["request_sha256"],
                 "public_request_sha256": matrix.digest(row["request"])}
    return case, row, integrity


def test_success_uses_distinct_scope_and_does_not_allocate_aggregate_to_call(tmp_path):
    case, row, integrity = make_row(tmp_path)
    audit = build_case_audit(case, row, integrity)
    assert audit["validation_pass"] is True, audit
    exchange = audit["exchanges"][0]
    assert exchange["schema_version"] == 1
    checks = exchange["checks"]
    assert checks["mainline_usage"]["reported_usage"]["output_tokens"] == 35
    assert checks["aggregate_image_usage"]["reported_usage"]["output_tokens"] == 196
    assert checks["aggregate_image_usage"]["per_call_allocation"] == "not_inferred_from_aggregate"
    assert checks["individual_image_usage"][0]["pass"] is None
    assert checks["exact_input_count_verified"] is False
    assert checks["exact_output_count_verified"] is False
    summary = summarize_case_audits([{"token_audit": audit}], [case])
    assert summary["pass"] is True
    assert summary["required_exchange_count"] == 1


@pytest.mark.parametrize("mutation", [
    lambda row: row["response"]["usage"].update(total_tokens=999),
    lambda row: row["response"]["tool_usage"]["image_gen"].update(total_tokens=999),
    lambda row: row["response"]["tool_usage"].clear(),
    lambda row: row["response"].update(id="wrong"),
    lambda row: row["response"].update(model="wrong"),
    lambda row: row["response"].update(status="incomplete"),
    lambda row: row["response"]["output"][0].update(status="in_progress"),
    lambda row: row["verdict"]["artifacts"][0].update(width=1),
    lambda row: row["verdict"].update(semantic_validation={"pass": False}),
    lambda row: row["verdict"].update(file_cleanup={"required": True, "pass": False}),
    lambda row: row.update(request_sha256="0" * 64),
])
def test_claimed_pass_cannot_hide_source_failure(tmp_path, mutation):
    case, row, integrity = make_row(tmp_path)
    mutation(row)
    assert build_case_audit(case, row, integrity)["validation_pass"] is False


def test_summary_recomputes_values_instead_of_trusting_pass_flags(tmp_path):
    case, row, integrity = make_row(tmp_path)
    audit = build_case_audit(case, row, integrity)
    audit["exchanges"][0]["evidence"]["row"]["response"]["usage"]["total_tokens"] = 999
    summary = summarize_case_audits([{"token_audit": audit}], [case])
    assert summary["pass"] is False
    assert "case_audit_evidence_mismatch" in summary["validation_failures"]


def test_pure_attributed_rejection_is_na_without_invented_generation(tmp_path):
    case, row, integrity = make_row(tmp_path, "reject_quality", status=400,
                                  payload={"error": {"param": "quality", "message": "invalid quality"}})
    audit = build_case_audit(case, row, integrity)
    summary = summarize_case_audits([{"token_audit": audit}], [case])
    assert summary["pass"] is True, summary
    assert summary["required_exchange_count"] == 0
    assert summary["expected_rejection_count"] == 1
    assert summary["validation_status"] == "not_applicable"


def test_account_denial_cannot_be_turned_into_expected_rejection(tmp_path):
    case, row, integrity = make_row(tmp_path, "reject_quality", status=403,
                                  payload={"error": {"param": "quality", "message": "access denied"}})
    row["verdict"].update({"pass": True, "parameter_rejection_proven": True})
    assert build_case_audit(case, row, integrity)["validation_pass"] is False


def test_policy_declared_only_for_exact_bound_ordinary_responses_image():
    config = load_config()
    plan = bound_plan(config)
    contract = build_result_validation_contract(config, image_plan=plan)
    assert contract["validation_policy"] == POLICY_NAME
    for changes in ({"execution_mode": "reference_observation"}, {"transport": "images-generations"},
                    {"endpoint": "https://example.invalid/responses"}, {"model_profile_database": {}},
                    {"reference_candidate": {"certified": False}}):
        other = build_result_validation_contract(config, image_plan={**plan, **changes})
        assert "validation_policy" not in other
        assert other["token_audit_schema_version"] == 4


def test_validator_requires_the_exact_frozen_selection_and_counts(tmp_path):
    config = load_config()
    plan = bound_plan(config)
    case, row, integrity = make_row(tmp_path)
    audit = build_case_audit(case, row, integrity)
    summary = summarize_case_audits([{"token_audit": audit}], [case])
    result = {"token_audit_summary": summary, "token_validation_pass": True, "pass": True,
              "case_count": 1, "planned_case_count": 1, "pass_count": 1, "failure_count": 0}
    spec = {"type": "image_param_test", "image_plan": plan}
    assert validate_policy_summary(spec, result) == (True, [])
    for mutate in (lambda r: r.update(case_count=2), lambda r: r["token_audit_summary"].update(exchange_count=0),
                   lambda r: r["token_audit_summary"].update(case_names=["other"]),
                   lambda r: r["token_audit_summary"]["case_audits"].clear()):
        bad = copy.deepcopy(result); mutate(bad)
        assert validate_policy_summary(spec, bad)[0] is False


def test_classifier_accepts_explicit_policy_and_keeps_identity_gate(tmp_path):
    from lib.job_spec import make_job_spec, classify_parameter_result

    config = load_config()
    plan = bound_plan(config)
    spec = make_job_spec(job_type="image_param_test", provider="openai_official", model=matrix.MODELS[0],
                         workload="image", request_mode="fixed", target_rpm=0, target_tpm=0,
                         api_form="openai_responses", transport=plan["transport"],
                         reference_contract_id=plan["model_profile_database"]["reference_contract_id"],
                         model_profile_database=plan["model_profile_database"], image_plan=plan,
                         result_contract=build_result_validation_contract(config, image_plan=plan))
    case, row, integrity = make_row(tmp_path)
    audit = build_case_audit(case, row, integrity)
    result = {"token_audit_summary": summarize_case_audits([{"token_audit": audit}], [case]),
              "token_validation_pass": True, "pass": True, "case_count": 1,
              "planned_case_count": 1, "pass_count": 1, "failure_count": 0,
              "model_profile_database": plan["model_profile_database"], "model": matrix.MODELS[0],
              "api_form": "openai_responses"}
    classification = classify_parameter_result(spec, result)
    assert classification["status"] == "current_pass", classification
    result["model"] = matrix.MODELS[1]
    assert classify_parameter_result(spec, result)["status"] == "identity_mismatch"
    result["model"] = matrix.MODELS[0]
    spec["result_contract"] = build_result_validation_contract(config)
    assert classify_parameter_result(spec, result)["pass"] is False
