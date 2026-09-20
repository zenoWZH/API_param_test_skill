from __future__ import annotations

import copy
import json
import pytest

from lib.banana_generate_content import build_banana_generate_content_cases, contract_id_for_model
from scripts import image_param_test


@pytest.mark.parametrize("origin, api_version", [
    ("https://generativelanguage.googleapis.com", "v1beta"),
    ("https://gateway.example", "v1"),
])
def test_cli_rebuilds_exact_pixels_from_restored_snapshot(monkeypatch, capsys, origin, api_version):
    model = "gemini-3-pro-image"
    case_id = build_banana_generate_content_cases(model, diagnostic=True)[0].name
    current = {
        "profile_status": "registered", "known_model": True, "known_api_profile": True,
        "route_profile_known": True, "parameter_test_enabled": True,
        "test_policy_parameter_test_enabled": True, "api_form": "gemini_generate_content",
        "transport": "gemini-generate-content", "route_profile": "google_ai_studio", "source_id": "google_ai_studio",
        "default_reference_source": contract_id_for_model(model),
        "model_profile_database": {"fixture": "current"},
        "image_case_expectations": {
            case_id: {"expectation": "supported", "expected_size": [1024, 1024],
                      "documentation_match": True, "evidence_refs": ["current_fact"]}
        },
    }
    restored = copy.deepcopy(current)
    restored["image_case_expectations"][case_id] = {
        "expectation": "supported", "expected_size": [1056, 1056],
        "documentation_match": False, "evidence_refs": ["old_fact1", "old_fact2", "old_fact3"],
    }
    binding = {"source_id": "google_ai_studio", "execution_target": {"request_model_id": model,
                                    "route_profile": "google_ai_studio",
                                    "api_form": "gemini_generate_content"},
               "interface": {"api_form": "gemini_generate_content"},
               "test_binding": restored,
               "reference_contract_id": contract_id_for_model(model),
               "parameter_test_binding": {"test_cases": ["fixture"]}}
    monkeypatch.setattr(image_param_test, "load_config", lambda: {})
    monkeypatch.setattr(image_param_test, "load_job_spec", lambda _: {
        "image_plan": {"api_version": api_version},
        "model_profile_database": {"fixture": "historical_snapshot"},
    })
    monkeypatch.setattr(image_param_test, "load_model_capability_profile", lambda *a, **k: copy.deepcopy(current))
    monkeypatch.setattr(image_param_test, "binding_from_database_snapshot", lambda _: binding)
    monkeypatch.setattr(image_param_test, "capability_profile_from_database_snapshot", lambda _: {
        **copy.deepcopy(restored), "modality": "image", "family": "banana", "model": model,
    })
    if hasattr(image_param_test, "capability_profile_from_binding"):
        monkeypatch.setattr(image_param_test, "capability_profile_from_binding", lambda _: copy.deepcopy(restored))
    monkeypatch.setattr(image_param_test, "capability_profile_snapshot", lambda *a, **k: {})
    for name, replacement in {
        "require_official_reference_binding": lambda value: value,
        "resolve_runtime_test_policy": lambda value: {},
        "database_snapshot": lambda value: {"fixture": "historical_snapshot"},
        "resolve_test_binding_id": lambda value: "fixture",
        "_snapshot_runtime_identity_conflicts": lambda *a, **k: [],
    }.items():
        if hasattr(image_param_test, name):
            monkeypatch.setattr(image_param_test, name, replacement)
    assert image_param_test.main([
        "--base-url", origin, "--model", model,
        "--family", "banana", "--route-profile", "google_ai_studio",
        "--transport", "gemini-generate-content", "--api-form", "gemini_generate_content",
        "--api-version", api_version, "--suite", "smoke", "--case", case_id,
        "--no-cross-control", "--dry-run",
    ]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["api_version"] == api_version
    assert plan["endpoint"] == f"{origin}/{api_version}/models/{model}:generateContent"
    assert plan["reference_api_version"] == "v1beta"
    if api_version == "v1":
        assert plan["comparison_scope"] == "adapter_only"
        assert plan["certified_route_contract_pass"] is False
    assert plan["cases"][0]["expected_size"] == [1056, 1056]
    assert plan["cases"][0]["metadata"]["beta_evidence_refs"] == ["old_fact1", "old_fact2", "old_fact3"]
    assert current["image_case_expectations"][case_id]["expected_size"] == [1024, 1024]
