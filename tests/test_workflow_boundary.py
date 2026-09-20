from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]


def _workflow_module():
    spec = importlib.util.spec_from_file_location(
        "standalone_workflow", SKILL_ROOT / "scripts" / "workflow.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_onboard_proposal_is_json_and_declares_no_mutation(
    monkeypatch, capsys
) -> None:
    workflow = _workflow_module()
    monkeypatch.setattr(
        workflow,
        "_load_instance",
        lambda provider, model: {"provider": provider, "model": model},
    )
    monkeypatch.setattr(
        workflow,
        "_build_profile_proposal",
        lambda instance: {
            "schema": "llm-api-test.mpdb-review-proposal.v1",
            "mutates_database": False,
        },
    )
    result = workflow.cmd_onboard_propose(
        SimpleNamespace(provider="p", model="m")
    )
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mutates_database"] is False


def test_profile_proposal_resolves_route_and_api_form_from_config(
    monkeypatch,
) -> None:
    workflow = _workflow_module()
    import lib.config as config

    monkeypatch.setattr(workflow, "_onboard_evidence", lambda instance: "evidence")
    monkeypatch.setattr(workflow, "_resolve_existing_mpdb_binding", lambda instance: None)
    monkeypatch.setattr(config, "load_config", lambda: {"sentinel": True})
    monkeypatch.setattr(
        config,
        "get_model_family",
        lambda loaded, model, provider: "family-from-config",
    )
    monkeypatch.setattr(
        config,
        "get_model_route_profile",
        lambda loaded, model, provider: "route-from-config",
    )
    monkeypatch.setattr(
        config,
        "get_model_api_form",
        lambda loaded, model, provider, **kwargs: "form-from-config",
    )

    proposal = workflow._build_profile_proposal(
        {"provider": "provider", "model": "model", "job_ids": {}}
    )
    target = proposal["execution_target"]
    assert target["family"] == "family-from-config"
    assert target["route_profile"] == "route-from-config"
    assert target["api_form"] == "form-from-config"


def test_onboard_apply_only_records_verified_existing_binding(
    monkeypatch, capsys
) -> None:
    workflow = _workflow_module()
    instance = {"provider": "p", "model": "m", "history": []}
    saved = []
    monkeypatch.setattr(workflow, "_load_instance", lambda provider, model: instance)
    monkeypatch.setattr(workflow, "_onboard_evidence", lambda value: "evidence")
    monkeypatch.setattr(
        workflow,
        "_resolve_existing_mpdb_binding",
        lambda value: {"profile_id": "text/source/family/model"},
    )
    monkeypatch.setattr(workflow, "_save_instance", lambda value: saved.append(value))
    result = workflow.cmd_onboard_apply(
        SimpleNamespace(
            provider="p",
            model="m",
            yes=True,
            review_ref="review-123",
            notes="approved",
        )
    )
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert payload["database_mutated"] is False
    assert payload["verified"] is True
    assert instance["current_node"] == "done"
    assert instance["history"][-1]["review_ref"] == "review-123"
    assert saved == [instance]


def test_existing_binding_keeps_source_and_contract_separate() -> None:
    workflow = _workflow_module()
    binding = workflow._resolve_existing_mpdb_binding(
        {"provider": "yibu", "model": "deepseek-v4-flash"}
    )
    assert binding["source_id"] == "deepseek"
    assert binding["reference_contract_id"] == "deepseek_chat"


def test_existing_binding_resolves_contract_within_explicit_source(
    monkeypatch,
) -> None:
    workflow = _workflow_module()
    import lib.config as config_module

    config = copy.deepcopy(config_module.load_config())
    provider = copy.deepcopy(config["providers"]["yibu"])
    provider["reference_source_id"] = "aliyun_maas"
    config["providers"]["cross_source"] = provider
    monkeypatch.setattr(config_module, "load_config", lambda: config)

    binding = workflow._resolve_existing_mpdb_binding(
        {"provider": "cross_source", "model": "deepseek-v4-flash"}
    )
    assert binding["source_id"] == "aliyun_maas"
    assert binding["profile_id"].startswith("text/aliyun_maas/")
    assert binding["reference_contract_id"] == "aliyun_deepseek_v4_openai_compat"


