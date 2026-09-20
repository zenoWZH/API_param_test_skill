from __future__ import annotations

import copy
import json
import math
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import (
    PROJECT_ROOT,
    deep_merge,
    get_active_provider_name,
    get_model_api_form,
    get_model_api_forms,
    get_model_family,
    get_model_route_profile,
    get_model_transport,
    get_provider_config,
    get_selected_model,
    infer_model_family,
    resolve_project_path,
    transport_for_api_form,
)
from .credential_security import validate_profile_request_headers
from .gemini_api_version import build_gemini_api_url, is_ai_studio_origin
from .parameter_reference_policy import (
    apply_non_thinking_probe_policy,
    non_thinking_probe_policy,
    validate_probe_reference_identity,
    validate_reference_request,
)


SUPPORTED_PARAMS = {
    "model",
    "messages",
    "do_sample",
    "enable_code_interpreter",
    "enable_search",
    "enable_thinking",
    "extra_body",
    "generationConfig",
    "max_completion_tokens",
    "thinking",
    "thinking_budget",
    "preserve_thinking",
    "reasoning_effort",
    "max_tokens",
    "n",
    "parallel_tool_calls",
    "presence_penalty",
    "prompt_cache_key",
    "prompt_cache_options",
    "repetition_penalty",
    "request_id",
    "response_format",
    "search_options",
    "safetySettings",
    "seed",
    "service_tier",
    "stop",
    "stream",
    "stream_options",
    "temperature",
    "tool_stream",
    "top_k",
    "top_p",
    "tools",
    "tool_choice",
    "logprobs",
    "metadata",
    "top_logprobs",
    "stop_sequences",
    "system",
    "user_id",
    "verbosity",
}

GLM_OPENAI_COMPATIBLE_PARAMS = {
    "model",
    "messages",
    "do_sample",
    "thinking",
    "reasoning_effort",
    "max_tokens",
    "response_format",
    "request_id",
    "stop",
    "stream",
    "temperature",
    "tool_stream",
    "top_p",
    "tools",
    "tool_choice",
    "user_id",
}

OPENAI_CHAT_BASE_PARAMS = {
    "model",
    "messages",
    "max_completion_tokens",
    "max_tokens",
    "reasoning_effort",
    "response_format",
    "stop",
    "stream",
    "stream_options",
    "temperature",
    "top_p",
    "tools",
    "tool_choice",
    "n",
    "presence_penalty",
    "prompt_cache_key",
    "prompt_cache_options",
    "logprobs",
    "top_logprobs",
    "verbosity",
}

# xAI Grok Chat Completions: reasoning models reject stop / penalties.
# stop / presence_penalty / frequency_penalty are allowed on reject probes only
# (see preserve_rejected_params); shared throughput profiles still strip them.
GROK_CHAT_PARAMS = {
    "model",
    "messages",
    "max_completion_tokens",
    "max_tokens",
    "reasoning_effort",
    "response_format",
    "stream",
    "stream_options",
    "temperature",
    "top_p",
    "tools",
    "tool_choice",
    "n",
    "stop",
    "presence_penalty",
    "frequency_penalty",
}

DEEPSEEK_PARAMS = {
    "model",
    "messages",
    "thinking",
    "reasoning_effort",
    "max_tokens",
    "response_format",
    "stop",
    "stream",
    "stream_options",
    "temperature",
    "top_p",
    "tools",
    "tool_choice",
    "logprobs",
    "top_logprobs",
    "user_id",
}

QWEN_OPENAI_COMPATIBLE_PARAMS = {
    "model",
    "messages",
    "enable_code_interpreter",
    "enable_thinking",
    "max_completion_tokens",
    "top_p",
    "top_k",
    "temperature",
    "repetition_penalty",
    "presence_penalty",
    "max_tokens",
    "n",
    "parallel_tool_calls",
    "preserve_thinking",
    "response_format",
    "search_options",
    "seed",
    "stream",
    "stop",
    "thinking_budget",
    "tools",
    "tool_choice",
    "tool_stream",
    "stream_options",
    "enable_search",
    "logprobs",
    "top_logprobs",
}

ALIYUN_OPENAI_COMPATIBLE_PARAMS = {
    "model",
    "messages",
    "enable_thinking",
    "max_completion_tokens",
    "max_tokens",
    "n",
    "parallel_tool_calls",
    "presence_penalty",
    "preserve_thinking",
    "reasoning_effort",
    "repetition_penalty",
    "response_format",
    "seed",
    "stop",
    "stream",
    "stream_options",
    "temperature",
    "thinking",
    "thinking_budget",
    "tool_choice",
    "tool_stream",
    "tools",
    "top_k",
    "top_p",
}

GEMINI_OPENAI_COMPATIBLE_PARAMS = {
    "model",
    "messages",
    "extra_body",
    "generationConfig",
    "max_tokens",
    "reasoning_effort",
    "response_format",
    "stop",
    "stream",
    "stream_options",
    "temperature",
    "top_p",
    "n",
    "service_tier",
    "safetySettings",
    "tools",
    "tool_choice",
}

CLAUDE_OPENAI_COMPATIBLE_PARAMS = {
    "model",
    "messages",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "stream",
    "stream_options",
    "temperature",
    "top_p",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "thinking",
    "extra_body",
}

SUPPORTED_PARAMS_BY_FAMILY = {
    "deepseek": DEEPSEEK_PARAMS,
    "glm": GLM_OPENAI_COMPATIBLE_PARAMS,
    "gpt": OPENAI_CHAT_BASE_PARAMS,
    "kimi": OPENAI_CHAT_BASE_PARAMS,
    "minimax": OPENAI_CHAT_BASE_PARAMS,
    "grok": GROK_CHAT_PARAMS,
    "qwen": QWEN_OPENAI_COMPATIBLE_PARAMS,
    "gemini": GEMINI_OPENAI_COMPATIBLE_PARAMS,
    "claude": CLAUDE_OPENAI_COMPATIBLE_PARAMS,
    "claude_fable": CLAUDE_OPENAI_COMPATIBLE_PARAMS,
}

# Logical load/cache groups → family-specific YAML tables. Records/metrics keep the
# logical group name; only request construction reads the remapped table.
FAMILY_PROFILE_GROUPS = {
    ("throughput_profiles", "qwen"): "qwen_throughput_profiles",
    ("cache_profiles", "qwen"): "qwen_cache_profiles",
}


def profile_group_for_family(group: str, family: str) -> str:
    """Map logical throughput/cache groups to family-native profile tables."""
    return FAMILY_PROFILE_GROUPS.get((group, family), group)

DEPRECATED_PARAMS = {"frequency_penalty"}

PROFILE_KEYS = {
    "extends",
    "prompt",
    "prompt_key",
    "prompt_fixture",
    "fixture",
    "fixture_chars",
    "fixture_repeat_to_chars",
    "tools_fixture",
    "multi_turn",
    "interaction_generation_config",
    "interaction_labels",
    "interaction_response_format",
    "interaction_store",
    "interaction_stream",
    "interaction_system_instruction",
    "interaction_tools",
    "native_cached_content",
    "native_generation_config",
    "native_labels",
    "native_safety_settings",
    "native_service_tier",
    "native_store",
    "native_system_instruction",
    "native_tool_config",
    "native_tools",
    "omit_params",
    "output_config",
    "request_headers",
    "send_deprecated",
    "preserve_rejected_params",
    "pass_reasoning_content",
    "skip_minimum_prompt",
    "run_success_mode",
    "transport",
}

REASONING_EFFORT_ALIASES = {
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "high",
    "max": "max",
}

# Official GPT-5 Chat Completions / Responses reasoning effort values.
OPENAI_GPT5_REASONING_EFFORTS = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
}

# GLM-5.2 exposes all OpenAI effort levels directly. Keep the requested value
# intact so parameter probes can distinguish aliases instead of normalizing
# them through the DeepSeek high/max mapping.
GLM_REASONING_EFFORTS = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
}

# Native GLM-5.3 Chat Completions: thinking is always on and reasoning_effort
# is only low|high|max. none/minimal/medium/xhigh are not mapped; they are
# sent as-is so origin-contract reject probes can observe 400/1210.
GLM53_REASONING_EFFORTS = {
    "low",
    "high",
    "max",
}

GLM53_DISPLAY_ALIASES = {
    "glm-5.3",
    "glm-5.3-flash",
    "glm5.3",
    "glm5.3-flash",
}


def _is_glm_5_3_model(model: str) -> bool:
    folded = str(model or "").strip().casefold().replace("_", "-")
    return folded in GLM53_DISPLAY_ALIASES


# Gemini 3 maps OpenAI-compatible effort values directly to thinkingLevel.
# Unlike DeepSeek aliases, minimal/low/medium/high are behaviorally distinct.
GEMINI_REASONING_EFFORTS = {
    "minimal",
    "low",
    "medium",
    "high",
}

# Official xAI Grok reasoning_effort values (Chat Completions).
# grok-4.5: low|medium|high (cannot disable). grok-4.3 also documents none.
GROK_REASONING_EFFORTS = {
    "none",
    "low",
    "medium",
    "high",
}

_USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
DEFAULT_MINIMUM_PROMPT_TOKENS = 100
_PROMPT_PADDING_MARKER = "测试输入长度填充；以下数字仅作为背景数据，回答时请忽略："


GEMINI_37_INTERACTIONS_PROFILES = {
    "gemini_3_7_flash_interactions_basic",
    "gemini_3_7_flash_interactions_system_instruction",
    "gemini_3_7_flash_interactions_response_format_json",
    "gemini_3_7_flash_interactions_stream",
    "gemini_3_7_flash_interactions_store_false",
    "gemini_3_7_flash_interactions_max_output_tokens",
    "gemini_3_7_flash_interactions_seed",
    "gemini_3_7_flash_interactions_stop_sequences",
    "gemini_3_7_flash_interactions_reject_thinking_minimal",
    "gemini_3_7_flash_interactions_thinking_low",
    "gemini_3_7_flash_interactions_thinking_medium",
    "gemini_3_7_flash_interactions_thinking_high",
    "gemini_3_7_flash_interactions_thinking_summaries_auto",
    "gemini_3_7_flash_interactions_tools_auto",
    "gemini_3_7_flash_interactions_tools_any",
    "gemini_3_7_flash_interactions_tools_none",
    "gemini_3_7_flash_interactions_tools_validated",
    "gemini_3_7_flash_interactions_labels",
}


@dataclass
class BuiltRequest:
    group: str
    profile: str
    body: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _ai_studio_endpoint_metadata(
    provider_cfg: dict[str, Any], transport: str, model: str,
) -> dict[str, Any]:
    interface = (provider_cfg.get("api_interfaces") or {}).get(transport) or {}
    base_url = str(interface.get("base_url") or provider_cfg.get("base_url") or "")
    if not is_ai_studio_origin(base_url):
        return {}
    default_path = (
        "/interactions" if transport == "gemini_interactions"
        else "/models/{model}:generateContent"
    )
    path = str(interface.get("path") or default_path).format(model=model)
    url = build_gemini_api_url(
        base_url, path,
        api_version=interface.get("api_version") or interface.get("default_api_version"),
    )
    return {"request_endpoint": urlsplit(url).path, "api_version": "v1beta"}


def apply_request_mode(
    body: dict[str, Any],
    transport: str,
    mode: str,
    nonce: str | None = None,
) -> bool:
    """Prepend a nonce to the first user content for cache-neutral load traffic."""
    if mode == "fixed":
        return False
    if mode != "unique":
        raise ValueError("request mode must be unique or fixed")
    marker = f"load-request-{nonce or secrets.token_hex(16)}|"
    if transport == "gemini_generate_content":
        for content in body.get("contents") or []:
            if not isinstance(content, dict) or content.get("role") != "user":
                continue
            for part in content.get("parts") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"] = marker + part["text"]
                    return True
        return False
    if transport == "openai_responses":
        response_input = body.get("input")
        if isinstance(response_input, str):
            body["input"] = marker + response_input
            return True
        if isinstance(response_input, list):
            for item in response_input:
                if not isinstance(item, dict) or item.get("role") != "user":
                    continue
                content = item.get("content")
                if isinstance(content, str):
                    item["content"] = marker + content
                    return True
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        for text_key in ("text", "input_text"):
                            if isinstance(block.get(text_key), str):
                                block[text_key] = marker + block[text_key]
                                return True
        return False
    if transport == "fim_completions":
        prompt = body.get("prompt")
        if isinstance(prompt, str):
            body["prompt"] = marker + prompt
            return True
        return False
    for message in body.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = marker + content
            return True
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    block["text"] = marker + block["text"]
                    return True
    return False


