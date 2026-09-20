"""Existing generic probes consume source constraints all the way to HTTP."""
import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from lib.client import OpenAICompatibleClient
from lib.config import load_config
from lib.deepseek_params import build_request
from lib.parameter_reference_policy import non_thinking_probe_policy
from lib.reference_specs import (
    capability_profile_snapshot, load_model_capability_profile,
    model_reference_spec_payload, pressure_profiles_for_model, resolve_profile_expectation,
)
from scripts.param_test import _parameter_coverage_for_profiles, run_one_profile

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parent if HERE.name == "app" else HERE
SOURCE = "kimi_openai_compat"
PROFILE = "sampling_non_thinking"
FORM = "openai_chat_completions"


@pytest.fixture
def config():
    cfg = load_config()
    cfg["active_provider"] = "kimi_official"
    return cfg


def capability(model):
    cap = load_model_capability_profile("text", "kimi", model, api_form=FORM,
                                        route_profile="vendor_direct", reference_source=SOURCE)
    cap = copy.deepcopy(cap)
    if model == "kimi-k2.6":
        candidate = json.loads((REPO / "references/kimi_sampling_constraint_candidate_20260907.json").read_text())
        # Exercise the reviewed additive data before/after the root-owned install.
        for operation in candidate["operations"]:
            name = operation["field_path"][1]
            if name in cap["parameter_constraints"]:
                assert cap["parameter_constraints"][name] == operation["value"]
            cap["parameter_constraints"][name] = copy.deepcopy(operation["value"])
    return cap


def build(cfg, model, cap, *, profile=PROFILE, enforce=False, overrides=None):
    cfg["providers"]["kimi_official"]["models"]["default"] = model
    return build_request(cfg, "compatibility_profiles", profile, overrides={"model": model, **(overrides or {})},
                         model_family_override="kimi", api_form_override=FORM,
                         route_profile_override="vendor_direct", reference_source=SOURCE,
                         enforce_model_capabilities=enforce, parameter_reference_profile=cap)


def native_response(model, *, reasoning=None):
    message = {"role": "assistant", "content": "OK"}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {"id": "offline", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
            "usage": {"prompt_tokens": 128, "completion_tokens": 8, "total_tokens": 136,
                      "completion_tokens_details": {"reasoning_tokens": 2}}}


def run_at_http_boundary(cfg, model, cap, status, payload):
    cfg["providers"]["kimi_official"]["models"]["default"] = model
    client = OpenAICompatibleClient("https://api.moonshot.ai/v1", "offline-fixture-key", provider="kimi_official",
                                    api_interfaces={"chat_completions": {"base_url": "https://api.moonshot.ai/v1", "path": "/chat/completions", "auth": "bearer"}})
    raw = json.dumps(payload)
    response = Mock(status_code=status, text=raw, content=raw.encode(), headers={})
    response.json.return_value = payload
    client.session.post = Mock(return_value=response)
    client.count_tokens = Mock(return_value=None)
    result = run_one_profile(cfg, client, "kimi_official", model, "kimi", SOURCE, "kimi", PROFILE, 1,
                             {"id": "offline-source-control", "prompt": "Reply OK."}, capability_profile=cap)
    return result, client.session.post


def test_k26_preserves_disabled_mode_and_uses_source_sampling_constants(config):
    cap = capability("kimi-k2.6")
    built = build(config, "kimi-k2.6", cap)
    assert built.body["thinking"] == {"type": "disabled"}
    assert built.body["temperature"] == 0.6 and built.body["top_p"] == 0.95
    assert not built.warnings
    assert resolve_profile_expectation("text", "kimi", "kimi-k2.6", PROFILE, capability_profile=cap, reference_source=SOURCE) == "supported"
    assert built.metadata["parameter_reference_policy"]["sampling_effect_tested"] is False


