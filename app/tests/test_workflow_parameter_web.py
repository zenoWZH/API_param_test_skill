"""Ordinary text previews freeze sampling and retain the old MPDB/token contract."""
from __future__ import annotations

import copy
import json
import threading
from unittest.mock import Mock

import pytest
import requests

from lib.config import load_config
from lib.job_spec import load_job_spec
from lib.parameter_job_controls import freeze_workflow_job
from lib.test_runner import compile_plan
from lib.test_runner.service import preview_test_plan, registry_for_plan, validate_current_parameter_job
from scripts import web_console as web


PAYLOAD = {"type": "param_test", "provider": "deepseek_official", "model": "deepseek-v4-pro",
           "api_form": "openai_responses", "reference_contract_id": "deepseek_v4_pro_0813_responses"}


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setenv("LLM_API_TEST_PROVIDERS_LOCAL", "/tmp/workflow-parameter-no-private.yaml")
    monkeypatch.setenv("LLM_API_TEST_DISABLE_AUTH", "1")
    for key in ("LOADTEST_PROVIDER", "LOADTEST_MODEL", "LOADTEST_ROUTE_PROFILE", "LOADTEST_API_FORM",
                "LOADTEST_PARAM_PROFILES", "LOADTEST_TOOL_VALIDATION_MODE"):
        monkeypatch.delenv(key, raising=False)
    config = load_config()
    monkeypatch.setattr(web, "load_config", lambda: copy.deepcopy(config))
    monkeypatch.setattr(web, "JOBS_ROOT", tmp_path)
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("preview sent HTTP")))
    manager = web.JobManager.__new__(web.JobManager)
    manager._lock = threading.Lock()
    manager._jobs = {}
    monkeypatch.setattr(web, "JOB_MANAGER", manager)
    monkeypatch.setattr(manager, "_start_locked", Mock())
    return config, manager


def test_preview_freezes_sampling_and_full_default_before_credentials(context, monkeypatch):
    config, manager = context
    credential = Mock(side_effect=AssertionError("preview read credentials"))
    monkeypatch.setattr(web, "provider_has_api_key", credential)
    response = web.app.test_client().post("/api/test-plan/preview", json=PAYLOAD)
    assert response.status_code == 200, response.json
    preview = response.json
    assert preview["plan"]["run_count"] == 1
    assert preview["selected_cases"][0] == "identity_probe"
    assert len(preview["selected_cases"]) > 2
    assert preview["plan_seed"]
    assert preview["job_spec"]["schema_version"] == 6
    assert "model_profile_database" in preview["job_spec"]
    assert "test_workflow_snapshot" not in preview["job_spec"]
    replay = preview_test_plan(config, {**PAYLOAD, "plan_seed": preview["plan_seed"], "plan_digest": preview["plan_digest"]})
    assert replay["plan"] == preview["plan"]
    rejected = web.app.test_client().post("/api/jobs", json={**PAYLOAD, "plan_seed": preview["plan_seed"], "plan_digest": "0" * 64})
    assert rejected.status_code == 400 and "changed since preview" in rejected.json["error"]
    credential.assert_not_called()
    monkeypatch.setattr(web, "provider_has_api_key", Mock(return_value=True))
    job = manager.create({**PAYLOAD, "plan_seed": preview["plan_seed"], "plan_digest": preview["plan_digest"]})
    persisted = load_job_spec(job.report_dir / "job_spec.json")
    assert persisted["execution_plan"] == preview["plan"]
    assert persisted["result_contract"]
    manager._start_locked.assert_called_once_with(job)
    requests.Session.request.assert_not_called()


def test_subset_runs_and_frozen_rebuild_reject_tampered_payload(context):
    config, _ = context
    selected = {**PAYLOAD, "cases": ["deepseek0813_responses_basic"], "run_count": 2, "plan_seed": "fixture-seed"}
    preview = preview_test_plan(config, selected)
    assert preview["selected_cases"] == ["identity_probe", "deepseek0813_responses_basic"]
    assert preview["plan"]["run_count"] == 2 and preview["request_cap"] == 4
    validate_current_parameter_job(config, preview["job_spec"])
    definition = copy.deepcopy(preview["plan"]["definition"])
    definition["steps"][1]["inputs"]["variants"][0]["prepared_request"]["body"]["input"] = "edited after preview"
    registry = registry_for_plan(preview["plan"], config=config)
    edited = compile_plan(definition, registry, run_count=2)
    job = freeze_workflow_job(preview["job_spec"], edited)
    with pytest.raises(ValueError, match="changed since preview"):
        validate_current_parameter_job(config, job)


@pytest.mark.parametrize("extra", [{"cases": ["unregistered"]}, {"suite": "unknown"}, {"runs": 0},
                                  {"run_count": 2, "param_test_runs": 3}, {"source_id": "aws_bedrock"}])
def test_invalid_parameter_selection_fails_before_credentials(context, extra):
    config, _ = context
    with pytest.raises(ValueError):
        preview_test_plan(config, {**PAYLOAD, **extra})


def test_frozen_parameter_cli_reuses_plan_and_rejects_duplicate_business_dispatch(context, monkeypatch, tmp_path):
    from scripts import param_test
    from lib.client import ChatResult
    config, _ = context
    preview = preview_test_plan(config, {**PAYLOAD, "cases": ["deepseek0813_responses_basic"], "plan_seed": "cli-fixture"})
    job = preview["job_spec"]
    path = tmp_path / "cli"
    path.mkdir()
    (path / "job_spec.json").write_text(json.dumps(job))
    monkeypatch.setattr(param_test, "load_config", lambda: copy.deepcopy(config))
    for name, value in (("LOADTEST_JOB_SPEC", str(path / "job_spec.json")), ("LOADTEST_REPORT_DIR", str(path)),
                        ("LOADTEST_PROVIDER", job["provider"]), ("LOADTEST_MODEL", job["model"]),
                        ("LOADTEST_API_FORM", job["api_form"]), ("LOADTEST_ROUTE_PROFILE", job["route_profile"]),
                        ("LOADTEST_REFERENCE_CONTRACT_ID", job["reference_contract_id"])):
        monkeypatch.setenv(name, value)
    calls = []
    class OfflineClient:
        def openai_responses(self, body):
            calls.append(copy.deepcopy(body))
            return ChatResult(success=False, status_code=400, latency_ms=1, timestamp=0,
                              response_json={"error": {"message": "offline rejection"}}, error="offline rejection")
        def count_tokens(self, *args):
            pytest.fail("Unconfigured helper sent a request")
    factory = Mock(return_value=OfflineClient())
    monkeypatch.setattr(param_test.DeepSeekClient, "from_config", factory)
    assert param_test.main() == 1
    verdict = json.loads((path / "verdict.json").read_text())
    assert verdict["workflow_result"]["plan_digest"] == preview["plan_digest"]
    assert len(calls) == 2 and verdict["pass"] is False
    original = (path / "verdict.json").read_bytes()
    with pytest.raises(FileExistsError):
        param_test.main()
    assert (path / "verdict.json").read_bytes() == original
    assert factory.call_count == 1 and len(calls) == 2
