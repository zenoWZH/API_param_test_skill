from __future__ import annotations

import copy

import pytest

from lib.parameter_output_limit import (
    MIN_PARAMETER_TEST_OUTPUT_TOKENS,
    configured_parameter_test_min_output_tokens,
    configured_parameter_test_output_budget,
    enforce_parameter_test_output_limit,
    parameter_targets_output_limit,
)

@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"messages": [{"role": "user", "content": "Reply OK."}]}, 256),
        ({"tools": [{"type": "function"}]}, 1024),
        ({"response_format": {"type": "json_schema"}}, 1024),
        ({"text": {"format": {"type": "json_object"}}}, 1024),
        ({"generationConfig": {"responseMimeType": "application/json"}}, 1024),
        ({"reasoning_effort": "high"}, 4096),
        ({"reasoning": {"effort": "low"}}, 4096),
        ({"thinking": {"type": "adaptive"}}, 4096),
        ({"thinking": {"type": "enabled", "budget_tokens": 8192}}, 8448),
        ({"generationConfig": {"thinkingConfig": {"thinkingBudget": 8192}}}, 8448),
        ({"generation_config": {"thinking_level": "high"}}, 4096),
        ({"generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}, 8192),
        ({"thinking": {"type": "disabled"}, "reasoning_effort": "none"}, 256),
    ],
)
def test_parameter_case_budget_leaves_room_for_complete_outputs(body, expected):
    assert configured_parameter_test_output_budget({}, body) == expected


def test_output_limit_probe_retains_declared_value_and_config_can_raise_budget():
    body = {"max_tokens": 512, "thinking": {"type": "enabled"}}
    minimum = configured_parameter_test_output_budget(
        {}, body, preserve_declared_limit=True
    )
    assert enforce_parameter_test_output_limit(body, "chat_completions", minimum=minimum) == {
        "max_tokens": 512
    }
    assert configured_parameter_test_output_budget(
        {"test_cases": {"output_token_budgets": {"reasoning": 8192}}},
        {"reasoning_effort": "high"},
    ) == 8192
    with pytest.raises(ValueError, match="output_token_budgets"):
        configured_parameter_test_output_budget(
            {"test_cases": {"output_token_budgets": {"reasoning": 32}}}, {}
        )
    assert not parameter_targets_output_limit("model, messages, max_tokens")
    assert parameter_targets_output_limit("max_tokens")
    assert parameter_targets_output_limit(
        "contents, generationConfig.maxOutputTokens, usage",
        profile="gemini_native_max_output_tokens",
    )


def _value_at(body: dict, path: str) -> object:
    value: object = body
    for key in path.split("."):
        assert isinstance(value, dict)
        value = value[key]
    return value


def test_configured_minimum_defaults_to_and_cannot_undercut_256() -> None:
    assert MIN_PARAMETER_TEST_OUTPUT_TOKENS == 256
    assert configured_parameter_test_min_output_tokens({}) == 256
    assert configured_parameter_test_min_output_tokens({"test_cases": None}) == 256
    assert configured_parameter_test_min_output_tokens(
        {"test_cases": {"minimum_output_tokens": 512}}
    ) == 512

    for invalid in (255, 0, -1, True, 256.0, "256", None):
        with pytest.raises(ValueError):
            configured_parameter_test_min_output_tokens(
                {"test_cases": {"minimum_output_tokens": invalid}}
            )


@pytest.mark.parametrize(
    ("transport", "body", "expected"),
    [
        ("chat_completions", {"max_tokens": 1}, {"max_tokens": 256}),
        (
            "chat_completions",
            {"max_completion_tokens": 16},
            {"max_completion_tokens": 256},
        ),
        (
            "openai_responses",
            {"max_output_tokens": 32},
            {"max_output_tokens": 256},
        ),
        ("custom", {"maxOutputTokens": 64}, {"maxOutputTokens": 256}),
        (
            "gemini_generate_content",
            {"generationConfig": {"maxOutputTokens": 96}},
            {"generationConfig.maxOutputTokens": 256},
        ),
        (
            "gemini_interactions",
            {"generation_config": {"max_output_tokens": 128}},
            {"generation_config.max_output_tokens": 256},
        ),
        (
            "custom",
            {"inferenceConfig": {"maxTokens": 192}},
            {"inferenceConfig.maxTokens": 256},
        ),
        ("custom", {"body": {"max_tokens": 8}}, {"body.max_tokens": 256}),
    ],
)
def test_existing_output_limit_paths_are_raised_to_256(
    transport: str, body: dict, expected: dict[str, int]
) -> None:
    effective = enforce_parameter_test_output_limit(body, transport)

    assert effective == expected
    for path, value in expected.items():
        assert _value_at(body, path) == value


