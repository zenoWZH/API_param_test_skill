import copy

import pytest

import lib.reference_specs as references
from lib.model_profile_catalog import get_model_profile_catalog


@pytest.mark.parametrize("interface_id,observation_key", [
    ("text/google_ai_studio/gemini/gemini-3.7-flash#gemini-generate-content-default", "bounded_parameter_observations_20260907"),
    ("text/minimax/minimax/minimax-m2.5#openai-chat-default", "bounded_parameter_observations_20260907"),
    ("text/minimax/minimax/minimax-m2.7#openai-chat-default", "bounded_parameter_observations_20260907"),
    ("text/minimax/minimax/minimax-m3#openai-chat-default", "bounded_parameter_observations_20260907"),
    ("text/openai/gpt/gpt-4o#openai-chat-default", "bounded_parameter_observations_20260907"),
    ("text/google_ai_studio/gemini/gemini-2.5-pro#gemini-generate-content-default", "bounded_schema_reference_by_api_version_20260907"),
    ("text/xai/grok/grok-4.5#openai-responses-default", "bounded_parameter_observations_20260907"),
    ("text/openai/gpt/gpt-4o-mini#openai-chat-default", "bounded_parameter_observations_20260907"),
    ("text/deepseek/deepseek/deepseek-v4-pro-0813#openai_fim_completions_beta-default", "fim_followup_observations_20260907"),
    ("text/moonshot/kimi/kimi-k3#openai-chat-default", "historical_dynamic_tools_content_20260804"),
])
def test_model_payload_preserves_bounded_source_observations_without_mutating_capability(interface_id, observation_key, monkeypatch):
    catalog = get_model_profile_catalog()
    interface = catalog.get_interface(interface_id)
    profile = catalog.get_profile(interface["profile_id"])
    original = copy.deepcopy(interface["source_conflicts"])
    assert observation_key in original
    cap = references.load_model_capability_profile(
        "text", profile["family_id"], profile["model_slug"], api_form=interface["api_form"],
        route_profile=interface["routing_mode"], reference_source=interface["default_contract_id"], read_only=True,
    )
    cap_before = copy.deepcopy(cap)
    monkeypatch.setattr(references, "load_model_capability_profile", lambda *args, **kwargs: cap)
    payload = references.model_reference_spec_payload(
        "text", profile["family_id"], profile["model_slug"], interface["default_contract_id"],
        api_form=interface["api_form"], route_profile=interface["routing_mode"],
    )
    exposed = payload["model_capability_profile"]["source_conflicts"]
    assert exposed == original
    if interface_id == "text/xai/grok/grok-4.5#openai-responses-default":
        controls = exposed[observation_key]["tool_choice"]["observed"]["controls"]
        negative = next(row for row in controls if row["label"] == "invalid_tool_choice_type")
        assert negative["original_strict_case_verdict"]["pass"] is False
        assert negative["derived_parameter_semantics"]["pass"] is True
        assert negative["derived_semantics_differs_from_original_strict"] is True
    if interface_id == "text/openai/gpt/gpt-4o-mini#openai-chat-default":
        observed = exposed[observation_key]["tool_choice"]["observed"]
        assert observed["forced_response_terminal"] == "stop"
        assert observed["forced_function_shape_and_arguments_observed"] is True
        assert observed["controls"]["forced_function"]["pass"] is False
        assert observed["tool_choice_three_controls_verified"] is False
    exposed[observation_key]["offline_consumer_mutation"] = True
    assert cap == cap_before
    assert catalog.get_interface(interface_id)["source_conflicts"] == original