def test_k26_real_runner_and_client_preserve_the_fields_at_http_boundary(config):
    result, post = run_at_http_boundary(config, "kimi-k2.6", capability("kimi-k2.6"), 200, native_response("kimi-k2.6"))
    assert post.call_count == 1
    actual = post.call_args.kwargs["json"]
    assert actual["thinking"] == {"type": "disabled"}
    assert actual["temperature"] == 0.6 and actual["top_p"] == 0.95 and actual["max_tokens"] >= 256
    assert result["status"] == "pass" and result["expectation"] == "supported"
    assert result["parameter_reference_policy"]["parameter_effect_verified"] is False


@pytest.mark.parametrize("field,value", [("temperature", 0.7), ("temperature", 0.8), ("top_p", 0.9)])
def test_explicit_negative_override_is_applied_after_source_defaults_and_not_rewritten(config, field, value):
    built = build(config, "kimi-k2.6", capability("kimi-k2.6"), overrides={field: value, "stop": ["KEEP"]})
    assert built.body[field] == value and built.body["stop"] == ["KEEP"]
    assert built.body["thinking"] == {"type": "disabled"}
    other = "top_p" if field == "temperature" else "temperature"
    assert built.body[other] == (0.95 if other == "top_p" else 0.6)
    policy = built.metadata["parameter_reference_policy"]
    assert policy["expectation"] == "unsupported" and policy["rejection_parameter"] == field
    assert field in policy["preserved_explicit_parameters"]


def test_customized_fixture_value_is_preserved_and_rejected_by_its_actual_field(config):
    config["compatibility_profiles"][PROFILE]["temperature"] = 0.8
    error = {"error": {"type": "invalid_request_error", "param": "temperature", "message": "must use 0.6"}}
    result, post = run_at_http_boundary(config, "kimi-k2.6", capability("kimi-k2.6"), 400, error)
    assert post.call_args.kwargs["json"]["temperature"] == 0.8
    assert post.call_args.kwargs["json"]["top_p"] == 0.95
    assert result["status"] == "expected_rejection" and result["parameter"] == "temperature"
    assert result["parameter_reference_policy"]["customized_fixture_parameters"] == ["temperature"]


def test_explicit_omission_is_not_reintroduced_as_a_default_control(config):
    built = build(config, "kimi-k2.6", capability("kimi-k2.6"), overrides={"omit_params": ["temperature"]})
    assert "temperature" not in built.body and built.body["top_p"] == 0.95
    assert "temperature" not in built.metadata["parameter_reference_policy"]["target_parameter"]


def test_nonzero_usage_reasoning_counter_alone_does_not_fail_disabled_mode(config):
    result, _ = run_at_http_boundary(config, "kimi-k2.6", capability("kimi-k2.6"), 200, native_response("kimi-k2.6"))
    assert result["status"] == "pass"
    result, _ = run_at_http_boundary(config, "kimi-k2.6", capability("kimi-k2.6"), 200,
                                    native_response("kimi-k2.6", reasoning="visible reasoning despite disabled"))
    assert result["status"] == "incompatible"
    assert result["failure_classification"] == "thinking_content_unexpected"


def test_other_success_status_cannot_bypass_disabled_reasoning_validation(config):
    result, _ = run_at_http_boundary(config, "kimi-k2.6", capability("kimi-k2.6"), 201,
                                    native_response("kimi-k2.6", reasoning="unexpected reasoning"))
    assert result["status"] == "incompatible" and not result["pass"]


def test_shared_contract_does_not_allow_another_models_capability(config):
    with pytest.raises(ValueError, match="reference identity"):
        build(config, "kimi-k2.7-code", capability("kimi-k2.6"))


def test_registered_runtime_alias_keeps_its_explicit_reference_context(config):
    cap = capability("kimi-k2.6")
    alias = "registered-runtime-alias"
    cap["model"] = alias
    cap["execution_target"]["request_model_id"] = alias
    if cap.get("model_profile_database"):
        cap["model_profile_database"]["execution_target"]["request_model_id"] = alias
    built = build(config, alias, cap)
    assert built.body["model"] == alias and built.body["thinking"] == {"type": "disabled"}


