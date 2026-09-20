"""Frozen workflow jobs, historical controls and cleanup result integrity offline."""
from __future__ import annotations

import copy
import json

import pytest

from lib.config import load_config
from lib.job_spec import (
    classify_workflow_result, load_job_spec, make_job_spec,
)
from lib.model_profile_catalog import resolve_runtime_parameter_config
from lib.parameter_job_controls import (
    bind_parameter_execution, bound_parameter_execution, freeze_workflow_job,
    frozen_execution_plan_from_job, make_parameter_execution,
    parameter_execution_from_job, workflow_execution_from_job,
    workflow_execution_result_state, workflow_termination_grace_seconds,
)
from lib.test_runner import HandlerRegistry, compile_plan, execute_plan
from lib.test_runner.common import digest_json


def offline_handler(context, inputs):
    return {"status": "passed", "accepted": True, "value": inputs}


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    import os
    for key in tuple(os.environ):
        if key.startswith("LOADTEST_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setenv("LLM_API_TEST_PROVIDERS_LOCAL", "/tmp/workflow-no-private.yaml")


@pytest.fixture
def registry():
    result = HandlerRegistry()
    result.register("offline", offline_handler)
    return result


@pytest.fixture
def base_job():
    config = load_config()
    resolved = resolve_runtime_parameter_config(
        config, "gemini", "gemini-3.7-flash", "gemini", "google_ai_studio",
        "gemini_generate_content",
    )
    return make_job_spec(
        job_type="param_test", provider="gemini", model="gemini-3.7-flash",
        model_family="gemini", api_form="gemini_generate_content",
        route_profile="google_ai_studio", reference_contract_id="gemini_3_7_flash_generate_content",
        model_profile_database=resolved["model_profile_database"],
        workload="short", request_mode="unique", target_rpm=0, target_tpm=0,
    )


def definition(job):
    return {
        "workflow_schema_version": 1, "id": "offline-controls",
        "target": {field: job[field] for field in ("provider", "model", "route_profile", "api_form")},
        "cases": [{"id": "seed"}, {"id": "dependent"}, {"id": "independent"}],
        "steps": [
            {"id": "seed", "case_id": "seed", "handler": "offline", "inputs": {}},
            {"id": "dependent", "case_id": "dependent", "handler": "offline", "inputs": {}, "depends_on": ["seed"]},
            {"id": "independent", "case_id": "independent", "handler": "offline", "inputs": {}},
        ],
        "limits": {"max_requests": 3, "request_timeout_seconds": 30,
                   "deadline_seconds": 100, "cleanup_max_requests": 2,
                   "cleanup_deadline_seconds": 20},
    }


def frozen_job(base_job, registry, **compile_options):
    return freeze_workflow_job(base_job, compile_plan(definition(base_job), registry, **compile_options))


def successful_report(job):
    plan = job["execution_plan"]
    return {
        "report_schema_version": 1, "plan_digest": plan["plan_digest"],
        "workflow_id": plan["workflow_id"], "target": plan["target"], "status": "passed",
        "runs": [{"run_id": f"run-{index}", "run_index": index, "status": "passed",
                  "steps": {step["id"]: {"status": "passed", "accepted": True} for step in plan["ordered_steps"]},
                  "attempts": [], "request_count": 0, "business_request_count": 0, "cleanup_request_count": 0,
                  "cleanup": {"status": "passed", "resources": [], "unknown_creations": []}}
                 for index in range(1, plan["run_count"] + 1)],
    }


def test_default_full_suite_one_run_and_deep_freeze(base_job, registry):
    assert make_parameter_execution()["runs"] == 1
    assert base_job["parameter_execution"]["runs"] == 1
    assert base_job["schema_version"] == 5
    original = compile_plan(definition(base_job), registry)
    spec = freeze_workflow_job(base_job, original)
    assert spec["schema_version"] == 6
    controls = parameter_execution_from_job(spec)
    assert controls["schema_version"] == 2 and controls["runs"] == 1
    assert controls["selected_cases"] == ["seed", "dependent", "independent"]
    original["limits"]["max_requests"] = 999
    controls["selected_cases"].clear()
    assert workflow_execution_from_job(spec)["selected_cases"] == ["seed", "dependent", "independent"]
    assert frozen_execution_plan_from_job(spec)["limits"]["max_requests"] == 3


def test_selected_cases_expand_prerequisites_and_repeats_stay_frozen(base_job, registry):
    spec = frozen_job(base_job, registry, selected_cases=["dependent"], run_count=3)
    controls = workflow_execution_from_job(spec)
    assert controls["requested_cases"] == ["dependent"]
    assert controls["selected_cases"] == ["seed", "dependent"] and controls["runs"] == 3
    report = successful_report(spec)
    assert workflow_execution_result_state(spec, report) == ("frozen", [])
    report["runs"][1]["run_id"] = report["runs"][0]["run_id"]
    assert "workflow_result_run_identity_invalid" in workflow_execution_result_state(spec, report)[1]


@pytest.mark.parametrize("mutation", [
    lambda s: s["parameter_execution"].update(runs=2),
    lambda s: s["parameter_execution"].update(runs=True),
    lambda s: s["parameter_execution"]["selected_cases"].pop(),
    lambda s: s["parameter_execution"]["limits"].update(max_requests=999),
    lambda s: s["execution_plan"]["definition"]["steps"][0].update(depends_on=["missing"]),
    lambda s: s["execution_plan"].update(plan_digest="0" * 64),
    lambda s: s.update(model="other-model"),
    lambda s: s.update(type="cache_suite"),
    lambda s: s.pop("execution_plan"),
])
def test_invalid_frozen_jobs_rejected_before_use(base_job, registry, mutation):
    spec = frozen_job(base_job, registry)
    mutation(spec)
    with pytest.raises(ValueError):
        bind_parameter_execution({}, spec, {})


def test_rehash_cannot_authorize_missing_prerequisites(base_job, registry):
    spec = frozen_job(base_job, registry)
    plan = spec["execution_plan"]
    plan["definition"]["steps"][1]["depends_on"] = ["missing"]
    plan["definition_digest"] = digest_json(plan["definition"])
    plan["plan_digest"] = digest_json({k: v for k, v in plan.items() if k != "plan_digest"})
    spec["parameter_execution"]["plan_digest"] = plan["plan_digest"]
    with pytest.raises(ValueError, match="prerequisite"):
        parameter_execution_from_job(spec)


@pytest.mark.parametrize("key,value", [
    ("LOADTEST_PARAM_TEST_RUNS", "2"), ("LOADTEST_TOOL_VALIDATION_MODE", "claude_native"),
    ("LOADTEST_TEST_CASES", '["seed"]'), ("LOADTEST_TEST_PLAN_DIGEST", "changed"),
])
def test_environment_cannot_override_frozen_workflow(base_job, registry, key, value):
    spec = frozen_job(base_job, registry)
    with pytest.raises(ValueError, match="immutable parameter_execution"):
        bind_parameter_execution({}, spec, {key: value})
    config = {}
    bind_parameter_execution(config, spec, {})
    spec["parameter_execution"]["runs"] = 12
    assert bound_parameter_execution(config)["runs"] == 1


def test_v5_historical_three_runs_are_not_rewritten():
    spec = {"schema_version": 5, "type": "param_test",
            "parameter_execution": make_parameter_execution(3)}
    before = copy.deepcopy(spec)
    assert parameter_execution_from_job(spec)["runs"] == 3
    assert workflow_execution_from_job(spec) is None
    assert spec == before
    for version in (1, 2, 3, 4):
        assert parameter_execution_from_job({"schema_version": version, "type": "param_test"}) is None


def test_v6_roundtrip_and_invalid_disk_plan(base_job, registry, tmp_path):
    spec = frozen_job(base_job, registry)
    path = tmp_path / "job_spec.json"
    path.write_text(json.dumps(spec))
    assert load_job_spec(path) == spec
    spec["execution_plan"]["limits"]["cleanup_deadline_seconds"] = 0
    path.write_text(json.dumps(spec))
    with pytest.raises(RuntimeError, match="execution controls"):
        load_job_spec(path)


@pytest.mark.parametrize("job_type", ["quick_load", "cache_suite", "staircase", "soak"])
def test_pressure_jobs_cannot_acquire_workflow_controls(base_job, registry, job_type):
    base_job.update(type=job_type, schema_version=4)
    base_job.pop("parameter_execution")
    assert parameter_execution_from_job(base_job) is None
    with pytest.raises(ValueError, match="reserved"):
        freeze_workflow_job(base_job, compile_plan(definition(base_job), registry))


@pytest.mark.parametrize("mutation,reason", [
    (lambda r: r.update(status=[]), "workflow_result_status_invalid"),
    (lambda r: r["runs"][0]["cleanup"].update(resources=[{"status": "deleted", "run_id": "other-run"}]), "workflow_cleanup_incomplete"),
    (lambda r: r.update(cleanup_only=True), "workflow_cleanup_only_result"),
    (lambda r: r.update(plan_digest="0" * 64), "workflow_result_plan_digest_mismatch"),
    (lambda r: r["target"].update(model="other"), "workflow_result_target_mismatch"),
    (lambda r: r["runs"].clear(), "workflow_execution_runs_mismatch"),
    (lambda r: r["runs"][0]["cleanup"].update(status="incomplete"), "workflow_cleanup_incomplete"),
    (lambda r: r["runs"][0]["cleanup"].update(unknown_creations=[{"step_id": "seed"}]), "workflow_cleanup_incomplete"),
    (lambda r: r["runs"][0]["cleanup"].update(resources=[{"status": "cleanup_failed"}]), "workflow_cleanup_incomplete"),
    (lambda r: r["runs"][0].update(status="failed"), "workflow_result_status_inconsistent"),
    (lambda r: r["runs"][0].update(steps={}), "workflow_result_steps_incomplete"),
    (lambda r: r["runs"][0].update(business_request_count=99), "workflow_result_request_budget_exceeded"),
])
def test_mismatch_and_cleanup_failure_cannot_pass(base_job, registry, mutation, reason):
    spec = frozen_job(base_job, registry)
    report = copy.deepcopy(successful_report(spec))
    assert classify_workflow_result(spec, report)["status"] == "current_pass"
    mutation(report)
    classified = classify_workflow_result(spec, report)
    assert not classified["pass"] and not classified["tested"]
    assert reason in classified["reasons"]


def test_termination_grace_reserves_remaining_request_and_cleanup(base_job, registry):
    spec = frozen_job(base_job, registry)
    assert workflow_termination_grace_seconds(spec) == 55
    assert workflow_termination_grace_seconds(spec, 4) == 29
    assert workflow_termination_grace_seconds(spec, 0) == 25
    assert workflow_termination_grace_seconds(spec, 100) == 55
    for invalid in (True, -1, float("nan"), float("inf"), "1"):
        with pytest.raises(ValueError):
            workflow_termination_grace_seconds(spec, invalid)


def test_real_executor_report_satisfies_job_controls(base_job, registry, tmp_path):
    spec = frozen_job(base_job, registry, run_count=2)
    def dispatch(request, **kwargs):
        raise AssertionError("Offline handler must not dispatch")
    report = execute_plan(spec["execution_plan"], registry, dispatch, evidence_dir=tmp_path)
    assert workflow_execution_result_state(spec, report) == ("frozen", [])
    assert classify_workflow_result(spec, report)["pass"] is True
    assert report["runs"][0]["run_id"] != report["runs"][1]["run_id"]


def test_make_job_spec_freezes_plan_and_rejects_conflicting_runs(base_job, registry):
    plan = compile_plan(definition(base_job), registry, run_count=2)
    options = {field: base_job[field] for field in (
        "provider", "model", "model_family", "api_form", "route_profile",
        "reference_contract_id", "model_profile_database", "workload", "request_mode",
        "target_rpm", "target_tpm",
    )}
    spec = make_job_spec(job_type="param_test", execution_plan=plan, **options)
    assert spec["schema_version"] == 6 and parameter_execution_from_job(spec)["runs"] == 2
    with pytest.raises(ValueError, match="conflicts"):
        make_job_spec(job_type="param_test", execution_plan=plan, param_test_runs=1, **options)
    pressure = make_job_spec(job_type="quick_load", **options)
    assert pressure["schema_version"] == 4 and "parameter_execution" not in pressure


def test_catalog_nested_target_binds_reference_and_runtime_identity(base_job, registry):
    workflow = definition(base_job)
    workflow["target"] = {
        "source_id": base_job["source_id"], "profile_id": base_job["profile_id"],
        "interface_id": base_job["interface_id"], "contract_id": base_job["reference_contract_id"],
        "execution_target": {"provider_id": base_job["provider"], "request_model_id": base_job["model"],
                             "api_form": base_job["api_form"], "route_profile": base_job["route_profile"]},
    }
    plan = compile_plan(workflow, registry)
    assert workflow_execution_from_job(freeze_workflow_job(base_job, plan))["runs"] == 1
    for path, value in (("provider_id", "other-provider"), ("route_profile", "other-route")):
        changed = copy.deepcopy(workflow)
        changed["target"]["execution_target"][path] = value
        with pytest.raises(ValueError, match="target conflicts"):
            freeze_workflow_job(base_job, compile_plan(changed, registry))
    changed = copy.deepcopy(workflow)
    changed["target"]["contract_id"] = "other-contract"
    with pytest.raises(ValueError, match="reference_contract_id"):
        freeze_workflow_job(base_job, compile_plan(changed, registry))


@pytest.fixture
def workflow_snapshot_job(registry):
    """Independent exact-workflow snapshot over a preserved disabled reference."""
    from lib.model_profile_catalog import get_model_profile_catalog
    from lib.test_runner.snapshot import make_workflow_snapshot, validate_workflow_snapshot
    catalog = get_model_profile_catalog()
    profile_id = "text/anthropic/claude_fable/claude-fable-5-1"
    interface_id = profile_id + "#anthropic-messages-default"
    contract_id = "claude_fable_5_1_native_messages"
    workflow_id = "offline-workflow-reference"
    binding_id = "workflow/" + workflow_id
    execution = {"provider_id": "anthropic_official", "request_model_id": "claude-fable-5-1",
                 "api_form": "anthropic_messages", "transport_adapter_id": "claude_messages"}
    target = {"source_id": "anthropic", "profile_id": profile_id, "interface_id": interface_id,
              "contract_id": contract_id, "test_binding_id": binding_id, "route_profile": "vendor_direct",
              "execution_target": execution}
    factory = {"factory_id": "fable_research", "version": "1", "source_sha256": "a" * 64}
    workflow = definition({"provider": "anthropic_official", "model": "claude-fable-5-1",
                           "route_profile": "vendor_direct", "api_form": "anthropic_messages"})
    workflow.update(id=workflow_id, target=target, factory=factory)
    resolved = {
        "test_binding_id": binding_id, "extension_type": "test_workflow", "workflow_schema_version": 1,
        "workflow_id": workflow_id, "source_id": "anthropic", "profile_id": profile_id,
        "interface_id": interface_id, "contract_id": contract_id,
        "provenance_record_id": "test-binding/" + binding_id, "enabled": True,
        "execution_target": execution,
        "execution_permission": {"scope": "exact_workflow", "mode": "scoped_research", "approval_id": "offline-fixture"},
        "workflow": {**factory, "case_ids": [row["id"] for row in workflow["cases"]]},
        "reference": {"source": catalog.get_source("anthropic"), "profile": catalog.get_profile(profile_id),
                      "interface": catalog.get_interface(interface_id), "contract": catalog.get_contract(contract_id)},
    }
    plan = compile_plan(workflow, registry)
    snapshot = make_workflow_snapshot(resolved, catalog.database_info(), plan)
    identity = validate_workflow_snapshot(snapshot)
    job = {key: value for key, value in identity.items()
           if key not in {"workflow_snapshot", "resolution_status", "legacy_unresolved"}}
    return freeze_workflow_job({**job, "type": "param_test", "test_workflow_snapshot": snapshot}, plan)


def test_workflow_snapshot_roundtrip_preserves_disabled_reference(workflow_snapshot_job, tmp_path, monkeypatch):
    from lib import model_profile_catalog
    from lib.job_spec import resolve_job_model_profile_snapshot
    spec = workflow_snapshot_job
    reference = spec["test_workflow_snapshot"]["reference"]["interface"]
    assert reference["enabled"] is False
    monkeypatch.setattr(model_profile_catalog, "binding_from_database_snapshot", lambda *args, **kwargs: pytest.fail("consulted old executable policy"))
    path = tmp_path / "workflow-job.json"
    path.write_text(json.dumps(spec))
    assert load_job_spec(path) == spec
    restored = resolve_job_model_profile_snapshot(spec)
    assert restored["resolution_status"] == "snapshot" and restored["source_id"] == "anthropic"
    assert reference["enabled"] is False
    assert "model_profile_database" not in spec
    assert classify_workflow_result(spec, successful_report(spec))["pass"] is True


def test_workflow_snapshot_tamper_rejected(workflow_snapshot_job, tmp_path):
    spec = copy.deepcopy(workflow_snapshot_job)
    spec["test_workflow_snapshot"]["binding"]["execution_target"]["provider_id"] = "other-provider"
    path = tmp_path / "workflow-job.json"
    path.write_text(json.dumps(spec))
    with pytest.raises(RuntimeError, match="workflow snapshot"):
        load_job_spec(path)
    assert not classify_workflow_result(spec, successful_report(spec))["pass"]
