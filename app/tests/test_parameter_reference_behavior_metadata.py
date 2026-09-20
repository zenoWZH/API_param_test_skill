"""Request acceptance and a deprecated field's effect remain separate facts."""
import pytest

from lib.param_outcome import map_probe_outcome
from lib.reference_specs import get_reference_source, load_reference_specs, reference_param_rows, resolve_profile_expectation


@pytest.mark.parametrize("contract,model", [("deepseek_chat", "deepseek-v4-flash"), ("deepseek_v4_pro_0813_chat", "deepseek-v4-pro")])
@pytest.mark.parametrize("parameter", ["frequency_penalty", "presence_penalty"])
def test_no_effect_metadata_survives_projection_without_changing_acceptance_expectation(contract, model, parameter):
    expected = {"deprecated": True, "effect_support": "none", "request_acceptance": "ignored_as_documented", "http_rejection_expected": False}
    projection = load_reference_specs()["reference_sources"][contract]["params"][parameter]
    source = get_reference_source(contract)["params"][parameter]
    row = next(row for row in reference_param_rows(contract) if row["parameter"] == parameter)
    for actual in (projection, source, row):
        assert {key: actual.get(key) for key in expected} == expected
    assert row["official"] == "unsupported"  # Effect support remains unsupported.
    expectation = resolve_profile_expectation("text", "deepseek", model, "deepseek_" + parameter,
                                               reference_source=contract, api_form="openai_chat_completions", route_profile="vendor_direct")
    assert expectation == "supported"
    assert map_probe_outcome(expectation, status_code=200, validation_ok=True)["status"] == "pass"
    assert map_probe_outcome(expectation, status_code=400)["status"] == "incompatible"


def test_copying_behavior_metadata_does_not_change_unrelated_rejection_expectations():
    assert map_probe_outcome("unsupported", status_code=400)["status"] == "expected_rejection"
    assert map_probe_outcome("unsupported", status_code=200)["status"] == "unexpected_acceptance"


@pytest.mark.parametrize("parameter", ["frequency_penalty", "presence_penalty"])
def test_fim_deprecated_penalty_remains_named_acceptance_diagnostic(parameter):
    contract = "deepseek_v4_pro_0813_fim_beta"
    case = "deepseek0813_fim_" + parameter + "_accepted_ignored"
    expected = {"deprecated": True, "effect_support": "none", "request_acceptance": "ignored_as_documented", "http_rejection_expected": False}
    source = get_reference_source(contract)
    projection = load_reference_specs()["reference_sources"][contract]["params"][parameter]
    row = next(row for row in reference_param_rows(contract) if row["parameter"] == parameter)
    for actual in (projection, source["params"][parameter], row):
        assert {key: actual.get(key) for key in expected} == expected
    assert source["source_id"] == "deepseek" and source["api_form"] == "openai_fim_completions_beta"
    assert row["official"] == "unsupported"
    assert row["test_profiles"] == [case] and case in source["test_profiles"]
    assert "deprecated_no_effect" in row["coverage"]
    expectation = resolve_profile_expectation("text", "deepseek", "deepseek-v4-pro", case,
                                               reference_source=contract, api_form="openai_fim_completions_beta", route_profile="vendor_direct")
    assert expectation == "supported"
    assert map_probe_outcome(expectation, status_code=200, validation_ok=True)["status"] == "pass"
    assert map_probe_outcome(expectation, status_code=400)["status"] == "incompatible"