def test_existing_binding_fails_closed_when_source_has_no_model_binding(
    monkeypatch,
) -> None:
    workflow = _workflow_module()
    import lib.config as config_module

    config = copy.deepcopy(config_module.load_config())
    provider = copy.deepcopy(config["providers"]["yibu"])
    provider["reference_source_id"] = "openai"
    config["providers"]["missing_source"] = provider
    monkeypatch.setattr(config_module, "load_config", lambda: config)

    with pytest.raises(ValueError, match="source='openai'"):
        workflow._resolve_existing_mpdb_binding(
            {"provider": "missing_source", "model": "deepseek-v4-flash"}
        )


def test_onboard_apply_requires_review_reference_and_does_not_save(
    monkeypatch, capsys
) -> None:
    workflow = _workflow_module()
    instance = {"provider": "p", "model": "m", "history": []}
    saved = []
    monkeypatch.setattr(workflow, "_load_instance", lambda provider, model: instance)
    monkeypatch.setattr(workflow, "_onboard_evidence", lambda value: "evidence")
    monkeypatch.setattr(
        workflow,
        "_resolve_existing_mpdb_binding",
        lambda value: {"profile_id": "text/source/family/model"},
    )
    monkeypatch.setattr(workflow, "_save_instance", lambda value: saved.append(value))
    result = workflow.cmd_onboard_apply(
        SimpleNamespace(
            provider="p", model="m", yes=True, review_ref="", notes=None
        )
    )
    assert result == 2
    payload = json.loads(capsys.readouterr().err)
    assert "--review-ref" in payload["error"]
    assert saved == []
    assert instance.get("current_node") is None


def test_legacy_pass_is_not_an_automatic_qualification_decision(monkeypatch, tmp_path):
    workflow = _workflow_module()
    report = tmp_path / "old-job"
    report.mkdir()
    spec = {"schema_version": 3, "type": "param_test", "provider": "p", "model": "m"}
    (report / "job_spec.json").write_text(json.dumps(spec))
    (report / "verdict.json").write_text(json.dumps({"pass": True}))
    monkeypatch.setattr(workflow, "_jobs_root", lambda: tmp_path)
    instance = {"provider": "p", "model": "m", "job_ids": {"param_test": "old-job"}}
    assert workflow._auto_outcome({"auto": "verdict_pass", "source_node": "param_test"}, instance) is None
    with pytest.raises(workflow.OnboardPrerequisiteError, match="historical_or_incomplete"):
        workflow._job_verdict(instance, "param_test")


def test_a_passed_research_workflow_does_not_claim_full_parameter_qualification(monkeypatch):
    workflow = _workflow_module()
    monkeypatch.setattr(workflow, "classify_result", lambda *_: {"current": True, "pass": True})
    result = workflow._qualification_verdict(
        {"type": "param_test", "schema_version": 6, "test_workflow_snapshot": {"fixture": True}},
        {"pass": True, "full_parameter_certification": False},
    )
    assert result["reported_pass"] is True and result["pass"] is None
    assert result["qualification_gap"] == "bounded_workflow_is_not_full_parameter_certification"


@pytest.mark.parametrize("spec,verdict", [
    ({"type": "param_test", "parameter_suite": "fixed-fim"}, {"pass": True, "full_parameter_matrix_verified": False}),
    ({"type": "param_test", "execution_plan": {"definition": {"factory": {"factory_id": "source_fixed_parameter"}}}}, {"pass": True}),
    ({"type": "param_test"}, {"pass": True, "bounded_beta_observations": {}, "full_parameter_matrix_verified": False}),
])
def test_fixed_subset_results_cannot_qualify_as_a_complete_parameter_matrix(monkeypatch, spec, verdict):
    workflow = _workflow_module()
    monkeypatch.setattr(workflow, "classify_result", lambda *_: {"current": True, "pass": True})
    result = workflow._qualification_verdict(spec, verdict)
    assert result["reported_pass"] is True and result["pass"] is None
    assert result["qualification_gap"] == "bounded_workflow_is_not_full_parameter_certification"