def test_gemini_schema_payload_preserves_v1_failure_and_v1beta_success_without_cross_version_inference():
    interface_id = "text/google_ai_studio/gemini/gemini-2.5-pro#gemini-generate-content-default"
    catalog = get_model_profile_catalog()
    before = catalog.get_interface(interface_id)
    payload = references.model_reference_spec_payload(
        "text", "gemini", "gemini-2.5-pro", "gemini_native_generate_content",
        api_form="gemini_generate_content", route_profile="google_ai_studio",
    )
    versions = payload["model_capability_profile"]["source_conflicts"]["bounded_schema_reference_by_api_version_20260907"]
    assert set(versions) == {"v1", "v1beta"}
    assert versions["v1"]["all_three_controls_passed"] is False
    assert versions["v1beta"]["all_three_controls_passed"] is True
    for version, observation in versions.items():
        assert observation["api_version"] == version
        assert observation["ordinary_interface_id"] == interface_id
        assert observation["execution_interface_id"] != interface_id
        assert observation["no_cross_version_success_inference"] is True
        assert observation["ordinary_execution_policy_unchanged"] is True
    assert catalog.get_interface(interface_id) == before


def test_read_only_core_display_has_no_execution_profiles_bindings_or_supported_fallback():
    model, contract, form, route = "gemini-2.5-pro", "gemini_native_generate_content", "gemini_generate_content", "google_ai_studio"
    payload = references.model_reference_spec_payload("text", "gemini", model, contract, api_form=form, route_profile=route)
    cap = payload["model_capability_profile"]
    assert cap["read_only_core_facts"] is True
    assert cap["test_binding_id"] is None and cap["parameter_test_binding_id"] is None
    for key in ("enabled", "executable", "runner_enabled", "parameter_test_enabled", "pressure_test_enabled"):
        assert cap[key] is False and payload[key] is False
    assert cap["resolved_expectations"] == {} and cap["supported_profiles"] == []
    assert payload["test_profiles"] == [] and payload["tested_params"] == []
    assert all(row["test_profiles"] == [] and row["local"] == "reference_only" and row["coverage_mode"] == "reference_only" for row in payload["params"])
    assert any(row["official"] == "supported" for row in payload["params"])
    assert cap["source_conflicts"] and cap["parameter_capabilities"]
    with pytest.raises(ValueError, match="Read-only"):
        references.resolve_profile_expectation("text", "gemini", model, "gemini_native_max_output_tokens", capability_profile=cap, reference_source=contract)
    with pytest.raises(ValueError, match="Read-only"):
        references.resolve_parameter_expectation("text", "gemini", model, "generationConfig.maxOutputTokens", capability_profile=cap)
    with pytest.raises((ValueError, KeyError)):
        references.load_model_capability_profile("text", "gemini", model, api_form=form, route_profile=route, reference_source=contract)
    iid = "text/google_ai_studio/gemini/" + model + "#gemini-generate-content-default"
    with pytest.raises((ValueError, KeyError)):
        get_model_profile_catalog().resolve_parameter_config(source_id=route, modality="text", family_id="gemini", model_slug=model,
            interface_id=iid, api_form=form, contract_id=contract)
    from lib.model_profile_catalog import resolve_runtime_parameter_config
    models = {"default": model, "candidates": [model], "families": {model: "gemini"},
              "reference_model_ids": {model: model}, "reference_source_ids": {model: route},
              "routes": {model: {route: {"api_forms": {form: {}}}}}, "default_routes": {model: route},
              "default_api_forms": {model: {route: form}}}
    config = {"active_provider": "display_fixture", "providers": {"display_fixture": {"name": "display_fixture", "reference_source_id": route, "models": models}}}
    with pytest.raises((ValueError, KeyError)):
        resolve_runtime_parameter_config(config, "display_fixture", model, "gemini", route, form)


@pytest.mark.parametrize("change", [
    {"reference_source": "google_ai_studio"},
    {"reference_source": "gemini"},
    {"route_profile": "google_vertex"},
    {"api_form": "gemini_interactions"},
    {"provider_override": {"parameter_test_enabled": True}},
])
def test_read_only_display_does_not_relax_source_form_route_override_or_unobserved_request_alias(change):
    selection = {"modality": "text", "family": "gemini", "model": "gemini-2.5-pro",
                 "api_form": "gemini_generate_content", "route_profile": "google_ai_studio", "reference_source": "gemini_native_generate_content", "read_only": True}
    selection.update(change)
    with pytest.raises((ValueError, KeyError)):
        references.load_model_capability_profile(**selection)