def test_k27_keeps_an_explicit_disabled_negative_without_sampling_confounders(config):
    cap = capability("kimi-k2.7-code")
    assert resolve_profile_expectation("text", "kimi", "kimi-k2.7-code", PROFILE, capability_profile=cap, reference_source=SOURCE) == "unsupported"
    built = build(config, "kimi-k2.7-code", cap)
    assert built.body["thinking"] == {"type": "disabled"}
    assert "temperature" not in built.body and "top_p" not in built.body
    assert built.metadata["parameter_reference_policy"]["target_parameter"] == "thinking.type"


def test_k27_preserves_explicit_sampling_but_does_not_misattribute_its_rejection(config):
    built = build(config, "kimi-k2.7-code", capability("kimi-k2.7-code"), overrides={"temperature": 0.2})
    assert built.body["temperature"] == 0.2 and "top_p" not in built.body
    assert built.metadata["parameter_reference_policy"]["preserved_explicit_parameters"] == ["temperature"]
    config["compatibility_profiles"][PROFILE]["temperature"] = 0.2
    error = {"error": {"type": "invalid_request_error", "param": "temperature", "message": "invalid temperature"}}
    result, post = run_at_http_boundary(config, "kimi-k2.7-code", capability("kimi-k2.7-code"), 400, error)
    assert post.call_args.kwargs["json"]["temperature"] == 0.2
    assert result["status"] == "incompatible" and result["parameter"] == "thinking.type"


def test_thinking_keep_error_does_not_prove_a_type_rejection(config):
    config["compatibility_profiles"][PROFILE]["thinking"] = {"type": "disabled", "keep": "invalid-fixture"}
    error = {"error": {"type": "invalid_request_error", "param": "thinking", "message": "thinking.keep must be null or all"}}
    result, post = run_at_http_boundary(config, "kimi-k2.7-code", capability("kimi-k2.7-code"), 400, error)
    assert post.call_args.kwargs["json"]["thinking"]["keep"] == "invalid-fixture"
    assert result["status"] == "incompatible" and result["failure_classification"] == "thinking_rejection_not_attributed"


@pytest.mark.parametrize("error", [
    {"type": "invalid_request_error", "param": "thinking.type", "message": "unsupported value"},
    {"type": "invalid_request_error", "message": "invalid thinking: only type=enabled is allowed for this model"},
])
def test_k27_only_attributed_thinking_rejection_passes_the_real_runner(config, error):
    result, post = run_at_http_boundary(config, "kimi-k2.7-code", capability("kimi-k2.7-code"), 400, {"error": error})
    assert post.call_count == 1 and post.call_args.kwargs["json"]["thinking"] == {"type": "disabled"}
    assert result["status"] == "expected_rejection" and result["pass"] is True
    assert result["parameter"] == "thinking.type"


@pytest.mark.parametrize("error", [
    {"type": "invalid_request_error", "param": "temperature", "message": "invalid thinking-like sampling"},
    {"type": "invalid_request_error", "message": "invalid request"},
    {"type": "billing_error", "param": "thinking.type", "message": "not enough credit"},
    {"type": "invalid_request_error", "message": "invalid thinking.keep: only enabled is allowed"},
])
def test_k27_other_400_errors_are_not_counted_as_the_expected_mode_rejection(config, error):
    result, post = run_at_http_boundary(config, "kimi-k2.7-code", capability("kimi-k2.7-code"), 400, {"error": error})
    assert post.call_count == 1
    assert result["status"] == "incompatible" and not result["pass"]
    assert result["parameter"] == "thinking.type"
    assert result["failure_classification"] == "thinking_rejection_not_attributed"


def test_k27_accepting_the_explicit_disabled_request_is_not_a_positive_pass(config):
    result, _ = run_at_http_boundary(config, "kimi-k2.7-code", capability("kimi-k2.7-code"), 200, native_response("kimi-k2.7-code"))
    assert result["status"] == "unexpected_acceptance" and not result["pass"]


