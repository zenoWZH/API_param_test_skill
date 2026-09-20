"""Documented Messages probes for two Fable models on two independent routes.

This module only constructs requests.  ``accept`` means that the cited document
permits the input, not that an API has accepted it.  InferenceAI observations
must retain their gateway identity; these bodies are not SigV4 Invoke requests.

Fixtures and previous responses are described under ``validation``.  A sender
must materialize them before freezing the wire package, and must never send a
``__fixture__`` placeholder.  Dependency case IDs refer only to this exact
provider/model group.  Deliberately malformed output limits are excluded so
that every request has an integer output allowance of at least 256 tokens.
"""
from __future__ import annotations

import copy
from typing import Any

MODELS = ("claude-fable-5", "claude-fable-5-1")
PROVIDERS = ("anthropic_official", "inferenceai_awsb")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
OUTPUT_ALLOWANCE = 8192
ANSWER = "323"
PROMPT = "Calculate 17 * 19. Reply with only the integer result, without explanation."

ANTHROPIC = "https://platform.claude.com/docs/en/"
AWS = "https://docs.aws.amazon.com/bedrock/latest/userguide/"
DOCS = {
    "messages": ANTHROPIC + "api/messages/create",
    "thinking": ANTHROPIC + "build-with-claude/thinking",
    "effort": ANTHROPIC + "build-with-claude/effort",
    "tools": ANTHROPIC + "agents-and-tools/tool-use/define-tools",
    "tool_streaming": ANTHROPIC + "agents-and-tools/tool-use/fine-grained-tool-streaming",
    "tool_reference": ANTHROPIC + "agents-and-tools/tool-use/tool-reference",
    "tool_search": ANTHROPIC + "agents-and-tools/tool-use/tool-search-tool",
    "mid": ANTHROPIC + "build-with-claude/mid-conversation-system-messages",
    "context": ANTHROPIC + "build-with-claude/context-editing",
    "fast": ANTHROPIC + "build-with-claude/fast-mode",
    "fallback": ANTHROPIC + "build-with-claude/refusals-and-fallback",
    "files": ANTHROPIC + "build-with-claude/files",
    "code_execution": ANTHROPIC + "agents-and-tools/tool-use/code-execution-tool",
    "web_search": ANTHROPIC + "agents-and-tools/tool-use/web-search-tool",
    "web_fetch": ANTHROPIC + "agents-and-tools/tool-use/web-fetch-tool",
    "advisor": ANTHROPIC + "agents-and-tools/tool-use/advisor-tool",
    "computer": ANTHROPIC + "agents-and-tools/tool-use/computer-use-tool",
    "browser": ANTHROPIC + "agents-and-tools/tool-use/browser-use-tool",
    "mcp": ANTHROPIC + "agents-and-tools/mcp-connector",
    "streaming": ANTHROPIC + "build-with-claude/streaming",
    "structured": ANTHROPIC + "build-with-claude/structured-outputs",
    "cache": ANTHROPIC + "build-with-claude/prompt-caching",
    "task_budget": ANTHROPIC + "build-with-claude/task-budgets",
    "compaction": ANTHROPIC + "build-with-claude/compaction",
    "preserved": ANTHROPIC + "build-with-claude/preserved-thinking",
    "vision": ANTHROPIC + "build-with-claude/vision",
    "vision_coordinates": ANTHROPIC + "build-with-claude/vision-coordinates",
    "pdf": ANTHROPIC + "build-with-claude/pdf-support",
    "bedrock": ANTHROPIC + "build-with-claude/claude-in-amazon-bedrock",
    "aws_messages": AWS + "inference-messages-api.html",
    "aws_thinking": AWS + "claude-messages-adaptive-thinking.html",
    "aws_binding": AWS + "claude-messages-thinking-block-binding.html",
    "aws_compaction": AWS + "claude-messages-compaction.html",
    "aws_tools": AWS + "model-parameters-anthropic-claude-messages-tool-use.html",
    "aws_cache": AWS + "prompt-caching.html",
    "aws_parameters": AWS + "model-parameters-anthropic-claude-messages-request-response.html",
}

RECORD_TOOL = {
    "name": "record_product",
    "description": "Record the exact integer product of the two supplied integers.",
    "input_schema": {
        "type": "object", "properties": {"product": {"type": "integer", "enum": [323]}},
        "required": ["product"], "additionalProperties": False,
    },
}
COLOR_TOOL = {
    "name": "record_color", "description": "Record the explicitly requested color.",
    "input_schema": {
        "type": "object", "properties": {"color": {"type": "string", "enum": ["blue"]}},
        "required": ["color"], "additionalProperties": False,
    },
}
JSON_SCHEMA = {
    "type": "object", "properties": {"product": {"type": "integer", "enum": [323]}},
    "required": ["product"], "additionalProperties": False,
}