@pytest.mark.parametrize(
    ("transport", "chat_field", "expected"),
    [
        ("chat_completions", "max_tokens", {"max_tokens": 256}),
        (
            "chat-completions",
            "max_completion_tokens",
            {"max_completion_tokens": 256},
        ),
        ("openai_responses", "max_tokens", {"max_output_tokens": 256}),
        ("claude_messages", "max_tokens", {"max_tokens": 256}),
        ("fim_completions", "max_tokens", {"max_tokens": 256}),
        (
            "gemini-generate-content",
            "max_tokens",
            {"generationConfig.maxOutputTokens": 256},
        ),
        (
            "gemini-interactions",
            "max_tokens",
            {"generation_config.max_output_tokens": 256},
        ),
    ],
)
def test_missing_caps_use_the_transport_native_field(
    transport: str, chat_field: str, expected: dict[str, int]
) -> None:
    body: dict = {}

    effective = enforce_parameter_test_output_limit(
        body,
        transport,
        chat_completion_field=chat_field,
    )

    assert effective == expected
    for path, value in expected.items():
        assert _value_at(body, path) == value


def test_every_present_alias_is_enforced_and_larger_values_are_preserved() -> None:
    body = {
        "max_tokens": 1,
        "max_completion_tokens": 512,
        "generationConfig": {"maxOutputTokens": 128},
    }

    assert enforce_parameter_test_output_limit(body, "chat_completions") == {
        "max_tokens": 256,
        "max_completion_tokens": 512,
        "generationConfig.maxOutputTokens": 256,
    }
    assert body == {
        "max_tokens": 256,
        "max_completion_tokens": 512,
        "generationConfig": {"maxOutputTokens": 256},
    }


def test_custom_minimum_can_only_raise_the_floor() -> None:
    body = {"max_tokens": 256}

    assert enforce_parameter_test_output_limit(
        body, "chat_completions", minimum=512
    ) == {"max_tokens": 512}
    assert body["max_tokens"] == 512


@pytest.mark.parametrize(
    "transport", ["images-generations", "images_generations", "gemini_image"]
)
def test_image_transports_without_token_fields_are_unchanged(transport: str) -> None:
    body = {"model": "image-model", "prompt": "draw a circle"}
    original = copy.deepcopy(body)

    assert enforce_parameter_test_output_limit(body, transport) == {}
    assert body == original


def test_invalid_limits_and_shapes_fail_closed() -> None:
    with pytest.raises(ValueError, match="request body must be an object"):
        enforce_parameter_test_output_limit(  # type: ignore[arg-type]
            None, "chat_completions"
        )
    with pytest.raises(ValueError, match="integer of at least 256"):
        enforce_parameter_test_output_limit({}, "chat_completions", minimum=255)
    with pytest.raises(ValueError, match="integer of at least 256"):
        enforce_parameter_test_output_limit({}, "chat_completions", minimum=True)
    with pytest.raises(ValueError, match="chat_completion_field"):
        enforce_parameter_test_output_limit(
            {}, "chat_completions", chat_completion_field="max_output_tokens"
        )
    with pytest.raises(ValueError, match="max_tokens must be an integer"):
        enforce_parameter_test_output_limit(
            {"max_tokens": True}, "chat_completions"
        )
    with pytest.raises(ValueError, match="generationConfig must be an object"):
        enforce_parameter_test_output_limit(
            {"generationConfig": "invalid"}, "gemini_generate_content"
        )
