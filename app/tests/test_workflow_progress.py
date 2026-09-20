"""Frozen workflow progress uses owned evidence and exposes no protocol payloads."""
from __future__ import annotations

import copy
import json

import pytest

from lib.parameter_job_controls import freeze_workflow_job
from lib.test_runner import HandlerRegistry, compile_plan, execute_plan
from lib.test_runner.common import digest_json
from lib.test_runner.ledger import ResourceLedger
from scripts import web_console as web
from test_workflow_job_controls import (
    base_job, registry, isolated_config, definition, frozen_job, successful_report,
)


def make_job(spec, directory, status="running"):
    (directory / "job_spec.json").write_text(json.dumps(spec))
    return web.Job(
        id="progress-job", type=spec["type"], provider=spec["provider"],
        provider_label="Offline", model=spec["model"], model_family="gemini",
        workload="short", users=None, spawn_rate=None, duration=None,
        report_dir=directory, command=[], job_spec=copy.deepcopy(spec), status=status,
    )


def progress(job, verdict=None):
    return web._job_progress(job, None, verdict, [{"status": "pass"}] * 100, None)


def ledger_for(job, run_id="run-1", index=1, complete=False):
    ledger = ResourceLedger(job.report_dir / "workflow_runs", run_id=run_id,
                            plan=job.job_spec["execution_plan"], run_index=index)
    if complete:
        ledger.state.update(status="passed", phase="complete", steps={
            step["id"]: {"status": "passed", "accepted": True}
            for step in job.job_spec["execution_plan"]["ordered_steps"]})
        ledger.save()
    ledger.close()
    return ledger


def persist(ledger, mutation, *, checksum=True):
    value = json.loads(ledger.path.read_text())
    mutation(value)
    if checksum:
        value.pop("ledger_digest", None)
        value["ledger_digest"] = digest_json(value)
    ledger.path.write_text(json.dumps(value))


def test_totals_expand_dependencies_and_repeat_frozen_cases(base_job, registry, tmp_path, monkeypatch):
    spec = frozen_job(base_job, registry, selected_cases=["dependent"], run_count=3)
    job = make_job(spec, tmp_path, "queued")
    job.param_test_runs = 99
    job.job_spec["execution_plan"]["run_count"] = 88
    monkeypatch.setattr(web, "_reference_profile_count", lambda *args: pytest.fail("legacy reference counts used"))
    result = progress(job)
    assert result["workflow"] and result["total_runs"] == 3
    assert result["requested_cases"] == ["dependent"]
    assert result["selected_cases"] == ["seed", "dependent"]
    assert result["total_cases"] == result["total_steps"] == 6
    assert result["completed_cases"] == result["completed_steps"] == 0
    assert result["percent"] == 0 and result["evidence_status"] == "pending"
    assert "reference cells" not in result["label"]


def probe_handler(context, inputs):
    context.dispatch({"body": "private request body", "Authorization": "secret-key"},
                     creates_resource=bool(inputs.get("create")))
    if inputs.get("create"):
        context.register_resource("owned-resource", cleanup_request={"body": "private delete body"})
    return {"status": "passed", "accepted": True, "body": "private handler body"}


def test_real_executor_progress_tracks_steps_attempts_cleanup_and_multiple_runs(base_job, tmp_path):
    handlers = HandlerRegistry()
    handlers.register("probe", probe_handler)
    workflow = definition(base_job)
    workflow["cases"] = [{"id": "compound"}]
    workflow["steps"] = [
        {"id": "create", "case_id": "compound", "handler": "probe", "inputs": {"create": True}},
        {"id": "check", "case_id": "compound", "handler": "probe", "depends_on": ["create"]},
    ]
    spec = freeze_workflow_job(base_job, compile_plan(workflow, handlers, run_count=2))
    job = make_job(spec, tmp_path)
    observations = []
    def dispatch(request, *, timeout, context):
        observations.append(progress(job))
        return {"status_code": 200, "body": "private response body", "deleted": context.phase == "cleanup"}
    report = execute_plan(spec["execution_plan"], handlers, dispatch,
                          evidence_dir=tmp_path / "workflow_runs")
    first, second, cleanup = observations[:3]
    assert first["current_step"] == "create" and first["current_case"] == "compound"
    assert first["attempt_count"] == 1 and first["attempt_counts"]["dispatching"] == 1
    assert first["step_counts"]["running"] == 1
    assert second["completed_steps"] == 1 and second["completed_cases"] == 0
    assert second["runs"][0]["attempts"][0]["http_status"] == 200
    assert cleanup["completed_steps"] == 2 and cleanup["completed_cases"] == 1
    assert cleanup["cleanup_status"] == "running" and cleanup["cleanup_request_count"] == 1
    assert not cleanup["pass"]
    job.status = "completed"
    result = progress(job, {"workflow_result": report})
    assert result["pass"] and result["status"] == "passed" and result["percent"] == 100
    assert result["completed_runs"] == result["completed_cases"] == 2
    assert result["completed_steps"] == 4 and result["attempt_count"] == 6
    assert result["business_request_count"] == 4 and result["cleanup_request_count"] == 2
    assert result["cleanup_status"] == "passed" and result["deleted_resource_count"] == 2
    public = json.dumps(observations + [result])
    assert all(secret not in public for secret in ("private", "secret-key", "Authorization", "receipt", "inputs"))


