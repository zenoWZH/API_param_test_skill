"""Common CLI preserves domain reports and recovers owned image resources offline."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from lib.config import load_config
from lib.credential_security import ProviderCredential
from lib.test_runner.adapters import image
from lib.test_runner.service import preview_test_plan
from scripts import workflow_test as cli


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setenv("LLM_API_TEST_PROVIDERS_LOCAL", "/tmp/workflow-common-cli-no-private.yaml")
    for key in ("LOADTEST_JOB_SPEC", "LOADTEST_PROVIDER", "LOADTEST_MODEL", "LOADTEST_PARAM_TEST_RUNS",
                "LOADTEST_TEST_PLAN_DIGEST", "LOADTEST_TEST_CASES", "LOADTEST_TOOL_VALIDATION_MODE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("unexpected real HTTP")))


def test_common_image_entrypoint_preserves_domain_failure_and_only_recovers_owned_file(tmp_path, monkeypatch):
    config = load_config()
    preview = preview_test_plan(config, {"type": "image_param_test", "provider": "openai_official",
        "model": "gpt-image-2.5-sunburst", "api_form": "openai_responses",
        "image_plan": {"suite": "full", "cases": ["gpt_image_25_responses_edit_file_id"], "visual_forensics": False}})
    credential = ProviderCredential.create(provider="openai_official", secret="isolated-fixture-key",
                                           base_urls=["https://api.openai.com/v1"])
    monkeypatch.setattr("lib.credential_security.credential_from_config", lambda *args: credential)
    calls = []
    deletes = 0
    def dispatch(request, **kwargs):
        nonlocal deletes
        calls.append(copy.deepcopy(request))
        if request["method"] == "DELETE":
            deletes += 1
            status, body = (503, {"error": {"message": "offline cleanup failure"}}) if deletes == 1 else (200, {"id": "file_owned", "deleted": True})
        elif request["path"] == "/v1/files":
            status, body = 200, {"id": "file_owned"}
        else:
            status, body = 400, {"error": {"message": "offline generation rejection"}}
        return {"http_status": status, "response": body, "response_complete": True,
                "request_bytes_sha256": hashlib.sha256(image.canonical_bytes(request.get("body", {}))).hexdigest()}
    monkeypatch.setattr(image, "make_image_dispatcher", lambda *args, **kwargs: dispatch)
    directory = tmp_path / "image"
    result = cli.execute_job(preview["job_spec"], config, directory)
    assert result["pass"] is False
    report = result["workflow_result"]
    assert report["plan_digest"] == preview["plan_digest"] and report["status"] == "incomplete"
    assert [row["method"] for row in calls] == ["POST", "POST", "DELETE"]
    original_summary = (directory / "summary.json").read_bytes()
    original_ledger = Path(report["runs"][0]["ledger_path"]).read_bytes()
    # Cleanup needs frozen ownership, not a new business selection/catalog lookup.
    monkeypatch.setattr("lib.test_runner.service._preview_image_plan", Mock(side_effect=AssertionError("cleanup rebuilt business cases")))
    recovered = cli.execute_job(preview["job_spec"], config, directory, cleanup_run_id=report["runs"][0]["run_id"], dispatcher=dispatch)
    assert recovered["workflow_result"]["status"] == "passed"
    assert [row["method"] for row in calls] == ["POST", "POST", "DELETE", "DELETE"]
    assert (directory / "summary.json").read_bytes() == original_summary
    assert Path(report["runs"][0]["ledger_path"]).read_bytes() == original_ledger
    requests.Session.request.assert_not_called()


@pytest.mark.parametrize("extra", [["--runs", "1"], ["--suite", "full"], ["--api-form", "openai_responses"], ["--type", "param_test"]])
def test_frozen_cli_rejects_mutable_selectors_before_loading_config(monkeypatch, extra):
    monkeypatch.setattr(cli, "load_config", Mock(side_effect=AssertionError("loaded configuration before selector validation")))
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--job-spec", "/tmp/does-not-exist.json", *extra])
    assert stopped.value.code == 2
    cli.load_config.assert_not_called()


@pytest.fixture
def image_cli(monkeypatch):
    config = load_config()
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "execute_job", Mock(side_effect=AssertionError("unexpected image execution")))
    monkeypatch.setattr("lib.credential_security.credential_from_config",
                        Mock(side_effect=AssertionError("preview looked up credentials")))
    return ["--type", "image_param_test", "--provider", "openai_official", "--model", "gpt-image-2"]


@pytest.mark.parametrize("selectors", [[], ["--route-profile", "vendor_direct"],
    ["--api-form", "openai_images_generations"],
    ["--route-profile", "vendor_direct", "--api-form", "openai_images_generations"]])
def test_image_cli_smoke_preview_uses_requested_suite_and_default_selectors(image_cli, selectors, capsys):
    assert cli.main([*image_cli, *selectors, "--suite", "smoke", "--preview"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["image_plan"]["suite"] == "smoke"
    assert preview["image_plan"]["route_profile"] == "vendor_direct"
    assert preview["image_plan"]["api_form"] == "openai_images_generations"
    assert preview["selected_cases"] == ["baseline_1024_square"]
    assert preview["image_plan"]["estimated_case_count"] == 1
    assert preview["request_cap"] == 4
    cli.execute_job.assert_not_called()
    requests.Session.request.assert_not_called()


def test_image_cli_case_subset_is_frozen_with_its_budget(image_cli, tmp_path, capsys):
    selected = ["baseline_1024_square", "standard_portrait"]
    arguments = [*image_cli, "--suite", "full", "--preview", "--output-dir", str(tmp_path)]
    for case in selected:
        arguments.extend(["--case", case])
    assert cli.main(arguments) == 0
    preview = json.loads(capsys.readouterr().out)
    job = json.loads((tmp_path / "job_spec.json").read_text())
    assert preview["selected_cases"] == selected
    assert job["image_plan"]["cases"] == selected
    assert job["execution_plan"]["selected_cases"] == selected
    assert preview["image_plan"]["estimated_case_count"] == 2
    assert preview["request_cap"] == job["execution_plan"]["limits"]["max_requests"] == 7
    assert {step["case_id"] for step in job["execution_plan"]["ordered_steps"]} == set(selected)
    cli.execute_job.assert_not_called()
    requests.Session.request.assert_not_called()


@pytest.mark.parametrize("preview", [False, True])
def test_image_cli_unknown_case_fails_before_preview_or_execution(image_cli, preview):
    arguments = [*image_cli, "--route-profile", "vendor_direct", "--api-form", "openai_images_generations",
                 "--suite", "smoke", "--case", "does-not-exist"]
    if preview:
        arguments.append("--preview")
    with pytest.raises(ValueError, match="Unknown image test case.*does-not-exist"):
        cli.main(arguments)
    cli.execute_job.assert_not_called()
    requests.Session.request.assert_not_called()


def test_image_cli_without_suite_defaults_to_full(image_cli, capsys):
    assert cli.main([*image_cli, "--preview"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["image_plan"]["suite"] == "full"
    assert len(preview["selected_cases"]) > 1
    cli.execute_job.assert_not_called()
    requests.Session.request.assert_not_called()
