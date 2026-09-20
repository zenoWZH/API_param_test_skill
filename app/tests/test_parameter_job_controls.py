"""Exercise persisted Web jobs, CLI preflight, and historical result controls offline."""
from __future__ import annotations
import copy
import json
import os
from unittest.mock import Mock

import pytest
from lib.config import load_config
from lib.job_spec import load_job_spec, classify_parameter_result
from lib.parameter_job_controls import (
    bind_parameter_execution, make_parameter_execution, parameter_execution_from_job,
)
from scripts import param_test as cli
from scripts import web_console as web

START_JOB = web.JobManager._start_locked
RESTORE_JOBS = web.JobManager._load_finished_jobs


def controls_spec(runs=7, mode="gemini_native"):
    return {"schema_version": 5, "type": "param_test",
            "parameter_execution": make_parameter_execution(runs, mode)}


@pytest.mark.parametrize("runs", [True, False, 0, -1, 1001, 1.5, "3", None])
def test_reject_invalid_snapshot_run_counts(runs):
    spec = controls_spec()
    spec["parameter_execution"]["runs"] = runs
    with pytest.raises(ValueError, match="runs must"):
        parameter_execution_from_job(spec)


@pytest.mark.parametrize("mutation", [
    lambda s: s.pop("parameter_execution"),
    lambda s: s.update(type="image_param_test"),
    lambda s: s.update(type="cache_suite"),
    lambda s: s["parameter_execution"].pop("tool_validation_mode"),
    lambda s: s["parameter_execution"].update(tool_validation_mode="unknown"),
    lambda s: s["parameter_execution"].update(schema_version=True),
    lambda s: s["parameter_execution"].update(schema_version=2),
    lambda s: s["parameter_execution"].update(extra=1),
    lambda s: s.update(parameter_suite="fixed_suite"),
    lambda s: s.update(api_form="deepseek_beta_chat_prefix"),
])
def test_reject_incomplete_or_conflicting_control_schema(mutation):
    spec=controls_spec();mutation(spec)
    with pytest.raises(ValueError):
        parameter_execution_from_job(spec)


@pytest.mark.parametrize("schema", [1, 2, 3, 4])
def test_legacy_control_values_are_not_invented(schema):
    spec={"schema_version": schema, "type": "param_test"}
    before=copy.deepcopy(spec)
    config={}
    bind_parameter_execution(config, controls_spec(), {})
    assert cli._param_test_runs(config)==7
    bind_parameter_execution(config, spec, {})
    assert parameter_execution_from_job(spec) is None
    assert "_parameter_execution_controls" not in config
    assert spec==before