def test_blocked_and_failed_steps_are_distinct_from_success(base_job, registry, tmp_path):
    job = make_job(frozen_job(base_job, registry), tmp_path)
    ledger = ledger_for(job)
    ledger.state["steps"] = {
        "seed": {"status": "failed", "accepted": False, "error": {"message": "secret-key"}},
        "dependent": {"status": "blocked", "accepted": False, "blocked_dependencies": ["seed"]},
    }
    ledger.save()
    result = progress(job)
    assert result["completed_steps"] == result["completed_cases"] == 2
    assert result["failed_steps"] == result["blocked_steps"] == 1
    assert result["failed_cases"] == result["blocked_cases"] == 1
    assert result["passed_steps"] == 0 and result["percent"] == 66
    assert result["runs"][0]["steps"][1]["blocked_dependencies"] == ["seed"]
    assert "secret-key" not in json.dumps(result)


def test_unknown_creation_survives_transport_failure_and_final_result(base_job, tmp_path):
    handlers = HandlerRegistry()
    handlers.register("probe", probe_handler)
    workflow = definition(base_job)
    workflow["cases"] = [{"id": "seed"}]
    workflow["steps"] = [{"id": "seed", "case_id": "seed", "handler": "probe", "inputs": {"create": True}}]
    spec = freeze_workflow_job(base_job, compile_plan(workflow, handlers))
    job = make_job(spec, tmp_path, "completed")
    def dispatch(*args, **kwargs):
        raise TimeoutError("secret-key")
    report = execute_plan(spec["execution_plan"], handlers, dispatch,
                          evidence_dir=tmp_path / "workflow_runs")
    result = progress(job, {"workflow_result": report})
    assert result["unknown_creation_count"] == 1 and result["attempt_counts"]["exception"] == 1
    assert result["cleanup_status"] == result["status"] == "incomplete" and not result["pass"]
    assert result["evidence_status"] == "verified"
    assert "secret-key" not in json.dumps(result)


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(plan_digest="foreign-plan"),
    lambda value: value["target"].update(model="foreign-model"),
    lambda value: value.update(run_id="foreign-run"),
    lambda value: value.update(run_index=0),
    lambda value: value.update(run_index=2),
    lambda value: value.update(run_index=True),
    lambda value: value.update(ledger_schema_version=True),
    lambda value: value.update(steps={"foreign-step": {"status": "passed", "accepted": True}}),
    lambda value: value["steps"]["seed"].update(status="pending"),
    lambda value: value["steps"]["seed"].update(accepted=1),
    lambda value: value.update(resources=[{"status": "deleted", "run_id": "foreign-run", "step_id": "seed"}]),
    lambda value: value.update(attempts=[{"status": "received", "id": "foreign-attempt", "step_id": "seed", "phase": "business"}]),
    lambda value: value.update(attempts=[{"status": "dispatching", "id": "run-1:attempt:1", "step_id": "seed", "phase": "business"}]),
])
def test_valid_checksum_does_not_override_ownership_or_structure(base_job, registry, tmp_path, mutation):
    job = make_job(frozen_job(base_job, registry), tmp_path, "completed")
    ledger = ledger_for(job, complete=True)
    persist(ledger, mutation)
    result = progress(job)
    assert not result["pass"] and result["evidence_status"] == "invalid"
    assert result["completed_steps"] == result["completed_cases"] == 0
    assert result["evidence_errors"] == ["ledger_invalid_or_unowned"]
    assert "invalid" in result["detail"]


