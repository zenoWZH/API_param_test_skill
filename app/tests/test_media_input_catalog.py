"""Optional media workflows respect configured API identities and old gates."""
from __future__ import annotations

import copy

import pytest

from scripts import media_input_catalog_addition as author


@pytest.fixture
def fixture(monkeypatch):
    from lib import media_input_matrix
    from lib.test_runner.adapters import media_input

    model, source = "gpt-example", "openai"
    profile_id = "text/openai/gpt/" + model
    catalog = {"sources": {source: {"source_type": "official_direct"}},
               "profiles": {profile_id: {"modality": "text", "source_id": source,
                   "model_slug": model, "request_model_ids": [model], "profile_state": "executable",
                   "interfaces": {}}}, "contracts": {}}
    bindings, references = {}, []
    routes = {"vendor_direct": {"api_forms": {}}}
    for form, transport in (("openai_chat_completions", "chat_completions"),
                            ("openai_responses", "openai_responses")):
        interface_id = profile_id + "#" + form
        catalog["profiles"][profile_id]["interfaces"][form] = {
            "api_form": form, "routing_mode": "vendor_direct", "transport_adapter_id": transport,
            "contract_ids": [form], "default_contract_id": form, "enabled": True, "executable": True}
        catalog["contracts"][form] = {"api_form": form}
        bindings["policy/" + form] = {"extension_type": "model_test_policy", "interface_id": interface_id,
                                     "reference_contract_ids": [form], "parameter_test_enabled": True}
        routes["vendor_direct"]["api_forms"][form] = {}
        references.append({"source_id": source, "model": model, "api_form": form,
                           "image": {"status": "supported", "evidence": ["image_doc"]},
                           "audio": {"status": "unsupported", "evidence": ["image_doc"]}})
    config = {"providers": {"openai_official": {"base_url": "https://api.openai.com/v1",
               "reference_source_id": source, "models": {"candidates": [model], "routes": {model: routes},
               "default_api_forms": {model: {"vendor_direct": "openai_chat_completions"}}}}}}
    reference = {"models": references,
                 "sources": [{"id": "image_doc", "url": "https://developers.openai.com/api/docs/guides/images-vision"}],
                 "protocols": [{"source_id": source, "api_form": row["api_form"], "evidence": ["image_doc"]}
                               for row in references]}
    monkeypatch.setattr(media_input_matrix, "load_reference", lambda: copy.deepcopy(reference))
    monkeypatch.setattr(media_input, "source_digest", lambda: "a" * 64)
    monkeypatch.setattr(media_input, "case_ids_for", lambda source, model, form: ["image_png", "image_control"])
    return catalog, bindings, config, reference


def test_all_configured_forms_published_without_changing_core_or_policy(fixture):
    catalog, bindings, config, _ = fixture
    before_catalog, before_bindings = copy.deepcopy(catalog), copy.deepcopy(bindings)
    additions = {"unrelated": ["preserve"]}
    report = author.apply_media_input_workflows(catalog, bindings, additions, config=config)
    assert report["workflows"] == 2
    assert catalog == before_catalog
    assert all(bindings[key] == value for key, value in before_bindings.items())
    assert additions["unrelated"] == ["preserve"]
    rows = [row for row in bindings.values() if row.get("extension_type") == "test_workflow"]
    assert {row["execution_target"]["api_form"] for row in rows} == author.SUPPORTED_FORMS & {
        "openai_chat_completions", "openai_responses"}
    assert all(row["execution_permission"] == {"scope": "exact_workflow", "mode": "inherit"} for row in rows)
    assert all(row["workflow"]["factory_id"] == "media_input" for row in rows)


@pytest.mark.parametrize("layer,field,value", [
    ("policy", "parameter_test_enabled", False), ("policy", "runner_enabled", False),
    ("policy", "disabled_reason", "disabled"), ("interface", "enabled", False),
    ("interface", "executable", False), ("interface", "test_binding_status", "not_certified"),
    ("contract", "test_binding_status", "not_certified"), ("contract", "executable", False),
    ("profile", "profile_state", "identity_only"),
])
def test_existing_disabled_policy_is_never_bypassed(fixture, layer, field, value):
    catalog, bindings, config, _ = fixture
    profile = next(iter(catalog["profiles"].values()))
    targets = {"policy": list(bindings.values()), "interface": list(profile["interfaces"].values()),
               "contract": list(catalog["contracts"].values()), "profile": [profile]}
    for target in targets[layer]:
        target[field] = value
    result = author.build_media_input_workflows(catalog, bindings, config=config)
    assert not result["bindings"]
    assert {row["reason"] for row in result["skipped"]} == {"existing_reference_execution_policy_disabled"}


