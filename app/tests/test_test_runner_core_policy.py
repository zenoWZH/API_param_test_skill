"""Frozen repeat policy evidence stays separate from original run outcomes."""
from __future__ import annotations

import copy

import pytest

from lib.parameter_job_controls import freeze_workflow_job, workflow_execution_result_state
from lib.test_runner import HandlerRegistry, PlanValidationError, compile_plan, execute_plan, summarize_plan_runs


TARGET = {"provider": "offline", "model": "fixture", "route_profile": "vendor_direct", "api_form": "openai_chat_completions"}


def build(rows, *, policy="any"):
    registered = HandlerRegistry()
    def handler(context, inputs):
        return copy.deepcopy(rows[context.run_index - 1])
    registered.register("stochastic", handler)
    definition = {"workflow_schema_version": 1, "id": "repeat-policy", "target": TARGET,
                  "cases": [{"id": "case", "success_policy": policy}],
                  "steps": [{"id": "probe", "case_id": "case", "handler": "stochastic"}]}
    plan = compile_plan(definition, registered, run_count=len(rows))
    job = freeze_workflow_job({"type": "param_test", **TARGET}, plan)
    return plan, registered, job


PASS = {"status": "passed", "accepted": True, "aggregation_key": "same-target-and-probe"}
SOFT = {"status": "failed", "accepted": False, "aggregate_eligible": True, "aggregation_key": "same-target-and-probe", "observation": "stochastic miss"}


def test_any_aggregates_only_group_outcome_and_preserves_raw_failure(tmp_path):
    rows = [copy.deepcopy(SOFT), copy.deepcopy(PASS)]
    plan, registered, job = build(rows)
    report = execute_plan(plan, registered, lambda *a, **kw: pytest.fail("No HTTP expected"), evidence_dir=tmp_path)
    assert report["status"] == "passed"
    assert [run["status"] for run in report["runs"]] == ["failed", "passed"]
    assert report["runs"][0]["steps"]["probe"] == SOFT
    assert report["case_outcomes"]["case"]["covered_failed_runs"] == [1]
    assert rows == [SOFT, PASS]
    assert workflow_execution_result_state(job, report) == ("frozen", [])


@pytest.mark.parametrize("change", [
    {"aggregate_eligible": False}, {"aggregation_key": "another-context"},
    {"error": {"type": "RuntimeError"}}, {"status": "blocked"}, {"status": "cancelled"},
])
def test_any_does_not_hide_hard_or_unrelated_outcomes(tmp_path, change):
    plan, registered, _ = build([{**SOFT, **change}, PASS])
    report = execute_plan(plan, registered, lambda *a, **kw: None, evidence_dir=tmp_path)
    assert report["status"] != "passed"
    assert report["case_outcomes"]["case"]["covered_failed_runs"] == []


def test_all_remains_strict_and_unknown_policy_fails_preflight(tmp_path):
    plan, registered, _ = build([SOFT, PASS], policy="all")
    report = execute_plan(plan, registered, lambda *a, **kw: None, evidence_dir=tmp_path)
    assert report["status"] == "failed"
    for invalid in ("majority", {}, True):
        with pytest.raises(PlanValidationError, match="success_policy"):
            build([PASS], policy=invalid)


@pytest.mark.parametrize("mutation", ["cleanup", "unknown", "fatal", "missing_run", "missing_step", "duplicate_run"])
def test_any_never_overrides_cleanup_fatal_or_incomplete_evidence(tmp_path, mutation):
    plan, registered, job = build([SOFT, PASS])
    report = execute_plan(plan, registered, lambda *a, **kw: None, evidence_dir=tmp_path)
    if mutation == "cleanup":
        report["runs"][0]["cleanup"]["status"] = "incomplete"
    elif mutation == "unknown":
        report["runs"][0]["cleanup"]["unknown_creations"] = [{"status": "unknown"}]
    elif mutation == "fatal":
        report["runs"][0]["fatal_reason"] = "Fatal HTTP 429"
    elif mutation == "missing_run":
        report["runs"].pop()
    elif mutation == "missing_step":
        report["runs"][0]["steps"].clear()
    else:
        report["runs"][1]["run_id"] = report["runs"][0]["run_id"]
    assert summarize_plan_runs(plan, report["runs"])["status"] != "passed"
    assert workflow_execution_result_state(job, report)[0] != "frozen"


def test_controls_recompute_case_outcomes_instead_of_trusting_claimed_aggregation(tmp_path):
    plan, registered, job = build([SOFT, PASS])
    report = execute_plan(plan, registered, lambda *a, **kw: None, evidence_dir=tmp_path)
    report["case_outcomes"]["case"]["covered_failed_runs"] = []
    assert "workflow_case_outcomes_mismatch" in workflow_execution_result_state(job, report)[1]
    report["case_outcomes"]["case"]["covered_failed_runs"] = [True]
    assert "workflow_case_outcomes_mismatch" in workflow_execution_result_state(job, report)[1]
