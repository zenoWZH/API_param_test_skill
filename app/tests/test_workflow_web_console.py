"""Read-only workflow preview and asynchronous, bounded Web cancellation."""
from __future__ import annotations

import copy
import json
import signal
import subprocess
import threading
from unittest.mock import Mock

import pytest

from scripts import web_console as web
from test_workflow_job_controls import base_job, registry, isolated_config, frozen_job, workflow_snapshot_job


@pytest.fixture
def manager_job(base_job, registry, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_TEST_DISABLE_AUTH", "1")
    spec = frozen_job(base_job, registry)
    (tmp_path / "job_spec.json").write_text(json.dumps(spec))
    manager = web.JobManager.__new__(web.JobManager)
    manager._lock = threading.Lock()
    process = Mock(pid=654321)
    process.poll.return_value = None
    process.wait.return_value = 0
    job = web.Job(
        id="offline-workflow", type="param_test", provider=base_job["provider"],
        provider_label="Offline", model=base_job["model"], model_family="gemini",
        workload="short", users=None, spawn_rate=None, duration=None,
        report_dir=tmp_path, command=[], job_spec=copy.deepcopy(spec),
        process=process, pid=process.pid, status="running",
    )
    manager._jobs = {job.id: job}
    monkeypatch.setattr(manager, "public", lambda value, **kwargs: {"id": value.id, "status": value.status})
    if hasattr(manager, "_discover_external_jobs"):
        monkeypatch.setattr(manager, "_discover_external_jobs", lambda: None)
    monkeypatch.setattr(web, "JOB_MANAGER", manager)
    monkeypatch.setattr(web, "build_provider_child_env", Mock(side_effect=AssertionError("cancellation read credentials")))
    monkeypatch.setattr(web, "provider_has_api_key", Mock(side_effect=AssertionError("cancellation read credentials")))
    return manager, job


@pytest.fixture
def deferred_threads(monkeypatch):
    started = []
    class DeferredThread:
        def __init__(self, *, target, args=(), kwargs=None, daemon=False):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}
        def start(self):
            started.append(self)
        def run(self):
            return self.target(*self.args, **self.kwargs)
    monkeypatch.setattr(web.threading, "Thread", DeferredThread)
    return started


def test_workflow_stop_returns_before_wait_and_uses_persisted_grace(manager_job, deferred_threads, monkeypatch):
    manager, job = manager_job
    job.job_spec["parameter_execution"]["limits"]["request_timeout_seconds"] = 999
    job.job_spec["parameter_execution"]["limits"]["cleanup_deadline_seconds"] = 999
    kill = Mock()
    monkeypatch.setattr(web.os, "killpg", kill)
    def public_after_monitor(value, **kwargs):
        assert deferred_threads, "Deadline supervisor must start before rendering job details"
        return {"id": value.id, "status": value.status}
    monkeypatch.setattr(manager, "public", public_after_monitor)
    response = web.app.test_client().post(f"/api/jobs/{job.id}/stop")
    assert response.status_code == 200 and response.json["status"] == "stopping"
    assert job.stop_requested and job.status == "stopping"
    job.process.wait.assert_not_called()
    kill.assert_called_once_with(job.pid, signal.SIGTERM)
    assert len(deferred_threads) == 1
    assert deferred_threads[0].kwargs["grace_seconds"] == 55
    deferred_threads[0].run()
    kill.assert_called_once_with(job.pid, signal.SIGTERM)
    job.process.wait.assert_called_once_with(timeout=55)
    web.provider_has_api_key.assert_not_called()
    web.build_provider_child_env.assert_not_called()


def test_repeated_stop_does_not_repeat_signals_or_monitors(manager_job, deferred_threads, monkeypatch):
    manager, job = manager_job
    monkeypatch.setattr(web.os, "killpg", Mock())
    assert manager.stop(job.id)["status"] == "stopping"
    assert manager.stop(job.id)["status"] == "stopping"
    assert len(deferred_threads) == 1