def test_unsupported_unknown_and_retired_are_inventory_only(fixture):
    catalog, bindings, config, reference = fixture
    reference["models"][0]["image"]["status"] = "unknown"
    reference["models"][1]["lifecycle"] = {"status": "retired"}
    result = author.build_media_input_workflows(catalog, bindings, config=config)
    assert not result["bindings"]
    assert {row["reason"] for row in result["skipped"]} == {
        "no_documented_supported_media_input", "documented_model_retired"}


def test_compatible_gateway_and_overridden_transport_host_are_excluded(fixture):
    catalog, bindings, config, _ = fixture
    provider = config["providers"]["openai_official"]
    config["providers"]["gateway"] = {**copy.deepcopy(provider), "base_url": "https://gateway.example/v1"}
    provider["api_interfaces"] = {"openai_responses": {"base_url": "https://gateway.example/v1"}}
    result = author.build_media_input_workflows(catalog, bindings, config=config)
    assert len(result["bindings"]) == 1
    assert all(row["execution_target"]["provider_id"] == "openai_official" for row in result["bindings"].values())
    assert result["skipped"][0]["reason"] == "transport_endpoint_not_official_source"


def test_registered_alias_uses_request_id_without_requiring_model_slug_equality(fixture):
    catalog, bindings, config, _ = fixture
    profile = next(iter(catalog["profiles"].values()))
    profile["model_slug"] = "gpt-example-snapshot"
    result = author.build_media_input_workflows(catalog, bindings, config=config)
    assert len(result["bindings"]) == 2
    assert all(row["execution_target"]["request_model_id"] == "gpt-example"
               for row in result["bindings"].values())
    profile["request_model_ids"] = ["gpt-example-snapshot"]
    assert not author.build_media_input_workflows(catalog, bindings, config=config)["bindings"]


def test_conflict_is_atomic_and_reapply_is_idempotent(fixture):
    catalog, bindings, config, _ = fixture
    additions = {}
    author.apply_media_input_workflows(catalog, bindings, additions, config=config)
    snapshot = copy.deepcopy((catalog, bindings, additions))
    author.apply_media_input_workflows(catalog, bindings, additions, config=config)
    assert (catalog, bindings, additions) == snapshot
    key = next(key for key in bindings if key.startswith(author.PREFIX))
    bindings[key]["workflow"]["case_ids"] = ["unrelated"]
    changed = copy.deepcopy((catalog, bindings, additions))
    with pytest.raises(ValueError, match="Conflicting existing"):
        author.apply_media_input_workflows(catalog, bindings, additions, config=config)
    assert (catalog, bindings, additions) == changed


def test_empty_cases_are_not_published(fixture, monkeypatch):
    from lib.test_runner.adapters import media_input

    catalog, bindings, config, _ = fixture
    monkeypatch.setattr(media_input, "case_ids_for", lambda *args: [])
    result = author.build_media_input_workflows(catalog, bindings, config=config)
    assert not result["bindings"]
    assert {row["reason"] for row in result["skipped"]} == {"no_executable_media_cases"}


def test_disabled_fable_requires_existing_exact_native_scoped_workflow():
    profile = {"profile_state": "identity_only"}
    interface = {"enabled": False}
    contract = {}
    row = {"source_id": "anthropic", "profile_id": "text/anthropic/claude_fable/claude-fable-5-1",
           "interface_id": "text/anthropic/claude_fable/claude-fable-5-1#anthropic-messages-default",
           "contract_id": "claude_fable_5_1_native_messages", "execution_target": {
               "provider_id": "anthropic_official", "request_model_id": "claude-fable-5-1",
               "api_form": "anthropic_messages", "transport_adapter_id": "claude_messages"}}
    assert author._execution_permission(profile, interface, contract, row, {}) is None
    prior = {**copy.deepcopy(row), "extension_type": "test_workflow", "enabled": True,
             "execution_permission": {"scope": "exact_workflow", "mode": "scoped_research", "approval_id": "existing"}}
    assert author._execution_permission(profile, interface, contract, row, {"prior": prior}) == prior["execution_permission"]
    prior["execution_target"]["provider_id"] = "some_gateway"
    assert author._execution_permission(profile, interface, contract, row, {"prior": prior}) is None