@pytest.mark.parametrize("model", ["gemini-3-flash", "gemini-3-flash-preview"])
def test_read_only_canonical_gemini_name_retains_actual_observed_request_id(model):
    cap = references.load_model_capability_profile("text", "gemini", model, api_form="gemini_generate_content",
            route_profile="google_ai_studio", reference_source="gemini_native_generate_content", read_only=True)
    assert cap["read_only_core_facts"] is True
    observations = cap["source_conflicts"]["bounded_schema_reference_by_api_version_20260907"]
    assert all(row["request_model_id"] == "gemini-3-flash-preview" for row in observations.values())


def test_raw_retention_date_is_copied_without_creating_fact_expiry(monkeypatch):
    cap = references.load_model_capability_profile(
        "text", "minimax", "minimax-m2.7", api_form="openai_chat_completions",
        route_profile="vendor_direct", reference_source="minimax_openai_compat",
    )
    cap["source_conflicts"] = {"retained_observation": {"raw_evidence_expires_at": "2000-01-01T00:00:00Z", "observation_verified": True}}
    monkeypatch.setattr(references, "load_model_capability_profile", lambda *args, **kwargs: cap)
    snapshot = references.capability_profile_snapshot(
        "text", "minimax", "minimax-m2.7", [], api_form="openai_chat_completions",
        route_profile="vendor_direct", reference_source="minimax_openai_compat",
    )
    assert snapshot["source_conflicts"] == cap["source_conflicts"]
    assert snapshot["source_conflicts"]["retained_observation"]["observation_verified"] is True
    assert "facts_expired" not in snapshot["source_conflicts"]["retained_observation"]


def test_completed_lifecycle_observation_is_queryable_while_generic_parameter_entry_stays_closed():
    catalog = get_model_profile_catalog()
    profile_id = "text/google_ai_studio/gemini/gemini-3.7-flash"
    interface_id = profile_id + "#gemini-interactions-stateful-bounded"
    contract = "gemini_3_7_flash_interactions_stateful_bounded"
    interface = catalog.get_interface(interface_id)
    observed = interface["source_conflicts"]["bounded_state_lifecycle_observation_20260907"]["observed"]
    assert observed["requests"] == 4 and observed["child_nonce_recall_verified"] is True
    assert observed["child_first_delete_http_completion_verified"] is True
    assert interface["lifecycle_test_enabled"] is True and interface["lifecycle_request_cap"] == 4
    source = references.get_reference_source(contract)
    for field in ("enabled", "executable", "parameter_test_enabled", "pressure_test_enabled"):
        assert source[field] is False
    with pytest.raises(ValueError, match="disabled|non-executable"):
        catalog.resolve_parameter_config(
            source_id="google_ai_studio", modality="text", family_id="gemini", model_slug="gemini-3.7-flash",
            interface_id=interface_id, api_form="gemini_interactions", contract_id=contract,
            test_binding_id="interface/" + interface_id.replace("#", "/"), parameter_test_binding_id="parameter/" + contract,
        )
    assert catalog.get_interface(profile_id + "#gemini-interactions-default")["executable"] is False


class _VariantCatalogView:
    def __init__(self, catalog, variant=None):
        self.catalog, self.variant = catalog, variant
    def __getattr__(self, name):
        return getattr(self.catalog, name)
    def get_interface(self, interface_id):
        result = self.catalog.get_interface(interface_id)
        if interface_id == "text/google_ai_studio/gemini/gemini-3.1-pro-preview#gemini-generate-content-default":
            namespace = "bounded_schema_reference_request_variants_20260907"
            conflicts = result.setdefault("source_conflicts", {})
            if self.variant is None:
                conflicts.pop(namespace, None)
            else:
                conflicts[namespace] = {"gemini-3.1-pro-preview-customtools": {"v1beta": copy.deepcopy(self.variant)}}
        return result


def _customtools_variant_fixture():
    return {"source_id": "google_ai_studio", "request_model_id": "gemini-3.1-pro-preview-customtools",
            "api_form": "gemini_generate_content", "api_version": "v1beta",
            "ordinary_interface_id": "text/google_ai_studio/gemini/gemini-3.1-pro-preview#gemini-generate-content-default",
            "execution_interface_id": "text/google_ai_studio/gemini/gemini-3.1-pro-preview#gemini-customtools-schema-v1beta-reference",
            "all_three_controls_passed": True, "no_cross_version_success_inference": True,
            "no_parent_request_id_success_inference": True, "ordinary_execution_policy_unchanged": True,
            "v1_was_not_tested_for_this_request_id": True}


