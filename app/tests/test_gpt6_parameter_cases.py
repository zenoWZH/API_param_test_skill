from __future__ import annotations
import json
import random
from types import SimpleNamespace
import pytest
from lib.config import load_config
from lib.deepseek_params import build_request
from lib.model_profile_catalog import get_model_profile_catalog
from lib.parameter_output_limit import configured_parameter_test_output_budget, enforce_parameter_test_output_limit, parameter_targets_output_limit
from lib.profile_validation import validate_profile_response
from lib.reference_specs import get_reference_source, load_model_capability_profile, parameter_label_for_profile
from scripts.param_test import _sample_inputs_for_profile, _input_group_for_profile, _preferred_chat_output_limit_field, _lookup_profile_settings

def test_all_47_enabled_gpt6_parameter_cases_build_and_enter_the_output_token_policy():
    catalog = get_model_profile_catalog()
    config = load_config()
    checked = set()
    for contract in ("openai_gpt6_astra_chat", "openai_gpt6_astra_responses"):
        source = get_reference_source(contract)
        cases = catalog.list_test_bindings(contract_id=contract, extension_type="parameter")[0]["test_cases"]
        capability = load_model_capability_profile(
            "text", "gpt", "gpt-6-astra", api_form=source["api_form"], reference_source=contract,
        )
        for case in cases:
            sample = _sample_inputs_for_profile(config, case, 1, random.Random(0))[0]
            overrides = {"model": "gpt-6-astra"}
            if not sample["id"].startswith("profile_defined:"):
                overrides["prompt"] = sample["prompt"]
            built = build_request(
                config, "compatibility_profiles", case, overrides=overrides,
                model_family_override="gpt", api_form_override=source["api_form"],
                route_profile_override=source.get("route_profile"), reference_source=contract,
                enforce_model_capabilities=False, parameter_reference_profile=capability,
                parameter_test=True,
            )
            settings = _lookup_profile_settings(config, case)
            for field in (
                "include", "temperature", "top_p", "top_logprobs", "logprobs",
                "verbosity", "prompt_cache_options", "parallel_tool_calls",
            ):
                if field in settings:
                    assert built.body.get(field) == settings[field], (case, field)
            if case in {"gpt6_responses_tools_strict", "gpt6_responses_parallel_tool_calls"}:
                assert built.body["tools"][0]["strict"] is True
            floor = configured_parameter_test_output_budget(
                config, built.body,
                preserve_declared_limit=parameter_targets_output_limit(
                    parameter_label_for_profile(contract, case), profile=case
                ),
            )
            limits = enforce_parameter_test_output_limit(
                built.body, built.metadata["transport"], minimum=floor,
                chat_completion_field=_preferred_chat_output_limit_field(source),
            )
            assert limits and min(limits.values()) >= 256
            assert "测试输入长度填充" not in json.dumps(built.body, ensure_ascii=False)
            assert "max_tokens" not in built.body
            checked.add(case)
    assert len(checked) == 47
    assert _input_group_for_profile(config, "gpt6_responses_tools_strict") == "tool_calls"

@pytest.mark.parametrize("profile,transport", [
    ("gpt6_chat_json_schema", "chat_completions"),
    ("gpt6_responses_json_schema", "openai_responses"),
])
def test_gpt6_schema_cases_require_the_closed_declared_schema(profile, transport):
    for value, valid in [({"summary":"ok"}, True), ({"summary":1}, False), ({"summary":"ok","extra":1}, False)]:
        text = json.dumps(value)
        response = ({"choices":[{"message":{"content":text}, "finish_reason":"stop"}]}
                    if transport == "chat_completions" else
                    {"output":[{"type":"message","content":[{"type":"output_text","text":text}]}], "status":"completed"})
        assert (validate_profile_response(profile, response, SimpleNamespace(success=True),
                                         transport=transport) is None) is valid