def build_request(
    config: dict[str, Any],
    group: str,
    profile: str,
    overrides: dict[str, Any] | None = None,
    model_family_override: str | None = None,
    api_form_override: str | None = None,
    route_profile_override: str | None = None,
    reference_source: str | None = None,
    enforce_model_capabilities: bool = True,
    pressure_policy_override: dict[str, Any] | None = None,
    parameter_reference_profile: dict[str, Any] | None = None,
    parameter_test: bool = False,
    provider_override: str | None = None,
    execution_route_override: str | None = None,
) -> BuiltRequest:
    reference_source = _pinned_reference_contract(config, reference_source)
    if (provider_override is not None or execution_route_override is not None) and not parameter_test:
        raise ValueError("provider_override is restricted to functional parameter requests")
    if parameter_test:
        # Keep parameter evidence tied to the declared input. Load workloads
        # may deliberately pad prompts; parameter probes must not add tokens.
        config = copy.deepcopy(config)
        config.pop("_functional_parameter_target", None)
        config["_parameter_test_exact_input"] = True
        if provider_override is not None:
            if not isinstance(provider_override, str) or not provider_override:
                raise ValueError("Parameter provider override must be a nonempty provider ID")
            selected_provider = get_provider_config(config, provider_override)["name"]
            selected_model = str((overrides or {}).get("model") or get_selected_model(config, selected_provider))
            config["_functional_parameter_target"] = {"provider": selected_provider, "model": selected_model}
            if execution_route_override is not None:
                config["_functional_parameter_target"]["route_profile"] = get_model_route_profile(
                    config, selected_model, selected_provider, route_profile=execution_route_override)
    provider_name = get_active_provider_name(config)
    provider_cfg = get_provider_config(config, provider_name)
    selected_model = get_selected_model(config, provider_name)
    requested_model = str((overrides or {}).get("model") or selected_model)
    legacy_family_override = str(model_family_override or "")
    route_profile = str(route_profile_override or "").strip()
    if legacy_family_override == "aliyun":
        model_family = infer_model_family(requested_model)
        route_profile = route_profile or "aliyun_maas"
    elif legacy_family_override == "openai":
        model_family = infer_model_family(requested_model)
    else:
        model_family = model_family_override or get_model_family(
            config, selected_model, provider_name
        )
    if model_family == "unknown":
        raise ValueError(
            f"Model {requested_model!r} has no registered model family."
        )
    logical_group = group
    yaml_group = profile_group_for_family(group, model_family)

    settings = resolve_profile(
        config,
        yaml_group,
        profile,
        model_family_override=model_family,
    )
    profile_default_settings = copy.deepcopy(settings)
    if overrides:
        settings = deep_merge(settings, overrides)
    omit_params = settings.get("omit_params") or []
    if not isinstance(omit_params, list) or not all(isinstance(item, str) for item in omit_params):
        raise ValueError(f"{yaml_group}.{profile}.omit_params must be a list of parameter names.")
    for parameter in omit_params:
        settings.pop(parameter, None)

    model = settings.get("model") or selected_model
    if model_family_override is None:
        route_profile = route_profile or get_model_route_profile(
            config,
            str(model),
            provider_name,
        )
    elif not route_profile:
        if reference_source:
            from .reference_specs import get_reference_source

            route_profile = str(
                get_reference_source(reference_source).get("route_profile") or ""
            )
        route_profile = route_profile or "dynamic_aggregator"

    requested_api_form: str | None = None
    if api_form_override:
        requested_api_form = str(api_form_override)
    elif settings.get("transport"):
        transport = str(settings["transport"])
        from .config import api_form_for_transport

        requested_api_form = api_form_for_transport(transport)
    elif logical_group == "compatibility_profiles":
        # Compatibility profiles without an explicit native transport model the
        # chat-completions interface by definition.
        transport = "chat_completions"
        requested_api_form = "openai_chat_completions"
    elif model_family_override is not None:
        # Family-isolated unit/reference construction is intentionally detached
        # from whichever local provider happens to be active.
        transport = _family_default_transport(logical_group, model_family)
        from .config import api_form_for_transport

        requested_api_form = api_form_for_transport(transport)

    if model_family_override is None:
        api_form = get_model_api_form(
            config,
            str(model),
            provider_name,
            route_profile=route_profile,
            api_form=requested_api_form,
        )
        transport = get_model_transport(
            config,
            str(model),
            provider_name,
            route_profile=route_profile,
            api_form=api_form,
        )
    else:
        api_form = str(requested_api_form or "openai_chat_completions")
        transport = transport_for_api_form(api_form)
    capability_metadata: dict[str, Any] = {}
    capability_warnings: list[str] = []
    if enforce_model_capabilities and model_family_override is None:
        capability_metadata, capability_warnings = _apply_model_pressure_capability(
            config,
            provider_name,
            settings,
            model_family,
            str(model),
            transport,
            api_form,
            route_profile,
            reference_source,
            pressure_policy_override,
        )
    # Shared load/cache workloads require adaptive/default-effort normalization;
    # compatibility probes must retain their exact source-scoped wire values.
    if model_family == "claude_fable" and logical_group in {
        "throughput_profiles",
        "cache_profiles",
    }:
        _apply_claude_fable_compat(settings, omit_sampling=True)
    elif model_family == "qwen":
        _apply_qwen_compat(settings)
    warnings: list[str] = list(capability_warnings)

    if transport == "gemini_interactions":
        if model_family != "gemini":
            raise ValueError(
                "gemini_interactions transport requires the gemini reference family."
            )
        if not model:
            raise ValueError("A model is required for Gemini Interactions.")
        if reference_source == "gemini_3_7_flash_interactions":
            if str(model) != "gemini-3.7-flash":
                raise ValueError(
                    "gemini_3_7_flash_interactions requires model gemini-3.7-flash."
                )
            if profile not in GEMINI_37_INTERACTIONS_PROFILES:
                raise ValueError(
                    f"Profile {profile!r} is outside the Gemini 3.7 Flash "
                    "Interactions safe text contract."
                )
        body = _build_gemini_interactions_body(config, settings, str(model))
        metadata = {
            "provider": provider_name,
            "provider_label": provider_cfg.get("label") or provider_name,
            "model_family": model_family,
            "api_form": api_form,
            "route_profile": route_profile,
            "reference_source": reference_source,
            "requested_model": str(model),
            "transport": transport,
            "request_endpoint": "/v1/interactions",
            **_ai_studio_endpoint_metadata(provider_cfg, transport, str(model)),
            # Stateful tool-result continuation is deliberately outside this
            # safe text runner. A requires_action response is the terminal
            # evidence for tool profiles.
            "multi_turn": False,
            "pass_reasoning_content": False,
            "prompt_source": _prompt_source(settings),
            "profile_group": yaml_group,
            **capability_metadata,
        }
        return BuiltRequest(
            group=logical_group,
            profile=profile,
            body=body,
            metadata=metadata,
            warnings=warnings,
        )
    if transport == "gemini_generate_content":
        if model_family != "gemini":
            raise ValueError("gemini_generate_content transport requires the gemini reference family.")
        if not model:
            raise ValueError("A model is required for Gemini GenerateContent.")
        body = _build_gemini_native_body(config, settings)
        if reference_source == "gemini_3_7_flash_generate_content":
            if str(model) != "gemini-3.7-flash":
                raise ValueError("gemini_3_7_flash_generate_content requires model gemini-3.7-flash.")
            _enforce_gemini_37_generate_content_safety(profile, settings, body)
        metadata = {
            "provider": provider_name,
            "provider_label": provider_cfg.get("label") or provider_name,
            "model_family": model_family,
            "api_form": api_form,
            "route_profile": route_profile,
            "reference_source": reference_source,
            "requested_model": str(model),
            "transport": transport,
            "request_endpoint": f"/models/{model}:generateContent",
            **_ai_studio_endpoint_metadata(provider_cfg, transport, str(model)),
            "multi_turn": bool(settings.get("multi_turn", False)),
            "pass_reasoning_content": False,
            "prompt_source": _prompt_source(settings),
            "profile_group": yaml_group,
            **capability_metadata,
        }
        request_headers = _request_headers_from_settings(settings)
        if request_headers:
            metadata["request_headers"] = request_headers
        return BuiltRequest(
            group=logical_group,
            profile=profile,
            body=body,
            metadata=metadata,
            warnings=warnings,
        )
    if transport == "claude_messages":
        if model_family not in {"claude", "claude_fable", "deepseek"}:
            raise ValueError(
                "claude_messages transport requires the claude, claude_fable, "
                "or DeepSeek Anthropic-compatible reference family."
            )
        if not model:
            raise ValueError("A model is required for Anthropic Messages.")
        body = (
            _build_deepseek_anthropic_messages_body(config, settings, str(model))
            if model_family == "deepseek"
            else _build_claude_messages_body(
                config, settings, str(model), reference_source=reference_source,
                # Family-isolated construction uses route_profile for the
                # reference Contract; caching must bind to the execution route.
                runtime_route_profile=(
                    route_profile if model_family_override is None else None
                ),
            )
        )
        metadata = {
            "provider": provider_name,
            "provider_label": provider_cfg.get("label") or provider_name,
            "model_family": model_family,
            "api_form": api_form,
            "route_profile": route_profile,
            "reference_source": reference_source,
            "requested_model": str(model),
            "transport": transport,
            "request_endpoint": "/messages",
            "multi_turn": bool(settings.get("multi_turn", False)),
            "pass_reasoning_content": False,
            "prompt_source": _prompt_source(settings),
            "profile_group": yaml_group,
            **capability_metadata,
        }
        return BuiltRequest(
            group=logical_group,
            profile=profile,
            body=body,
            metadata=metadata,
            warnings=warnings,
        )
    if transport == "openai_responses":
        if not model:
            raise ValueError("A model is required for OpenAI Responses.")
        body = _build_openai_responses_body(
            config, settings, str(model), reference_source=reference_source
        )
        metadata = {
            "provider": provider_name,
            "provider_label": provider_cfg.get("label") or provider_name,
            "model_family": model_family,
            "api_form": api_form,
            "route_profile": route_profile,
            "reference_source": reference_source,
            "requested_model": str(model),
            "transport": transport,
            "request_endpoint": "/responses",
            "multi_turn": bool(settings.get("multi_turn", False)),
            "pass_reasoning_content": False,
            "prompt_source": _prompt_source(settings),
            "profile_group": yaml_group,
            **capability_metadata,
        }
        return BuiltRequest(
            group=logical_group,
            profile=profile,
            body=body,
            metadata=metadata,
            warnings=warnings,
        )
    if transport == "fim_completions":
        if model_family != "deepseek":
            raise ValueError(
                "fim_completions transport requires the DeepSeek reference family."
            )
        if not model:
            raise ValueError("A model is required for DeepSeek FIM Completions.")
        body = _build_fim_completion_body(config, settings, str(model))
        metadata = {
            "provider": provider_name,
            "provider_label": provider_cfg.get("label") or provider_name,
            "model_family": model_family,
            "api_form": api_form,
            "route_profile": route_profile,
            "reference_source": reference_source,
            "requested_model": str(model),
            "transport": transport,
            "request_endpoint": "/beta/completions",
            "multi_turn": False,
            "pass_reasoning_content": False,
            "prompt_source": _prompt_source(settings),
            "profile_group": yaml_group,
            **capability_metadata,
        }
        return BuiltRequest(
            group=logical_group,
            profile=profile,
            body=body,
            metadata=metadata,
            warnings=warnings,
        )
    if transport != "chat_completions":
        raise ValueError(f"Unsupported profile transport: {transport}")

    parameter_reference_policy: dict[str, Any] = {}
    if (logical_group == "compatibility_profiles" and not enforce_model_capabilities
            and profile == "sampling_non_thinking"
            and model_family == "kimi" and reference_source):
        from .reference_specs import get_reference_source, load_model_capability_profile

        reference_capability = parameter_reference_profile
        if reference_capability is None:
            reference_capability = load_model_capability_profile(
                "text", model_family, str(model), api_form=api_form,
                route_profile=route_profile, reference_source=reference_source,
            )
        if reference_capability.get("reference_source") != reference_source:
            raise ValueError("parameter capability does not match the selected reference Contract")
        validate_probe_reference_identity(
            reference_capability, model=str(model), family=model_family,
            api_form=api_form, reference_source=reference_source,
            source_id=str(get_reference_source(reference_source)["source_id"]),
        )
        selected_policy = non_thinking_probe_policy(reference_capability, profile)
        parameter_reference_policy = apply_non_thinking_probe_policy(
            settings, selected_policy,
            default_settings=profile_default_settings,
            explicit_overrides=overrides,
            omitted_parameters=omit_params,
        )

    body: dict[str, Any] = {
        "model": model,
        "messages": _build_messages(config, settings),
    }

    if not body["model"]:
        raise ValueError("A model is required. Set models.default or profile.model in config.yaml.")

    if "tools_fixture" in settings and "tools" not in settings:
        settings["tools"] = _read_json_fixture(settings["tools_fixture"])

    family_supported = set(_supported_params_for_family(model_family, route_profile))
    if parameter_reference_policy:
        family_supported.add("thinking")
    deprecated_params = set(DEPRECATED_PARAMS)
    if model_family == "deepseek":
        deprecated_params.add("presence_penalty")
    allow_deprecated = bool(
        _family_param_config(config, model_family).get("allow_deprecated", False)
        or settings.get("send_deprecated", False)
    )
    for key, value in settings.items():
        if key in PROFILE_KEYS or key == "model" or key == "messages":
            continue
        if key in deprecated_params:
            message = f"{key} is deprecated and is not sent by default."
            warnings.append(message)
            if allow_deprecated:
                body[key] = value
            continue
        if key in SUPPORTED_PARAMS:
            if key not in family_supported:
                warnings.append(f"{key} is not in {model_family} supported params and was not sent.")
                continue
            body[key] = value
            continue
        raise ValueError(f"Unsupported request/profile key in {yaml_group}.{profile}: {key}")

    _normalize_body(
        body,
        model_family,
        preserve_rejected_params=bool(settings.get("preserve_rejected_params", False)),
        fable_official_compat_profile=(
            logical_group == "compatibility_profiles" and model_family == "claude_fable"
            and str(model) == "claude-fable-5" and api_form == "openai_chat_completions"
            and route_profile == "vendor_compat" and reference_source == "claude_fable_openai_compat"
        ),
        preserve_claude_parameter_body=(
            logical_group == "compatibility_profiles" and model_family == "claude"
            and api_form == "openai_chat_completions" and route_profile == "vendor_compat"
            and reference_source == "claude_openai_compat"
            and (bool(settings.get("preserve_rejected_params", False))
                 or "thinking" in (overrides or {}))
        ),
        preserve_fable_parameter_body=(
            logical_group == "compatibility_profiles" and model_family == "claude_fable"
            and str(model) == "claude-fable-5" and api_form == "openai_chat_completions"
            and route_profile == "vendor_compat" and reference_source == "claude_fable_openai_compat"
            and (parameter_test or bool(settings.get("preserve_rejected_params", False)))
        ),
    )
    if (logical_group == "compatibility_profiles" and api_form == "openai_chat_completions"
            and route_profile == "vendor_compat"
            and (model_family, reference_source) in {
                ("claude", "claude_openai_compat"), ("claude_fable", "claude_fable_openai_compat")}
            and not parameter_test and not bool(settings.get("preserve_rejected_params", False))
            and "max_tokens" in body and "max_completion_tokens" in body):
        explicit_limits = {name for name in ("max_tokens", "max_completion_tokens")
                           if overrides is not None and name in overrides and name not in omit_params}
        if len(explicit_limits) != 1:
            raise ValueError("Anthropic official Chat accepts one output-limit field; choose max_tokens or max_completion_tokens.")
        selected_limit = next(iter(explicit_limits))
        body.pop("max_tokens" if selected_limit == "max_completion_tokens" else "max_completion_tokens")
    if (logical_group == "compatibility_profiles" and model_family == "claude"
            and api_form == "openai_chat_completions" and route_profile == "vendor_compat"
            and reference_source == "claude_openai_compat"
            and not parameter_test and not bool(settings.get("preserve_rejected_params", False))
            and "temperature" in body and "top_p" in body):
        explicit_sampling = {name for name in ("temperature", "top_p")
                             if overrides is not None and name in overrides and name not in omit_params}
        if len(explicit_sampling) > 1:
            raise ValueError("Anthropic official Chat sampling requires one field; choose temperature or top_p.")
        selected_sampling = next(iter(explicit_sampling)) if explicit_sampling else "temperature"
        body.pop("top_p" if selected_sampling == "temperature" else "temperature")
    _validate_body(body, settings, model_family)
    validate_reference_request(body, parameter_reference_policy)

    metadata = {
        "provider": provider_name,
        "provider_label": provider_cfg.get("label") or provider_name,
        "model_family": model_family,
        "api_form": api_form,
        "route_profile": route_profile,
        "reference_source": reference_source,
        "requested_model": body.get("model"),
        "transport": transport,
        "request_endpoint": "/chat/completions",
        "multi_turn": bool(settings.get("multi_turn", False)),
        "pass_reasoning_content": bool(settings.get("pass_reasoning_content", False)),
        "prompt_source": _prompt_source(settings),
        "profile_group": yaml_group,
        **capability_metadata,
        **({"parameter_reference_policy": parameter_reference_policy} if parameter_reference_policy else {}),
    }
    return BuiltRequest(group=logical_group, profile=profile, body=body, metadata=metadata, warnings=warnings)