def test_invalid_digest_cannot_be_masked_by_successful_final_report(base_job, registry, tmp_path):
    spec = frozen_job(base_job, registry)
    job = make_job(spec, tmp_path, "completed")
    ledger = ledger_for(job, complete=True)
    persist(ledger, lambda value: value.update(events=[{"name": "tampered"}]), checksum=False)
    result = progress(job, {"workflow_result": successful_report(spec)})
    assert result["status"] == "invalid" and not result["pass"]
    assert result["cleanup_status"] != "passed"


def test_duplicate_run_indices_are_not_double_counted(base_job, registry, tmp_path):
    job = make_job(frozen_job(base_job, registry), tmp_path, "completed")
    ledger_for(job, "first-run", complete=True)
    ledger_for(job, "second-run", complete=True)
    result = progress(job)
    assert not result["pass"] and result["evidence_status"] == "invalid"
    assert result["completed_steps"] == result["completed_runs"] == 0


@pytest.mark.parametrize("status", ["stopping", "stopped"])
def test_stop_labels_and_unknown_cleanup_are_retained(base_job, registry, tmp_path, status):
    job = make_job(frozen_job(base_job, registry), tmp_path, status)
    ledger = ledger_for(job, complete=True)
    ledger.state["resources"] = [{"run_id": "run-1", "step_id": "seed", "status": "cleanup_unknown"}]
    ledger.state["status"] = "cancelled"
    ledger.save()
    result = progress(job)
    assert result["status"] == status and status in result["label"]
    assert result["cleanup_status"] == "incomplete" and not result["pass"]
    assert result["cleanup_failed_resource_count"] == 1


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(status="incomplete"),
    lambda value: value.update(resources=[{"run_id": "run-1", "step_id": "seed", "status": "cleanup_failed"}]),
])
def test_final_report_cleanup_failure_cannot_be_green(base_job, registry, tmp_path, mutation):
    spec = frozen_job(base_job, registry)
    job = make_job(spec, tmp_path, "completed")
    report = successful_report(spec)
    mutation(report["runs"][0]["cleanup"])
    result = progress(job, {"workflow_result": report})
    assert result["completed_steps"] == result["total_steps"]
    assert result["cleanup_status"] == result["status"] == "incomplete"
    assert not result["pass"]


def test_final_report_without_ledgers_supports_image_workflows(base_job, registry, tmp_path):
    spec = frozen_job(base_job, registry, run_count=2)
    spec["type"] = "image_param_test"
    job = make_job(spec, tmp_path, "completed")
    (tmp_path / "summary.json").write_text(json.dumps({"workflow_result": successful_report(spec)}))
    result = progress(job)
    assert result["pass"] and result["completed_runs"] == 2
    assert result["total_cases"] == 6 and result["completed_steps"] == 6


def test_cancelled_final_report_preserves_partial_run_count(base_job, registry, tmp_path):
    spec = frozen_job(base_job, registry, run_count=3)
    job = make_job(spec, tmp_path, "stopped")
    report = successful_report(spec)
    report.update(status="cancelled", runs=report["runs"][:1])
    report["runs"][0]["status"] = "cancelled"
    result = progress(job, {"workflow_result": report})
    assert result["evidence_status"] == "verified" and result["completed_runs"] == 1
    assert result["total_runs"] == 3 and result["percent"] == 33
    assert result["cleanup_status"] == "passed" and not result["pass"]


@pytest.mark.parametrize("bad_report", [[], {"runs": [None]}, {"status": "passed"}])
def test_malformed_final_evidence_stays_readable(base_job, registry, tmp_path, bad_report):
    job = make_job(frozen_job(base_job, registry), tmp_path, "completed")
    result = progress(job, {"workflow_result": bad_report})
    assert result["evidence_status"] == "invalid" and not result["pass"]
    assert result["completed_steps"] == 0


def test_symlinked_evidence_is_ignored(base_job, registry, tmp_path):
    job = make_job(frozen_job(base_job, registry), tmp_path, "completed")
    ledger = ledger_for(job, complete=True)
    actual = tmp_path / "outside.json"
    ledger.path.rename(actual)
    ledger.path.symlink_to(actual)
    result = progress(job)
    assert not result["pass"] and result["completed_steps"] == 0
    assert result["evidence_status"] == "invalid"


def test_cleanup_recovery_does_not_certify_workflow_success(base_job, registry, tmp_path):
    job = make_job(frozen_job(base_job, registry), tmp_path, "completed")
    ledger = ledger_for(job, complete=True)
    persist(ledger, lambda value: value.update(recovery_count=1))
    result = progress(job)
    assert result["cleanup_status"] == "passed"
    assert result["status"] == "incomplete" and not result["pass"]