@pytest.fixture
def context(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("LOADTEST_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setenv("LLM_API_TEST_PROVIDERS_LOCAL", "/tmp/approved-no-private.yaml")
    monkeypatch.setenv("LLM_API_TEST_DISABLE_AUTH", "1")
    config=load_config()
    monkeypatch.setattr(web, "load_config", lambda: copy.deepcopy(config))
    monkeypatch.setattr(cli, "load_config", lambda: copy.deepcopy(config))
    monkeypatch.setattr(web, "JOBS_ROOT", tmp_path/"jobs")
    monkeypatch.setattr(web, "REPORTS_ROOT", tmp_path)
    monkeypatch.setattr(web, "provider_has_api_key", lambda *a: True)
    monkeypatch.setattr(web.JobManager, "_load_finished_jobs", lambda *a: None)
    monkeypatch.setattr(web.JobManager, "_start_locked", lambda *a: None)
    manager=web.JobManager()
    monkeypatch.setattr(web, "JOB_MANAGER", manager)
    response=web.app.test_client().post("/api/jobs", json={
        "type": "param_test", "provider": "gemini", "model": "gemini-3.7-flash",
        "route_profile": "google_ai_studio", "api_form": "gemini_generate_content",
        "reference_contract_id": "gemini_3_7_flash_generate_content",
        "param_test_runs": 7, "tool_validation_mode": "gemini_native",
    })
    assert response.status_code==201, response.json
    job=manager._jobs[response.json["id"]]
    monkeypatch.setenv("LOADTEST_PROVIDER", job.provider)
    monkeypatch.setenv("LOADTEST_MODEL", job.model)
    monkeypatch.setenv("LOADTEST_ROUTE_PROFILE", job.route_profile)
    monkeypatch.setenv("LOADTEST_API_FORM", job.api_form)
    monkeypatch.setenv("LOADTEST_REFERENCE_CONTRACT_ID", job.reference_source)
    monkeypatch.setenv("LOADTEST_REFERENCE_SOURCE", job.reference_source)
    monkeypatch.setenv("LOADTEST_JOB_SPEC", str(job.report_dir/"job_spec.json"))
    monkeypatch.setenv("LOADTEST_REPORT_DIR", str(job.report_dir))
    # Any test reaching an unmocked client boundary must stop before real I/O.
    monkeypatch.setattr(cli.DeepSeekClient, "from_config", Mock(side_effect=AssertionError("unexpected client construction")))
    return manager, job


def test_web_freezes_selected_controls_on_disk(context):
    _, job=context
    spec=load_job_spec(job.report_dir/"job_spec.json")
    assert spec["schema_version"]==6
    controls=parameter_execution_from_job(spec)
    assert controls["schema_version"]==2 and controls["runs"]==7 and controls["tool_validation_mode"]=="gemini_native"
    assert controls["plan_digest"]==spec["execution_plan"]["plan_digest"]


@pytest.mark.parametrize("key,value", [
    ("LOADTEST_PARAM_TEST_RUNS", "8"), ("LOADTEST_PARAM_TEST_RUNS", "invalid"),
    ("LOADTEST_PARAM_TEST_RUNS", "10001"), ("LOADTEST_TOOL_VALIDATION_MODE", "auto"),
])
def test_cli_conflicting_environment_stops_before_client(context,monkeypatch,key,value):
    monkeypatch.setenv(key,value)
    with pytest.raises(ValueError,match="conflicts with immutable parameter_execution"):
        cli.main()
    cli.DeepSeekClient.from_config.assert_not_called()


def test_cli_uses_snapshot_controls_after_environment_changes(context,monkeypatch):
    _,job=context
    calls=[]
    monkeypatch.setattr(cli.DeepSeekClient,"from_config",lambda *a:object())
    def probe(config,*args):
        monkeypatch.setenv("LOADTEST_PARAM_TEST_RUNS","99")
        monkeypatch.setenv("LOADTEST_TOOL_VALIDATION_MODE","auto")
        return None
    def matrix(config,client,provider,model,family,reference_source,reference_family,runs,output_dir,**kwargs):
        calls.append((runs,cli._param_test_runs(config),cli._tool_validation_mode(config)))
        return []
    monkeypatch.setattr(cli,"run_identity_probe",probe)
    monkeypatch.setattr(cli,"run_param_tests",matrix)
    cli.main()
    assert calls==[(7,7,"gemini_native")]
    result=json.loads((job.report_dir/"verdict.json").read_text())
    assert result["param_test_runs"]==7 and result["tool_validation_mode"]=="gemini_native"


def test_launcher_uses_persisted_controls_despite_mutated_job(context,monkeypatch):
    manager,job=context
    job.param_test_runs=99;job.tool_validation_mode="auto"
    job.job_spec["parameter_execution"]["runs"]=88
    monkeypatch.setattr(web,"build_provider_child_env",lambda config,provider,env:dict(env))
    popen=Mock(return_value=Mock(pid=123456))
    monkeypatch.setattr(web.subprocess,"Popen",popen)
    monkeypatch.setattr(web.threading,"Thread",Mock())
    START_JOB(manager,job)
    env=popen.call_args.kwargs["env"]
    assert env["LOADTEST_PARAM_TEST_RUNS"]=="7"
    assert env["LOADTEST_TOOL_VALIDATION_MODE"]=="gemini_native"
    assert job.param_test_runs==7 and job.tool_validation_mode=="gemini_native"


def test_incomplete_disk_snapshot_prevents_process_start(context,monkeypatch):
    manager,job=context
    path=job.report_dir/"job_spec.json";spec=json.loads(path.read_text())
    spec.pop("parameter_execution");path.write_text(json.dumps(spec))
    monkeypatch.setattr(web,"build_provider_child_env",lambda config,provider,env:dict(env))
    popen=Mock();monkeypatch.setattr(web.subprocess,"Popen",popen)
    with pytest.raises(RuntimeError,match="parameter execution controls"):
        START_JOB(manager,job)
    popen.assert_not_called()


def complete_result(spec):
    result={key:copy.deepcopy(spec[key]) for key in (
        "provider","model","route_profile","api_form","source_id","profile_id",
        "interface_id","test_binding_id","reference_contract_id","model_profile_database")}
    result.update({"pass":True,"token_validation_pass":True,
        "param_test_runs":7,"tool_validation_mode":"gemini_native",
        "token_audit_summary":{"schema_version":4,"validation_status":"pass","pass":True,
            "exchange_count":1,"required_exchange_count":1,"validated_exchange_count":1,
            "validation_failure_count":0,"missing_audit_result_count":0,"invalid_audit_result_count":0,
            "missing_usage_count":0,"gross_check_count":1,"gross_failure_count":0,"gross_partial_count":0,
            "completion_check_count":1,"completion_failure_count":0,"completion_unverified_count":0,
            "arithmetic_check_count":1,"arithmetic_failure_count":0}})
    if spec.get("schema_version")==6:
        from test_workflow_job_controls import successful_report
        from lib.test_runner import summarize_plan_runs
        report=successful_report(spec)
        report.update(summarize_plan_runs(spec["execution_plan"], report["runs"]))
        result["workflow_result"]=report
    return result


def test_current_history_requires_matching_controls(context):
    _,job=context;spec=load_job_spec(job.report_dir/"job_spec.json")
    result=complete_result(spec)
    assert classify_parameter_result(spec,result)["status"]=="current_pass"
    for key,value in (("param_test_runs",3),("param_test_runs",True),("tool_validation_mode","auto")):
        bad={**result,key:value}
        classified=classify_parameter_result(spec,bad)
        assert classified["status"]=="parameter_execution_mismatch" and not classified["tested"]
    legacy=copy.deepcopy(spec);legacy["schema_version"]=4;legacy.pop("parameter_execution");legacy.pop("execution_plan",None)
    old=classify_parameter_result(legacy,result)
    assert old["status"]=="current_pass" and old["parameter_execution_status"]=="legacy_unfrozen"


def test_history_restores_snapshot_selection_and_flags_conflicting_result(context):
    manager,job=context;spec=load_job_spec(job.report_dir/"job_spec.json")
    bad=complete_result(spec);bad.update(param_test_runs=99,tool_validation_mode="auto")
    (job.report_dir/"verdict.json").write_text(json.dumps(bad))
    manager._jobs.clear()
    RESTORE_JOBS(manager)
    restored=manager._jobs[job.id]
    assert restored.param_test_runs==7 and restored.tool_validation_mode=="gemini_native"
    assert classify_parameter_result(restored.job_spec,bad)["status"]=="parameter_execution_mismatch"


def test_formal_profile_validation_receives_frozen_mode(monkeypatch):
    import hashlib
    from pathlib import Path
    from types import SimpleNamespace
    from lib.client import OpenAICompatibleClient
    root=Path(__file__).resolve().parents[1]
    repo=root.parent if root.name=="app" else root
    batch=repo/"reports/approved_live_20260907/anthropic_compat_sampling_20260909T120825Z_f2b1a3b0"
    if not (batch/"request_package.json").exists():
        pytest.skip("P1M sampling originals unavailable")
    case=json.loads((batch/"request_package.json").read_bytes())["cases"][0]
    raw=(batch/"case_01_response.bin").read_bytes()
    record=json.loads((batch/"case_01_observation.json").read_bytes())
    assert hashlib.sha256(raw).hexdigest()==record["response_sha256"]
    config=load_config();config["active_provider"]="anthropic_official"
    config["providers"]["anthropic_official"]["models"]["default_routes"][case["model"]]="vendor_compat"
    config["compatibility_profiles"]["claude_temperature"]["max_tokens"]=4096
    for key in ("LOADTEST_PROVIDER","LOADTEST_MODEL","LOADTEST_API_FORM","LOADTEST_ROUTE_PROFILE"):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY","offline-controls-replay")
    bind_parameter_execution(config,controls_spec(1,"openai_compat"),{})
    monkeypatch.setenv("LOADTEST_TOOL_VALIDATION_MODE","gemini_native")
    client=OpenAICompatibleClient.from_config(config,provider="anthropic_official")
    client.session.post=Mock(return_value=SimpleNamespace(status_code=record["status_code"],headers={},
        content=raw,text=raw.decode(),json=lambda:json.loads(raw)))
    client.count_tokens=Mock(return_value=None)
    validation=Mock(wraps=cli.validate_profile_response)
    monkeypatch.setattr(cli,"validate_profile_response",validation)
    result=cli.run_one_profile(config,client,"anthropic_official",case["model"],"claude",
        "claude_openai_compat","claude","claude_temperature",1,
        {"id":"offline","prompt":case["body"]["messages"][0]["content"]})
    assert result["pass"] is True
    assert validation.call_args.kwargs["tool_validation_mode"]=="openai_compat"
    assert client.session.post.call_count==1
    assert client.session.post.call_args.kwargs["json"]==case["body"]