def resolve_profile(
    config: dict[str, Any],
    group: str,
    profile: str,
    model_family_override: str | None = None,
) -> dict[str, Any]:
    profiles = config.get(group) or {}
    if profile not in profiles:
        raise KeyError(f"Profile {group}.{profile} not found in config.yaml")

    provider_name = get_active_provider_name(config)
    model_family = model_family_override or get_model_family(config, get_selected_model(config, provider_name), provider_name)
    default = copy.deepcopy(_family_param_config(config, model_family).get("default", {}))
    resolved = _resolve_profile_extends(profiles, profile, stack=())
    return deep_merge(default, resolved)


def profile_names(config: dict[str, Any], group: str) -> list[str]:
    return list((config.get(group) or {}).keys())


def _pinned_reference_contract(
    config: dict[str, Any], requested: str | None
) -> str | None:
    """Keep request construction on the immutable runtime MPDB binding."""

    database = config.get("_model_profile_database")
    pinned = (
        str(database.get("reference_contract_id") or "").strip()
        if isinstance(database, dict)
        else ""
    )
    selected = str(requested or "").strip()
    if pinned and selected and pinned != selected:
        raise ValueError(
            "Requested reference Contract conflicts with the runtime MPDB snapshot."
        )
    return selected or pinned or None


def _runtime_model_capability(
    config: dict[str, Any],
    provider: str,
    family: str,
    model: str,
    transport: str,
    api_form: str,
    route_profile: str,
    reference_source: str | None,
) -> dict[str, Any]:
    from .model_profile_catalog import capability_profile_from_database_snapshot
    from .reference_specs import load_model_capability_profile

    database = config.get("_model_profile_database")
    if not isinstance(database, dict):
        return load_model_capability_profile(
            "text",
            family,
            model,
            api_form=api_form,
            route_profile=route_profile,
            reference_source=reference_source,
            provider_override=get_model_api_forms(
                config,
                model,
                provider,
                route_profile=route_profile,
            )[api_form],
        )

    capability = capability_profile_from_database_snapshot(database)
    target = capability.get("execution_target") or {}
    expected = {
        "provider_id": provider,
        "request_model_id": model,
        "route_profile": route_profile,
        "api_form": api_form,
        "transport": transport,
    }
    conflicts = [
        field
        for field, value in expected.items()
        if target.get(field) not in (None, "", value)
    ]
    if str(capability.get("modality") or "") != "text":
        conflicts.append("modality")
    if str(capability.get("family") or "") != family:
        conflicts.append("family")
    if reference_source and (
        str(capability.get("reference_contract_id") or "") != reference_source
    ):
        conflicts.append("reference_contract_id")
    if conflicts:
        raise ValueError(
            "Request construction conflicts with immutable MPDB snapshot: "
            + ", ".join(sorted(set(conflicts)))
        )
    return capability


def _family_default_transport(group: str, family: str) -> str:
    if family == "claude" and group == "throughput_profiles":
        return "claude_messages"
    if family == "claude_fable" and group in {"throughput_profiles", "cache_profiles"}:
        return "claude_messages"
    return "chat_completions"


def load_pressure_capability(
    config: dict[str, Any],
    provider: str,
    family: str,
    model: str,
    api_form: str,
    route_profile: str,
    reference_source: str | None = None,
) -> dict[str, Any]:
    """Restore pressure policy from the selected immutable MPDB snapshot."""
    from .reference_specs import catalog_model_id_for_runtime, load_model_capability_profile

    snapshot = config.get("_model_profile_database")
    cached = config.get("_model_capability_profile")
    if cached is not None and not isinstance(cached, dict):
        raise ValueError("Cached pressure capability must be an object.")
    if snapshot is None and isinstance(cached, dict):
        snapshot = cached.get("model_profile_database")
        if not isinstance(snapshot, dict):
            raise ValueError("Cached pressure capability requires an immutable MPDB snapshot.")
    if snapshot is None:
        return load_model_capability_profile(
            "text", family, catalog_model_id_for_runtime(config, model, provider),
            api_form=api_form, route_profile=route_profile,
            reference_source=reference_source,
            provider_override=get_model_api_forms(
                config, model, provider, route_profile=route_profile,
            )[api_form],
        )

    from .model_profile_catalog import binding_from_database_snapshot

    binding = binding_from_database_snapshot(snapshot)
    target = binding["execution_target"]
    expected_target = {
        "provider_id": provider, "request_model_id": model,
        "api_form": api_form, "route_profile": route_profile,
    }
    conflicts = [
        field for field, expected in expected_target.items()
        if target.get(field) not in (None, "", expected)
    ]
    expected_identity = {
        "modality": "text", "suite_family_id": family, "api_form": api_form,
    }
    if reference_source:
        expected_identity["reference_contract_id"] = reference_source
    conflicts.extend(
        field for field, expected in expected_identity.items()
        if snapshot.get(field) != expected
    )
    accepted_models = {
        str(value) for value in (
            snapshot.get("model_slug"),
            *(binding["profile"].get("request_model_ids") or []),
        ) if value
    }
    if not target.get("request_model_id") and model not in accepted_models:
        conflicts.append("request_model_id")
    policy = copy.deepcopy(binding.get("test_binding") or {})
    if isinstance(cached, dict):
        cached_identity = {
            "modality": "text", "family": family, "api_form": api_form,
            "route_profile": route_profile,
            "reference_source": snapshot["reference_contract_id"],
            **{field: snapshot[field] for field in (
                "source_id", "profile_id", "interface_id", "test_binding_id",
                "reference_contract_id",
            )},
        }
        conflicts.extend(
            "capability." + field for field, expected in cached_identity.items()
            if cached.get(field) not in (None, "", expected)
        )
        if cached.get("model") not in (None, "", model, *accepted_models):
            conflicts.append("capability.model")
        if cached.get("model_profile_database", snapshot) != snapshot:
            conflicts.append("capability.model_profile_database")
        if cached.get("disabled_reason") or any(
            cached.get(field) is False for field in (
                "pressure_test_enabled", "enabled", "executable", "runner_enabled",
                "known_model", "known_api_profile", "route_profile_known",
            )
        ):
            raise ValueError("Pressure testing is disabled by the frozen capability.")
    if conflicts:
        raise ValueError("Pressure snapshot conflicts with runtime selection: " + ", ".join(conflicts))
    if (policy.get("pressure_test_enabled") is not True
            or policy.get("disabled_reason")
            or any(policy.get(field) is False for field in ("enabled", "executable", "runner_enabled"))
            or binding["reference_contract"].get("test_binding_status", "required") != "required"):
        raise ValueError("Pressure testing is disabled by the frozen MPDB policy.")
    return {
        **policy, **expected_identity,
        "model": model, "family": family, "route_profile": route_profile,
        **{field: snapshot[field] for field in (
            "source_id", "profile_id", "interface_id", "test_binding_id",
            "reference_contract_id",
        )},
        "transport": binding["interface"].get("transport_adapter_id"),
        "known_model": True, "known_api_profile": True, "route_profile_known": True,
        "profile_status": "registered", "model_profile_database": copy.deepcopy(snapshot),
        "evidence": binding["interface"].get("evidence") or binding["profile"].get("evidence"),
    }


def weighted_workload_profiles(
    config: dict[str, Any],
    workload: str,
    *,
    api_form: str | None = None,
    route_profile: str | None = None,
    reference_source: str | None = None,
) -> list[tuple[str, str, int]]:
    reference_source = _pinned_reference_contract(config, reference_source)
    if workload.startswith("throughput") or workload in {"mixed_compat", "cache_suite"}:
        provider = get_active_provider_name(config)
        model = get_selected_model(config, provider)
        family = get_model_family(config, model, provider)
        from .reference_specs import pressure_test_runnable

        selected_route_profile = route_profile or get_model_route_profile(
            config, model, provider
        )
        selected_api_form = get_model_api_form(
            config, model, provider,
            route_profile=selected_route_profile, api_form=api_form,
        )
        capability = load_pressure_capability(
            config, provider, family, model, selected_api_form,
            selected_route_profile, reference_source,
        )
        if (
            capability.get("known_model") is not True
            or capability.get("known_api_profile") is not True
            or capability.get("route_profile_known") is not True
        ):
            raise ValueError(
                f"Missing registered text model/API/route profile for "
                f"{family}/{selected_api_form}/{model}/{selected_route_profile}."
            )
        if not pressure_test_runnable(capability):
            raise ValueError(
                f"Pressure testing is disabled for {family}/{model}: "
                f"{capability.get('disabled_reason') or 'model profile policy'}."
            )
    if workload.startswith("throughput"):
        weights = config.get("profile_weights", {}).get(workload, {})
        names = profile_names(config, "throughput_profiles")
        entries = [
            ("throughput_profiles", name, int(weights.get(name, 0 if weights else 1)))
            for name in names
            if int(weights.get(name, 0 if weights else 1)) > 0
        ]
        if not entries:
            raise ValueError(f"profile_weights.{workload} must contain at least one positive throughput profile weight.")
        return entries

    if workload == "mixed_compat":
        weights = config.get("profile_weights", {}).get("mixed_compat", {})
        provider = get_active_provider_name(config)
        model = get_selected_model(config, provider)
        family = get_model_family(config, model, provider)
        from .reference_specs import (
            default_reference_source_for_model,
            pressure_profiles_for_model,
        )

        snapshot = capability.get("model_profile_database")
        selected_reference_source = reference_source or capability.get("reference_contract_id") or (
            default_reference_source_for_model(
                config, family, model, provider,
                api_form=selected_api_form, route_profile=selected_route_profile,
            )
        )
        if isinstance(snapshot, dict) and (
            config.get("_model_profile_database") is not None
            or isinstance(config.get("_model_capability_profile"), dict)
        ):
            from .reference_specs import resolve_profile_expectation

            suite_profiles = list((snapshot.get("parameter_test_binding") or {}).get("test_cases") or [])
            configured = (capability.get("pressure_profiles") or {}).get(selected_reference_source)
            candidates = list(configured) if configured is not None else suite_profiles
            if set(candidates) - set(suite_profiles):
                raise ValueError("Frozen pressure profiles are outside the selected MPDB test suite.")
            allowed_profiles = [
                name for name in candidates
                if resolve_profile_expectation(
                    "text", family, model, name,
                    capability_profile=capability,
                    reference_source=selected_reference_source, parameter_probe=False,
                ) == "supported"
            ]
        else:
            from .reference_specs import catalog_model_id_for_runtime

            allowed_profiles = pressure_profiles_for_model(
                family, catalog_model_id_for_runtime(config, model, provider),
                selected_reference_source, api_form=selected_api_form,
                route_profile=selected_route_profile,
            )
        entries = [
            (
                "compatibility_profiles",
                name,
                int(weights[name]),
            )
            for name in allowed_profiles
            if name in weights and int(weights[name]) > 0
        ]
        if "list_models" in weights:
            entries.append(("control", "list_models", int(weights["list_models"])))
        if not entries:
            raise ValueError(
                f"profile_weights.mixed_compat has no positive profiles for "
                f"{family}/{selected_api_form}/{selected_reference_source}."
            )
        return entries

    if workload == "cache_suite":
        return [("cache_profiles", name, 1) for name in profile_names(config, "cache_profiles")]

    raise ValueError(f"Unsupported LOADTEST_WORKLOAD={workload!r}")