def test_nonworkflow_pressure_retains_five_second_grace(manager_job, deferred_threads, monkeypatch):
    manager, job = manager_job
    monkeypatch.setattr(web.os, "killpg", Mock())
    job.type = "quick_load"
    job.job_spec = {"schema_version": 4, "type": "quick_load"}
    (job.report_dir / "job_spec.json").write_text(json.dumps(job.job_spec))
    manager.stop(job.id)
    assert deferred_threads[0].kwargs["grace_seconds"] == 5


def test_invalid_workflow_snapshot_cannot_shorten_cleanup_grace(manager_job, deferred_threads):
    _, job = manager_job
    persisted = json.loads((job.report_dir / "job_spec.json").read_text())
    persisted["execution_plan"]["limits"]["cleanup_deadline_seconds"] = 0
    (job.report_dir / "job_spec.json").write_text(json.dumps(persisted))
    response = web.app.test_client().post(f"/api/jobs/{job.id}/stop")
    assert response.status_code == 400
    assert not job.stop_requested and not deferred_threads
    job.process.wait.assert_not_called()


def test_background_termination_escalates_only_after_frozen_grace(monkeypatch):
    process = Mock(pid=654321)
    process.wait.side_effect = subprocess.TimeoutExpired("offline", 55)
    signals = []
    monkeypatch.setattr(web.os, "killpg", lambda pid, value: signals.append((pid, value)))
    web._terminate_process_group(process, grace_seconds=55)
    assert signals == [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)]
    process.wait.assert_called_once_with(timeout=55)


