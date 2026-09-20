from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib.config import load_config
from lib.deepseek_params import build_request
from lib.profile_validation import (
    STOP_PARAMETER_PROFILES, STOP_PROBE_VISIBLE_TEXT,
    validate_parameter_stop_request, validate_profile_response,
)


def test_every_configured_stop_profile_declares_a_closed_task_for_its_real_first_stop():
    config = load_config()
    declared_stop_profiles = set()
    for name, settings in config["compatibility_profiles"].items():
        if not isinstance(settings, dict):
            continue
        nested = [
            settings.get(key) or {} for key in (
                "generationConfig", "native_generation_config", "interaction_generation_config"
            )
        ]
        if ("stop" in settings or "stop_sequences" in settings or any(
            isinstance(value, dict) and ("stopSequences" in value or "stop_sequences" in value)
            for value in nested
        )):
            declared_stop_profiles.add(name)
    assert declared_stop_profiles <= STOP_PARAMETER_PROFILES
    checked = set()
    for profile in sorted(STOP_PARAMETER_PROFILES):
        if profile not in config["compatibility_profiles"]:
            continue
        api_form = "openai_chat_completions"
        if profile.startswith("gemini"):
            family, model = "gemini", "gemini-3.7-flash"
            if "native" in profile:
                api_form = "gemini_generate_content"
            elif "interactions" in profile:
                api_form = "gemini_interactions"
        elif profile.startswith("claude"):
            family, model, api_form = "claude", "claude-sonnet-4-6", "anthropic_messages"
        elif profile.startswith("glm"):
            family, model = "glm", "glm-5.3"
        elif profile.startswith("gpt"):
            family, model = "gpt", "gpt-5.6-luna"
        elif profile.startswith("grok"):
            family, model = "grok", "grok-4.5"
        elif profile.startswith("aliyun"):
            family, model = "qwen", "qwen3.7-max"
        else:
            family, model = "deepseek", "deepseek-v4-pro-0813"
            if "anthropic" in profile:
                api_form = "anthropic_messages"
            elif "fim" in profile:
                api_form = "openai_fim_completions_beta"
        built = build_request(
            config, "compatibility_profiles", profile, overrides={"model": model},
            model_family_override=family, api_form_override=api_form,
            enforce_model_capabilities=False, parameter_test=True,
        )
        assert validate_parameter_stop_request(profile, built.body) is None, profile
        checked.add(profile)
    assert declared_stop_profiles == checked


@pytest.mark.parametrize("stop_path", ["stop", "stop_sequences", "generationConfig", "generation_config"])
def test_stop_probe_checks_the_actual_first_stop_and_exact_complete_visible_answer(stop_path):
    marker = "\n\n" if stop_path == "stop" else "END"
    body = {"messages": [{"role": "user", "content":
        "Repeat exactly: TOKEN_AUDIT_OK" + marker + "TOKEN_AUDIT_TAIL"}]}
    if stop_path == "generationConfig":
        body[stop_path] = {"stopSequences": [marker]}
    elif stop_path == "generation_config":
        body[stop_path] = {"stop_sequences": [marker]}
    else:
        body[stop_path] = [marker]
    result = SimpleNamespace(success=True)
    for visible in (STOP_PROBE_VISIBLE_TEXT, "TOKEN_AUDIT_", STOP_PROBE_VISIBLE_TEXT + marker):
        response = {"choices": [{"message": {"content": visible}, "finish_reason": "stop"}]}
        error = validate_profile_response("stop_sequences", response, result, request_body=body)
        assert error == (
            None if visible == STOP_PROBE_VISIBLE_TEXT
            else "stop_probe_visible_output_mismatch"
        )
    body["messages"][0]["content"] = "An unrelated open-ended essay."
    assert validate_parameter_stop_request("stop_sequences", body) == "stop_probe_declared_input_mismatch"
