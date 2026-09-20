"""Image preview and Web creation share one frozen plan without losing legacy policy gates."""
from __future__ import annotations

import copy
import json
import threading
from unittest.mock import Mock

import pytest
import requests

from lib.config import validate_provider_config
from lib.job_spec import load_job_spec
from scripts import web_console as web
from test_image_console import _image_config
from test_workflow_job_controls import isolated_config


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_TEST_DISABLE_AUTH", "1")
    config = _image_config()
    validate_provider_config(config)
    monkeypatch.setattr(web, "load_config", lambda: copy.deepcopy(config))
    monkeypatch.setattr(web, "JOBS_ROOT", tmp_path)
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("image preview sent HTTP")))
    manager = web.JobManager.__new__(web.JobManager)
    manager._lock = threading.Lock()
    manager._jobs = {}
    monkeypatch.setattr(web, "JOB_MANAGER", manager)
    monkeypatch.setattr(manager, "_start_locked", Mock())
    return config, manager


def test_image_preview_and_create_share_frozen_plan_before_key_lookup(context, monkeypatch):
    _, manager = context
    credentials = Mock(side_effect=AssertionError("preview looked up credentials"))
    monkeypatch.setattr(web, "image_provider_has_api_key", credentials)
    payload = {"type": "image_param_test", "provider": "fake", "model": "gpt-image-2", "run_count": 2,
               "image_plan": {"suite": "smoke", "visual_forensics": False}}
    response = web.app.test_client().post("/api/image-plan/preview", json=payload)
    assert response.status_code == 200, response.json
    preview = response.json
    assert preview["estimated_case_count"] == 1
    assert preview["job_spec"]["schema_version"] == 6 and preview["plan"]["run_count"] == 2
    assert "model_profile_database" in preview["job_spec"]
    assert "test_workflow_snapshot" not in preview["job_spec"]
    credentials.assert_not_called()
    rejected = web.app.test_client().post("/api/jobs", json={**payload, "plan_digest": "0" * 64})
    assert rejected.status_code == 400 and "changed since preview" in rejected.json["error"]
    credentials.assert_not_called()
    manager._start_locked.assert_not_called()
    monkeypatch.setattr(web, "image_provider_has_api_key", Mock(return_value=True))
    job = manager.create({**payload, "plan_digest": preview["plan_digest"]})
    spec = load_job_spec(job.report_dir / "job_spec.json")
    assert spec["schema_version"] == 6 and spec["execution_plan"] == preview["plan"]
    assert job.param_test_runs == 2 and job.image_plan == preview["image_plan"]
    assert job.command[1] == "scripts/image_param_test.py"
    assert "--api-key-env" in job.command
    manager._start_locked.assert_called_once_with(job)
    requests.Session.request.assert_not_called()


def test_image_subset_and_default_full_use_same_compiler(context):
    _, _ = context
    client = web.app.test_client()
    full = client.post("/api/image-plan/preview", json={"provider": "fake", "model": "gpt-image-2"})
    assert full.status_code == 200, full.json
    assert full.json["image_plan"]["suite"] == "full"
    assert full.json["image_plan"]["include_4k"] is True
    assert full.json["plan"]["run_count"] == 1
    name = full.json["image_plan"]["cases"][0]
    selected = client.post("/api/image-plan/preview", json={"provider": "fake", "model": "gpt-image-2",
        "image_plan": {"suite": "full", "cases": [name]}})
    assert selected.status_code == 200, selected.json
    assert selected.json["estimated_case_count"] == 1
    assert selected.json["plan_digest"] != full.json["plan_digest"]
    requests.Session.request.assert_not_called()


@pytest.mark.parametrize("prompt", [None, "Draw a small blue circle on white."])
def test_created_v6_image_job_cli_uses_exact_plan_and_independent_runs(context, monkeypatch, prompt):
    from scripts import image_param_test
    from lib.test_runner.adapters import image
    config, manager = context
    monkeypatch.setattr(web, "image_provider_has_api_key", Mock(return_value=True))
    job = manager.create({"type": "image_param_test", "provider": "fake", "model": "gpt-image-2",
        "run_count": 2, "prompt": prompt, "image_plan": {"suite": "smoke", "visual_forensics": False}})
    monkeypatch.setattr(image_param_test, "load_config", lambda: copy.deepcopy(config))
    monkeypatch.setenv("LOADTEST_JOB_SPEC", str(job.report_dir / "job_spec.json"))
    key_name = job.command[job.command.index("--api-key-env") + 1]
    monkeypatch.setenv(key_name, "isolated-test-credential")
    sent = []
    def dispatch(request, **kwargs):
        sent.append(copy.deepcopy(request))
        response = ({"data": [{"id": "gpt-image-2"}]} if request["method"] == "GET"
                    else {"error": {"param": "size", "message": "offline rejection"}})
        return {"http_status": 200 if request["method"] == "GET" else 400,
                "response_complete": True, "response": response}
    monkeypatch.setattr(image, "make_image_dispatcher", lambda *args, **kwargs: dispatch)
    assert image_param_test.main(job.command[2:]) == 1  # Domain failure remains a failure.
    summary = json.loads((job.report_dir / "summary.json").read_text())
    workflow = summary["workflow_result"]
    assert workflow["plan_digest"] == job.job_spec["execution_plan"]["plan_digest"]
    assert len(workflow["runs"]) == 2
    assert len({run["run_id"] for run in workflow["runs"]}) == 2
    assert len(sent) == 4 and sum(run["request_count"] for run in workflow["runs"]) == 4
    assert summary["pass"] is False
    if prompt is not None:
        assert all(request["body"]["prompt"] == prompt for request in sent if request["method"] == "POST")
    assert (job.report_dir / "workflow_runs").is_dir()
    requests.Session.request.assert_not_called()