def _apply_model_pressure_capability(
    config: dict[str, Any],
    provider: str,
    settings: dict[str, Any],
    family: str,
    model: str,
    transport: str,
    api_form: str,
    route_profile: str,
    reference_source: str | None,
    pressure_policy_override: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Remove parameters a model profile marks unsafe for pressure traffic.

    Parameter probes bypass this policy through ``model_family_override`` so
    unsupported values are still sent and their rejection can be verified.
    """
    if pressure_policy_override is None:
        from .reference_specs import pressure_test_runnable

        try:
            capability = load_pressure_capability(
                config, provider, family, model, api_form, route_profile,
                reference_source,
            )
        except KeyError as exc:
            raise ValueError(
                f"Missing text capability family/profile for {family}/{model}."
            ) from exc
        if (
            capability.get("known_model") is not True
            or capability.get("known_api_profile") is not True
            or capability.get("route_profile_known") is not True
        ):
            raise ValueError(
                f"Missing registered text model/API/route profile for "
                f"{family}/{api_form}/{model}/{route_profile}."
            )
        runnable = pressure_test_runnable(capability)
    else:
        capability = copy.deepcopy(pressure_policy_override)
        runnable = bool(
            capability.get("pressure_test_enabled") is True
            and capability.get("disabled_reason") in (None, "")
            and all(
                capability.get(field) is not False
                for field in ("enabled", "executable", "runner_enabled")
            )
        )
    if not runnable:
        raise ValueError(
            f"Pressure testing is disabled for {family}/{model}: "
            f"{capability.get('disabled_reason') or 'model profile policy'}."
        )
    transport_policy = (
        capability.get("pressure_transport_overrides") or {}
    ).get(transport) or {}
    parameter_aliases = dict(capability.get("pressure_parameter_aliases") or {})
    parameter_aliases.update(transport_policy.get("parameter_aliases") or {})
    applied_aliases: dict[str, str] = {}
    for source, target in parameter_aliases.items():
        source_name = str(source)
        target_name = str(target)
        if source_name not in settings:
            continue
        value = settings.pop(source_name)
        settings.setdefault(target_name, value)
        applied_aliases[source_name] = target_name
    pressure_overrides = copy.deepcopy(
        capability.get("pressure_overrides") or {}
    )
    pressure_overrides = deep_merge(
        pressure_overrides,
        transport_policy.get("overrides") or {},
    )
    explicit_unsupported = [
        str(parameter)
        for parameter, expectation in (
            capability.get("parameter_expectations") or {}
        ).items()
        if str(expectation) == "unsupported"
    ]
    requested_omits = list(capability.get("pressure_omit_params") or [])
    requested_omits.extend(transport_policy.get("omit_params") or [])
    omit_params = list(dict.fromkeys(requested_omits + explicit_unsupported))
    removed: list[str] = []
    for parameter in omit_params:
        removed.extend(_remove_pressure_parameter(settings, parameter))
    # Fixed safe values are applied after removals so an equivalence class such
    # as reasoning/reasoning_effort can be cleared and then rebuilt in the
    # transport-native shape.
    for parameter, value in pressure_overrides.items():
        settings[str(parameter)] = value
    metadata = {
        "capability_profile_id": capability.get("profile_id"),
        "capability_profile_status": capability.get("profile_status"),
        "capability_evidence": capability.get("evidence"),
        "capability_omitted_params": sorted(set(removed)),
        "capability_pressure_aliases": applied_aliases,
        "capability_pressure_overrides": pressure_overrides,
        "capability_pressure_transport": transport,
    }
    warnings = [
        f"{parameter} was omitted by the {family}/{model} pressure capability profile."
        for parameter in sorted(set(removed))
    ]
    warnings.extend(
        f"{source} was rewritten to {target} by the {family}/{model} pressure capability profile."
        for source, target in sorted(applied_aliases.items())
    )
    warnings.extend(
        f"{parameter} was fixed by the {family}/{model} pressure capability profile."
        for parameter in sorted(pressure_overrides)
    )
    return metadata, warnings


def _remove_pressure_parameter(
    settings: dict[str, Any],
    parameter: str,
) -> list[str]:
    parameter_path = str(parameter)
    path_parts = parameter_path.split(".")
    root = path_parts[0]
    if len(path_parts) > 1:
        parent: Any = settings
        for segment in path_parts[:-1]:
            if not isinstance(parent, dict) or not isinstance(parent.get(segment), dict):
                return []
            parent = parent[segment]
        if path_parts[-1] in parent:
            parent.pop(path_parts[-1], None)
            return [parameter_path]
        return []

    aliases = {
        "input": {"input", "messages", "prompt", "prompt_key", "prompt_fixture"},
        "max_output_tokens": {"max_output_tokens", "max_tokens"},
        "reasoning": {"reasoning", "reasoning_effort"},
        "reasoning_effort": {"reasoning_effort", "reasoning"},
        "text": {"text", "response_format"},
        "tools": {"tools", "tools_fixture", "native_tools", "multi_turn"},
        "tool_choice": {"tool_choice", "native_tool_config"},
    }
    keys = aliases.get(root, {root})
    removed: list[str] = []
    for key in keys:
        if key in settings:
            settings.pop(key, None)
            removed.append(parameter_path)
    return removed


def build_tool_followup_request(
    original_body: dict[str, Any],
    first_response: dict[str, Any],
    pass_reasoning_content: bool = False,
) -> dict[str, Any]:
    tool_calls = extract_tool_calls(first_response)
    if not tool_calls:
        raise ValueError("Cannot build tool follow-up because response has no tool_calls.")

    first_message = extract_message(first_response)
    assistant_message = copy.deepcopy(first_message)
    assistant_message["role"] = "assistant"
    assistant_message["tool_calls"] = copy.deepcopy(tool_calls)
    assistant_message.setdefault("content", "")
    if not pass_reasoning_content:
        assistant_message.pop("reasoning_content", None)

    followup = copy.deepcopy(original_body)
    followup["messages"] = list(original_body["messages"]) + [assistant_message]
    for tool_call in tool_calls:
        followup["messages"].append(_mock_tool_message(tool_call))

    followup["stream"] = False
    followup.pop("stream_options", None)
    followup.pop("response_format", None)
    followup.pop("logprobs", None)
    followup.pop("top_logprobs", None)
    if "tools" in followup and "tool_choice" in original_body:
        followup["tool_choice"] = "none"
    return followup


def build_native_tool_followup_request(
    original_body: dict[str, Any],
    first_response: dict[str, Any],
) -> dict[str, Any]:
    candidates = first_response.get("candidates") or []
    if not candidates or not isinstance(candidates[0], dict):
        raise ValueError("Cannot build native tool follow-up because response has no candidate.")
    model_content = copy.deepcopy(candidates[0].get("content") or {})
    if not isinstance(model_content, dict) or not isinstance(model_content.get("parts"), list):
        raise ValueError("Cannot build native tool follow-up because candidate content is malformed.")
    calls = extract_native_function_calls(first_response)
    if not calls:
        raise ValueError("Cannot build native tool follow-up because response has no functionCall.")

    model_content["role"] = "model"
    response_parts: list[dict[str, Any]] = []
    for call in calls:
        name = str(call.get("name") or "")
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        function_response: dict[str, Any] = {
            "name": name,
            "response": _mock_tool_payload(name=name, args=args),
        }
        if call.get("id") is not None:
            function_response["id"] = call["id"]
        response_parts.append({"functionResponse": function_response})

    followup = copy.deepcopy(original_body)
    followup["contents"] = (
        list(copy.deepcopy(original_body.get("contents") or []))
        + [model_content, {"role": "user", "parts": response_parts}]
    )
    return followup


def build_claude_tool_followup_request(
    original_body: dict[str, Any],
    first_response: dict[str, Any],
) -> dict[str, Any]:
    tool_uses = extract_claude_tool_uses(first_response)
    if not tool_uses:
        raise ValueError("Cannot build Claude tool follow-up because response has no tool_use block.")

    followup = copy.deepcopy(original_body)
    assistant_content = copy.deepcopy(first_response.get("content") or [])
    followup["messages"] = list(copy.deepcopy(original_body.get("messages") or [])) + [
        {"role": "assistant", "content": assistant_content}
    ]
    followup["messages"].append(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": str(tool_use.get("id") or ""),
                    "content": json.dumps(
                        _mock_tool_payload(
                            name=str(tool_use.get("name") or ""),
                            args=tool_use.get("input") if isinstance(tool_use.get("input"), dict) else {},
                        ),
                        ensure_ascii=False,
                    ),
                }
                for tool_use in tool_uses
            ],
        }
    )
    followup["stream"] = False
    followup.pop("tool_choice", None)
    return followup


def build_openai_responses_tool_followup_request(
    original_body: dict[str, Any],
    first_response: dict[str, Any],
) -> dict[str, Any]:
    calls = extract_openai_responses_function_calls(first_response)
    if not calls:
        raise ValueError(
            "Cannot build OpenAI Responses tool follow-up because response has no function_call."
        )

    followup = copy.deepcopy(original_body)
    prior_output = copy.deepcopy(first_response.get("output") or [])
    original_input = original_body.get("input")
    followup_input: list[dict[str, Any]] = (
        copy.deepcopy(original_input) if isinstance(original_input, list)
        else [{"role": "user", "content": original_input}]
        if isinstance(original_input, str)
        else []
    )
    if isinstance(prior_output, list):
        followup_input.extend(item for item in prior_output if isinstance(item, dict))
    for call in calls:
        args_raw = call.get("arguments")
        args: dict[str, Any] = {}
        if isinstance(args_raw, str) and args_raw.strip():
            try:
                parsed = json.loads(args_raw)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                args = parsed
        followup_input.append(
            {
                "type": "function_call_output",
                "call_id": str(call.get("call_id") or ""),
                "output": json.dumps(
                    _mock_tool_payload(name=str(call.get("name") or ""), args=args),
                    ensure_ascii=False,
                ),
            }
        )
    followup["input"] = followup_input
    followup["stream"] = False
    followup.pop("tool_choice", None)
    return followup


def _build_openai_responses_body(
    config: dict[str, Any],
    settings: dict[str, Any],
    model: str,
    *,
    reference_source: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model}

    if "input" in settings:
        body["input"] = copy.deepcopy(settings["input"])
    elif "messages" in settings:
        body["input"] = copy.deepcopy(settings["messages"])
    else:
        body["input"] = ensure_minimum_prompt_text(config, _resolve_prompt(config, settings))

    if isinstance(body.get("input"), str):
        body["input"] = ensure_minimum_prompt_text(config, str(body["input"]))
    elif isinstance(body.get("input"), list):
        _ensure_responses_input_minimum_prompt(config, body["input"])

    for key in (
        "instructions",
        "stream",
        "store",
        "max_output_tokens",
        "tool_choice",
        "parallel_tool_calls",
        "previous_response_id",
        "metadata",
        "prompt_cache_key",
        "prompt_cache_options",
        # Reject probes may intentionally send stop (unsupported on reasoning models).
        "stop",
    ):
        if key in settings:
            body[key] = copy.deepcopy(settings[key])

    if reference_source == "deepseek_v4_pro_0813_responses":
        # The official contract tests accepted-but-ignored fields as well as
        # effective parameters. Dropping them here would make those probes
        # vacuous. Keep this parity increment on its exact source contract.
        if model != "deepseek-v4-pro":
            raise ValueError("DeepSeek 0813 Responses requires model deepseek-v4-pro.")
        for key in (
            "temperature", "top_p", "top_logprobs", "user", "max_tool_calls",
            "conversation", "background", "include", "truncation", "service_tier",
            "safety_identifier", "prompt_cache_retention", "context_management",
            "stream_options",
        ):
            if key in settings:
                body[key] = copy.deepcopy(settings[key])
        if "responses_prompt" in settings:
            body["prompt"] = copy.deepcopy(settings["responses_prompt"])
        for key, maximum in (("temperature", 2), ("top_p", 1)):
            if key in body:
                value = body[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= maximum:
                    raise ValueError(f"DeepSeek Responses {key} must be numeric in [0, {maximum}].")
        if "top_logprobs" in body and (
            type(body["top_logprobs"]) is not int or not 0 <= body["top_logprobs"] <= 20
        ):
            raise ValueError("DeepSeek Responses top_logprobs must be an integer in [0, 20].")

    if reference_source == "openai_gpt6_astra_responses":
        # Keep GPT-6's supported include and explicit rejection probes on the
        # wire; dropping a rejected field would make the test meaningless.
        for key in ("include", "temperature", "top_p", "top_logprobs"):
            if key in settings:
                body[key] = copy.deepcopy(settings[key])

    if "reasoning" in settings:
        reasoning = copy.deepcopy(settings["reasoning"])
        if not isinstance(reasoning, dict):
            raise ValueError("reasoning must be an object with effort/mode fields.")
        effort = reasoning.get("effort")
        if effort is not None:
            effort_raw = str(effort).lower()
            if effort_raw not in OPENAI_GPT5_REASONING_EFFORTS:
                raise ValueError(
                    "reasoning.effort must be one of: "
                    + ", ".join(sorted(OPENAI_GPT5_REASONING_EFFORTS))
                )
            reasoning["effort"] = effort_raw
        context = reasoning.get("context")
        if context is not None:
            context_raw = str(context).lower()
            if context_raw not in {"auto", "all_turns", "current_turn"}:
                raise ValueError(
                    "reasoning.context must be auto/all_turns/current_turn."
                )
            reasoning["context"] = context_raw
        mode = reasoning.get("mode")
        if mode is not None:
            mode_raw = str(mode).lower()
            if mode_raw != "pro":
                raise ValueError("reasoning.mode currently supports only pro.")
            if reasoning.get("effort") in {"none", "minimal", "low"}:
                raise ValueError(
                    "reasoning.mode=pro requires effort medium/high/xhigh/max."
                )
            reasoning["mode"] = mode_raw
        body["reasoning"] = reasoning

    if "text" in settings:
        text_config = copy.deepcopy(settings["text"])
        if not isinstance(text_config, dict) or not text_config:
            raise ValueError("text must be a non-empty object.")
        verbosity = text_config.get("verbosity")
        if verbosity is not None:
            verbosity_raw = str(verbosity).lower()
            if verbosity_raw not in {"low", "medium", "high"}:
                raise ValueError("text.verbosity must be low/medium/high.")
            text_config["verbosity"] = verbosity_raw
        body["text"] = text_config

    tools = None
    if "tools_fixture" in settings:
        tools = _read_json_fixture(settings["tools_fixture"])
    elif "tools" in settings:
        tools = settings["tools"]
    if tools is not None:
        body["tools"] = _openai_responses_tools(tools)

    if "max_output_tokens" in body and int(body["max_output_tokens"]) <= 0:
        raise ValueError("max_output_tokens must be positive.")
    prompt_cache_options = body.get("prompt_cache_options")
    if prompt_cache_options is not None:
        if not isinstance(prompt_cache_options, dict) or not prompt_cache_options:
            raise ValueError("prompt_cache_options must be a non-empty object.")
        if "mode" in prompt_cache_options:
            mode = str(prompt_cache_options.get("mode") or "").lower()
            if mode not in {"implicit", "explicit"}:
                raise ValueError("prompt_cache_options.mode must be implicit/explicit.")
            prompt_cache_options["mode"] = mode
        ttl = prompt_cache_options.get("ttl")
        if ttl is not None and (not isinstance(ttl, str) or not ttl.strip()):
            raise ValueError("prompt_cache_options.ttl must be a non-empty string.")
    if "prompt_cache_key" in body and (
        not isinstance(body["prompt_cache_key"], str)
        or not body["prompt_cache_key"].strip()
    ):
        raise ValueError("prompt_cache_key must be a non-empty string.")
    return body


def _openai_responses_tools(raw_tools: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_tools, list) or not raw_tools:
        raise ValueError("OpenAI Responses tools must be a non-empty list.")
    tools: list[dict[str, Any]] = []
    for tool in raw_tools:
        if not isinstance(tool, dict):
            raise ValueError("OpenAI Responses tools must contain objects.")
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            function = tool["function"]
            converted = {
                "type": "function",
                "name": str(function.get("name") or ""),
                "description": str(function.get("description") or ""),
                "parameters": copy.deepcopy(function.get("parameters") or {}),
            }
            if "strict" in function:
                if not isinstance(function["strict"], bool):
                    raise ValueError("OpenAI Responses function strict must be boolean.")
                converted["strict"] = function["strict"]
            tools.append(converted)
        elif tool.get("type") == "function" and tool.get("name"):
            tools.append(copy.deepcopy(tool))
        else:
            raise ValueError(
                "OpenAI Responses tools require type=function with name/parameters "
                "or OpenAI Chat Completions function fixture format."
            )
    if any(not tool.get("name") for tool in tools):
        raise ValueError("OpenAI Responses function tools require a name.")
    return tools


def _ensure_responses_input_minimum_prompt(
    config: dict[str, Any],
    items: list[Any],
) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            item["content"] = ensure_minimum_prompt_text(config, content)
            return
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    block["text"] = ensure_minimum_prompt_text(config, block["text"])
                    return


def ensure_minimum_prompt_text(config: dict[str, Any], prompt: str) -> str:
    """Pad short test prompts with deterministic, tokenizer-safe numeric tokens."""
    if config.get("_parameter_test_exact_input"):
        return str(prompt)
    minimum = _minimum_prompt_tokens(config)
    text = str(prompt)
    # Only trust unpadded text when the cheap estimate is four times the floor;
    # this avoids treating 100 CJK characters as 100 provider tokens.
    if (
        _PROMPT_PADDING_MARKER in text
        or _estimated_text_token_units(text) >= minimum * 4
    ):
        return text

    # Standalone five-digit values are deliberately separated by spaces.
    # Each value consumes at least one token across the supported tokenizers,
    # giving a conservative lower bound even when tokenization differs.
    padding = " ".join(str(10_000 + index) for index in range(minimum))
    return (
        f"{text}\n\n"
        f"{_PROMPT_PADDING_MARKER}"
        f"{padding}"
    )


def extract_message(response_json: dict[str, Any]) -> dict[str, Any]:
    choices = response_json.get("choices") or []
    if not choices:
        return {}
    return choices[0].get("message") or choices[0].get("delta") or {}


def extract_content(response_json: dict[str, Any]) -> str:
    message = extract_message(response_json)
    content = message.get("content")
    if isinstance(content, str):
        return content
    native_blocks = response_json.get("content")
    if isinstance(native_blocks, list):
        return "".join(
            str(block.get("text") or "")
            for block in native_blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
    candidates = response_json.get("candidates") or []
    if candidates and isinstance(candidates[0], dict):
        native_content = candidates[0].get("content") or {}
        parts = native_content.get("parts") or []
        return "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))
    responses_text = extract_openai_responses_text(response_json)
    if responses_text:
        return responses_text
    interactions_text = extract_gemini_interactions_text(response_json)
    if interactions_text:
        return interactions_text
    return ""


def extract_openai_responses_text(response_json: dict[str, Any]) -> str:
    texts: list[str] = []
    for item in response_json.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {"output_text", "text"} and isinstance(block.get("text"), str):
                texts.append(block["text"])
    return "".join(texts)


def extract_openai_responses_function_calls(response_json: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in response_json.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "function_call":
            calls.append(item)
    return calls


def extract_gemini_interactions_text(response_json: dict[str, Any]) -> str:
    texts: list[str] = []
    for step in response_json.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        for block in step.get("content") or []:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                texts.append(block["text"])
    return "".join(texts)


def extract_gemini_interactions_function_calls(
    response_json: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        step
        for step in response_json.get("steps") or []
        if isinstance(step, dict) and step.get("type") == "function_call"
    ]


def extract_gemini_interactions_thought_summaries(
    response_json: dict[str, Any],
) -> list[str]:
    summaries: list[str] = []
    for step in response_json.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "thought":
            continue
        raw_summary = step.get("summary") or []
        if isinstance(raw_summary, dict):
            raw_summary = [raw_summary]
        if isinstance(raw_summary, str):
            raw_summary = [{"type": "text", "text": raw_summary}]
        for block in raw_summary if isinstance(raw_summary, list) else []:
            if isinstance(block, dict):
                content = block.get("content")
                if isinstance(content, dict):
                    block = content
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    summaries.append(text)
    return summaries


def extract_reasoning_content(response_json: dict[str, Any]) -> str:
    message = extract_message(response_json)
    content = message.get("reasoning_content")
    if isinstance(content, str):
        return content
    return ""


def extract_tool_calls(response_json: dict[str, Any]) -> list[dict[str, Any]]:
    message = extract_message(response_json)
    tool_calls = message.get("tool_calls") or []
    return tool_calls if isinstance(tool_calls, list) else []


def extract_native_function_calls(response_json: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = response_json.get("candidates") or []
    if not candidates or not isinstance(candidates[0], dict):
        return []
    content = candidates[0].get("content") or {}
    parts = content.get("parts") if isinstance(content, dict) else []
    calls: list[dict[str, Any]] = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        call = part.get("functionCall")
        if isinstance(call, dict):
            calls.append(call)
    return calls


def extract_claude_tool_uses(response_json: dict[str, Any]) -> list[dict[str, Any]]:
    content = response_json.get("content") or []
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]


def extract_finish_reason(response_json: dict[str, Any]) -> str | None:
    stop_reason = response_json.get("stop_reason")
    if stop_reason is not None:
        return str(stop_reason)
    choices = response_json.get("choices") or []
    if not choices:
        return None
    reason = choices[0].get("finish_reason")
    return str(reason) if reason is not None else None


def extract_usage(response_json: dict[str, Any]) -> dict[str, Any]:
    usage = response_json.get("usage") or {}
    return usage if isinstance(usage, dict) else {}


def cache_tokens_from_usage(usage: dict[str, Any]) -> tuple[int | None, int | None]:
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    if hit is None and miss is None:
        direct_cached = _optional_int(usage.get("cached_tokens"))
        direct_prompt = _optional_int(usage.get("prompt_tokens"))
        if direct_cached is not None and direct_prompt is not None:
            return direct_cached, direct_prompt - direct_cached
        cache_read = _optional_int(usage.get("cache_read_input_tokens"))
        cache_creation = _optional_int(usage.get("cache_creation_input_tokens"))
        input_tokens = _optional_int(usage.get("input_tokens"))
        if cache_read is not None or cache_creation is not None:
            uncached = (input_tokens or 0) + (cache_creation or 0)
            return cache_read or 0, uncached
        cached_content = _optional_int(usage.get("cachedContentTokenCount"))
        if cached_content is None:
            cached_content = _optional_int(usage.get("cached_content_token_count"))
        prompt_tokens = _optional_int(usage.get("promptTokenCount"))
        if prompt_tokens is None:
            prompt_tokens = _optional_int(usage.get("prompt_token_count"))
        if cached_content is not None:
            return cached_content, max((prompt_tokens or 0) - cached_content, 0)
        details = usage.get("prompt_tokens_details")
        if not isinstance(details, dict):
            details = usage.get("input_tokens_details")
        if isinstance(details, dict):
            cached_tokens = _optional_int(details.get("cached_tokens"))
            prompt_tokens = _optional_int(usage.get("prompt_tokens"))
            if prompt_tokens is None:
                prompt_tokens = _optional_int(usage.get("input_tokens"))
            if cached_tokens is not None and prompt_tokens is not None:
                return cached_tokens, max(prompt_tokens - cached_tokens, 0)
    return _optional_int(hit), _optional_int(miss)


def prompt_tokens_from_usage(usage: dict[str, Any]) -> int | None:
    prompt_tokens = _optional_int(usage.get("prompt_tokens"))
    if prompt_tokens is not None:
        return prompt_tokens
    input_tokens = _optional_int(usage.get("input_tokens"))
    if input_tokens is not None:
        cache_creation = _optional_int(usage.get("cache_creation_input_tokens")) or 0
        cache_read = _optional_int(usage.get("cache_read_input_tokens")) or 0
        return input_tokens + cache_creation + cache_read
    prompt_tokens = _optional_int(usage.get("promptTokenCount"))
    if prompt_tokens is not None:
        return prompt_tokens
    return _optional_int(usage.get("prompt_token_count"))


def completion_tokens_from_usage(usage: dict[str, Any]) -> int | None:
    completion_tokens = _optional_int(usage.get("completion_tokens"))
    if completion_tokens is not None:
        return completion_tokens
    candidates_tokens = _optional_int(usage.get("candidatesTokenCount"))
    if candidates_tokens is None:
        candidates_tokens = _optional_int(usage.get("candidates_token_count"))
    thoughts_tokens = _optional_int(usage.get("thoughtsTokenCount"))
    if thoughts_tokens is None:
        thoughts_tokens = _optional_int(usage.get("thoughts_token_count"))
    if candidates_tokens is not None or thoughts_tokens is not None:
        return (candidates_tokens or 0) + (thoughts_tokens or 0)
    return _optional_int(usage.get("output_tokens"))


def total_tokens_from_usage(usage: dict[str, Any]) -> int | None:
    prompt_tokens = prompt_tokens_from_usage(usage)
    completion_tokens = completion_tokens_from_usage(usage)
    if prompt_tokens is not None and completion_tokens is not None:
        # prompt + completion is the primary accounting basis. In particular,
        # completion already includes reasoning for OpenAI-compatible APIs.
        return prompt_tokens + completion_tokens
    total_tokens = _optional_int(usage.get("total_tokens"))
    if total_tokens is None:
        total_tokens = _optional_int(usage.get("totalTokenCount"))
    if total_tokens is None:
        total_tokens = _optional_int(usage.get("total_token_count"))
    if total_tokens is not None:
        return total_tokens
    if prompt_tokens is None and completion_tokens is None:
        return None
    return (prompt_tokens or 0) + (completion_tokens or 0)


def _resolve_profile_extends(
    profiles: dict[str, Any],
    profile: str,
    stack: tuple[str, ...],
) -> dict[str, Any]:
    if profile in stack:
        raise ValueError(f"Profile inheritance cycle: {' -> '.join(stack + (profile,))}")

    current = copy.deepcopy(profiles.get(profile) or {})
    parent = current.pop("extends", None)
    if not parent:
        return current
    if parent not in profiles:
        raise KeyError(f"Profile {profile} extends unknown profile {parent}")
    base = _resolve_profile_extends(profiles, str(parent), stack + (profile,))
    return deep_merge(base, current)


def _build_messages(config: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, Any]]:
    if "messages" in settings:
        messages = copy.deepcopy(settings["messages"])
        _validate_messages(messages)
        _ensure_messages_minimum_prompt(config, messages)
        return messages

    prompt = _resolve_prompt(config, settings)
    messages = [{"role": "user", "content": prompt}]
    if not config.get("_parameter_test_exact_input"):
        messages.insert(0, {
            "role": "system",
            "content": "You are a concise assistant for API compatibility and load testing.",
        })
    _validate_messages(messages)
    return messages


def _build_gemini_interactions_body(
    config: dict[str, Any],
    settings: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    """Build the stateless, text-only Gemini Interactions v1 request shape."""
    if settings.get("store") is True:
        raise ValueError(
            "Gemini Interactions safe text profiles do not implement store=true."
        )
    if settings.get("previous_interaction_id") is not None:
        raise ValueError(
            "Gemini Interactions previous_interaction_id is outside the stateless text runner."
        )
    if settings.get("background") is not None:
        raise ValueError(
            "Gemini Interactions background execution is outside the synchronous text runner."
        )
    if settings.get("safety_settings") is not None:
        raise ValueError(
            "Gemini Interactions safety_settings is not enabled for the model-specific safe matrix."
        )

    body: dict[str, Any] = {
        "model": model,
        "input": _resolve_prompt(config, settings),
        # Never create retained server-side state from the parameter runner.
        "store": False,
    }

    store = settings.get("interaction_store")
    if store is not None:
        if store is not False:
            raise ValueError(
                "Gemini Interactions safe text profiles require interaction_store=false."
            )
        body["store"] = False

    stream = settings.get("interaction_stream")
    if stream is not None:
        if not isinstance(stream, bool):
            raise ValueError("interaction_stream must be a boolean.")
        body["stream"] = stream

    system_instruction = settings.get("interaction_system_instruction")
    if system_instruction is not None:
        if not isinstance(system_instruction, str) or not system_instruction.strip():
            raise ValueError("interaction_system_instruction must be non-empty text.")
        body["system_instruction"] = system_instruction

    generation_config = settings.get("interaction_generation_config")
    if generation_config is not None:
        if not isinstance(generation_config, dict) or not generation_config:
            raise ValueError("interaction_generation_config must be a non-empty object.")
        generation = copy.deepcopy(generation_config)
        allowed_generation_keys = {
            "max_output_tokens",
            "seed",
            "stop_sequences",
            "thinking_level",
            "thinking_summaries",
            "tool_choice",
        }
        unknown = sorted(set(generation) - allowed_generation_keys)
        if unknown:
            raise ValueError(
                "Unsupported Gemini Interactions generation_config fields: "
                + ", ".join(unknown)
            )
        max_output_tokens = generation.get("max_output_tokens")
        if max_output_tokens is not None and (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or max_output_tokens <= 0
        ):
            raise ValueError("generation_config.max_output_tokens must be a positive integer.")
        seed = generation.get("seed")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise ValueError("generation_config.seed must be an integer.")
        stop_sequences = generation.get("stop_sequences")
        if stop_sequences is not None and (
            not isinstance(stop_sequences, list)
            or not stop_sequences
            or not all(isinstance(item, str) and item for item in stop_sequences)
        ):
            raise ValueError(
                "generation_config.stop_sequences must be a non-empty string list."
            )
        thinking_level = generation.get("thinking_level")
        if thinking_level is not None and thinking_level not in {
            "minimal",
            "low",
            "medium",
            "high",
        }:
            raise ValueError(
                "generation_config.thinking_level must be minimal/low/medium/high."
            )
        thinking_summaries = generation.get("thinking_summaries")
        if thinking_summaries is not None and thinking_summaries not in {"auto", "none"}:
            raise ValueError(
                "generation_config.thinking_summaries must be auto or none."
            )
        tool_choice = generation.get("tool_choice")
        if tool_choice is not None:
            _validate_gemini_interactions_tool_choice(tool_choice)
        body["generation_config"] = generation

    tools = settings.get("interaction_tools")
    if tools is not None:
        if not isinstance(tools, list) or not tools:
            raise ValueError("interaction_tools must be a non-empty list.")
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                raise ValueError(
                    "Gemini Interactions safe text profiles permit function tools only."
                )
            if not isinstance(tool.get("name"), str) or not tool["name"].strip():
                raise ValueError("Gemini Interactions function tools require a name.")
            parameters = tool.get("parameters")
            if not isinstance(parameters, dict) or not parameters:
                raise ValueError(
                    "Gemini Interactions function tools require a JSON Schema parameters object."
                )
        body["tools"] = copy.deepcopy(tools)

    response_format = settings.get("interaction_response_format")
    if response_format is not None:
        if not isinstance(response_format, dict) or not response_format:
            raise ValueError("interaction_response_format must be a non-empty object.")
        if response_format.get("type") != "text":
            raise ValueError(
                "Gemini Interactions text runner only permits response_format.type=text."
            )
        if response_format.get("mime_type") not in {"text/plain", "application/json"}:
            raise ValueError(
                "Gemini Interactions text response_format mime_type is unsupported."
            )
        schema = response_format.get("schema")
        if schema is not None and not isinstance(schema, dict):
            raise ValueError("interaction_response_format.schema must be an object.")
        body["response_format"] = copy.deepcopy(response_format)

    labels = settings.get("interaction_labels")
    if labels is not None:
        if not isinstance(labels, dict) or not labels:
            raise ValueError("interaction_labels must be a non-empty object.")
        if not all(
            isinstance(key, str)
            and key.strip()
            and isinstance(value, str)
            and value.strip()
            for key, value in labels.items()
        ):
            raise ValueError("interaction_labels keys and values must be non-empty strings.")
        body["labels"] = copy.deepcopy(labels)

    generation = body.get("generation_config")
    if (
        isinstance(generation, dict)
        and generation.get("tool_choice") is not None
        and not body.get("tools")
    ):
        raise ValueError(
            "Gemini Interactions generation_config.tool_choice requires tools."
        )

    return body


def _validate_gemini_interactions_tool_choice(value: Any) -> None:
    if isinstance(value, str):
        if value not in {"auto", "any", "none", "validated"}:
            raise ValueError(
                "generation_config.tool_choice must be auto/any/none/validated."
            )
        return
    if not isinstance(value, dict) or set(value) != {"allowed_tools"}:
        raise ValueError(
            "generation_config.tool_choice must be a mode string or allowed_tools object."
        )
    allowed = value.get("allowed_tools")
    if not isinstance(allowed, dict):
        raise ValueError("generation_config.tool_choice.allowed_tools must be an object.")
    if allowed.get("mode") not in {"auto", "any", "none", "validated"}:
        raise ValueError(
            "generation_config.tool_choice.allowed_tools.mode is invalid."
        )
    names = allowed.get("tools")
    if names is not None and (
        not isinstance(names, list)
        or not names
        or not all(isinstance(name, str) and name for name in names)
    ):
        raise ValueError(
            "generation_config.tool_choice.allowed_tools.tools must be a non-empty string list."
        )


def _build_gemini_native_body(config: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": _resolve_prompt(config, settings)}],
            }
        ]
    }
    generation_config = settings.get("native_generation_config")
    if generation_config is not None:
        if not isinstance(generation_config, dict) or not generation_config:
            raise ValueError("native_generation_config must be a non-empty object.")
        body["generationConfig"] = copy.deepcopy(generation_config)
    safety_settings = settings.get("native_safety_settings")
    if safety_settings is not None:
        if not isinstance(safety_settings, list) or not safety_settings:
            raise ValueError("native_safety_settings must be a non-empty list.")
        body["safetySettings"] = copy.deepcopy(safety_settings)
    cached_content = settings.get("native_cached_content")
    if cached_content is not None:
        if not isinstance(cached_content, str) or not cached_content:
            raise ValueError("native_cached_content must be a non-empty resource name.")
        body["cachedContent"] = cached_content
    tools = settings.get("native_tools")
    if tools is not None:
        if not isinstance(tools, list) or not tools:
            raise ValueError("native_tools must be a non-empty list.")
        body["tools"] = copy.deepcopy(tools)
    tool_config = settings.get("native_tool_config")
    if tool_config is not None:
        if not isinstance(tool_config, dict) or not tool_config:
            raise ValueError("native_tool_config must be a non-empty object.")
        body["toolConfig"] = copy.deepcopy(tool_config)
    system_instruction = settings.get("native_system_instruction")
    if system_instruction is not None:
        if isinstance(system_instruction, str) and system_instruction:
            body["systemInstruction"] = {"parts": [{"text": system_instruction}]}
        elif isinstance(system_instruction, dict) and system_instruction:
            body["systemInstruction"] = copy.deepcopy(system_instruction)
        else:
            raise ValueError("native_system_instruction must be text or a non-empty object.")
    labels = settings.get("native_labels")
    if labels is not None:
        if not isinstance(labels, dict) or not labels:
            raise ValueError("native_labels must be a non-empty object of string key/value pairs.")
        normalized_labels: dict[str, str] = {}
        for key, value in labels.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("native_labels keys must be non-empty strings.")
            if not isinstance(value, str) or not value.strip():
                raise ValueError("native_labels values must be non-empty strings.")
            normalized_labels[key] = value
        body["labels"] = normalized_labels
    service_tier = settings.get("native_service_tier")
    if service_tier is not None:
        if str(service_tier) not in {"unspecified", "standard", "flex", "priority"}:
            raise ValueError("native_service_tier must be unspecified/standard/flex/priority.")
        body["serviceTier"] = str(service_tier)
    store = settings.get("native_store")
    if store is not None:
        if not isinstance(store, bool):
            raise ValueError("native_store must be a boolean.")
        body["store"] = store
    return body


def _request_headers_from_settings(settings: dict[str, Any]) -> dict[str, str]:
    request_headers = settings.get("request_headers")
    if request_headers is None:
        return {}
    if not isinstance(request_headers, dict) or not request_headers:
        raise ValueError("request_headers must be a non-empty object of string key/value pairs.")
    return validate_profile_request_headers(request_headers)


def _build_fim_completion_body(
    config: dict[str, Any],
    settings: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    preserve_rejected_params = bool(settings.get("preserve_rejected_params", False))
    if "prompt" in settings:
        prompt = settings["prompt"]
    else:
        prompt = _resolve_prompt(config, settings)
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("DeepSeek FIM prompt must be non-empty text.")

    body: dict[str, Any] = {"model": model, "prompt": prompt}
    for key in (
        "suffix",
        "echo",
        "logprobs",
        "max_tokens",
        "stop",
        "stream",
        "stream_options",
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
    ):
        if key in settings:
            body[key] = copy.deepcopy(settings[key])

    suffix = body.get("suffix")
    if suffix is not None and not isinstance(suffix, str):
        raise ValueError("DeepSeek FIM suffix must be text.")
    if "echo" in body and not isinstance(body["echo"], bool):
        raise ValueError("DeepSeek FIM echo must be a boolean.")
    if "logprobs" in body:
        logprobs = body["logprobs"]
        if isinstance(logprobs, bool) or not isinstance(logprobs, int):
            raise ValueError("DeepSeek FIM logprobs must be an integer.")
        if not preserve_rejected_params and not 0 <= logprobs <= 20:
            raise ValueError("DeepSeek FIM logprobs must be an integer in [0, 20].")
    if "max_tokens" in body:
        max_tokens = body["max_tokens"]
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens <= 0
        ):
            raise ValueError("DeepSeek FIM max_tokens must be a positive integer.")
    if "stream" in body and not isinstance(body["stream"], bool):
        raise ValueError("DeepSeek FIM stream must be a boolean.")
    if "stream_options" in body:
        stream_options = body["stream_options"]
        if body.get("stream") is not True:
            raise ValueError("DeepSeek FIM stream_options requires stream=true.")
        if not isinstance(stream_options, dict):
            raise ValueError("DeepSeek FIM stream_options must be an object.")
        if "include_usage" in stream_options and not isinstance(
            stream_options["include_usage"], bool
        ):
            raise ValueError(
                "DeepSeek FIM stream_options.include_usage must be a boolean."
            )
    if "stop" in body:
        stop = body["stop"]
        stop_values = stop if isinstance(stop, list) else [stop]
        if not all(isinstance(item, str) for item in stop_values):
            raise ValueError("DeepSeek FIM stop values must be strings.")
        if not preserve_rejected_params and len(stop_values) > 16:
            raise ValueError(
                "DeepSeek FIM stop must be a string or a list of up to 16 strings."
            )
    for key, maximum in (("temperature", 2), ("top_p", 1)):
        if key not in body:
            continue
        value = body[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"DeepSeek FIM {key} must be numeric.")
        if not math.isfinite(float(value)):
            raise ValueError(f"DeepSeek FIM {key} must be finite.")
        if not preserve_rejected_params and not 0 <= float(value) <= maximum:
            raise ValueError(f"DeepSeek FIM {key} must be in [0, {maximum}].")
    for key in ("frequency_penalty", "presence_penalty"):
        if key not in body:
            continue
        value = body[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"DeepSeek FIM {key} must be numeric when provided.")
        if not math.isfinite(float(value)):
            raise ValueError(f"DeepSeek FIM {key} must be finite.")
    return body


def _build_deepseek_anthropic_messages_body(
    config: dict[str, Any],
    settings: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    return _build_claude_messages_body(
        config,
        settings,
        model,
        validation_family="deepseek",
    )


def _build_claude_messages_body(
    config: dict[str, Any],
    settings: dict[str, Any],
    model: str,
    *,
    validation_family: str = "claude",
    reference_source: str | None = None,
    runtime_route_profile: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": int(settings.get("max_tokens") or 128),
        "messages": _build_claude_messages(config, settings),
    }
    system = settings.get(
        "system",
        None if config.get("_parameter_test_exact_input") else
        "You are a concise assistant for API compatibility and load testing.",
    )
    if isinstance(system, str) and system.strip():
        body["system"] = system
    elif isinstance(system, list) and system:
        body["system"] = copy.deepcopy(system)

    if "cache_control" in settings:
        body["cache_control"] = _claude_native_cache_control(
            config, settings, model, reference_source=reference_source,
            validation_family=validation_family, route_profile=runtime_route_profile,
        )

    for key in (
        "stream",
        "temperature",
        "top_p",
        "top_k",
        "metadata",
        "container",
        "mcp_servers",
        "service_tier",
    ):
        if key in settings:
            body[key] = copy.deepcopy(settings[key])

    stop_sequences = settings.get("stop_sequences")
    if stop_sequences is not None:
        body["stop_sequences"] = copy.deepcopy(stop_sequences)

    thinking = settings.get("thinking")
    if thinking is not None:
        body["thinking"] = copy.deepcopy(thinking)

    output_config = settings.get("output_config")
    if output_config is not None:
        body["output_config"] = copy.deepcopy(output_config)

    tools = None
    if "tools_fixture" in settings:
        tools = _read_json_fixture(settings["tools_fixture"])
    elif "tools" in settings:
        tools = settings["tools"]
    if tools is not None:
        body["tools"] = _claude_native_tools(tools)

    if "tool_choice" in settings:
        body["tool_choice"] = _claude_native_tool_choice(settings["tool_choice"])

    _validate_claude_messages_body(
        body,
        family=validation_family,
        preserve_rejected_params=bool(
            settings.get("preserve_rejected_params", False)
        ),
    )
    return body


def _claude_native_cache_control(
    config: dict[str, Any], settings: dict[str, Any], model: str, *,
    reference_source: str | None, validation_family: str,
    route_profile: str | None,
) -> Any:
    """Keep explicit automatic caching only within its installed native source.

    https://platform.claude.com/docs/en/build-with-claude/prompt-caching
    The documented top-level field selects the last cacheable input block.
    This builder adds no default and makes no cache-hit or TTL-effect claim.
    """
    from .model_profile_catalog import resolve_runtime_parameter_config
    from .reference_specs import default_reference_source_for_model

    provider = get_active_provider_name(config)
    runtime_route = route_profile or get_model_route_profile(config, model, provider)
    if validation_family != "claude":
        raise ValueError("cache_control requires an Anthropic Messages reference binding.")
    try:
        get_model_api_form(config, model, provider, route_profile=runtime_route,
                           api_form="anthropic_messages")
        contract_id = reference_source or default_reference_source_for_model(
            config, "claude", model, provider, api_form="anthropic_messages", route_profile=runtime_route
        )
        selected = resolve_runtime_parameter_config(
            config, provider, model, "claude", runtime_route, "anthropic_messages", contract_id=contract_id
        )
    except (KeyError, ValueError, RuntimeError) as exc:
        raise ValueError("cache_control requires a valid runtime-to-official parameter binding.") from exc
    if contract_id != "claude_native_messages":
        raise ValueError("cache_control requires the supported claude_native_messages Contract.")
    contract, profile, interface = selected["contract"], selected["profile"], selected["interface"]
    # An adapter's endpoint, route and wire model belong to execution_target.
    # Their strict mapping supplies the official reference identity; requiring
    # an origin-vendor endpoint here would break legitimate comparisons.
    target = selected["execution_target"]
    if (selected.get("source_id") != "anthropic" or selected.get("contract_id") != contract_id
            or any(target.get(k) != v for k, v in {"provider_id": provider, "request_model_id": model,
                       "route_profile": runtime_route, "api_form": "anthropic_messages"}.items())
            or profile.get("source_id") != "anthropic" or profile.get("lifecycle") != "active"
            or contract.get("source_id") != "anthropic" or contract.get("source_ids") != ["anthropic"]
            or contract.get("family_id") != "claude" or contract.get("api_form") != "anthropic_messages"
            or contract.get("routing_mode") != "vendor_direct"):
        raise ValueError("cache_control Contract identity differs from the native Anthropic source.")
    capability = interface.get("parameter_capabilities", {}).get(
        "cache_control", contract.get("parameter_capabilities", {}).get("cache_control", {})
    )
    if (interface.get("source_id") != "anthropic" or interface.get("api_form") != "anthropic_messages"
            or interface.get("routing_mode") != "vendor_direct" or contract_id not in interface.get("contract_ids", [])
            or interface.get("enabled") is not True or interface.get("executable") is not True
            or not isinstance(capability, dict) or capability.get("state") != "supported"):
        raise ValueError("cache_control is not supported by the exact native Interface.")
    constraint = deep_merge(contract.get("parameter_constraints", {}).get("cache_control", {}),
                            interface.get("parameter_constraints", {}).get("cache_control", {}))
    value = settings["cache_control"]
    if not settings.get("preserve_rejected_params", False):
        properties = constraint.get("properties", {})
        valid = (constraint.get("type") == "object" and isinstance(value, dict)
                 and set(constraint.get("required", [])) <= set(value)
                 and (constraint.get("additionalProperties") is not False or set(value) <= set(properties))
                 and all(key not in properties or "enum" not in properties[key]
                         or value[key] in properties[key]["enum"] for key in value))
        if not valid:
            raise ValueError("cache_control does not match the installed native Contract; explicit negative probes require preserve_rejected_params.")
    return copy.deepcopy(value)


def _build_claude_messages(config: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, Any]]:
    if "messages" in settings:
        messages = copy.deepcopy(settings["messages"])
        _validate_messages(messages)
        native_messages = [
            message
            for message in messages
            if message.get("role") in {"user", "assistant"}
        ]
        _ensure_messages_minimum_prompt(config, native_messages)
        return native_messages

    return [
        {
            "role": "user",
            "content": _resolve_prompt(config, settings),
        }
    ]


def _claude_native_tools(raw_tools: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_tools, list) or not raw_tools:
        raise ValueError("Claude native tools must be a non-empty list.")
    tools: list[dict[str, Any]] = []
    for tool in raw_tools:
        if not isinstance(tool, dict):
            raise ValueError("Claude native tools must contain objects.")
        if tool.get("type") == "function":
            function = tool.get("function")
            if not isinstance(function, dict):
                raise ValueError("OpenAI function tool fixture is malformed.")
            tools.append(
                {
                    "name": str(function.get("name") or ""),
                    "description": str(function.get("description") or ""),
                    "input_schema": copy.deepcopy(function.get("parameters") or {}),
                }
            )
        elif "input_schema" in tool:
            tools.append(copy.deepcopy(tool))
        else:
            raise ValueError("Claude native tools require input_schema or OpenAI function fixture format.")
    if any(not tool.get("name") or not isinstance(tool.get("input_schema"), dict) for tool in tools):
        raise ValueError("Claude native tools require name and input_schema.")
    return tools


def _claude_native_tool_choice(raw_choice: Any) -> dict[str, Any]:
    if isinstance(raw_choice, dict):
        return copy.deepcopy(raw_choice)
    choice = str(raw_choice)
    if choice in {"auto", "any", "none"}:
        return {"type": choice}
    if choice == "required":
        return {"type": "any"}
    return {"type": "tool", "name": choice}


def _validate_claude_messages_body(
    body: dict[str, Any],
    *,
    family: str = "claude",
    preserve_rejected_params: bool = False,
) -> None:
    if not body.get("model"):
        raise ValueError("Claude Messages body requires model.")
    if int(body.get("max_tokens") or 0) <= 0:
        raise ValueError("Claude Messages max_tokens must be positive.")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("Claude Messages body requires non-empty messages.")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            raise ValueError("Claude Messages messages must use user/assistant roles.")
        if "content" not in message:
            raise ValueError("Claude Messages messages require content.")
    if "system" in body and not isinstance(body["system"], (str, list)):
        raise ValueError("Claude Messages system must be text or a block list.")
    if "stream" in body and not isinstance(body["stream"], bool):
        raise ValueError("Claude Messages stream must be a boolean.")
    temperature_max = 2 if family == "deepseek" else 1
    if family == "deepseek" and "temperature" in body and (
        isinstance(body["temperature"], bool)
        or not isinstance(body["temperature"], (int, float))
    ):
        raise ValueError("DeepSeek Anthropic Messages temperature must be numeric.")
    if (
        "temperature" in body
        and not (family == "deepseek" and preserve_rejected_params)
        and not 0 <= float(body["temperature"]) <= temperature_max
    ):
        raise ValueError(
            f"{'DeepSeek Anthropic' if family == 'deepseek' else 'Claude'} "
            f"Messages temperature must be in [0, {temperature_max}]."
        )
    if family == "deepseek" and "top_p" in body and (
        isinstance(body["top_p"], bool)
        or not isinstance(body["top_p"], (int, float))
    ):
        raise ValueError("DeepSeek Anthropic Messages top_p must be numeric.")
    if (
        "top_p" in body
        and not (family == "deepseek" and preserve_rejected_params)
        and not 0 <= float(body["top_p"]) <= 1
    ):
        raise ValueError("Claude Messages top_p must be in [0, 1].")
    if "top_k" in body and int(body["top_k"]) < 0:
        raise ValueError("Claude Messages top_k must be non-negative.")
    if "metadata" in body and not isinstance(body["metadata"], dict):
        raise ValueError("Claude Messages metadata must be an object.")
    if "stop_sequences" in body:
        stops = body["stop_sequences"]
        if not isinstance(stops, list) or not all(isinstance(item, str) for item in stops):
            raise ValueError("Claude Messages stop_sequences must be a string list.")
    if "tools" in body and not isinstance(body["tools"], list):
        raise ValueError("Claude Messages tools must be a list.")
    if "tool_choice" in body and not isinstance(body["tool_choice"], dict):
        raise ValueError("Claude Messages tool_choice must be an object.")
    thinking = body.get("thinking")
    if thinking is not None:
        allowed_types = (
            ("enabled", "disabled")
            if family == "deepseek"
            else ("enabled", "disabled", "adaptive")
        )
        if not isinstance(thinking, dict):
            label = "DeepSeek Anthropic" if family == "deepseek" else "Claude"
            raise ValueError(f"{label} Messages thinking must be an object.")
        if (
            thinking.get("type") not in allowed_types
            and not (family == "deepseek" and preserve_rejected_params)
        ):
            allowed = " / ".join(allowed_types)
            label = "DeepSeek Anthropic" if family == "deepseek" else "Claude"
            raise ValueError(
                f"{label} Messages thinking must be {{'type': '{allowed}'}}."
            )
        if "budget_tokens" in thinking and int(thinking["budget_tokens"]) <= 0:
            raise ValueError("Claude Messages thinking.budget_tokens must be positive.")
    output_config = body.get("output_config")
    if output_config is not None:
        if not isinstance(output_config, dict) or not output_config:
            raise ValueError("Claude Messages output_config must be a non-empty object.")
        if "effort" in output_config:
            effort = str(output_config.get("effort") or "").lower()
            allowed_efforts = (
                {"high", "max"}
                if family == "deepseek"
                else {"low", "medium", "high", "xhigh", "max"}
            )
            if effort not in allowed_efforts and not (
                family == "deepseek" and preserve_rejected_params
            ):
                allowed = "/".join(sorted(allowed_efforts))
                raise ValueError(
                    f"{'DeepSeek Anthropic' if family == 'deepseek' else 'Claude'} "
                    f"Messages output_config.effort must be {allowed}."
                )


def _resolve_prompt(config: dict[str, Any], settings: dict[str, Any]) -> str:
    if "prompt" in settings:
        prompt = str(settings["prompt"])
    elif "prompt_fixture" in settings:
        prompt = _read_text_fixture(settings["prompt_fixture"])
    elif "fixture" in settings:
        text = _read_text_fixture(settings["fixture"])
        if "fixture_chars" in settings and "fixture_repeat_to_chars" in settings:
            raise ValueError(
                "fixture_chars and fixture_repeat_to_chars are mutually exclusive."
            )
        if "fixture_repeat_to_chars" in settings:
            try:
                fixture_repeat_to_chars = int(settings["fixture_repeat_to_chars"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "fixture_repeat_to_chars must be a positive integer."
                ) from exc
            if fixture_repeat_to_chars <= 0:
                raise ValueError(
                    "fixture_repeat_to_chars must be a positive integer."
                )
            if not text:
                raise ValueError("Cannot repeat an empty fixture.")
            repeat_unit = f"{text}\n"
            repeat_count = (
                fixture_repeat_to_chars + len(repeat_unit) - 1
            ) // len(repeat_unit)
            text = (repeat_unit * repeat_count)[:fixture_repeat_to_chars]
        elif "fixture_chars" in settings:
            try:
                fixture_chars = int(settings["fixture_chars"])
            except (TypeError, ValueError) as exc:
                raise ValueError("fixture_chars must be a positive integer.") from exc
            if fixture_chars <= 0:
                raise ValueError("fixture_chars must be a positive integer.")
            text = text[:fixture_chars]
        prompt = f"{text}\n\n问题：请用一句话总结以上内容。"
    elif "tools_fixture" in settings:
        prompt = "请调用 get_weather 查询北京当前天气，单位使用 celsius。"
    else:
        prompt_key = settings.get("prompt_key", "medium")
        prompts = config.get("prompts") or {}
        if prompt_key not in prompts:
            raise KeyError(f"Prompt key {prompt_key!r} not found in config.prompts")
        prompt = str(prompts[prompt_key])
    if settings.get("skip_minimum_prompt"):
        return str(prompt)
    return ensure_minimum_prompt_text(config, prompt)


def _ensure_messages_minimum_prompt(
    config: dict[str, Any],
    messages: list[dict[str, Any]],
) -> None:
    if config.get("_parameter_test_exact_input"):
        return
    user_messages = [
        message
        for message in messages
        if message.get("role") == "user" and isinstance(message.get("content"), str)
    ]
    if not user_messages:
        return
    combined = "\n".join(str(message["content"]) for message in user_messages)
    minimum = _minimum_prompt_tokens(config)
    if (
        _PROMPT_PADDING_MARKER in combined
        or _estimated_text_token_units(combined) >= minimum * 4
    ):
        return
    user_messages[-1]["content"] = ensure_minimum_prompt_text(
        config,
        str(user_messages[-1]["content"]),
    )


def _minimum_prompt_tokens(config: dict[str, Any]) -> int:
    raw = (config.get("test_cases") or {}).get(
        "minimum_prompt_tokens",
        DEFAULT_MINIMUM_PROMPT_TOKENS,
    )
    try:
        minimum = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("test_cases.minimum_prompt_tokens must be a positive integer.") from exc
    if minimum <= 0:
        raise ValueError("test_cases.minimum_prompt_tokens must be a positive integer.")
    return minimum


def _estimated_text_token_units(text: str) -> float:
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    return (ascii_chars / 4.0) + (len(text) - ascii_chars)


def _prompt_source(settings: dict[str, Any]) -> str:
    if "messages" in settings:
        return "messages:inline"
    for key in ("prompt", "prompt_fixture", "fixture", "prompt_key", "tools_fixture"):
        if key in settings:
            return f"{key}:{settings[key]}"
    return "prompt_key:medium"


def profile_unsupported_params(config: dict[str, Any], group: str, profile: str, family: str) -> list[str]:
    settings = resolve_profile(config, group, profile)
    supported = _supported_params_for_family(family)
    unsupported: list[str] = []
    for key in settings:
        if key in PROFILE_KEYS or key in ("model", "messages") or key in DEPRECATED_PARAMS:
            continue
        if key in SUPPORTED_PARAMS and key not in supported:
            unsupported.append(key)
    return unsupported


def _supported_params_for_family(
    family: str,
    route_profile: str | None = None,
) -> set[str]:
    if route_profile == "aliyun_maas":
        return ALIYUN_OPENAI_COMPATIBLE_PARAMS
    if family not in SUPPORTED_PARAMS_BY_FAMILY:
        raise ValueError(f"Unknown model family {family!r}; no parameter policy is registered.")
    return SUPPORTED_PARAMS_BY_FAMILY[family]


def _family_param_config(config: dict[str, Any], family: str) -> dict[str, Any]:
    if family == "deepseek":
        return config.get("deepseek_params") or {}
    return config.get(f"{family}_params") or {}


def _normalize_body(
    body: dict[str, Any],
    family: str,
    *,
    preserve_rejected_params: bool = False,
    preserve_fable_parameter_body: bool = False,
    preserve_claude_parameter_body: bool = False,
    fable_official_compat_profile: bool = False,
) -> None:
    if "reasoning_effort" in body:
        raw = str(body["reasoning_effort"]).lower()
        if family in {"gpt", "kimi", "minimax"}:
            # OpenAI-wire families keep their own explicit effort values.
            # Do not remap through DeepSeek's high/max aliases.
            if raw not in OPENAI_GPT5_REASONING_EFFORTS:
                raise ValueError(
                    f"{family} reasoning_effort must be one of: "
                    + ", ".join(sorted(OPENAI_GPT5_REASONING_EFFORTS))
                )
            body["reasoning_effort"] = raw
        elif family == "grok":
            if raw not in GROK_REASONING_EFFORTS:
                raise ValueError(
                    "grok reasoning_effort must be one of: "
                    + ", ".join(sorted(GROK_REASONING_EFFORTS))
                )
            body["reasoning_effort"] = raw
        elif family == "glm":
            if raw not in GLM_REASONING_EFFORTS:
                raise ValueError(
                    "glm reasoning_effort must be one of: "
                    + ", ".join(sorted(GLM_REASONING_EFFORTS))
                )
            # GLM-5.3 origin contract does not map none/minimal/medium/xhigh.
            body["reasoning_effort"] = raw
        elif family == "gemini":
            if raw not in GEMINI_REASONING_EFFORTS:
                raise ValueError(
                    "gemini reasoning_effort must be one of: "
                    + ", ".join(sorted(GEMINI_REASONING_EFFORTS))
                )
            body["reasoning_effort"] = raw
        else:
            if raw not in REASONING_EFFORT_ALIASES:
                raise ValueError(
                    "deepseek reasoning_effort must be low/high/max or a supported alias."
                )
            model = str(body.get("model") or "").casefold()
            if raw == "xhigh" and "v4-pro" in model:
                body["reasoning_effort"] = "max"
            else:
                body["reasoning_effort"] = REASONING_EFFORT_ALIASES[raw]
    if family == "claude_fable":
        if not preserve_fable_parameter_body:
            if fable_official_compat_profile:
                # Recorded official Chat controls reject explicit adaptive
                # thinking; omission is the successful baseline. Keep existing
                # sampling cleanup without injecting native Messages defaults.
                body.pop("top_p", None)
                body.pop("thinking", None)
            else:
                _apply_claude_fable_compat(body)
        return
    if family == "qwen":
        _apply_qwen_compat(body)
        return
    if family == "claude":
        thinking = body.get("thinking")
        # Shared load defaults may request disabled thinking on models that
        # reject it. Explicit official parameter probes keep their HTTP field.
        if (not preserve_claude_parameter_body and isinstance(thinking, dict)
                and thinking.get("type") == "disabled"):
            body.pop("thinking", None)
    if family == "grok":
        # Shared throughput profiles may inject thinking.disabled / stop; drop them.
        thinking = body.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "disabled":
            body.pop("thinking", None)
        if not preserve_rejected_params:
            body.pop("stop", None)
            body.pop("presence_penalty", None)
            body.pop("frequency_penalty", None)
    if family == "glm" and _is_glm_5_3_model(str(body.get("model") or "")):
        # Shared throughput/stream/json profiles send thinking.disabled.
        # Native GLM-5.3 rejects that; rewrite to enabled+low unless this is
        # an explicit reject probe.
        thinking = body.get("thinking")
        if (
            isinstance(thinking, dict)
            and thinking.get("type") == "disabled"
            and not preserve_rejected_params
        ):
            thinking["type"] = "enabled"
            body.setdefault("reasoning_effort", "low")


def _apply_claude_fable_compat(
    payload: dict[str, Any], *, omit_sampling: bool = False
) -> None:
    """Rewrite shared load/cache settings into Fable-compatible Messages shape."""
    payload.pop("top_p", None)
    if omit_sampling:
        # Shared load/cache profiles use temperature=0, which is not a valid
        # explicit Fable sampling value. Preserve the provider default.
        payload.pop("temperature", None)
        payload.pop("top_k", None)
    output_config = payload.get("output_config")
    if isinstance(output_config, dict):
        output_config = copy.deepcopy(output_config)
    else:
        output_config = {}
    # Shared throughput/cache profiles use thinking.disabled or enabled+budget;
    # Fable only accepts adaptive + output_config.effort.
    payload["thinking"] = {"type": "adaptive"}
    if "effort" not in output_config:
        output_config["effort"] = "medium"
    payload["output_config"] = output_config


def _apply_qwen_compat(payload: dict[str, Any]) -> None:
    """Rewrite DeepSeek-style thinking into Qwen OpenAI-compat native fields."""
    thinking = payload.pop("thinking", None)
    if isinstance(thinking, dict):
        thinking_type = thinking.get("type")
        if thinking_type == "disabled":
            payload.setdefault("enable_thinking", False)
        elif thinking_type == "enabled":
            payload.setdefault("enable_thinking", True)
            budget = thinking.get("budget_tokens")
            if budget is not None and "thinking_budget" not in payload:
                payload["thinking_budget"] = int(budget)
    # DeepSeek-only identity field; Qwen allowlist drops it, remove early to avoid warnings.
    payload.pop("user_id", None)


def _validate_body(body: dict[str, Any], settings: dict[str, Any], family: str) -> None:
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        raise ValueError("messages must be a non-empty list.")
    _validate_messages(body["messages"])

    thinking = body.get("thinking")
    if thinking is not None:
        allowed_types = (
            ("enabled", "disabled", "adaptive")
            if family in {"claude", "claude_fable"}
            else ("enabled", "disabled")
        )
        if not isinstance(thinking, dict) or thinking.get("type") not in allowed_types:
            allowed = " / ".join(allowed_types)
            raise ValueError(f"thinking must be {{'type': '{allowed}'}}.")
        if "clear_thinking" in thinking and not isinstance(thinking["clear_thinking"], bool):
            raise ValueError("thinking.clear_thinking must be a boolean.")
        if "budget_tokens" in thinking and int(thinking["budget_tokens"]) <= 0:
            raise ValueError("thinking.budget_tokens must be positive.")

    if "max_tokens" in body and int(body["max_tokens"]) <= 0:
        raise ValueError("max_tokens must be positive.")

    if "max_completion_tokens" in body and int(body["max_completion_tokens"]) <= 0:
        raise ValueError("max_completion_tokens must be positive.")

    if "n" in body and not 1 <= int(body["n"]) <= 4:
        raise ValueError("n must be in [1, 4].")

    if "seed" in body and not 0 <= int(body["seed"]) <= 2**64 - 1:
        raise ValueError("seed must be an unsigned 64-bit integer.")

    if "presence_penalty" in body and not -2 <= float(body["presence_penalty"]) <= 2:
        raise ValueError("presence_penalty must be in [-2, 2].")

    if "enable_search" in body and not isinstance(body["enable_search"], bool):
        raise ValueError("enable_search must be a boolean.")

    if "enable_code_interpreter" in body and not isinstance(body["enable_code_interpreter"], bool):
        raise ValueError("enable_code_interpreter must be a boolean.")

    if "enable_thinking" in body and not isinstance(body["enable_thinking"], bool):
        raise ValueError("enable_thinking must be a boolean.")

    if "preserve_thinking" in body and not isinstance(body["preserve_thinking"], bool):
        raise ValueError("preserve_thinking must be a boolean.")

    if "thinking_budget" in body and int(body["thinking_budget"]) <= 0:
        raise ValueError("thinking_budget must be positive.")

    if "search_options" in body and not isinstance(body["search_options"], dict):
        raise ValueError("search_options must be an object.")

    if "prompt_cache_key" in body and (
        not isinstance(body["prompt_cache_key"], str)
        or not body["prompt_cache_key"].strip()
    ):
        raise ValueError("prompt_cache_key must be a non-empty string.")

    if "extra_body" in body and not isinstance(body["extra_body"], dict):
        raise ValueError("extra_body must be an object.")

    if "generationConfig" in body and not isinstance(body["generationConfig"], dict):
        raise ValueError("generationConfig must be an object.")

    if "safetySettings" in body and not isinstance(body["safetySettings"], list):
        raise ValueError("safetySettings must be a list.")

    temperature_max = 1 if family in {"glm", "claude"} else 2
    if "temperature" in body and not 0 <= float(body["temperature"]) <= temperature_max:
        raise ValueError(f"temperature must be in [0, {temperature_max}] for {family}.")

    if "top_p" in body and not 0 <= float(body["top_p"]) <= 1:
        raise ValueError("top_p must be in [0, 1].")

    if "top_k" in body and int(body["top_k"]) < 0:
        raise ValueError("top_k must be non-negative.")

    if "repetition_penalty" in body and float(body["repetition_penalty"]) <= 0:
        raise ValueError("repetition_penalty must be positive.")

    if "do_sample" in body and not isinstance(body["do_sample"], bool):
        raise ValueError("do_sample must be a boolean.")

    if "parallel_tool_calls" in body and not isinstance(body["parallel_tool_calls"], bool):
        raise ValueError("parallel_tool_calls must be a boolean.")

    if "tool_stream" in body:
        if not isinstance(body["tool_stream"], bool):
            raise ValueError("tool_stream must be a boolean.")
        if body["tool_stream"] and body.get("stream") is not True:
            raise ValueError("tool_stream=true requires stream=true.")

    if "stop" in body:
        stop = body["stop"]
        stop_values = stop if isinstance(stop, list) else [stop]
        if len(stop_values) > 16 or not all(isinstance(item, str) for item in stop_values):
            raise ValueError("stop must be a string or a list of up to 16 strings.")

    if "stream_options" in body and body.get("stream") is not True:
        raise ValueError("stream_options is valid only when stream=true.")

    if "top_logprobs" in body and not body.get("logprobs"):
        raise ValueError("top_logprobs requires logprobs=true.")
    if "logprobs" in body and not isinstance(body["logprobs"], bool):
        raise ValueError("logprobs must be a boolean.")
    if "verbosity" in body and str(body["verbosity"]).lower() not in {"low", "medium", "high"}:
        raise ValueError("verbosity must be low/medium/high.")
    prompt_cache_options = body.get("prompt_cache_options")
    if prompt_cache_options is not None:
        if not isinstance(prompt_cache_options, dict) or not prompt_cache_options:
            raise ValueError("prompt_cache_options must be a non-empty object.")
        ttl = prompt_cache_options.get("ttl")
        if ttl is not None and (not isinstance(ttl, str) or not ttl.strip()):
            raise ValueError("prompt_cache_options.ttl must be a non-empty string.")

    top_logprobs_max = 5 if family == "qwen" else 20
    if "top_logprobs" in body and not 0 <= int(body["top_logprobs"]) <= top_logprobs_max:
        raise ValueError(f"top_logprobs must be in [0, {top_logprobs_max}].")

    if family == "qwen" and "seed" in body and not 0 <= int(body["seed"]) <= 2**31 - 1:
        raise ValueError("seed must be in [0, 2^31-1] for Qwen.")

    if family == "qwen" and "n" in body and body.get("enable_thinking") is True:
        raise ValueError("Qwen n is only supported when enable_thinking=false.")

    response_format = body.get("response_format")
    if (
        family == "qwen"
        and isinstance(response_format, dict)
        and response_format.get("type") == "json_object"
        and body.get("enable_thinking") is True
    ):
        raise ValueError("Qwen response_format=json_object requires enable_thinking=false.")

    if "service_tier" in body and str(body["service_tier"]) not in {"auto", "default", "standard", "flex", "priority"}:
        raise ValueError("service_tier must be one of auto/default/standard/flex/priority.")

    if "tools" in body:
        if not isinstance(body["tools"], list) or len(body["tools"]) > 128:
            raise ValueError("tools must be a list with at most 128 functions.")

    if "user_id" in body and not _USER_ID_RE.match(str(body["user_id"])):
        raise ValueError("user_id must match [a-zA-Z0-9_-] and be at most 512 chars.")

    if "request_id" in body and not 6 <= len(str(body["request_id"])) <= 64:
        raise ValueError("request_id must be 6-64 characters.")

    response_format = body.get("response_format") or {}
    if response_format.get("type") == "json_object":
        prompt_text = json.dumps(body["messages"], ensure_ascii=False).casefold()
        if "json" not in prompt_text:
            raise ValueError("JSON Output profile prompts must contain the word JSON.")


def _validate_messages(messages: list[dict[str, Any]]) -> None:
    allowed_roles = {"system", "user", "assistant", "tool"}
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(f"messages[{index}] must be an object.")
        role = message.get("role")
        if role not in allowed_roles:
            raise ValueError(f"messages[{index}].role must be one of {sorted(allowed_roles)}.")
        if "name" in message and not isinstance(message["name"], str):
            raise ValueError(f"messages[{index}].name must be a string.")
        if role == "tool" and "tool_call_id" not in message:
            raise ValueError(f"messages[{index}] with role=tool requires tool_call_id.")
        if "tools" in message:
            if role != "system":
                raise ValueError(
                    f"messages[{index}].tools is only valid on a system message."
                )
            if message.get("content") not in (None, ""):
                raise ValueError(
                    f"messages[{index}] with tools must have empty content."
                )
            if not isinstance(message["tools"], list) or not message["tools"]:
                raise ValueError(
                    f"messages[{index}].tools must be a non-empty list."
                )
            if len(message["tools"]) > 128:
                raise ValueError(
                    f"messages[{index}].tools must contain at most 128 functions."
                )


def _read_text_fixture(path: str | Path) -> str:
    return resolve_project_path(path).read_text(encoding="utf-8").strip()


def _read_json_fixture(path: str | Path) -> Any:
    return json.loads(resolve_project_path(path).read_text(encoding="utf-8"))


def _mock_tool_message(tool_call: dict[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function") or {}
    args_text = function.get("arguments") or "{}"
    try:
        args = json.loads(args_text)
    except json.JSONDecodeError:
        args = {}

    payload = _mock_tool_payload(name=function.get("name"), args=args)
    return {
        "role": "tool",
        "tool_call_id": tool_call.get("id", "mock_tool_call"),
        "name": function.get("name") or "get_weather",
        "content": json.dumps(payload, ensure_ascii=False),
    }


def _mock_tool_payload(
    *,
    name: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    city = args.get("city") or args.get("location") or "Beijing"
    unit = args.get("unit") or "celsius"
    return {
        "tool": str(name or "get_weather"),
        "city": city,
        "unit": unit,
        "temperature": 21,
        "condition": "clear",
        "source": "mock_loadtest_tool",
    }


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


GEMINI_37_GENERATE_CONTENT_SAFE_PROFILES = {
    "gemini_native_tools",
    "gemini_native_tool_config",
    "gemini_native_system_instruction",
    "gemini_native_service_tier",
    "gemini_native_store",
    "gemini_native_stop_sequences",
    "gemini_native_response_mime_type",
    "gemini_native_response_schema",
    "gemini_native_response_json_schema",
    "gemini_native_response_modalities",
    "gemini_native_candidate_count",
    "gemini_native_max_output_tokens",
    "gemini_native_temperature",
    "gemini_native_top_p",
    "gemini_native_top_k",
    "gemini_native_seed",
    "gemini_native_presence_penalty",
    "gemini_native_frequency_penalty",
    "gemini_native_logprobs",
    "gemini_native_civic_answers",
    "gemini_native_thinking_minimal",
    "gemini_native_thinking_low",
    "gemini_native_thinking_medium",
    "gemini_native_thinking_high",
    "gemini_native_media_resolution",
    "gemini_native_response_format",
}


def _enforce_gemini_37_generate_content_safety(
    profile: str,
    settings: dict[str, Any],
    body: dict[str, Any],
) -> None:
    if profile not in GEMINI_37_GENERATE_CONTENT_SAFE_PROFILES:
        raise ValueError(
            f"Profile {profile!r} is outside the Gemini 3.7 Flash "
            "GenerateContent safe text contract."
        )
    if settings.get("native_cached_content") is not None or "cachedContent" in body:
        raise ValueError("Gemini 3.7 Flash safe text profiles must not use cachedContent.")
    generation = body.get("generationConfig")
    if isinstance(generation, dict):
        # The exact 3.7 official JSON retry needed 2048 tokens to finish valid
        # JSON. Keep this correction local to that contract and preserve larger
        # user caps; the shared profiles also describe other Gemini models.
        if profile in {
            "gemini_native_response_mime_type", "gemini_native_response_schema",
            "gemini_native_response_json_schema", "gemini_native_response_format",
        }:
            generation["maxOutputTokens"] = max(generation.get("maxOutputTokens", 2048), 2048)
        modalities = generation.get("responseModalities") or []
        if isinstance(modalities, str):
            modalities = [modalities]
        if any(str(value).casefold() == "image" for value in modalities):
            raise ValueError(
                "Gemini 3.7 Flash safe text profiles must not request IMAGE output."
            )
        if "imageConfig" in generation:
            raise ValueError(
                "Gemini 3.7 Flash safe text profiles must not include imageConfig."
            )
    for setting in body.get("safetySettings") or []:
        if isinstance(setting, dict) and str(setting.get("threshold") or "").upper() in {"BLOCK_NONE", "OFF"}:
            raise ValueError(
                "Gemini 3.7 Flash safe text profiles must not disable safety blocking."
            )
