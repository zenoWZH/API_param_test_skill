from __future__ import annotations

from typing import Any


MIN_PARAMETER_TEST_OUTPUT_TOKENS = 256

_OUTPUT_LIMIT_PATHS = (
    ("max_tokens",),
    ("max_completion_tokens",),
    ("max_output_tokens",),
    ("maxOutputTokens",),
    ("generationConfig", "maxOutputTokens"),
    ("generation_config", "max_output_tokens"),
    ("inferenceConfig", "maxTokens"),
    ("body", "max_tokens"),
)

_TRANSPORT_ALIASES = {
    "chat-completions": "chat_completions",
    "gemini-generate-content": "gemini_generate_content",
    "gemini-interactions": "gemini_interactions",
}

_DEFAULT_OUTPUT_LIMIT_PATHS = {
    "chat_completions": ("max_tokens",),
    "deepseek-beta-chat-prefix": ("max_tokens",),
    "deepseek_beta_chat_prefix": ("max_tokens",),
    "openai_responses": ("max_output_tokens",),
    "claude_messages": ("max_tokens",),
    "gemini_generate_content": ("generationConfig", "maxOutputTokens"),
    "gemini_interactions": ("generation_config", "max_output_tokens"),
    "fim_completions": ("max_tokens",),
}


def configured_parameter_test_min_output_tokens(config: dict[str, Any]) -> int:
    """Return the fail-closed output allowance for parameter-test requests."""

    test_cases = config.get("test_cases")
    test_cases = test_cases if isinstance(test_cases, dict) else {}
    value = test_cases.get(
        "minimum_output_tokens", MIN_PARAMETER_TEST_OUTPUT_TOKENS
    )
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("test_cases.minimum_output_tokens must be an integer.")
    if value < MIN_PARAMETER_TEST_OUTPUT_TOKENS:
        raise ValueError(
            "test_cases.minimum_output_tokens must be at least "
            f"{MIN_PARAMETER_TEST_OUTPUT_TOKENS}."
        )
    return value


def enforce_parameter_test_output_limit(
    body: dict[str, Any],
    transport: str,
    *,
    minimum: int = MIN_PARAMETER_TEST_OUTPUT_TOKENS,
    chat_completion_field: str = "max_tokens",
) -> dict[str, int]:
    """Ensure every applicable outbound parameter-test cap is at least ``minimum``.

    The body is mutated immediately before dispatch. Existing aliases are all
    raised so a request can never retain a second, lower effective cap. If a
    text transport has no explicit cap, the transport-native field is added.
    Image-generation endpoints have no output-token field and are left alone.
    """

    if not isinstance(body, dict):
        raise ValueError("parameter-test request body must be an object.")
    if (
        isinstance(minimum, bool)
        or not isinstance(minimum, int)
        or minimum < MIN_PARAMETER_TEST_OUTPUT_TOKENS
    ):
        raise ValueError(
            "parameter-test minimum output tokens must be an integer of at least "
            f"{MIN_PARAMETER_TEST_OUTPUT_TOKENS}."
        )
    if chat_completion_field not in {"max_tokens", "max_completion_tokens"}:
        raise ValueError(
            "chat_completion_field must be max_tokens or max_completion_tokens."
        )

    normalized_transport = _TRANSPORT_ALIASES.get(transport, transport)
    present_paths = [path for path in _OUTPUT_LIMIT_PATHS if _has_path(body, path)]
    if not present_paths:
        if normalized_transport == "chat_completions":
            generation_config = body.get("generationConfig")
            path = (
                ("generationConfig", "maxOutputTokens")
                if isinstance(generation_config, dict)
                else (chat_completion_field,)
            )
        else:
            path = _DEFAULT_OUTPUT_LIMIT_PATHS.get(normalized_transport)
        if path is None:
            return {}
        _set_path(body, path, minimum)
        present_paths = [path]

    effective: dict[str, int] = {}
    for path in present_paths:
        value = _get_path(body, path)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{'.'.join(path)} must be an integer.")
        if value < minimum:
            _set_path(body, path, minimum)
            value = minimum
        effective[".".join(path)] = value
    return effective


