import copy

import pytest

import lib.reference_specs as references
from lib.model_profile_catalog import get_model_profile_catalog


STREAM_PARAMETER = "stream_options.include_obfuscation"
CHAT_MODELS = (
    ("gpt-4o", "openai_chat_base"),
    ("gpt-4o-mini", "openai_chat_base"),
    ("gpt-5-nano", "openai_gpt5_chat"),
    ("gpt-5.1", "openai_gpt5_chat"),
    ("gpt-5.2", "openai_gpt5_chat"),
    ("gpt-5.4", "openai_gpt5_chat"),
    ("gpt-5.4-mini", "openai_gpt5_chat"),
    ("gpt-5.4-nano", "openai_gpt5_chat"),
    ("gpt-5.5", "openai_gpt5_chat"),
    ("gpt-5.6-luna", "openai_gpt56_chat"),
    ("gpt-5.6-sol", "openai_gpt56_chat"),
    ("gpt-5.6-terra", "openai_gpt56_chat"),
    ("gpt-6-astra", "openai_gpt6_astra_chat"),
)
RESPONSE_MODELS = tuple(
    (model, {
        "openai_chat_base": "openai_responses",
        "openai_gpt5_chat": "openai_responses",
        "openai_gpt56_chat": "openai_gpt56_responses",
        "openai_gpt6_astra_chat": "openai_gpt6_astra_responses",
    }[contract])
    for model, contract in CHAT_MODELS
) + (("gpt-5.3-codex", "openai_responses"),)


def _payload(model="gpt-4o", contract="openai_chat_base", form="openai_chat_completions"):
    return references.model_reference_spec_payload(
        "text", "gpt", model, contract, api_form=form, route_profile="vendor_direct",
    )


def _rows(payload):
    return {row["parameter"]: row for row in payload["comparison"]}


@pytest.mark.parametrize("model,contract,form", [
    *(tuple(pair) + ("openai_chat_completions",) for pair in CHAT_MODELS),
    *(tuple(pair) + ("openai_responses",) for pair in RESPONSE_MODELS),
])
def test_interface_stream_parameter_is_visible_without_fabricated_coverage(model, contract, form):
    source_before = references.reference_spec_payload(contract)
    assert STREAM_PARAMETER not in _rows(source_before)
    payload = _payload(model, contract, form)
    capability = payload["model_capability_profile"]
    row = _rows(payload)[STREAM_PARAMETER]
    assert row["official"] == row["local"] == row["model_expectation"] == "supported"
    assert row["state"] == "supported"
    assert row["coverage_mode"] == "not_tested" and row["coverage"] == "not tested"
    assert row["test_profiles"] == [] and row["profile_expectations"] == {}
    assert STREAM_PARAMETER in payload["untested_params"]
    assert STREAM_PARAMETER not in payload["tested_params"]
    assert payload["test_profiles"] == source_before["test_profiles"]
    assert row["parameter_constraints"] == {
        "allowed_values": [True, False], "default": True,
        "requires": {"stream": True}, "value_type": "boolean",
    }
    assert row["source_id"] == "openai"
    assert row["interface_id"] == capability["interface_id"]
    assert row["official_sources"] == source_before["official_sources"]
    assert payload["params"] == payload["comparison"]
    assert payload["param_count"] == len(payload["params"]) == len(_rows(payload))
    assert capability["known_model"] is True and capability["known_api_profile"] is True
    assert not capability.get("read_only_core_facts")
    assert references.reference_spec_payload(contract) == source_before


@pytest.mark.parametrize("model,contract,form,parameter", [
    ("gpt-5-nano", "openai_responses", "openai_responses", "stop"),
    ("gpt-5-nano", "openai_responses", "openai_responses", "temperature"),
    ("gpt-6-astra", "openai_gpt6_astra_responses", "openai_responses", "top_logprobs"),
    ("gpt-6-astra", "openai_gpt6_astra_chat", "openai_chat_completions", "tools"),
])
def test_model_specific_unsupported_state_is_not_replaced_by_contract_default(model, contract, form, parameter):
    payload = _payload(model, contract, form)
    row = _rows(payload)[parameter]
    assert row["state"] == row["official"] == row["model_expectation"] == row["local"] == "unsupported"
    if parameter not in _rows(references.reference_spec_payload(contract)):
        assert row["coverage_mode"] == "not_tested" and row["test_profiles"] == []
        assert parameter not in payload["tested_params"]


def test_existing_case_coverage_is_retained_and_nested_views_are_independent():
    source_before = references.reference_spec_payload("openai_chat_base")
    catalog = get_model_profile_catalog()
    interface_id = "text/openai/gpt/gpt-4o#openai-chat-default"
    interface_before = catalog.get_interface(interface_id)
    payload = _payload()
    rows = _rows(payload)
    for parameter, before in _rows(source_before).items():
        assert rows[parameter]["coverage"] == before["coverage"]
        assert rows[parameter]["coverage_mode"] == before["coverage_mode"]
        assert rows[parameter]["test_profiles"] == before["test_profiles"]
    assert payload["tested_params"] == source_before["tested_params"]
    row = rows[STREAM_PARAMETER]
    row["parameter_constraints"]["requires"]["stream"] = False
    row["official_sources"].append("https://example.invalid/mutation")
    assert payload["model_capability_profile"]["parameter_constraints"][STREAM_PARAMETER]["requires"] == {"stream": True}
    assert payload["official_sources"] == source_before["official_sources"]
    assert _rows(_payload())[STREAM_PARAMETER]["parameter_constraints"]["requires"] == {"stream": True}
    assert catalog.get_interface(interface_id) == interface_before
    assert references.reference_spec_payload("openai_chat_base") == source_before


