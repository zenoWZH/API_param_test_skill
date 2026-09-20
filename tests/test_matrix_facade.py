"""Observable standalone behavior for the current matrix and frozen-plan entry."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("skill_matrix_facade", ROOT / "scripts/matrix.py")
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setenv("LLM_API_TEST_PROVIDERS_LOCAL", str(tmp_path / "no-private.yaml"))
    for key in tuple(__import__("os").environ):
        if key.startswith("LOADTEST_") and key != "LOADTEST_SKIP_DOTENV":
            monkeypatch.delenv(key)
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("unexpected HTTP")))
    from lib import config
    monkeypatch.setattr(config, "get_api_key", Mock(side_effect=AssertionError("preview read credentials")))


def test_preview_freezes_request_alias_and_v6_plan_without_traffic(tmp_path, capsys):
    directory = tmp_path / "plan with spaces"
    assert matrix.main(["preview", "--provider", "deepseek_official", "--model", "deepseek-v4-pro",
                        "--api-form", "openai_responses", "--case", "deepseek0813_responses_basic",
                        "--output-dir", str(directory)]) == 0
    result = json.loads(capsys.readouterr().out)
    job = json.loads((directory / "job_spec.json").read_text())
    assert job["schema_version"] == 6
    assert job["model"] == job["model_capability_profile"]["model"] == "deepseek-v4-pro"
    assert job["execution_plan"]["plan_digest"] == result["plan_digest"]
    assert result["selected_cases"] == ["identity_probe", "deepseek0813_responses_basic"]
    assert result["request_cap"] == 2
    requests.Session.request.assert_not_called()


def test_matrix_execution_requires_explicit_confirmation_before_reading_job(tmp_path, capsys):
    assert matrix.main(["run", "--job-spec", str(tmp_path / "missing.json")]) == 2
    assert "--yes" in json.loads(capsys.readouterr().err)["error"]
    assert not (tmp_path / "run.json").exists()


def test_research_matrix_is_listed_without_promoting_generic_execution(capsys):
    assert matrix.main(["list", "--source", "deepseek", "--model", "deepseek-flash"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [row["model_slug"] for row in result["profiles"]] == ["deepseek-v4.1-flash"]
    research = [row for row in result["bindings"] if row["extension_type"] == "research_parameter_matrix"]
    assert len(research) == 5
    assert sum(row["case_count"] for row in research) == 61
    assert all(row["migration_status"] == "existing_disabled" for row in research)
    # Separately registered opt-in media workflows do not promote these five
    # research bindings into generic parameter or pressure execution.
    assert all(row["extension_type"] in {"research_parameter_matrix", "test_workflow"}
               for row in result["bindings"])
    assert result["live_verification"] == "not_performed"
    requests.Session.request.assert_not_called()


def test_skill_installation_is_never_a_plan_destination():
    with pytest.raises(ValueError, match="outside"):
        matrix._outside_bundle(ROOT / "reports" / "example")


def test_dedicated_research_definitions_match_the_bundled_mpdb():
    from lib.deepseek_v4_1_matrix import build_cases
    from lib.model_profile_catalog import get_model_profile_catalog
    catalog = get_model_profile_catalog()
    bindings = [row for row in catalog.list_test_bindings(source="deepseek", extension_type="research_parameter_matrix")
                if row["profile_id"].endswith("/deepseek-v4.1-flash")]
    cases = build_cases()
    assert len(cases) == 61
    assert sum(case["smoke"] for case in cases) == 10
    for binding in bindings:
        assert binding["case_definitions"] == [case for case in cases if case["api_form"] == binding["api_form"]]
        assert binding["parameter_test_enabled"] is False
        assert binding["pressure_test_enabled"] is False
        assert binding["dedicated_research_runner"]["generic_dispatch"] is False


def test_stopping_foreground_matrix_only_signals_its_own_process(tmp_path, monkeypatch):
    loader = importlib.util.spec_from_file_location("skill_matrix_jobs", ROOT / "scripts/jobs.py")
    jobs = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(jobs)
    run = {"pid": 123456, "pid_marker": "matrix.py", "signal_scope": "process", "returncode": None}
    (tmp_path / "run.json").write_text(json.dumps(run))
    monkeypatch.setattr(jobs, "_pid_alive", lambda *args: True)
    kill, killpg = Mock(), Mock(side_effect=AssertionError("signalled the invoking shell group"))
    monkeypatch.setattr(jobs.os, "kill", kill)
    monkeypatch.setattr(jobs.os, "killpg", killpg)
    assert jobs._stop_job(tmp_path)["stopped"] is True
    kill.assert_called_once_with(123456, jobs.signal.SIGTERM)
    killpg.assert_not_called()


def test_condensed_workflow_result_preserves_cleanup_failure_without_raw_response():
    loader = importlib.util.spec_from_file_location("skill_matrix_result", ROOT / "scripts/result.py")
    result = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(result)
    summary = result._workflow_summary({"status": "incomplete", "plan_digest": "digest",
        "requested_run_count": 1, "completed_run_count": 1,
        "runs": [{"run_id": "owned", "status": "incomplete", "request_count": 3,
                  "cleanup": {"status": "incomplete", "unknown_creations": []},
                  "steps": {"case": {"response": "raw provider content"}}}]})
    assert summary["runs"][0]["cleanup"]["status"] == "incomplete"
    assert "raw provider content" not in json.dumps(summary)


def test_accelerated_catalog_yaml_preserves_safe_types_and_rejects_python_objects(tmp_path):
    import yaml
    from model_profile_db.compiler import _read_yaml, CatalogValidationError
    path = tmp_path / "source.yaml"
    sample = "enabled: false\nnumber: 12\nratio: 0.5\nitems: [null, '001', true]\n"
    path.write_text(sample)
    assert _read_yaml(path) == yaml.safe_load(sample)
    path.write_text("value: !!python/object/apply:os.system ['false']\n")
    with pytest.raises(CatalogValidationError, match="Cannot read YAML"):
        _read_yaml(path)


def test_frozen_plan_executes_once_and_records_a_foreground_job(tmp_path, monkeypatch, capsys):
    from scripts import workflow_test
    directory = tmp_path / "frozen run"
    assert matrix.main(["preview", "--provider", "deepseek_official", "--model", "deepseek-v4-pro",
                        "--api-form", "openai_responses", "--case", "deepseek0813_responses_basic",
                        "--output-dir", str(directory)]) == 0
    capsys.readouterr()
    frozen = json.loads((directory / "job_spec.json").read_text())
    dispatch = Mock(return_value={"pass": False, "workflow_result": {"status": "failed"}})
    monkeypatch.setattr(workflow_test, "execute_job", dispatch)
    argv = ["run", "--job-spec", str(directory / "job_spec.json"), "--yes"]
    assert matrix.main(argv) == 1
    assert dispatch.call_args.args[0] == frozen
    state = json.loads((directory / "run.json").read_text())
    assert state["returncode"] == 1 and state["finished_at"]
    assert state["signal_scope"] == "process"
    assert matrix.main(argv) == 2
    assert dispatch.call_count == 1
    requests.Session.request.assert_not_called()


def test_standalone_job_view_does_not_promote_a_legacy_parameter_pass(tmp_path):
    loader = importlib.util.spec_from_file_location("skill_matrix_legacy_jobs", ROOT / "scripts/jobs.py")
    jobs = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(jobs)
    (tmp_path / "job_spec.json").write_text(json.dumps({"schema_version": 3, "type": "param_test",
        "provider": "historical", "model": "historical"}))
    (tmp_path / "verdict.json").write_text(json.dumps({"pass": True, "token_audit_schema_version": 3}))
    row = jobs._job_status(tmp_path)
    assert row["reported_pass"] is True
    assert row["pass"] is False
    assert row["result_validation"]["current"] is False


@pytest.mark.parametrize("model", ["gemini-3.6-flash", "gemini-3.5-flash-lite"])
def test_missing_public_media_models_have_only_the_registered_chat_mapping(model):
    from lib.config import get_model_api_forms, load_config
    from lib.test_runner.service import preview_test_plan
    config = load_config()
    assert set(get_model_api_forms(config, model, "gemini", route_profile="google_ai_studio")) == {"openai_chat_completions"}
    preview = preview_test_plan(config, {"type": "param_test", "provider": "gemini", "model": model,
        "workflow_id": f"media-input/gemini/{model}/openai_chat_completions", "cases": ["image_png"]})
    assert preview["selected_cases"] == ["image_png"]
    assert preview["request_cap"] == 1
    requests.Session.request.assert_not_called()
