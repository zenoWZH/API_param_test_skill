"""Billable Responses image prerequisites belong in the frozen Web plan."""
from __future__ import annotations

import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path

import pytest
import yaml

from lib.gpt_image_25_responses import (
    MODELS, expand_responses_case_dependencies, responses_cases,
    responses_case_request_cap, responses_image_cases, responses_image_request_cap,
    select_responses_image_cases,
)
from lib.job_spec import resolve_image_plan


PREFIX = "gpt_image_25_responses_"
SEED = PREFIX + "multiturn_seed"
EDIT = PREFIX + "edit_previous_response_id"
IMAGE_ID_EDIT = PREFIX + "edit_image_call_id"


@pytest.fixture
def public_config():
    # Read only the checked-in config, never dotenv or provider-local overlays.
    return yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text())


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("selected", [[EDIT], [EDIT, SEED], [EDIT, IMAGE_ID_EDIT]])
def test_selection_includes_seed_once_preserves_definitions_and_counts_requests(model, selected):
    matrix = responses_image_cases(model, "resolution", include_4k=False, include_negative=False)
    original = [case.public() for case in matrix]
    rows = select_responses_image_cases(model, matrix, selected)
    expected = [SEED, EDIT] + ([IMAGE_ID_EDIT] if IMAGE_ID_EDIT in selected else [])
    assert [case.name for case in rows] == expected
    assert responses_image_request_cap(model, rows) == len(expected)
    assert [case.public() for case in matrix] == original
    assert select_responses_image_cases(model, matrix, expected) == rows


def test_selection_cannot_restore_a_prerequisite_removed_by_suite_or_filter():
    matrix = [case for case in responses_image_cases(MODELS[0]) if case.name != SEED]
    with pytest.raises(ValueError, match="dependency unavailable"):
        select_responses_image_cases(MODELS[0], matrix, [EDIT])
    with pytest.raises(ValueError, match="case not in selected"):
        select_responses_image_cases(MODELS[0], responses_image_cases(MODELS[0], "smoke"), [EDIT])


def test_dependency_closure_is_transitive_and_rejects_cycles_missing_and_foreign_ids():
    rows = [{"case_id": "third", "depends_on": "second"},
            {"case_id": "second", "depends_on": "first"}, {"case_id": "first"}]
    assert [row["case_id"] for row in expand_responses_case_dependencies(rows, ["third"])] == ["first", "second", "third"]
    with pytest.raises(ValueError, match="cyclic"):
        expand_responses_case_dependencies([*rows[:2], {"case_id": "first", "depends_on": "third"}])
    with pytest.raises(ValueError, match="dependency unavailable"):
        expand_responses_case_dependencies(rows[:2], ["third"])
    with pytest.raises(ValueError, match="unknown"):
        expand_responses_case_dependencies(rows, ["foreign-model/seed"])
    with pytest.raises(ValueError, match="duplicate"):
        expand_responses_case_dependencies([*rows, rows[0]])


def test_request_cap_includes_file_uploads_and_cleanup_and_rejects_unexpanded_plan():
    model = MODELS[0]
    matrix = responses_image_cases(model)
    rows = select_responses_image_cases(model, matrix, [EDIT, PREFIX + "edit_mask_file_id", PREFIX + "edit_file_id"])
    assert responses_image_request_cap(model, rows) == 10  # seed + edit + (1+4) + (1+2)
    with pytest.raises(ValueError, match="requires all dependencies"):
        responses_image_request_cap(model, [case for case in matrix if case.name == EDIT])


