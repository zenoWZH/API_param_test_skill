from __future__ import annotations

import copy
import json
import random

import pytest

from lib.config import load_config
from lib.deepseek_params import build_openai_responses_tool_followup_request, build_request
from scripts.param_test import _sample_inputs_for_profile


@pytest.mark.parametrize(
    ("family", "model", "api_form", "profile", "input_key"),
    [
        ("deepseek", "deepseek-v4-flash", "openai_chat_completions", {}, "messages"),
        ("gpt", "gpt-5.6-luna", "openai_responses", {}, "input"),
        ("claude", "claude-sonnet-4-6", "anthropic_messages", {}, "messages"),
        ("gemini", "gemini-3.7-flash", "gemini_generate_content", {}, "contents"),
        ("deepseek", "deepseek-v4-flash", "openai_fim_completions_beta",
         {"suffix": "\n"}, "prompt"),
    ],
)
def test_parameter_builder_preserves_short_declared_input_without_padding_or_system(
    family, model, api_form, profile, input_key
):
    config = load_config()
    config["compatibility_profiles"]["integrity_case"] = profile
    original = copy.deepcopy(config)
    prompt = "Return exactly OK."
    built = build_request(
        config, "compatibility_profiles", "integrity_case",
        overrides={"model": model, "prompt": prompt},
        model_family_override=family, api_form_override=api_form,
        enforce_model_capabilities=False, parameter_test=True,
    )
    value = built.body[input_key]
    if input_key == "messages":
        assert value == [{"role": "user", "content": prompt}]
    elif input_key == "contents":
        assert value == [{"role": "user", "parts": [{"text": prompt}]}]
    else:
        assert value == prompt
    assert "system" not in built.body
    assert "10000 10001" not in json.dumps(built.body)
    assert config == original


def test_declared_system_and_inline_messages_are_preserved_exactly():
    config = load_config()
    messages = [
        {"role": "system", "content": "Return compact JSON for the user's record."},
        {"role": "user", "content": "Record: name Ada, count 3."},
    ]
    config["compatibility_profiles"]["integrity_case"] = {"messages": messages}
    built = build_request(
        config, "compatibility_profiles", "integrity_case",
        overrides={"model": "deepseek-v4-flash"},
        model_family_override="deepseek", enforce_model_capabilities=False,
        parameter_test=True,
    )
    assert built.body["messages"] == messages


def test_repeated_samples_do_not_inject_run_identifiers():
    config = {
        "compatibility_profiles": {"basic": {}},
        "param_test_inputs": {"general": [{"id": "closed", "prompt": "Return OK."}]},
    }
    assert _sample_inputs_for_profile(config, "basic", 3, random.Random(0)) == [
        {"id": "closed", "prompt": "Return OK."},
        {"id": "closed", "prompt": "Return OK."},
        {"id": "closed", "prompt": "Return OK."},
    ]


def test_source_defined_prompt_survives_profile_inheritance_and_sampling():
    config = {
        "compatibility_profiles": {
            "parent": {"prompt": "Return only the integer 7.", "stop": ["\n"]},
            "case": {"extends": "parent", "temperature": 0.1},
        },
        "param_test_inputs": {"general": [{"id": "unrelated", "prompt": "Long essay."}]},
    }
    assert _sample_inputs_for_profile(config, "case", 2, random.Random(0)) == [
        {"id": "profile_defined:case", "prompt": "Return only the integer 7."},
        {"id": "profile_defined:case", "prompt": "Return only the integer 7."},
    ]


@pytest.mark.parametrize("original_input", [
    "Call get_weather for Shanghai.",
    [{"role": "user", "content": "Call get_weather for Shanghai."}],
])
def test_responses_tool_followup_preserves_original_input_and_adds_only_exchange(original_input):
    original = {"model": "gpt-5.6-luna", "input": original_input, "max_output_tokens": 1024}
    snapshot = copy.deepcopy(original)
    output = {
        "type": "function_call", "call_id": "weather_1", "name": "get_weather",
        "arguments": json.dumps({"city": "Shanghai", "unit": "celsius"}),
    }
    followup = build_openai_responses_tool_followup_request(original, {"output": [output]})
    assert followup["input"][0] == {
        "role": "user", "content": "Call get_weather for Shanghai."
    }
    assert followup["input"][1] == output
    assert followup["input"][2]["type"] == "function_call_output"
    assert followup["input"][2]["call_id"] == "weather_1"
    assert len(followup["input"]) == 3
    assert original == snapshot