def test_missing_fixed_sampling_reference_fails_before_http(config):
    cap = capability("kimi-k2.6")
    cap["parameter_constraints"].pop("temperature")
    result, post = run_at_http_boundary(config, "kimi-k2.6", cap, 200, native_response("kimi-k2.6"))
    post.assert_not_called()
    assert result["status"] == "fail"


def test_snapshot_and_model_rows_do_not_attribute_thinking_rejection_to_temperature():
    cap = capability_profile_snapshot("text", "kimi", "kimi-k2.7-code", [PROFILE], reference_source=SOURCE,
                                      api_form=FORM, route_profile="vendor_direct")
    assert cap["parameter_constraints"]["thinking.type"]["always_on"] is True
    assert cap["resolved_expectations"][PROFILE] == "unsupported"
    assert cap["resolved_parameter_expectations"]["temperature"] == "supported"
    report = model_reference_spec_payload("text", "kimi", "kimi-k2.7-code", SOURCE, api_form=FORM, route_profile="vendor_direct")
    for row in report["params"]:
        if row["parameter"] in {"temperature", "top_p"}:
            assert row["model_expectation"] == "supported" and row["test_profiles"] == []
            assert row["not_exercised_controls"] == {PROFILE: "thinking.type"}
            assert row["parameter"] in report["untested_params"]
    tested, untested = _parameter_coverage_for_profiles(SOURCE, [PROFILE], capability_profile=cap)
    assert "temperature" not in tested and "top_p" not in tested
    assert "temperature" in untested and "top_p" in untested


def test_summary_coverage_uses_the_effective_explicit_negative_target(config):
    config["compatibility_profiles"][PROFILE]["temperature"] = 0.8
    error = {"error": {"type": "invalid_request_error", "param": "temperature", "message": "invalid temperature"}}
    cap = capability("kimi-k2.6")
    result, _ = run_at_http_boundary(config, "kimi-k2.6", cap, 400, error)
    tested, untested = _parameter_coverage_for_profiles(SOURCE, [PROFILE], capability_profile=cap, results=[result])
    assert "temperature" in tested and "top_p" in untested


@pytest.mark.parametrize("model", ["kimi-k2.6", "kimi-k2.7-code"])
def test_json_mode_policy_and_pressure_selection_are_unchanged(config, model):
    cap = capability(model)
    assert resolve_profile_expectation("text", "kimi", model, "json_output", capability_profile=cap, reference_source=SOURCE) == "unsupported"
    built = build(config, model, cap, profile="json_output")
    assert built.body["response_format"] == {"type": "json_object"}
    assert "parameter_reference_policy" not in built.metadata
    assert PROFILE in pressure_profiles_for_model("kimi", model, SOURCE, api_form=FORM, route_profile="vendor_direct")
    pressure_style = build(config, model, cap, enforce=True)
    assert "parameter_reference_policy" not in pressure_style.metadata
    assert pressure_style.body["temperature"] == 0.7 and pressure_style.body["top_p"] == 0.9


def test_other_source_does_not_inherit_moonshot_mode_policy():
    cap = capability("kimi-k2.6")
    cap["source_id"] = "aliyun_maas"
    assert non_thinking_probe_policy(cap, PROFILE) == {}


def test_supported_response_capability_does_not_open_arbitrary_request_roots(config):
    cap = capability("kimi-k2.6")
    cap["parameter_capabilities"]["response.model"] = {"state": "supported"}
    with pytest.raises(ValueError, match="Unsupported request/profile key"):
        build(config, "kimi-k2.6", cap, overrides={"response": {"model": "unrequested"}})
    cap["parameter_capabilities"]["thinking.type"] = {"state": "unsupported"}
    with pytest.raises(ValueError, match="does not certify"):
        build(config, "kimi-k2.6", cap)