def test_preview_is_read_only_and_delegates_exact_selection(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_API_TEST_DISABLE_AUTH", "1")
    config = {"offline": True}
    payload = {"workflow_id": "official-suite", "provider": "official", "model": "model",
               "cases": ["dependent"], "runs": 2}
    preview = {"plan": {"offline": True}, "plan_digest": "digest", "selected_cases": ["seed", "dependent"],
               "ordered_steps": ["seed", "dependent"], "request_cap": 4, "cleanup_request_cap": 2,
               "preconditions": [], "target": {"provider": "official", "model": "model"}}
    resolver = Mock(return_value=preview)
    monkeypatch.setattr(web, "load_config", lambda: config)
    monkeypatch.setattr(web, "_preview_workflow", resolver)
    monkeypatch.setattr(web, "provider_has_api_key", Mock(side_effect=AssertionError("preview read credentials")))
    monkeypatch.setattr(web.subprocess, "Popen", Mock(side_effect=AssertionError("preview spawned a job")))
    monkeypatch.setattr(web, "JOBS_ROOT", tmp_path)
    response = web.app.test_client().post("/api/test-plan/preview", json=payload)
    assert response.status_code == 200 and response.json == preview
    resolver.assert_called_once_with(config, payload)
    assert not list(tmp_path.iterdir())
    web.provider_has_api_key.assert_not_called()
    web.subprocess.Popen.assert_not_called()


@pytest.mark.parametrize("payload", [None, [], "suite", 1])
def test_preview_rejects_nonobject_input(payload, monkeypatch):
    monkeypatch.setenv("LLM_API_TEST_DISABLE_AUTH", "1")
    resolver = Mock(side_effect=AssertionError("invalid input reached compiler"))
    monkeypatch.setattr(web, "_preview_workflow", resolver)
    response = web.app.test_client().post("/api/test-plan/preview", json=payload)
    assert response.status_code == 400
    resolver.assert_not_called()


def test_preview_preserves_disabled_binding_rejection(monkeypatch):
    monkeypatch.setenv("LLM_API_TEST_DISABLE_AUTH", "1")
    monkeypatch.setattr(web, "load_config", lambda: {})
    monkeypatch.setattr(web, "_preview_workflow", Mock(side_effect=ValueError("Workflow binding is disabled")))
    response = web.app.test_client().post("/api/test-plan/preview", json={"workflow_id": "disabled"})
    assert response.status_code == 400 and "disabled" in response.json["error"]


def test_new_web_defaults_are_one_run():
    assert web.DEFAULT_PARAM_TEST_RUNS == 1


def test_external_workflow_stop_preserves_pid_marker_and_budget(manager_job, deferred_threads, monkeypatch):
    manager, job = manager_job
    monkeypatch.setattr(manager, "_pid_alive", Mock(return_value=True))
    monkeypatch.setattr(web.os, "killpg", Mock())
    job.process = None
    job.external = True
    (job.report_dir / "run.json").write_text(json.dumps({"pid_marker": "owned-workflow-marker"}))
    assert manager.stop(job.id)["status"] == "stopping"
    assert deferred_threads[0].args == (job.pid, "owned-workflow-marker", 55)
    assert deferred_threads[0].target == manager._monitor_external_termination


def test_external_termination_rechecks_ownership_before_kill(manager_job, monkeypatch):
    manager, job = manager_job
    signals = []
    monkeypatch.setattr(manager, "_pid_alive", Mock(side_effect=[True, False]))
    monkeypatch.setattr(web.os, "killpg", lambda pid, value: signals.append((pid, value)))
    manager._monitor_external_termination(job.pid, "owned-marker", 55)
    assert signals == [(job.pid, signal.SIGTERM)]
    assert manager._pid_alive.call_args.args == (job.pid, "owned-marker")


def test_external_foreground_matrix_stop_signals_only_its_owned_pid(manager_job, deferred_threads, monkeypatch):
    manager, job = manager_job
    monkeypatch.setattr(manager, "_pid_alive", Mock(return_value=True))
    kill, killpg = Mock(), Mock(side_effect=AssertionError("foreground shell group was signalled"))
    monkeypatch.setattr(web.os, "kill", kill)
    monkeypatch.setattr(web.os, "killpg", killpg)
    job.process = None
    job.external = True
    (job.report_dir / "run.json").write_text(json.dumps({"pid_marker": "matrix.py", "signal_scope": "process"}))
    assert manager.stop(job.id)["status"] == "stopping"
    kill.assert_called_once_with(job.pid, signal.SIGTERM)
    killpg.assert_not_called()
    assert deferred_threads[0].kwargs["process_only"] is True
    assert json.loads((job.report_dir / "run.json").read_text())["stop_requested"] is True


def test_workflow_creation_freezes_separate_permission_before_credentials(workflow_snapshot_job, tmp_path, monkeypatch):
    spec = workflow_snapshot_job
    monkeypatch.setenv("LLM_API_TEST_DISABLE_AUTH", "1")
    manager = web.JobManager.__new__(web.JobManager)
    manager._lock = threading.Lock()
    manager._jobs = {}
    monkeypatch.setattr(web, "JOBS_ROOT", tmp_path)
    monkeypatch.setattr(web, "load_config", lambda: {})
    preview = {"job_spec": spec, "plan": spec["execution_plan"]}
    monkeypatch.setattr(web, "_preview_workflow", Mock(return_value=preview))
    monkeypatch.setattr(web, "get_provider_config", lambda *args: {"label": "Offline"})
    keys = Mock(return_value=True)
    monkeypatch.setattr(web, "provider_has_api_key", keys)
    launcher = Mock()
    monkeypatch.setattr(manager, "_start_locked", launcher)
    job = manager.create({"type": "param_test", "provider": spec["provider"], "model": spec["model"],
                          "workflow_id": spec["execution_plan"]["workflow_id"]})
    disk = web.load_job_spec(job.report_dir / "job_spec.json")
    assert disk == spec and "model_profile_database" not in disk
    assert job.command[1:3] == ["scripts/workflow_test.py", "--job-spec"]
    assert job.param_test_runs == 1
    launcher.assert_called_once_with(job)
    keys.assert_called_once()
    bad = copy.deepcopy(spec)
    bad["execution_plan"]["limits"]["max_requests"] = 999
    monkeypatch.setattr(web, "_preview_workflow", Mock(return_value={"job_spec": bad}))
    keys.reset_mock()
    with pytest.raises(ValueError):
        manager.create({"type": "param_test", "workflow_id": "corrupt"})
    keys.assert_not_called()
    wrong_type = {**copy.deepcopy(spec), "type": "image_param_test"}
    monkeypatch.setattr(web, "_preview_workflow", Mock(return_value={"job_spec": wrong_type}))
    with pytest.raises(ValueError, match="modality"):
        manager.create({"type": "image_param_test", "workflow_id": "text-workflow"})
    keys.assert_not_called()


def test_workflow_default_selection_reuses_service_preview(workflow_snapshot_job, tmp_path, monkeypatch):
    spec = workflow_snapshot_job
    manager = web.JobManager.__new__(web.JobManager)
    manager._lock = threading.Lock()
    manager._jobs = {}
    monkeypatch.setattr(web, "load_config", lambda: {})
    preview = {"job_spec": spec}
    maybe = Mock(return_value=preview)
    monkeypatch.setattr(web, "_maybe_preview_workflow", maybe)
    creator = Mock(return_value="workflow-job")
    monkeypatch.setattr(manager, "_create_workflow_job", creator)
    payload = {"type": "param_test", "provider": spec["provider"], "model": spec["model"]}
    assert manager.create(payload) == "workflow-job"
    creator.assert_called_once_with({}, payload, preview=preview)


def test_workflow_history_does_not_claim_legacy_token_audit(workflow_snapshot_job):
    from test_workflow_job_controls import successful_report
    spec = workflow_snapshot_job
    result = {"workflow_result": successful_report(spec), "pass": True,
              "proof_scope": "workflow_execution_and_domain_validators"}
    classified = web._classify_functional_result(spec, result, job_type="param_test")
    assert classified["pass"] is True and "token_validation_pass" not in classified
    result["workflow_result"]["runs"][0]["cleanup"]["status"] = "incomplete"
    assert web._classify_functional_result(spec, result)["pass"] is False


def test_stop_uses_checksummed_owned_inflight_deadline(manager_job, monkeypatch):
    from lib.test_runner import digest_json
    _, job = manager_job
    plan = job.job_spec["execution_plan"]
    ledger_dir = job.report_dir / "workflow_runs" / "run-1"
    ledger_dir.mkdir(parents=True)
    ledger = {"ledger_schema_version": 1, "plan_digest": plan["plan_digest"], "target": plan["target"],
              "run_id": "run-1", "run_index": 1, "status": "running", "phase": "business",
              "active_request": {"started_at": 990.0, "deadline_at": 1020.0,
                                 "timeout_seconds": 30.0, "step_id": "seed", "phase": "business"}}
    def persist():
        saved = {**ledger, "ledger_digest": digest_json(ledger)}
        (ledger_dir / "ledger.json").write_text(json.dumps(saved))
    persist()
    monkeypatch.setattr(web.time, "time", lambda: 1000.0)
    assert web._termination_grace_for_job(job) == 45
    saved = json.loads((ledger_dir / "ledger.json").read_text())
    saved["active_request"]["deadline_at"] = 1001.0
    (ledger_dir / "ledger.json").write_text(json.dumps(saved))
    assert web._termination_grace_for_job(job) == 55
    ledger["plan_digest"] = "other-plan"
    persist()
    assert web._termination_grace_for_job(job) == 55


def test_workflow_handler_drift_precedes_credential_loading(manager_job, workflow_snapshot_job, monkeypatch):
    from lib.test_runner import service
    manager, job = manager_job
    spec = workflow_snapshot_job
    job.job_spec = spec
    for key in ("provider", "model", "route_profile", "api_form"):
        setattr(job, key, spec[key])
    (job.report_dir / "job_spec.json").write_text(json.dumps(spec))
    monkeypatch.setattr(service, "registry_for_plan", Mock(side_effect=ValueError("handler digest drift")))
    with pytest.raises(ValueError, match="digest drift"):
        manager._start_locked(job)
    web.build_provider_child_env.assert_not_called()