def configured_parameter_test_output_budget(
    config: dict[str, Any],
    body: dict[str, Any],
    *,
    preserve_declared_limit: bool = False,
    reasoning_expected: bool = False,
) -> int:
    """Allow a bounded task to finish, including hidden reasoning and tools.

    Explicit output-limit probes retain their tested allowance (subject to the
    global 256 floor). A truncation is still a failed exchange, never a reason
    to silently retry a different parameter value.
    """
    minimum = configured_parameter_test_min_output_tokens(config)
    settings = (config.get("test_cases") or {}).get("output_token_budgets") or {}
    if not isinstance(settings, dict):
        raise ValueError("test_cases.output_token_budgets must be an object.")
    budgets = {"structured": 1024, "reasoning": 4096, "image": 8192}
    for key, default in budgets.items():
        value = settings.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 256:
            raise ValueError(
                f"test_cases.output_token_budgets.{key} must be an integer of at least 256."
            )
        budgets[key] = value
    if preserve_declared_limit:
        return minimum

    native = body.get("generationConfig") or {}
    interaction = body.get("generation_config") or {}
    native = native if isinstance(native, dict) else {}
    interaction = interaction if isinstance(interaction, dict) else {}
    text = body.get("text") or {}
    text = text if isinstance(text, dict) else {}
    structured = bool(
        body.get("tools")
        or body.get("response_format")
        or text.get("format")
        or native.get("responseSchema")
        or native.get("responseJsonSchema")
        or native.get("responseFormat")
        or "json" in str(native.get("responseMimeType") or "").lower()
    )
    modalities = native.get("responseModalities") or body.get("modalities") or []
    image_output = (
        isinstance(modalities, list)
        and any(str(value).lower() == "image" for value in modalities)
    )
    response_format = body.get("response_format") or {}
    if isinstance(response_format, dict):
        image_output |= response_format.get("type") == "image"
    thinking_configs = [
        body.get("thinking"),
        native.get("thinkingConfig"),
        interaction.get("thinking_config"),
    ]
    extra_body = body.get("extra_body") or {}
    if isinstance(extra_body, dict):
        thinking_configs.append(extra_body.get("thinking"))
        google = extra_body.get("google") or {}
        if isinstance(google, dict):
            thinking_configs.append(google.get("thinking_config"))
    reasoning_configs = [body.get("reasoning"), body.get("output_config")]
    efforts = [
        body.get("reasoning_effort"),
        interaction.get("thinking_level"),
    ]
    efforts.extend(
        item.get("effort") for item in reasoning_configs if isinstance(item, dict)
    )
    explicit_budget = 0
    for thinking in thinking_configs:
        if not isinstance(thinking, dict):
            continue
        mode = str(thinking.get("type") or "").lower()
        reasoning_expected |= mode in {"enabled", "adaptive"}
        efforts.extend([thinking.get("thinkingLevel"), thinking.get("thinking_level")])
        for key in ("budget_tokens", "thinkingBudget", "thinking_budget"):
            value = thinking.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                reasoning_expected |= value != 0
                explicit_budget = max(explicit_budget, value)
    for key in ("thinking_budget", "reasoning_budget"):
        value = body.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            explicit_budget = max(explicit_budget, value)
            reasoning_expected |= value != 0
    reasoning_expected |= body.get("enable_thinking") is True
    reasoning_expected |= any(
        value is not None
        and str(value).lower() not in {"", "none", "disabled", "off"}
        for value in efforts
    )
    return max(
        minimum,
        budgets["structured"] if structured else minimum,
        budgets["reasoning"] if reasoning_expected else minimum,
        max(8192, budgets["image"]) if image_output else minimum,
        explicit_budget + minimum if explicit_budget else minimum,
    )


def parameter_targets_output_limit(parameter: str, *, profile: str = "") -> bool:
    """Identify an output cap that is itself the parameter under test."""
    label = str(parameter).strip().lower()
    field_names = {path[-1].lower() for path in _OUTPUT_LIMIT_PATHS}
    exact_labels = {".".join(path).lower() for path in _OUTPUT_LIMIT_PATHS} | field_names
    case_id = str(profile).lower()
    return (
        label in exact_labels
        or any(field in case_id for field in field_names)
        or case_id == "deepseek_json_output_256"
    )


def _has_path(body: dict[str, Any], path: tuple[str, ...]) -> bool:
    current: Any = body
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    return True


def _get_path(body: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = body
    for key in path:
        current = current[key]
    return current


def _set_path(body: dict[str, Any], path: tuple[str, ...], value: int) -> None:
    current = body
    for key in path[:-1]:
        child = current.get(key)
        if child is None:
            child = {}
            current[key] = child
        if not isinstance(child, dict):
            raise ValueError(f"{'.'.join(path[:-1])} must be an object.")
        current = child
    current[path[-1]] = value
