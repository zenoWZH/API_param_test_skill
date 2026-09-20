"""Keep the documented V4.1 Flash identity separate from older live evidence."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages" / "model-profile-db"
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))

from model_profile_db import ParameterConfigResolutionError, load_catalog  # noqa: E402


MODEL_SLUG = "deepseek-v4.1-flash"
PROFILE_ID = "text/deepseek/deepseek/" + MODEL_SLUG
CANONICAL_ID = "deepseek/" + MODEL_SLUG
REQUEST_MODEL_ID = "deepseek-flash"
API_FORMS = {
    "openai_chat_completions",
    "openai_responses",
    "anthropic_messages",
    "deepseek_beta_chat_prefix",
    "openai_fim_completions_beta",
}


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


@pytest.fixture(scope="module")
def profile(catalog):
    return catalog.get_profile(PROFILE_ID)


def _selection(interface, **overrides):
    return {
        "source_id": "deepseek",
        "modality": "text",
        "family_id": "deepseek",
        "model_slug": MODEL_SLUG,
        "interface_id": interface["interface_id"],
        "api_form": interface["api_form"],
        **overrides,
    }


def _contract(catalog, profile, api_form):
    interface = next(row for row in profile["interfaces"] if row["api_form"] == api_form)
    return catalog.get_contract(interface["default_contract_id"])


def test_official_request_id_resolves_only_the_new_source_scoped_identity(catalog, profile):
    canonical = catalog.get_model(CANONICAL_ID)
    assert canonical["owner_source_id"] == "deepseek"
    assert canonical["family_id"] == "deepseek"
    assert canonical["aliases"] == []
    assert profile["source_id"] == "deepseek"
    assert profile["canonical_model_id"] == CANONICAL_ID
    assert profile["request_model_ids"] == [REQUEST_MODEL_ID]
    assert profile["request_model_id_semantics"] == "exact_source_model_id"
    assert profile["verification_status"] == "exact_official_source_model_id"
    for model in (MODEL_SLUG, REQUEST_MODEL_ID, CANONICAL_ID):
        rows = catalog.list_profiles(source="deepseek", family="deepseek", model=model)
        assert [row["profile_id"] for row in rows] == [PROFILE_ID]
    assert catalog.list_profiles(source="aliyun_maas", model=REQUEST_MODEL_ID) == []
    assert catalog.list_profiles(source="deepseek", family="gpt", model=REQUEST_MODEL_ID) == []


def test_previous_flash_and_pro_request_ids_keep_their_historical_profiles(catalog, profile):
    for request_id, historical_slug in (
        ("deepseek-v4-flash", "deepseek-v4-flash-0731"),
        ("deepseek-v4-pro", "deepseek-v4-pro-0813"),
    ):
        rows = catalog.list_profiles(source="deepseek", model=request_id)
        assert [row["model_slug"] for row in rows] == [historical_slug]
        assert all(row["profile_id"] != PROFILE_ID for row in rows)
    assert profile.get("response_identity", {}).get("model_version") != "DeepSeek-V4-Flash-0731"
    new_contracts = {contract_id for interface in profile["interfaces"] for contract_id in interface["contract_ids"]}
    historical_contracts = {
        contract_id
        for row in catalog.list_profiles(source="deepseek")
        if row["profile_id"] != PROFILE_ID
        for interface in catalog.get_profile(row["profile_id"])["interfaces"]
        for contract_id in interface["contract_ids"]
    }
    assert new_contracts.isdisjoint(historical_contracts)


def test_all_documented_forms_have_separate_non_executable_contracts(catalog, profile):
    assert profile["profile_state"] == "identity_only"
    assert {interface["api_form"] for interface in profile["interfaces"]} == API_FORMS
    contract_ids = []
    for interface in profile["interfaces"]:
        assert interface["request_model_ids"] == [REQUEST_MODEL_ID]
        assert interface["enabled"] is False
        assert interface["executable"] is False
        assert interface["disabled_reason"]
        assert interface["test_binding_status"] != "required"
        assert len(interface["contract_ids"]) == 1
        contract_id = interface["default_contract_id"]
        contract_ids.append(contract_id)
        contract = catalog.get_contract(contract_id)
        assert contract["source_id"] == "deepseek"
        assert contract["source_ids"] == ["deepseek"]
        assert contract["family_id"] == "deepseek"
        assert contract["api_form"] == interface["api_form"]
        assert not contract.get("parent_contract_id")
        assert not contract.get("extends")
        bindings = catalog.list_test_bindings(interface_id=interface["interface_id"], extension_type="research_parameter_matrix")
        assert len(bindings) == 1
        assert bindings[0]["extension_type"] == "research_parameter_matrix"
        assert bindings[0]["dedicated_execution_enabled"] is True
        assert bindings[0]["parameter_test_enabled"] is False
        assert bindings[0]["pressure_test_enabled"] is False
        assert catalog.list_test_bindings(contract_id=contract_id, extension_type="research_parameter_matrix") == bindings
    assert len(set(contract_ids)) == len(API_FORMS)


def test_documentation_cannot_be_executed_by_parameter_resolution(catalog, profile):
    assert catalog.list_parameter_configs(source_id="deepseek", model_slug=MODEL_SLUG) == []
    for interface in profile["interfaces"]:
        with pytest.raises(ParameterConfigResolutionError, match="not executable"):
            catalog.resolve_parameter_config(**_selection(interface))


@pytest.mark.parametrize("override", [{"source_id": "aliyun_maas"}, {"family_id": "gpt"}, {"model_slug": REQUEST_MODEL_ID}])
def test_parameter_resolution_does_not_infer_or_substitute_identity(catalog, profile, override):
    with pytest.raises(KeyError):
        catalog.resolve_parameter_config(**_selection(profile["interfaces"][0], **override))


def test_new_metadata_has_current_source_local_provenance(catalog, profile):
    records = catalog.payload["provenance_records"]
    rows = [catalog.get_model(CANONICAL_ID), profile]
    for interface in profile["interfaces"]:
        rows.extend([interface, catalog.get_contract(interface["default_contract_id"])])
    for row in rows:
        record = records[row["provenance_record_id"]]
        assert record["source_id"] == "deepseek"
        assert record["retrieved_at"] == "2026-09-17"
        assert record["official_urls"]
        assert all(url.startswith("https://api-docs.deepseek.com/") for url in record["official_urls"])


def test_chat_has_v4_1_limits_and_mode_specific_sampling(catalog, profile):
    contract = _contract(catalog, profile, "openai_chat_completions")
    caps = contract["parameter_capabilities"]
    constraints = contract["parameter_constraints"]
    assert caps["model"]["required"] is True
    assert caps["messages"]["required"] is True
    assert constraints["max_tokens"]["minimum"] == 1
    assert constraints["max_tokens"]["maximum"] == 393216
    assert constraints["max_tokens"]["default_by_mode"] == {
        "non_thinking": 8192,
        "thinking": 65536,
        "thinking_max_effort": 131072,
    }
    assert constraints["reasoning_effort"]["enum"] == ["none", "low", "high", "max"]
    assert constraints["reasoning_effort"]["default"] == "high"
    assert caps["temperature"]["state"] == "conditional"
    assert constraints["temperature"]["thinking_enabled_behavior"] == "accepted but ignored"
    assert caps["top_p"]["state"] == "conditional"
    assert constraints["top_p"]["thinking_effective_minimum"] == 0.95
    assert constraints["top_p"]["non_thinking_behavior"] == "fixed at 1.0; supplied value ignored"
    assert caps["logprobs"]["state"] == "supported"
    assert constraints["top_logprobs"]["maximum"] == 20
    assert constraints["top_logprobs"]["requires"] == {"logprobs": True}
    assert caps["response_format.json_schema"]["state"] == "unsupported"
    for parameter in ("seed", "n", "logit_bias", "max_completion_tokens", "parallel_tool_calls"):
        assert caps[parameter]["state"] == "unknown"


def test_tool_choice_restrictions_remain_specific_to_each_api_form(catalog, profile):
    chat = _contract(catalog, profile, "openai_chat_completions")["parameter_constraints"]
    assert chat["tool_choice"]["thinking_allowed"] == ["none", "auto"]
    assert chat["tool_choice"]["thinking_disallowed_error_status"] == 400
    for form, parameter in (("openai_responses", "tool_choice"), ("anthropic_messages", "tool_choice.type")):
        contract = _contract(catalog, profile, form)
        assert contract["parameter_capabilities"][parameter]["state"] == "supported"
        constraints = contract["parameter_constraints"][parameter]
        assert constraints["thinking_mode_restrictions"] == "unknown_for_this_api_form"
        assert "thinking_disallowed_error_status" not in constraints


def test_fim_bound_and_sampling_are_not_copied_from_chat(catalog, profile):
    contract = _contract(catalog, profile, "openai_fim_completions_beta")
    caps = contract["parameter_capabilities"]
    constraints = contract["parameter_constraints"]
    assert caps["model"]["required"] is True
    assert caps["prompt"]["required"] is True
    assert constraints["max_tokens"]["maximum"] == 4096
    assert caps["top_p"]["state"] == "supported"
    assert "non_thinking_behavior" not in constraints["top_p"]
    assert set(constraints["echo"]["incompatible_with"]) == {"suffix", "logprobs"}
    assert constraints["logprobs"]["type"] == "integer"
    assert constraints["logprobs"]["maximum"] == 20


def test_prefix_requires_beta_and_last_assistant_message(catalog, profile):
    contract = _contract(catalog, profile, "deepseek_beta_chat_prefix")
    constraints = contract["parameter_constraints"]
    assert constraints["messages[-1].role"]["required_value"] == "assistant"
    assert constraints["messages[-1].prefix"]["required_value"] is True
    assert contract["protocol_constraints"]["path_template"] == "/beta/chat/completions"


def test_stream_usage_contract_matches_deepseek_terminal_chunk(catalog, profile):
    for form in ("openai_chat_completions", "openai_fim_completions_beta"):
        constraints = _contract(catalog, profile, form)["parameter_constraints"]
        usage = constraints["stream_options.include_usage"]
        assert usage["requires"] == {"stream": True}
        assert usage["separate_usage_only_chunk"] is False
        assert usage["final_choices_count"] == 1


def test_compatibility_forms_preserve_documented_thinking_and_statefulness(catalog, profile):
    responses = _contract(catalog, profile, "openai_responses")
    constraints = responses["parameter_constraints"]
    assert constraints["reasoning.effort"]["enum"] == ["none", "low", "high", "max"]
    assert constraints["max_output_tokens"]["includes_reasoning_tokens"] is True
    assert constraints["parallel_tool_calls"]["effective_value"] is True
    assert responses["parameter_capabilities"]["previous_response_id"]["state"] == "unsupported"
    assert constraints["previous_response_id"]["request_behavior"] == "silently_ignored"
    anthropic = _contract(catalog, profile, "anthropic_messages")
    constraints = anthropic["parameter_constraints"]
    for field in ("model", "messages", "max_tokens"):
        assert anthropic["parameter_capabilities"][field]["required"] is True
    assert constraints["thinking.type"]["enum"] == ["enabled", "disabled"]
    assert constraints["thinking.type"]["default"] == "enabled"
    assert constraints["output_config.effort"]["enum"] == ["low", "high", "max"]
    assert constraints["top_p"]["thinking_effective_minimum"] == 0.95


def test_ignored_parameters_do_not_claim_semantic_effect(catalog, profile):
    for form, names in (
        ("openai_chat_completions", ("frequency_penalty", "presence_penalty")),
        ("openai_fim_completions_beta", ("frequency_penalty", "presence_penalty")),
        ("openai_responses", ("parallel_tool_calls", "reasoning.summary", "text.verbosity")),
        ("anthropic_messages", ("thinking.budget_tokens", "top_k", "tool_choice.disable_parallel_tool_use")),
    ):
        contract = _contract(catalog, profile, form)
        for name in names:
            assert contract["parameter_capabilities"][name]["state"] == "conditional"
            constraints = contract["parameter_constraints"][name]
            assert constraints["accepted_but_ignored"] is True
            assert constraints["has_effect"] is False