@pytest.mark.parametrize("model", MODELS)
def test_web_preview_frozen_job_and_cli_show_the_same_two_generations(public_config, model, tmp_path, monkeypatch, capsys):
    from scripts import image_param_test, run_gpt_image_25_responses_reference as runner, web_console

    monkeypatch.setenv("LLM_API_TEST_DISABLE_AUTH", "1")
    monkeypatch.setenv("LLM_API_TEST_SKIP_HISTORY", "1")
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setattr(web_console, "load_config", lambda: copy.deepcopy(public_config))
    monkeypatch.setattr(image_param_test, "load_config", lambda: copy.deepcopy(public_config))
    monkeypatch.setattr(web_console, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(web_console, "image_provider_has_api_key", lambda *args: True)
    monkeypatch.setattr(web_console.JobManager, "_start_locked", lambda *args: None)

    def no_network_or_credentials(*args, **kwargs):
        raise AssertionError("planning must not read credentials or make a request")

    monkeypatch.setattr(runner, "designated_credential", no_network_or_credentials)
    monkeypatch.setattr(runner.requests.sessions.Session, "request", no_network_or_credentials)
    payload = {"type": "image_param_test", "provider": "openai_official", "model": model,
               "image_plan": {"suite": "resolution", "cases": [EDIT],
                              "api_form": "openai_responses", "route_profile": "vendor_direct"}}
    before = copy.deepcopy(payload)
    response = web_console.app.test_client().post("/api/image-plan/preview", json=payload)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["estimated_case_count"] == 2
    manager = web_console.JobManager()
    job = manager.create(payload)
    frozen_path = job.report_dir / "job_spec.json"
    frozen = json.loads(frozen_path.read_text())
    plan = frozen["image_plan"]
    assert payload == before
    assert plan["cases"] == plan["test_profiles"] == [SEED, EDIT]
    assert plan["estimated_case_count"] == plan["request_cap"] == 2
    command_cases = [job.command[index + 1] for index, arg in enumerate(job.command) if arg == "--case"]
    assert command_cases == plan["cases"]
    monkeypatch.setenv("LOADTEST_JOB_SPEC", str(frozen_path))
    assert image_param_test.main([*job.command[2:], "--dry-run"]) == 0
    actual = json.loads(capsys.readouterr().out)
    assert [case["name"] for case in actual["cases"]] == actual["test_profiles"] == plan["cases"]
    assert actual["request_cap"] == plan["request_cap"]
    package = runner.build_package([model], [name.removeprefix(PREFIX) for name in plan["cases"]])
    assert [case["case_id"] for case in package["cases"]] == [case["metadata"]["case_id"] for case in actual["cases"]]
    assert package["request_cap"] == 2
    # An older persisted plan without the dependency cannot be expanded at run time.
    frozen["image_plan"].update(cases=[EDIT], test_profiles=[EDIT], estimated_case_count=1, request_cap=1)
    frozen_path.write_text(json.dumps(frozen))
    with redirect_stdout(io.StringIO()), pytest.raises(ValueError, match="immutable.*(case|plan|selection)|frozen.*(case|plan|selection)"):
        image_param_test.main([*job.command[2:], "--dry-run"])


@pytest.mark.parametrize("selected,expected_count", [([EDIT], 2), ([EDIT, SEED], 2), ([EDIT, IMAGE_ID_EDIT], 3)])
def test_explicit_plan_uses_expanded_case_profiles_and_request_cap(public_config, selected, expected_count):
    plan = resolve_image_plan(public_config, {"image_plan": {
        "suite": "resolution", "cases": selected, "no_negative": True,
        "api_form": "openai_responses", "route_profile": "vendor_direct",
    }}, "openai_official", MODELS[0], 120)
    assert plan["cases"][0] == SEED
    assert plan["cases"] == plan["test_profiles"]
    assert plan["estimated_case_count"] == plan["request_cap"] == expected_count


def test_existing_package_factory_order_and_budget_are_preserved():
    from scripts import run_gpt_image_25_responses_reference as runner
    package = runner.build_package(list(MODELS))
    expected = [row for model in MODELS for row in responses_cases(model)]
    assert package["cases"] == expected
    assert package["request_cap"] == responses_case_request_cap(expected)