def test_media_provenance_adds_only_exact_documentation_to_owned_records(fixture):
    catalog, bindings, config, _ = fixture
    author.apply_media_input_workflows(catalog, bindings, {}, config=config)
    records = {"unrelated": {"official_urls": ["https://example.test/old"]}}
    for key, row in bindings.items():
        if key.startswith(author.PREFIX):
            records[row["provenance_record_id"]] = {"official_urls": ["https://developers.openai.com/api/docs/models"]}
    author.attach_media_input_provenance(records, bindings)
    assert records["unrelated"] == {"official_urls": ["https://example.test/old"]}
    assert all("https://developers.openai.com/api/docs/guides/images-vision" in row["section_summary"]
               for key, row in records.items() if key != "unrelated")
    assert all(row["official_urls"] == ["https://developers.openai.com/api/docs/models"]
               for key, row in records.items() if key != "unrelated")


@pytest.mark.skip(reason="Source-only catalog regeneration pipeline is not part of the standalone runtime")
def test_owned_yaml_update_preserves_unrelated_bytes():
    from scripts.build_test_workflow_artifacts import update_workflow_yaml

    original = "test_bindings:\n  unrelated: {x: 1} # keep this comment\nprovenance_records:\n  old: {x: 2}\n"
    expected = {"test_bindings": {"unrelated": {"x": 1}, "workflow/media-input/fixture": {"enabled": True}},
                "provenance_records": {"old": {"x": 2}, "test-binding/workflow/media-input/fixture": {"x": 3}}}
    updated = update_workflow_yaml(original, expected)
    assert "  unrelated: {x: 1} # keep this comment\n" in updated
    assert "  old: {x: 2}\n" in updated
    assert update_workflow_yaml(updated, expected) == updated


def test_sanitized_local_provider_target_is_published_without_private_config(fixture):
    catalog, bindings, config, _ = fixture
    target = {"provider_id": "local_official", "source_id": "openai", "model": "gpt-example",
              "route_profile": "vendor_direct", "api_form": "openai_responses",
              "profile_id": "text/openai/gpt/gpt-example",
              "interface_id": "text/openai/gpt/gpt-example#openai_responses",
              "contract_id": "openai_responses", "transport_adapter_id": "openai_responses",
              "endpoint_class": "official_direct"}
    result = author.build_media_input_workflows(catalog, bindings, config=config, configured_targets=[target])
    assert len(result["bindings"]) == 3
    row = result["bindings"]["workflow/media-input/local_official/gpt-example/openai_responses"]
    assert row["execution_target"]["provider_id"] == "local_official"
    assert "base_url" not in str(row)
    # Existing public settings win: a snapshot cannot replace a gateway URL.
    config["providers"]["local_official"] = {"base_url": "https://gateway.example/v1"}
    result = author.build_media_input_workflows(catalog, bindings, config=config, configured_targets=[target])
    assert len(result["bindings"]) == 2


def test_sanitized_local_selector_cannot_override_catalog_identity(fixture):
    catalog, bindings, config, _ = fixture
    target = {"provider_id": "local_official", "source_id": "openai", "model": "gpt-example",
              "route_profile": "vendor_direct", "api_form": "openai_responses",
              "interface_id": "text/openai/gpt/some-other-model#openai_responses"}
    result = author.build_media_input_workflows(catalog, bindings, config=config, configured_targets=[target])
    assert len(result["bindings"]) == 2
    assert result["skipped"][0]["reason"] == "exact_reference_identity_missing_or_ambiguous"


def test_coding_endpoint_cannot_inherit_general_media_documentation(fixture):
    catalog, bindings, config, _ = fixture
    target = {"provider_id": "local_official", "source_id": "openai", "model": "gpt-example",
              "route_profile": "vendor_direct", "api_form": "openai_responses", "endpoint_class": "zai_coding"}
    result = author.build_media_input_workflows(catalog, bindings, config=config, configured_targets=[target])
    assert len(result["bindings"]) == 2
    assert result["skipped"][0]["reason"] == "coding_endpoint_media_not_documented"


def test_workflow_identifier_is_lowercase_but_request_model_preserves_case(fixture):
    catalog, bindings, config, reference = fixture
    models = config["providers"]["openai_official"]["models"]
    models["candidates"] = ["MiniMax-M3"]
    models["routes"]["MiniMax-M3"] = models["routes"].pop("gpt-example")
    next(iter(catalog["profiles"].values()))["request_model_ids"] = ["MiniMax-M3"]
    for row in reference["models"]:
        row["model"] = "MiniMax-M3"
    result = author.build_media_input_workflows(catalog, bindings, config=config)
    assert len(result["bindings"]) == 2
    assert all("/minimax-m3/" in key for key in result["bindings"])
    assert all(row["execution_target"]["request_model_id"] == "MiniMax-M3"
               for row in result["bindings"].values())