def test_final_report_cannot_replace_another_run_identity(base_job, registry, tmp_path):
    spec = frozen_job(base_job, registry)
    job = make_job(spec, tmp_path, "completed")
    ledger_for(job, "owned-run", complete=True)
    result = progress(job, {"workflow_result": successful_report(spec)})
    assert result["evidence_errors"] == ["final_result_run_identity_conflict"]
    assert result["completed_steps"] == 0 and not result["pass"]


def test_missing_and_corrupt_evidence_do_not_invent_completion(base_job, registry, tmp_path):
    job = make_job(frozen_job(base_job, registry), tmp_path, "completed")
    result = progress(job)
    assert result["status"] == "incomplete" and result["percent"] == 0
    assert result["cleanup_status"] == "unknown" and not result["pass"]
    (tmp_path / "job_spec.json").write_text('{"corrupt":')
    result = progress(job)
    assert result["status"] == "invalid" and result["evidence_errors"] == ["frozen_job_spec_invalid"]


def test_nonworkflow_progress_retains_legacy_behavior(base_job, tmp_path):
    job = make_job(base_job, tmp_path, "completed")
    assert web._workflow_job_progress(job) is None
    result = progress(job)
    assert result["percent"] == 100 and result["label"] == "completed"
    assert "workflow" not in result


def test_frozen_any_policy_reports_aggregate_without_rewriting_failed_runs(base_job, registry, tmp_path):
    from lib.test_runner import summarize_plan_runs
    workflow = definition(base_job)
    workflow["cases"] = [{"id": "seed", "success_policy": "any"}]
    workflow["steps"] = [workflow["steps"][0]]
    spec = freeze_workflow_job(base_job, compile_plan(workflow, registry, run_count=2))
    job = make_job(spec, tmp_path, "completed")
    report = successful_report(spec)
    report["runs"][0]["status"] = "failed"
    report["runs"][0]["steps"]["seed"] = {"status": "failed", "accepted": False,
        "aggregate_eligible": True, "aggregation_key": "reviewed-same-input"}
    report["runs"][1]["steps"]["seed"]["aggregation_key"] = "reviewed-same-input"
    report.update(summarize_plan_runs(spec["execution_plan"], report["runs"]))
    result = progress(job, {"workflow_result": report})
    assert result["pass"] is True and result["status"] == "passed"
    assert result["failed_cases"] == result["failed_steps"] == 1
    assert result["runs"][0]["status"] == "failed"
    assert result["case_outcomes"]["seed"]["covered_failed_runs"] == [1]
    assert result["case_policies"] == {"seed": "any"}
    assert "aggregation_key" not in json.dumps(result)
    report["runs"][0]["fatal_reason"] = "private auth failure"
    report.update(summarize_plan_runs(spec["execution_plan"], report["runs"]))
    failed = progress(job, {"workflow_result": report})
    assert not failed["pass"] and failed["status"] == "failed"
    assert "private auth" not in json.dumps(failed)


def test_any_policy_is_recomputed_from_owned_ledgers(base_job, registry, tmp_path):
    workflow = definition(base_job)
    workflow["cases"] = [{"id": "seed", "success_policy": "any"}]
    workflow["steps"] = [workflow["steps"][0]]
    spec = freeze_workflow_job(base_job, compile_plan(workflow, registry, run_count=2))
    job = make_job(spec, tmp_path, "completed")
    first = ledger_for(job, "run-1", 1, complete=True)
    second = ledger_for(job, "run-2", 2, complete=True)
    persist(first, lambda raw: raw.update(status="failed", steps={"seed": {
        "status": "failed", "accepted": False, "aggregate_eligible": True, "aggregation_key": "same"}}))
    persist(second, lambda raw: raw["steps"]["seed"].update(aggregation_key="same"))
    assert progress(job)["pass"] is True
    persist(first, lambda raw: raw.update(fatal_reason="fatal"))
    assert not progress(job)["pass"]
    persist(first, lambda raw: raw.update(fatal_reason=None))
    first.path.unlink()
    incomplete = progress(job)
    assert not incomplete["pass"]
    assert incomplete["case_outcomes"]["seed"]["passed_runs"] == [2]
    assert incomplete["case_outcomes"]["seed"]["missing_runs"] == [1]