def build_cases(provider: str, model: str) -> list[dict[str, Any]]:
    """Return independent, deterministic, JSON-serializable case descriptions."""
    if provider not in PROVIDERS:
        raise ValueError("Fable matrix requires one of the two designated providers")
    if model not in MODELS:
        raise ValueError("Fable matrix requires an exact supported model ID")
    aws = provider == "inferenceai_awsb"
    newer = model == "claude-fable-5-1"
    source = "aws_bedrock" if aws else "anthropic"
    prefix = provider + "/" + model + "/"
    model_url = (AWS + "model-card-anthropic-" + model + ".html" if aws else
                 ANTHROPIC + "models/" + model.removeprefix("claude-") + "/migration-guide")
    base = {"model": model, "max_tokens": OUTPUT_ALLOWANCE, "stream": False,
            "messages": [{"role": "user", "content": PROMPT}],
            "output_config": {"effort": "low"}}
    rows: list[dict[str, Any]] = []

    def add(name: str, *, changes: dict | None = None, omit: tuple[str, ...] = (),
            expected: str = "accept", parameters: tuple[str, ...] = (),
            kind: str = "text_exact", validation: dict | None = None,
            docs: tuple[str, ...] = ("messages",), beta: str | None = None,
            depends: tuple[str, ...] = ()) -> dict:
        body = copy.deepcopy(base)
        body.update(copy.deepcopy(changes or {}))
        for key in omit:
            body.pop(key, None)
        rules: dict[str, Any] = {"kind": "field_rejection" if expected == "reject" else kind}
        if rules["kind"] in {"text_exact", "stream_text"}:
            rules["expected_text"] = ANSWER
        rules.update(copy.deepcopy(validation or {}))
        if expected == "reject":
            rules.setdefault("error_parameter_paths", list(parameters))
            rules.setdefault("require_field_attribution", True)
            rules.setdefault("positive_control", prefix + "baseline")
        urls = [model_url]
        if aws:
            urls.append(DOCS["aws_messages"])
        urls.extend(DOCS[key] for key in docs)
        case = {
            "case_id": prefix + name, "provider": provider, "model": model,
            "name": name, "source_id": source, "api_form": "anthropic_messages",
            "body": body, "headers": {"anthropic-beta": beta} if beta else {},
            "expected_outcome": expected, "target_parameters": list(parameters),
            "validation": rules, "official_urls": list(dict.fromkeys(urls)),
            "expectation_basis": "official_documentation_not_live_verification",
            "execution_scope": "gateway_messages_with_aws_documented_parameter_mapping" if aws else "origin_vendor_messages",
        }
        if depends:
            case["depends_on"] = [prefix + name for name in depends]
        fixture_kind = rules.get("fixture", {}).get("kind")
        case["phase"] = ("platform" if rules.get("requires_specialized_sender") else
                         "stateful" if depends or fixture_kind == "synthetic_cache_prefix" or kind == "thinking_seed" else
                         "multimodal" if fixture_kind in {"synthetic_image", "synthetic_pdf"} else "core")
        rows.append(case)
        return case

    add("baseline", parameters=("model", "messages", "max_tokens", "output_config.effort"))
    add("default_effort", omit=("output_config",), parameters=("output_config",), docs=("effort",))
    add("system_string", changes={"system": "For this test, answer the arithmetic question with the integer alone."}, parameters=("system",))
    add("system_text_blocks", changes={"system": [{"type": "text", "text": "Answer with the integer alone."}]}, parameters=("system",))
    add("user_text_blocks", changes={"messages": [{"role": "user", "content": [{"type": "text", "text": PROMPT}]}]}, parameters=("messages[].content",))
    add("multiturn", changes={"messages": [{"role": "user", "content": "Remember the first integer: 17."},
        {"role": "assistant", "content": "The first integer is 17."},
        {"role": "user", "content": "Multiply the remembered integer by 19. Reply only with the integer product."}]}, parameters=("messages",))
    # Content-bearing mid-conversation system messages follow the current user,
    # unlike empty effort-only updates which affect the next user turn.
    add("midconversation_system", changes={"messages": [{"role": "user", "content": PROMPT},
        {"role": "system", "content": "Reply with only the exact integer product."}]}, expected="observe" if aws else "accept",
        parameters=("messages[role=system].content",), docs=("mid",))
    for clear_at in ("never", "next_user_message"):
        add("midconversation_clear_" + clear_at, changes={"messages": [{"role": "user", "content": PROMPT},
            {"role": "system", "content": "Reply with only the exact integer product.", "clear_at": clear_at}]},
            expected="observe" if aws else "accept", parameters=("messages[role=system].clear_at",), docs=("mid",),
            beta="mid-conversation-system-clear-at-2026-08-21", validation={"clearing_effect_verified": False})
    # Required model/messages controls retain a bounded, valid max_tokens.
    add("missing_model", omit=("model",), expected="reject", parameters=("model",))
    add("model_wrong_type", changes={"model": [model]}, expected="reject", parameters=("model",))
    add("missing_messages", omit=("messages",), expected="reject", parameters=("messages",))
    add("messages_wrong_type", changes={"messages": "invalid_messages_fixture"}, expected="reject", parameters=("messages",))
    add("messages_empty", changes={"messages": []}, expected="reject", parameters=("messages",))
    add("message_role_invalid", changes={"messages": [{"role": "invalid_role", "content": PROMPT}]}, expected="reject", parameters=("messages[].role",))
    add("message_content_wrong_type", changes={"messages": [{"role": "user", "content": 323}]}, expected="reject", parameters=("messages[].content",))
    add("system_wrong_type", changes={"system": 323}, expected="reject", parameters=("system",))
    for cap in (256, 512):
        add("output_limit_" + str(cap), changes={"max_tokens": cap, "messages": [{"role": "user", "content":
            "List successive positive integers starting at 1, one per line, until 10000. Do not abbreviate or explain."}]},
            parameters=("max_tokens",), kind="output_limit", validation={"maximum_output_tokens": cap,
                "require_visible_task_progress": True, "record_limit_reached": True,
                "independent_output_token_exactness_verified": False})
    max_case = add("output_limit_documented_maximum", changes={"max_tokens": 128000}, expected="observe" if aws else "accept", parameters=("max_tokens",),
        validation={"documented_maximum": "128K" if aws else 128000, "require_full_budget_consumption": False})
    above_max_case = add("output_limit_above_documented_maximum", changes={"max_tokens": 128001}, expected="observe" if aws else "reject", parameters=("max_tokens",),
        validation={"documented_maximum": "128K" if aws else 128000, "positive_control": prefix + "output_limit_documented_maximum",
                    "aws_128k_integer_interpretation_unverified": aws})
    if not aws:
        for case in (max_case, above_max_case):
            case["expectation_basis"] = "official_documentation_and_20260912_models_api_discovery"
            case["validation"]["discovery_evidence_required"] = {"endpoint": "/v1/models", "model_id": model, "max_tokens": 128000}
    add("stream", changes={"stream": True}, parameters=("stream",), kind="stream_text", docs=("streaming",))
    add("stream_wrong_type", changes={"stream": "yes"}, expected="reject", parameters=("stream",))

    add("thinking_adaptive", changes={"thinking": {"type": "adaptive"}}, parameters=("thinking.type",), docs=("thinking",))
    add("thinking_disabled", changes={"thinking": {"type": "disabled"}}, expected="reject", parameters=("thinking.type",), docs=("thinking",))
    add("thinking_manual_budget", changes={"thinking": {"type": "enabled", "budget_tokens": 1024}}, expected="reject",
        parameters=("thinking.type", "thinking.budget_tokens"), docs=("thinking",))
    add("thinking_invalid_type", changes={"thinking": {"type": "invalid_mode"}}, expected="reject", parameters=("thinking.type",), docs=("thinking",))
    for display in ("omitted", "summarized"):
        add("thinking_display_" + display, changes={"thinking": {"type": "adaptive", "display": display}},
            parameters=("thinking.display",), docs=("thinking",), validation={"thinking_display": display,
                "thinking_block_required": False, "hidden_thinking_effect_verified": False})
    add("thinking_display_updates", changes={"thinking": {"type": "adaptive", "display": "updates"}, "stream": True},
        parameters=("thinking.display", "stream"), kind="stream_text", docs=("thinking",),
        beta="thinking-display-updates-2026-08-18", validation={"thinking_display": "updates", "updates_required": False})
    add("thinking_display_invalid", changes={"thinking": {"type": "adaptive", "display": "invalid_display"}}, expected="reject", parameters=("thinking.display",), docs=("thinking",))
    for effort in EFFORTS:
        add("effort_" + effort, changes={"output_config": {"effort": effort}}, parameters=("output_config.effort",), docs=("effort",),
            validation={"requested_effort": effort, "reasoning_intensity_effect_verified": False})
    add("effort_invalid_enum", changes={"output_config": {"effort": "invalid_effort"}}, expected="reject", parameters=("output_config.effort",), docs=("effort",))
    add("effort_wrong_type", changes={"output_config": {"effort": []}}, expected="reject", parameters=("output_config.effort",), docs=("effort",))
    add("effort_inside_thinking", changes={"thinking": {"type": "adaptive", "effort": "low"}}, expected="reject",
        parameters=("thinking.effort",), docs=("effort", "aws_thinking") if aws else ("effort",), validation={"positive_control": prefix + "effort_low"})

    for label, value in (("one", 1), ("half", 0.5), ("zero", 0), ("two", 2)):
        add("temperature_" + label, changes={"temperature": value}, expected="accept" if value == 1 else "reject",
            parameters=("temperature",), docs=("messages", "thinking"), validation={"sampling_distribution_effect_verified": False})
    for label, value in (("099", 0.99), ("0995", 0.995), ("one", 1), ("09", 0.9)):
        allowed = value >= 0.99 if not aws else (value == 0.99 if newer else 0.99 <= value < 1)
        add("top_p_" + label, changes={"top_p": value}, expected="accept" if allowed else "reject", parameters=("top_p",),
            docs=("messages",) if not aws else ("aws_parameters",), validation={
                "documented_allowed_value": allowed, "known_prior_source_conflict": "Anthropic Fable 5 rejected explicit top_p on 2026-09-09" if not aws and not newer else None,
                "sampling_distribution_effect_verified": False})
    add("top_k_explicit", changes={"top_k": 40}, expected="reject", parameters=("top_k",), docs=("thinking",))
    add("temperature_and_top_p", changes={"temperature": 1, "top_p": 0.99}, expected="reject" if aws and newer else "observe",
        parameters=("temperature", "top_p"), docs=("messages",) if not aws else ("aws_parameters",),
        validation={"combination_documented_for_exact_model": aws and newer, "sampling_distribution_effect_verified": False})

    stop_prompt = "Reply with exactly ALPHA BETA GAMMA DELTA, with single spaces and no other text."
    add("stop_baseline", changes={"messages": [{"role": "user", "content": stop_prompt}]}, parameters=("stop_sequences",), validation={"expected_text": "ALPHA BETA GAMMA DELTA"})
    for label, marker, text in (("beta", " BETA", "ALPHA"), ("gamma", " GAMMA", "ALPHA BETA")):
        add("stop_before_" + label, changes={"messages": [{"role": "user", "content": stop_prompt}], "stop_sequences": [marker]},
            parameters=("stop_sequences",), kind="stop_sequence", depends=("stop_baseline",),
            validation={"expected_text": text, "stop_sequence": marker, "stop_reason": "stop_sequence", "positive_control": prefix + "stop_baseline"})
    add("stop_wrong_type", changes={"stop_sequences": {"invalid": True}}, expected="reject", parameters=("stop_sequences",))

    tool_body = {"tools": [RECORD_TOOL], "messages": [{"role": "user", "content": "Use record_product to record 17 multiplied by 19."}]}
    for label, choice in (("default", None), ("auto", {"type": "auto"}), ("none", {"type": "none"}),
                          ("any", {"type": "any"}), ("named", {"type": "tool", "name": "record_product"})):
        changes = copy.deepcopy(tool_body)
        if choice is not None:
            changes["tool_choice"] = choice
        if label == "none":
            changes["messages"] = [{"role": "user", "content": PROMPT}]
        add("tools_" + label, changes=changes, expected="reject" if newer and label in {"any", "named"} else "accept",
            parameters=("tools", "tool_choice"), kind="text_exact" if label == "none" else "tool_call", docs=("tools", "aws_tools") if aws else ("tools",),
            validation={"forbid_tool_calls": label == "none", "tool_name": "record_product", "expected_arguments": {"product": 323},
                "strict_schema_enforcement_proven": False, "tool_execution": False})
    add("tool_choice_invalid", changes={**tool_body, "tool_choice": {"type": "invalid_choice"}}, expected="reject", parameters=("tool_choice",), docs=("tools",))
    add("tools_wrong_type", changes={"tools": "invalid_tools"}, expected="reject", parameters=("tools",), docs=("tools",))
    add("tool_schema_wrong_type", changes={"tools": [{**RECORD_TOOL, "input_schema": []}]}, expected="reject", parameters=("tools[].input_schema",), docs=("tools",))
    add("tool_input_examples", changes={**tool_body, "tools": [{**RECORD_TOOL, "input_examples": [{"product": 323}]}]},
        expected="observe" if aws else "accept", parameters=("tools[].input_examples",), kind="tool_call", docs=("tools",),
        validation={"tool_name": "record_product", "expected_arguments": {"product": 323}})
    add("tool_input_examples_wrong_type", changes={**tool_body, "tools": [{**RECORD_TOOL, "input_examples": "invalid_examples"}]},
        expected="observe" if aws else "reject", parameters=("tools[].input_examples",), docs=("tools",))
    for label, tool_name, allowed in (("one", "x", True), ("128", "x" * 128, True), ("129", "x" * 129, False), ("invalid", "invalid name!", False)):
        add("tool_name_" + label, changes={"tools": [{**RECORD_TOOL, "name": tool_name}], "messages": [{"role": "user", "content":
            "Use the declared tool to record the product of 17 and 19."}]}, expected="accept" if allowed else "reject", parameters=("tools[].name",),
            kind="tool_call", docs=("tools",), validation={"tool_name": tool_name, "expected_arguments": {"product": 323}})
    for eager in (False, True):
        add("tool_eager_streaming_" + str(eager).lower(), changes={**tool_body, "stream": True,
            "tools": [{**RECORD_TOOL, "eager_input_streaming": eager}]}, expected="observe" if aws else "accept",
            parameters=("tools[].eager_input_streaming", "stream"), kind="tool_call", docs=("tool_streaming",),
            validation={"tool_name": "record_product", "expected_arguments": {"product": 323}, "requires_stream_assembly": True,
                        "individual_json_fragments_need_not_parse": True})
    add("tool_allowed_callers_direct", changes={**tool_body, "tools": [{**RECORD_TOOL, "allowed_callers": ["direct"]}]},
        expected="observe" if aws else "accept", parameters=("tools[].allowed_callers",), kind="tool_call", docs=("tool_reference",),
        validation={"tool_name": "record_product", "expected_arguments": {"product": 323}, "security_boundary_verified": False})
    add("midconversation_tool_removal", changes={"tools": [RECORD_TOOL], "messages": [{"role": "user", "content": PROMPT},
        {"role": "system", "content": [{"type": "tool_removal", "tool": {"type": "tool_reference", "name": "record_product"}}]}]},
        expected="observe" if aws else "accept", parameters=("messages[role=system].content[].tool_removal",), docs=("mid",),
        beta="mid-conversation-tool-changes-2026-07-01", validation={"forbid_tool_calls": True})
    add("midconversation_tool_addition", changes={"tools": [{**RECORD_TOOL, "defer_loading": True}, COLOR_TOOL],
        "messages": [{"role": "user", "content": "Use record_product to record 17 multiplied by 19."},
                     {"role": "system", "content": [{"type": "tool_addition", "tool": {"type": "tool_reference", "name": "record_product"}}]}]},
        expected="observe" if aws else "accept", parameters=("messages[role=system].content[].tool_addition", "tools[].defer_loading"),
        kind="tool_call", docs=("mid", "tool_reference"), beta="mid-conversation-tool-changes-2026-07-01",
        validation={"tool_name": "record_product", "expected_arguments": {"product": 323}})
    add("midconversation_tool_unknown_reference", changes={"tools": [RECORD_TOOL], "messages": [{"role": "user", "content": PROMPT},
        {"role": "system", "content": [{"type": "tool_addition", "tool": {"type": "tool_reference", "name": "undeclared_tool"}}]}]},
        expected="observe" if aws else "reject", parameters=("messages[role=system].content[].tool",), docs=("mid",),
        beta="mid-conversation-tool-changes-2026-07-01")
    for disabled in (False, True):
        add("parallel_disabled_" + str(disabled).lower(), changes={"tools": [RECORD_TOOL, COLOR_TOOL],
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": disabled}, "messages": [{"role": "user", "content":
                "Use both tools: record_product for 17 times 19, and record_color for blue. These are independent."}]},
            parameters=("tool_choice.disable_parallel_tool_use",), kind="parallel_tools", docs=("tools",),
            validation={"disable_parallel_tool_use": disabled, "allowed_tools": ["record_product", "record_color"],
                "expected_arguments_by_tool": {"record_product": {"product": 323}, "record_color": {"color": "blue"}},
                "minimum_calls": 1, "maximum_calls": 1 if disabled else None,
                "parallel_effect_requires_multiple_calls": not disabled, "tool_execution": False})
    add("tool_result_roundtrip", changes={"tools": [RECORD_TOOL]}, parameters=("messages[].content[].tool_result",),
        kind="tool_roundtrip", docs=("tools",), depends=("tools_auto",), validation={
            "dependency": {"case_id": prefix + "tools_auto", "operation": "append_tool_result", "tool_name": "record_product",
                "tool_result_content": "Recorded product: 323. Reply only with 323.", "preserve_all_assistant_content": True,
                "require_matching_tool_use_id": True}, "expected_text": ANSWER, "real_external_tool_execution": False})

    # AWS explicitly lists structured outputs as unavailable, without promising
    # a field-specific HTTP error.  Preserve acceptance/ignore/rejection as an
    # observation instead of manufacturing a successful grammar guarantee.
    structured_expectation = "observe" if aws else "accept"
    structured_limit = {"documented_platform_support": "unsupported" if aws else "supported",
                        "strict_schema_enforcement_proven": False}
    add("tool_strict", changes={**tool_body, "tools": [{**RECORD_TOOL, "strict": True}]}, expected=structured_expectation,
        parameters=("tools[].strict",), kind="tool_call", docs=("structured", "bedrock") if aws else ("structured",),
        validation={**structured_limit, "tool_name": "record_product", "expected_arguments": {"product": 323}})
    json_body = {"messages": [{"role": "user", "content": "Return the product of 17 and 19 as JSON with the key product."}],
                 "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": JSON_SCHEMA}}}
    add("json_schema", changes=json_body, expected=structured_expectation, parameters=("output_config.format",), kind="json_schema",
        docs=("structured", "bedrock") if aws else ("structured",), validation={**structured_limit, "schema": JSON_SCHEMA, "expected_json": {"product": 323}})
    add("json_schema_wrong_type", changes={"output_config": {"effort": "low", "format": {"type": "json_schema", "schema": []}}},
        expected="observe" if aws else "reject", parameters=("output_config.format.schema",), docs=("structured", "bedrock") if aws else ("structured",),
        validation=structured_limit)
    constrained_schema = {**JSON_SCHEMA, "properties": {"product": {"type": "integer", "minimum": 323}}}
    add("json_schema_unsupported_constraint", changes={"output_config": {"effort": "low", "format": {"type": "json_schema", "schema": constrained_schema}}},
        expected="observe" if aws else "reject", parameters=("output_config.format.schema",), docs=("structured", "bedrock") if aws else ("structured",),
        validation={**structured_limit, "unsupported_schema_keyword": "minimum"})

    add("metadata_user_id", changes={"metadata": {"user_id": "fable_matrix_synthetic_user"}}, expected="observe" if aws else "accept",
        parameters=("metadata.user_id",), validation={"server_side_metadata_effect_verified": False})
    add("metadata_user_id_wrong_type", changes={"metadata": {"user_id": []}}, expected="observe" if aws else "reject", parameters=("metadata.user_id",))
    add("metadata_user_id_too_long", changes={"metadata": {"user_id": "u" * 513}}, expected="observe" if aws else "reject", parameters=("metadata.user_id",))
    add("metadata_user_id_null", changes={"metadata": {"user_id": None}}, expected="observe" if aws else "accept", parameters=("metadata.user_id",))
    add("metadata_user_id_maximum_length", changes={"metadata": {"user_id": "u" * 512}}, expected="observe" if aws else "accept", parameters=("metadata.user_id",))
    for tier in (("default", "standard_only", "auto") if aws else ("standard_only", "auto")):
        add("service_tier_" + tier, changes={"service_tier": tier}, expected="observe" if aws else "accept", parameters=("service_tier",),
            validation={"requested_service_tier": tier, "gateway_to_aws_tier_mapping_verified": False,
                        "aws_documented_tier": "default" if aws else None})
    add("service_tier_invalid", changes={"service_tier": "invalid_tier"}, expected="observe" if aws else "reject", parameters=("service_tier",))
    add("speed_standard", changes={"speed": "standard"}, expected="observe", parameters=("speed",), docs=("fast",), beta="fast-mode-2026-02-01")
    add("speed_fast_unsupported", changes={"speed": "fast"}, expected="observe" if aws else "reject", parameters=("speed",), docs=("fast",),
        beta="fast-mode-2026-02-01", validation={"documented_platform_support": "unsupported", "unsupported_exact_models": list(MODELS)})
    add("inference_geo_null", changes={"inference_geo": None}, expected="observe", parameters=("inference_geo",),
        validation={"regional_execution_verified": False, "documented_support": "generic_schema_only"})
    add("inference_geo_wrong_type", changes={"inference_geo": []}, expected="observe" if aws else "reject", parameters=("inference_geo",))

    # Cache preparation injects a run-specific prefix nonce before freezing the
    # package.  The repeat MUST reuse the exact materialized cold body.
    cache_fixture = {"kind": "synthetic_cache_prefix", "minimum_input_tokens": 512,
        "synthetic_rows": 160, "run_nonce_required": True, "answer_instruction": PROMPT}
    cache_body = {"system": [{"type": "text", "text": {"__fixture__": "cache_prefix"}, "cache_control": {"type": "ephemeral", "ttl": "5m"}}]}
    add("cache_block_cold", changes=cache_body, parameters=("system[].cache_control",), kind="cache_control", docs=("cache", "aws_cache") if aws else ("cache",),
        validation={"fixture": cache_fixture, "cache_group": prefix + "cache_block", "cache_role": "cold", "expected_text": ANSWER,
                    "cache_effect_requires_trio": True})
    add("cache_block_repeat", changes=cache_body, parameters=("system[].cache_control",), kind="cache_control", depends=("cache_block_cold",), docs=("cache",),
        validation={"dependency": {"case_id": prefix + "cache_block_cold", "operation": "reuse_exact_request_body"},
                    "cache_group": prefix + "cache_block", "cache_role": "repeat", "require_cache_read_tokens_positive": True, "expected_text": ANSWER})
    add("cache_block_negative", changes=cache_body, parameters=("system[].cache_control",), kind="cache_control", depends=("cache_block_repeat",), docs=("cache",),
        validation={"fixture": {**cache_fixture, "distinct_prefix_from": prefix + "cache_block_cold"},
                    "cache_group": prefix + "cache_block", "cache_role": "negative", "expected_text": ANSWER})
    for ttl in ("5m", "1h"):
        add("cache_automatic_" + ttl, changes={"system": {"__fixture__": "cache_prefix"}, "cache_control": {"type": "ephemeral", "ttl": ttl}},
            expected="observe" if aws else "accept", parameters=("cache_control",), kind="cache_control", docs=("cache", "aws_cache") if aws else ("cache",),
            validation={"fixture": cache_fixture, "cache_role": "configuration", "expected_text": ANSWER,
                        "top_level_gateway_cache_mapping_verified": False})
    add("cache_block_1h", changes={"system": [{"type": "text", "text": {"__fixture__": "cache_prefix"},
        "cache_control": {"type": "ephemeral", "ttl": "1h"}}]}, parameters=("system[].cache_control.ttl",), kind="cache_control", docs=("cache",),
        validation={"fixture": cache_fixture, "cache_role": "configuration", "expected_text": ANSWER, "one_hour_retention_elapsed_verified": False})
    add("cache_message_block", changes={"messages": [{"role": "user", "content": [{"type": "text", "text": {"__fixture__": "cache_prefix"},
        "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": PROMPT}]}]}, parameters=("messages[].content[].cache_control",),
        kind="cache_control", docs=("cache",), validation={"fixture": cache_fixture, "cache_role": "configuration", "expected_text": ANSWER})
    add("cache_tool_block", changes={**tool_body, "tools": [{**RECORD_TOOL, "cache_control": {"type": "ephemeral"}}]},
        parameters=("tools[].cache_control",), kind="tool_call", docs=("cache",), validation={"tool_name": "record_product",
            "expected_arguments": {"product": 323}, "cache_hit_required": False, "may_be_below_cache_minimum": True})
    add("cache_ttl_invalid", changes={"system": [{"type": "text", "text": "Synthetic cache type control", "cache_control": {"type": "ephemeral", "ttl": "2h"}}]},
        expected="reject", parameters=("system[].cache_control.ttl",), docs=("cache",))
    add("cache_type_invalid", changes={"system": [{"type": "text", "text": "Synthetic cache type control", "cache_control": {"type": "permanent"}}]},
        expected="reject", parameters=("system[].cache_control.type",), docs=("cache",))
    add("cache_five_breakpoints", changes={"system": [{"type": "text", "text": "Synthetic checkpoint " + str(n), "cache_control": {"type": "ephemeral"}} for n in range(5)]},
        expected="reject", parameters=("cache_control",), docs=("cache",), validation={"checkpoint_count": 5})

    per_turn = {"messages": [{"role": "user", "content": "Remember the integer 17."},
        {"role": "assistant", "content": "Remembered 17."},
        {"role": "system", "content": [], "output_config": {"effort": "low"}},
        {"role": "user", "content": "Multiply the remembered integer by 19; reply only the product."}]}
    add("per_message_effort", changes=per_turn, expected="accept" if newer else "reject", parameters=("messages[].output_config.effort",),
        docs=("effort", "aws_thinking") if aws else ("effort",), beta="mid-conversation-effort-2026-08-01" if aws else "mid-conversation-output-config-2026-07-01")
    add("per_message_effort_without_beta", changes=per_turn, expected="reject" if aws else "observe", parameters=("messages[].output_config.effort",),
        docs=("effort",), validation={"beta_required_by_documentation": True})
    for remaining in (None, 15000):
        budget = {"type": "tokens", "total": 20000}
        if remaining is not None:
            budget["remaining"] = remaining
        add("task_budget_" + ("total" if remaining is None else "remaining"), changes={"output_config": {"effort": "low", "task_budget": budget}},
            expected="observe" if aws else "accept", parameters=("output_config.task_budget",), docs=("task_budget",), beta="task-budgets-2026-03-13",
            validation={"task_budget_effect_verified": False, "max_tokens_remains_independent": True})
    add("task_budget_invalid_type", changes={"output_config": {"effort": "low", "task_budget": {"type": "tokens", "total": "invalid_total"}}},
        expected="observe" if aws else "reject", parameters=("output_config.task_budget.total",), docs=("task_budget",), beta="task-budgets-2026-03-13")
    add("task_budget_below_minimum", changes={"output_config": {"effort": "low", "task_budget": {"type": "tokens", "total": 19999}}},
        expected="observe" if aws else "reject", parameters=("output_config.task_budget.total",), docs=("task_budget",), beta="task-budgets-2026-03-13")
    add("per_message_task_budget", changes={"messages": [{"role": "user", "content": "Remember the integer 17."},
        {"role": "assistant", "content": "Remembered 17."}, {"role": "system", "content": [],
        "output_config": {"task_budget": {"type": "tokens", "total": 20000}}}, {"role": "user", "content": PROMPT}]},
        expected="reject" if aws else "observe", parameters=("messages[].output_config.task_budget",), docs=("effort", "task_budget"),
        beta="mid-conversation-output-config-2026-07-01,task-budgets-2026-03-13")
    compaction = {"edits": [{"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": 50000},
        "pause_after_compaction": False, "instructions": "Preserve all integers and the user's latest arithmetic request."}]}
    add("compaction_configuration", changes={"context_management": compaction}, expected="observe" if aws else "accept",
        parameters=("context_management.edits",), docs=("compaction", "aws_compaction") if aws else ("compaction",), beta="compact-2026-01-12",
        validation={"compaction_trigger_reached": False, "actual_compaction_verified": False})
    invalid_compaction = copy.deepcopy(compaction)
    invalid_compaction["edits"][0]["trigger"]["value"] = 49999
    add("compaction_trigger_below_minimum", changes={"context_management": invalid_compaction}, expected="observe" if aws else "reject",
        parameters=("context_management.edits[].trigger.value",), docs=("compaction",), beta="compact-2026-01-12")
    for strategy in ("clear_thinking_20251015", "clear_tool_uses_20250919"):
        edit = ({"type": strategy, "keep": {"type": "thinking_turns", "value": 1}} if strategy.startswith("clear_thinking") else
                {"type": strategy, "trigger": {"type": "input_tokens", "value": 100000},
                 "keep": {"type": "tool_uses", "value": 3}, "exclude_tools": ["record_product"], "clear_tool_inputs": False})
        add("context_" + strategy, changes={"context_management": {"edits": [edit]}}, expected="observe" if aws else "accept",
            parameters=("context_management.edits",), docs=("context",), beta="context-management-2025-06-27",
            validation={"context_edit_trigger_reached": False, "actual_context_edit_verified": False})

    # Preserve real signed opaque blocks.  Fabricated thinking or signature
    # strings would test authentication errors instead of prefix binding.
    add("preserved_thinking_seed", changes={"thinking": {"type": "adaptive", "display": "summarized"}, "output_config": {"effort": "high"},
        "messages": [{"role": "user", "content": "Compute 3 to the power 17. Check your arithmetic carefully, then reply with only the integer."}]},
        parameters=("thinking",), kind="thinking_seed", docs=("preserved",),
        validation={"expected_text": "129140163", "require_signed_thinking_for_dependencies": True, "thinking_content_in_public_facts": False})
    binding_field = "mismatch_behavior" if aws else "prefix_mismatch_behavior"
    for mismatch, changed_prefix in (("error", False), ("error", True), ("drop_block", True)):
        label = "preserved_" + mismatch + ("_changed_prefix" if changed_prefix else "_matching_prefix")
        expected = "observe" if aws else ("reject" if newer and mismatch == "error" and changed_prefix else "accept")
        add(label, changes={"thinking": {"type": "adaptive", "block_binding": {binding_field: mismatch}}}, expected=expected,
            parameters=("thinking.block_binding." + binding_field,), kind="preserved_thinking", docs=("preserved", "aws_binding") if aws else ("preserved",),
            beta="thinking-binding-controls-2026-08-01", depends=("preserved_thinking_seed",), validation={
                "dependency": {"case_id": prefix + "preserved_thinking_seed", "operation": "preserve_signed_thinking",
                    "change_system_prefix": changed_prefix, "followup_text": PROMPT, "preserve_all_assistant_content": True},
                "expected_text": ANSWER, "binding_behavior": mismatch, "exact_prefix_enforcement_documented": newer,
                "require_dropped_block_evidence": newer and mismatch == "drop_block" and not aws,
                "documented_wire_name_conflict": aws, "thinking_content_in_public_facts": False,
                "error_parameter_paths": ["thinking.block_binding." + binding_field, "messages[].content[].signature"],
                "error_concepts": ["thinking", "signature", "prefix", "mismatch"]})

    add("image_base64", changes={"messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64",
        "media_type": "image/png", "data": {"__fixture__": "blue_square_png_base64"}}},
        {"type": "text", "text": "Name the color of the large square. Reply with only the lowercase color name."}]}]},
        parameters=("messages[].content[].image",), validation={"expected_text": "blue",
            "fixture": {"kind": "synthetic_image", "format": "png", "width": 128, "height": 128, "color": "blue"}}, docs=("vision",))
    image_case = rows[-1]
    for format_name, media_type in (("jpeg", "image/jpeg"), ("gif", "image/gif"), ("webp", "image/webp")):
        body = copy.deepcopy(image_case["body"])
        body["messages"][0]["content"][0]["source"].update(media_type=media_type, data={"__fixture__": "blue_square_" + format_name + "_base64"})
        add("image_base64_" + format_name, changes=body, parameters=("messages[].content[].source.media_type",),
            validation={"expected_text": "blue", "fixture": {"kind": "synthetic_image", "format": format_name,
                "width": 128, "height": 128, "color": "blue"}}, docs=("vision",))
    for action in ("error", "downsize"):
        body = copy.deepcopy(image_case["body"])
        body["messages"][0]["content"][0]["transformations"] = {"oversized_image": action}
        add("image_oversized_" + action, changes=body, expected="observe" if aws else ("reject" if action == "error" else "accept"),
            parameters=("messages[].content[].transformations.oversized_image",), docs=("vision_coordinates",),
            validation={"expected_text": "blue", "fixture": {"kind": "synthetic_image", "format": "png",
                "width": 3000, "height": 128, "color": "blue"}, "documented_native_long_edge": 2576,
                "expected_transformation": action})
    pdf_body = {"messages": [{"role": "user", "content": [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
        "data": {"__fixture__": "synthetic_pdf_base64"}}}, {"type": "text", "text": "Copy the document's verification code exactly, with no other text."}]}]}
    add("pdf_base64", changes=pdf_body, expected="observe" if aws else "accept", parameters=("messages[].content[].document",),
        validation={"expected_text": "FABLE_MATRIX_323", "fixture": {"kind": "synthetic_pdf", "pages": 1, "text": "Verification code: FABLE_MATRIX_323"}}, docs=("pdf", "bedrock") if aws else ("pdf",))
    citations = copy.deepcopy(pdf_body)
    citations["messages"][0]["content"][0]["citations"] = {"enabled": True}
    citations["messages"][0]["content"][1]["text"] = "Give the document's verification code and cite the page containing it. Include no unrelated information."
    add("pdf_citations", changes=citations, expected="observe" if aws else "accept", parameters=("messages[].content[].citations",),
        kind="citations", validation={"fixture": {"kind": "synthetic_pdf", "pages": 1, "text": "Verification code: FABLE_MATRIX_323"},
            "expected_text_contains": "FABLE_MATRIX_323", "require_citation": True}, docs=("pdf",))
    add("citations_with_json_schema", changes={**citations, "output_config": json_body["output_config"]}, expected="observe" if aws else "reject",
        parameters=("output_config.format", "messages[].content[].citations"), docs=("structured", "pdf"),
        validation={"fixture": {"kind": "synthetic_pdf", "pages": 1, "text": "Verification code: FABLE_MATRIX_323"}})
    add("assistant_prefill", changes={"messages": [{"role": "user", "content": PROMPT}, {"role": "assistant", "content": "32"}]},
        expected="reject", parameters=("messages",), docs=("thinking",), validation={"rejection_reason": "assistant_prefill_unsupported"})
    add("image_media_type_invalid", changes={"messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64",
        "media_type": "image/invalid-format", "data": {"__fixture__": "blue_square_png_base64"}}}, {"type": "text", "text": PROMPT}]}]},
        expected="reject", parameters=("messages[].content[].source.media_type",), docs=("vision",),
        validation={"fixture": {"kind": "synthetic_image", "format": "png", "width": 128, "height": 128, "color": "blue"}})

    for source_type in ("text", "content"):
        document_source = ({"type": "text", "media_type": "text/plain", "data": "Verification code: FABLE_MATRIX_323"} if source_type == "text" else
                           {"type": "content", "content": [{"type": "text", "text": "Verification code: FABLE_MATRIX_323"}]})
        add("document_source_" + source_type, changes={"messages": [{"role": "user", "content": [{"type": "document",
            "source": document_source, "title": "Synthetic verification note", "context": "A synthetic parameter test fixture."},
            {"type": "text", "text": "Reply with the exact verification code from the document, without explanation."}]}]},
            expected="observe" if aws else "accept", parameters=("messages[].content[].source", "messages[].content[].title", "messages[].content[].context"),
            validation={"expected_text": "FABLE_MATRIX_323"}, docs=("pdf",))

    # These require external resource preparation or server-owned tools; a
    # runner may execute them only after implementing the explicit descriptor.
    platform_support = "unsupported" if aws else "supported"
    for name, block_type, fixture_url in (
        ("platform_image_url", "image", "https://platform.claude.com/docs/images/vision-example.jpg"),
        ("platform_document_url", "document", "https://assets.anthropic.com/m/1cd9d098ac3e6467/original/Claude-3-Model-Card-October-Addendum.pdf"),
    ):
        add(name, changes={"messages": [{"role": "user", "content": [{"type": block_type, "source": {"type": "url", "url": fixture_url}},
            {"type": "text", "text": {"__fixture__": "verified_url_fixture_question"}}]}]},
            expected="observe" if aws else "accept", parameters=("messages[].content[].source.url",), kind="platform_capability", docs=("bedrock", "vision" if block_type == "image" else "pdf"),
            validation={"documented_platform_support": platform_support, "requires_specialized_sender": True,
                "fixture": {"kind": "platform_feature", "feature": "url_" + block_type, "url": fixture_url,
                            "download_and_verify_before_freezing": True, "freeze_expected_semantics_from_actual_fixture": True},
                "baseline_response_cannot_prove_feature": True})
    for block_type in ("image", "document"):
        add("platform_file_" + block_type, changes={"messages": [{"role": "user", "content": [{"type": block_type,
            "source": {"type": "file", "file_id": {"__fixture__": "uploaded_" + block_type + "_file_id"}}},
            {"type": "text", "text": "Reply only the square's color." if block_type == "image" else "Reply only the verification code."}]}]},
            expected="observe" if aws else "accept", parameters=("messages[].content[].source.file_id",), kind="platform_capability", docs=("files", "bedrock"),
            validation={"documented_platform_support": platform_support, "requires_specialized_sender": True,
                "fixture": {"kind": "platform_feature", "feature": "files_api", "media": block_type,
                            "prepare_synthetic_fixture": True, "upload_and_cleanup_required": True, "never_fabricate_file_id": True},
                "expected_text": "blue" if block_type == "image" else "FABLE_MATRIX_323", "baseline_response_cannot_prove_feature": True})
    for version in ("20250825", "20260120", "20260521"):
        add("platform_code_execution_" + version, changes={"tools": [{"type": "code_execution_" + version, "name": "code_execution"}],
            "messages": [{"role": "user", "content": "Use the code execution tool to calculate 17*19, then reply with only the result. Do not read files or use network access."}]},
            expected="observe" if aws else "accept", parameters=("tools[type=code_execution_" + version + "]",),
            kind="platform_capability", docs=("code_execution", "bedrock"), validation={"documented_platform_support": platform_support,
                "requires_specialized_sender": True, "fixture": {"kind": "platform_feature", "feature": "code_execution", "tool_version": version},
                "expected_text": ANSWER, "require_server_tool_result": True, "baseline_response_cannot_prove_feature": True})
    for search in ("regex", "bm25"):
        add("platform_tool_search_" + search, changes={"tools": [
            {"type": "tool_search_tool_" + search + "_20251119", "name": "tool_search_tool_" + search},
            {**RECORD_TOOL, "defer_loading": True}], "messages": [{"role": "user", "content":
                "Find the record_product tool and call it with the product of 17 and 19."}]},
            expected="observe" if aws else "accept", parameters=("tools[type=tool_search_tool]", "tools[].defer_loading"),
            kind="platform_capability", docs=("tool_search", "bedrock"), validation={"documented_platform_support": "unknown" if aws else "supported",
                "requires_specialized_sender": True, "fixture": {"kind": "platform_feature", "feature": "tool_search", "search": search},
                "tool_name": "record_product", "expected_arguments": {"product": 323}, "require_matching_tool_reference": True,
                "baseline_response_cannot_prove_feature": True})
    for feature, tool_type, domain in (("web_search", "web_search_20260318", "platform.claude.com"),
                                       ("web_fetch", "web_fetch_20260318", "platform.claude.com")):
        tool = {"type": tool_type, "name": feature, "max_uses": 1, "allowed_domains": [domain]}
        if feature == "web_fetch":
            tool.update(max_content_tokens=2048, citations={"enabled": True})
        add("platform_" + feature, changes={"tools": [tool], "messages": [{"role": "user", "content":
            "Use " + feature + " to inspect https://platform.claude.com/docs/en/api/messages/create and name its main API endpoint briefly."}]},
            expected="observe", parameters=("tools[type=" + tool_type + "]",), kind="platform_capability", docs=(feature, "bedrock"),
            validation={"documented_platform_support": "unsupported" if aws else "model_version_acceptance_to_verify",
                "requires_specialized_sender": True, "fixture": {"kind": "platform_feature", "feature": feature},
                "maximum_server_tool_uses": 1, "require_server_tool_result": True, "tool_errors_inside_200_are_not_success": True,
                "baseline_response_cannot_prove_feature": True})
    add("platform_fallbacks", changes={"fallbacks": {"__fixture__": "discovered_allowed_fallbacks"}},
        expected="observe" if aws else "accept", parameters=("fallbacks",), kind="platform_capability", docs=("fallback", "bedrock"),
        beta="server-side-fallback-2026-07-01", validation={"documented_platform_support": platform_support,
            "requires_specialized_sender": True, "fixture": {"kind": "platform_feature", "feature": "fallbacks",
                "discover_allowed_targets_first": True, "allowed_model_scope": list(MODELS), "exclude_primary": model,
                "maximum_entries": 1, "each_fallback_max_tokens": OUTPUT_ALLOWANCE}, "expected_text": ANSWER,
            "fallback_was_triggered_required_for_effect_proof": True, "baseline_response_cannot_prove_feature": True})
    for feature, beta in (("advisor", "advisor-tool-2026-03-01"), ("browser", None), ("computer", None), ("mcp", "mcp-client-2025-11-20")):
        for variant in ("positive", "invalid_schema"):
            body = {"tools": [{"__fixture__": feature + "_tool_definition"}], "messages": [{"role": "user", "content":
                {"__fixture__": feature + "_synthetic_test_question"}}]}
            if feature == "mcp":
                body["mcp_servers"] = {"__fixture__": "mcp_servers"}
            add("platform_" + feature + "_" + variant, changes=body,
                expected="observe" if aws else ("accept" if variant == "positive" else "reject"), parameters=("tools", "mcp_servers") if feature == "mcp" else ("tools",),
                kind="platform_capability", docs=(feature, "bedrock"), beta=beta,
                validation={"documented_platform_support": "unsupported" if aws and feature == "advisor" else "unknown" if aws else "supported",
                    "requires_specialized_sender": True, "fixture": {"kind": "platform_feature", "feature": feature, "variant": variant,
                        "external_fixture_required": feature == "mcp" and variant == "positive",
                        "schema_only_not_execution": feature == "mcp" and variant == "invalid_schema",
                        "maximum_advisor_output_tokens": OUTPUT_ALLOWANCE if feature == "advisor" else None,
                        "allowed_advisor_models": [model] if feature == "advisor" else None},
                    "tool_execution": "server" if feature in {"advisor", "mcp"} else "declaration_and_call_only_no_client_action",
                    "baseline_response_cannot_prove_feature": True})

    ids = [row["case_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise AssertionError("Duplicate case ID")
    seen: set[str] = set()
    for row in rows:
        if any(dep not in seen for dep in row.get("depends_on", [])):
            raise AssertionError("Case dependency must precede the dependent case")
        seen.add(row["case_id"])
        if type(row["body"].get("max_tokens")) is not int or row["body"]["max_tokens"] < 256:
            raise AssertionError("Every wire body must retain a bounded output allowance")
    return rows