def test_declarations_are_generic_and_unknown_state_stays_unknown_without_changing_snapshot(monkeypatch):
    cap = references.load_model_capability_profile(
        "text", "gpt", "gpt-4o", reference_source="openai_chat_base",
        api_form="openai_chat_completions", route_profile="vendor_direct",
    )
    parameter = "model_specific.preview_option"
    cap["parameter_capabilities"][parameter] = {
        "state": "unknown", "required": False, "request_acceptance": "unknown",
        "official_sources": ["https://example.invalid/preview-reference"],
    }
    cap["parameter_constraints"][parameter] = {"allowed_values": ["preview"]}
    before = copy.deepcopy(cap)
    monkeypatch.setattr(references, "load_model_capability_profile", lambda *args, **kwargs: cap)
    row = _rows(_payload())[parameter]
    assert row["state"] == row["official"] == row["local"] == row["model_expectation"] == "unknown"
    assert row["request_acceptance"] == "unknown"
    assert row["official_sources"] == ["https://example.invalid/preview-reference"]
    assert row["coverage_mode"] == "not_tested" and row["test_profiles"] == []
    row["parameter_constraints"]["allowed_values"].append("mutated")
    row["official_sources"].clear()
    assert cap == before


def test_model_and_api_form_specific_declaration_does_not_leak(monkeypatch):
    load = references.load_model_capability_profile
    parameter = "model_specific.chat_only_option"

    def selected_capability(*args, **kwargs):
        cap = load(*args, **kwargs)
        if cap["interface_id"] == "text/openai/gpt/gpt-4o#openai-chat-default":
            cap["parameter_capabilities"][parameter] = {"state": "unsupported"}
        return cap

    monkeypatch.setattr(references, "load_model_capability_profile", selected_capability)
    assert _rows(_payload())[parameter]["official"] == "unsupported"
    assert parameter not in _rows(_payload("gpt-4o", "openai_responses", "openai_responses"))
    assert parameter not in _rows(_payload("gpt-4o-mini"))
    assert parameter not in _rows(references.reference_spec_payload("openai_chat_base"))
    other = references.model_reference_spec_payload(
        "text", "minimax", "minimax-m2.7", "minimax_openai_compat",
        api_form="openai_chat_completions", route_profile="vendor_direct",
    )
    assert STREAM_PARAMETER not in _rows(other) and parameter not in _rows(other)


def test_read_only_interface_additions_keep_execution_closed(monkeypatch):
    selection = dict(api_form="gemini_generate_content", route_profile="google_ai_studio")
    contract = "gemini_native_generate_content"
    cap = references.load_model_capability_profile(
        "text", "gemini", "gemini-2.5-pro", reference_source=contract, read_only=True, **selection,
    )
    parameter = "generationConfig.previewOption"
    cap["parameter_capabilities"][parameter] = {"state": "unknown"}
    cap["parameter_constraints"][parameter] = {"allowed_values": ["preview"]}
    before = copy.deepcopy(cap)
    monkeypatch.setattr(references, "load_model_capability_profile", lambda *args, **kwargs: cap)
    payload = references.model_reference_spec_payload("text", "gemini", "gemini-2.5-pro", contract, **selection)
    row = _rows(payload)[parameter]
    assert row["official"] == row["state"] == "unknown"
    assert row["local"] == row["model_expectation"] == row["coverage_mode"] == "reference_only"
    assert row["execution_coverage"] == "not_executed"
    assert row["test_profiles"] == [] and row["profile_expectations"] == {}
    assert payload["test_profiles"] == [] and payload["tested_params"] == []
    assert parameter in payload["untested_params"]
    assert payload["param_count"] == len(payload["params"])
    for key in ("enabled", "executable", "runner_enabled", "parameter_test_enabled", "pressure_test_enabled"):
        assert payload[key] is False and payload["model_capability_profile"][key] is False
    assert cap == before


@pytest.mark.parametrize("change", [
    {"source_id": "openai"},
    {"source_id": "openai_responses"},
    {"api_form": "openai_responses"},
    {"model": "unregistered-model"},
])
def test_parameter_projection_does_not_relax_exact_binding_guards(change):
    selection = dict(modality="text", family="gpt", model="gpt-4o", source_id="openai_chat_base",
                     api_form="openai_chat_completions", route_profile="vendor_direct")
    selection.update(change)
    with pytest.raises((ValueError, KeyError)):
        references.model_reference_spec_payload(**selection)
