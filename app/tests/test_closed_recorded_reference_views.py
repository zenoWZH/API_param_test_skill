"""Exact closed observation views remain independent of execution bindings."""
import copy

import pytest

import lib.reference_specs as references
from lib.model_profile_catalog import get_model_profile_catalog
from model_profile_db import Catalog
from model_profile_db.recorded_reference import recorded_reference_capability

TARGETS = (
    ("gemini", "gemini-3.5-flash", "gemini_vertex_generate_content", "gemini_generate_content", "google_vertex",
     "text/google_vertex/gemini/gemini-3.5-flash#gemini-generate-content-default",
     "designated_resource_safety_parameter_observations_20260908"),
    ("glm", "glm-5.3", "zai_general_glm_5_3_chat", "openai_chat_completions", "vendor_direct",
     "text/zhipu/glm/glm-5.3#zai-general-chat", "bounded_parameter_observations_20260908"),
)
FLAGS = ("enabled", "executable", "runner_enabled", "parameter_test_enabled", "pressure_test_enabled")


def selection(target):
    family, model, contract, form, route, _, _ = target
    return dict(modality="text", family=family, model=model, reference_contract_id=contract,
                api_form=form, route_profile=route)


def payload(target, **override):
    family, model, contract, form, route, _, _ = target
    args = dict(modality="text", family=family, model=model, source_id=contract, api_form=form, route_profile=route)
    args.update(override)
    return references.model_reference_spec_payload(**args)


def assert_closed(value):
    cap = value["model_capability_profile"]
    assert value["reference_only"] and value["read_only_core_facts"]
    assert all(value[key] is False and cap[key] is False for key in FLAGS)
    assert value["test_profiles"] == value["tested_params"] == []
    assert cap["test_binding_id"] is None and cap["parameter_test_binding_id"] is None
    assert cap["execution_target"] is None and cap["execution_target_boundary"] == {
        "included": False, "used_for_reference_resolution": False}
    assert "model_profile_database" not in cap and "snapshot_digest" not in cap
    assert value["params"] and all(row["coverage_mode"] == "reference_only"
        and row["execution_coverage"] == "not_executed" and row["test_profiles"] == [] for row in value["params"])


@pytest.mark.parametrize("target", TARGETS)
def test_actual_closed_model_payload_preserves_complete_source_scoped_observations(target):
    catalog = get_model_profile_catalog()
    expected = catalog.get_interface(target[5])["source_conflicts"]
    value = payload(target)
    assert_closed(value)
    cap = value["model_capability_profile"]
    assert cap["interface_id"] == target[5] and cap["source_conflicts"] == expected
    cap["source_conflicts"].clear()
    assert payload(target)["model_capability_profile"]["source_conflicts"] == expected


def test_safety_resource_authority_and_versions_stay_visible():
    value = payload(TARGETS[0])
    cap = value["model_capability_profile"]
    assert "InferenceAI" in value["label"] and cap["execution_provider"] == "inferenceai_gemini"
    assert cap["observation_authority"] == "user_designated_vertex_resource_via_inferenceai"
    assert cap["google_reference_api_version"] == "v1" and cap["supplier_wire_version"] == "v1beta"
    assert cap["google_direct"] is cap["physical_upstream_verified"] is cap["filtering_effect_proven"] is False


def test_general_failed_controls_and_distinct_route_remain_visible():
    cap = payload(TARGETS[1])["model_capability_profile"]
    assert cap["observed_source_id"] == "zai_general"
    assert cap["reference_scope"]["url"] == "https://api.z.ai/api/paas/v4/chat/completions"
    assert cap["coding_equivalence_verified"] is cap["mainland_equivalence_verified"] is False
    assert cap["response_format_effect_verified"] is False and cap["original_false_verdicts_preserved"]
    rows = cap["source_conflicts"][TARGETS[1][6]]
    assert [row["original_verdict"]["pass"] for row in rows] == [False, True, False]


@pytest.mark.parametrize("target", TARGETS)
def test_core_only_catalog_can_display_but_cannot_resolve_parameter_execution(target, monkeypatch):
    import lib.model_profile_catalog as bridge
    catalog = Catalog(get_model_profile_catalog().payload, include_test_extensions=False)
    assert not catalog.test_extensions_loaded
    monkeypatch.setattr(bridge, "get_model_profile_catalog", lambda: catalog)
    monkeypatch.setattr(catalog, "list_test_bindings", lambda **kwargs: pytest.fail("Closed views must not read Test Bindings"))
    assert_closed(payload(target))
    kwargs = selection(target)
    with pytest.raises(RuntimeError, match="test-extension"):
        catalog.resolve_parameter_config(source_id="google_vertex" if target[0] == "gemini" else "zhipu",
            modality="text", family_id=target[0], model_slug=target[1], interface_id=target[5],
            api_form=target[3], contract_id=target[2])


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("override", [
    {"model": "other-model"}, {"model": "models/glm-5.3"}, {"family": "other-family"},
    {"modality": "image"}, {"api_form": None}, {"api_form": "other-form"},
    {"route_profile": None}, {"route_profile": "other-source"},
    {"provider_override": {"enabled": True}}, {"provider_override": {"api_version": "v1beta"}},
])
def test_wrong_selectors_aliases_missing_route_and_overrides_cannot_choose_closed_view(target, override):
    with pytest.raises(ValueError): payload(target, **override)


@pytest.mark.parametrize("target", TARGETS)
def test_readonly_path_does_not_enable_the_existing_runtime_resolver(target):
    with pytest.raises((ValueError, RuntimeError)):
        references.load_model_capability_profile("text", target[0], target[1], reference_source=target[2],
            api_form=target[3], route_profile=target[4], read_only=False)
    assert recorded_reference_capability(get_model_profile_catalog(), **{**selection(target), "reference_contract_id": None}) is None


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("mutation", ["source", "model", "contract", "route", "open_gate", "missing_observation", "cross_source_observation"])
def test_core_record_drift_cannot_be_borrowed_or_turn_the_view_executable(target, mutation, monkeypatch):
    catalog = get_model_profile_catalog()
    get = catalog.get_interface
    changed = copy.deepcopy(get(target[5]))
    if mutation == "source": changed["source_id"] = "anthropic"
    elif mutation == "model": changed["request_model_ids"] = ["different-model"]
    elif mutation == "contract": changed["default_contract_id"] = "other-contract"
    elif mutation == "route": changed["routing_mode"] = "other-source"
    elif mutation == "open_gate": changed["enabled"] = True
    elif mutation == "missing_observation": changed["source_conflicts"].pop(target[6])
    elif target[0] == "gemini": changed["source_conflicts"][target[6]]["source_id"] = "google_ai_studio"
    else: changed["source_conflicts"][target[6]][0]["url"] = "https://api.z.ai/api/coding/paas/v4/chat/completions"
    monkeypatch.setattr(catalog, "get_interface", lambda iid: copy.deepcopy(changed) if iid == target[5] else get(iid))
    with pytest.raises(ValueError): recorded_reference_capability(catalog, **selection(target))