def test_unobserved_customtools_request_id_still_cannot_borrow_parent_success(monkeypatch):
    import lib.model_profile_catalog as bindings
    catalog = bindings.get_model_profile_catalog()
    monkeypatch.setattr(bindings, "get_model_profile_catalog", lambda: _VariantCatalogView(catalog))
    with pytest.raises((ValueError, KeyError)):
        references.load_model_capability_profile("text", "gemini", "gemini-3.1-pro-preview-customtools",
            api_form="gemini_generate_content", route_profile="google_ai_studio",
            reference_source="gemini_native_generate_content", read_only=True)


def test_observed_customtools_view_exposes_only_child_v1beta_and_keeps_parent_versions(monkeypatch):
    import lib.model_profile_catalog as bindings
    catalog = bindings.get_model_profile_catalog()
    iid = "text/google_ai_studio/gemini/gemini-3.1-pro-preview#gemini-generate-content-default"
    namespace = "bounded_schema_reference_by_api_version_20260907"
    parent_before = copy.deepcopy(catalog.get_interface(iid)["source_conflicts"][namespace])
    monkeypatch.setattr(bindings, "get_model_profile_catalog", lambda: _VariantCatalogView(catalog, _customtools_variant_fixture()))
    payload = references.model_reference_spec_payload("text", "gemini", "gemini-3.1-pro-preview-customtools", "gemini_native_generate_content",
                api_form="gemini_generate_content", route_profile="google_ai_studio")
    cap = payload["model_capability_profile"]
    selected = cap["source_conflicts"][namespace]
    assert set(selected) == {"v1beta"}
    assert selected["v1beta"]["request_model_id"] == "gemini-3.1-pro-preview-customtools"
    assert selected["v1beta"]["all_three_controls_passed"] is True
    assert cap["observation_projection"] == "request_variant_only"
    assert cap["test_binding_id"] is None and cap["parameter_test_binding_id"] is None
    assert all(cap[key] is False for key in ("enabled", "executable", "runner_enabled", "parameter_test_enabled", "pressure_test_enabled"))
    parent = references.model_reference_spec_payload("text", "gemini", "gemini-3.1-pro-preview", "gemini_native_generate_content",
                api_form="gemini_generate_content", route_profile="google_ai_studio")
    assert parent["model_capability_profile"]["source_conflicts"][namespace] == parent_before
    assert parent_before["v1"]["all_three_controls_passed"] is False
    assert catalog.get_interface(iid)["source_conflicts"][namespace] == parent_before
    with pytest.raises((ValueError, KeyError)):
        references.load_model_capability_profile("text", "gemini", "gemini-3.1-pro-preview-customtools", api_form="gemini_generate_content",
            route_profile="google_ai_studio", reference_source="gemini_native_generate_content")


@pytest.mark.parametrize("mutation", ["parent_id", "wrong_source", "wrong_version", "allow_parent"])
def test_variant_observations_require_exact_request_id_source_and_version(monkeypatch, mutation):
    import lib.model_profile_catalog as bindings
    catalog = bindings.get_model_profile_catalog()
    variant = _customtools_variant_fixture()
    if mutation == "parent_id": variant["request_model_id"] = "gemini-3.1-pro-preview"
    elif mutation == "wrong_source": variant["source_id"] = "google_vertex"
    elif mutation == "wrong_version": variant["api_version"] = "v1"
    else: variant["no_parent_request_id_success_inference"] = False
    monkeypatch.setattr(bindings, "get_model_profile_catalog", lambda: _VariantCatalogView(catalog, variant))
    with pytest.raises(ValueError):
        references.load_model_capability_profile("text", "gemini", "gemini-3.1-pro-preview-customtools", api_form="gemini_generate_content",
            route_profile="google_ai_studio", reference_source="gemini_native_generate_content", read_only=True)
