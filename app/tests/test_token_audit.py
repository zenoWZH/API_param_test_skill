from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.token_audit import (
    audit_exchange,
    audit_image_usage,
    combine_exchange_audits,
    flatten_token_audits,
    normalize_usage,
    summarize_token_audits,
    token_range,
)


CONFIG = {
    "test_cases": {
        "token_accuracy": {
            "enabled": True,
            "relative_tolerance": 0.50,
            "input_absolute_tolerance": 16,
            "output_absolute_tolerance": 8,
        }
    }
}


def _result(usage: dict, response_json: dict, *, complete: bool = True) -> SimpleNamespace:
    response_json = deepcopy(response_json)
    if complete:
        if "choices" in response_json:
            for choice in response_json["choices"]:
                choice.setdefault("finish_reason", "stop")
        elif "candidates" in response_json:
            for candidate in response_json["candidates"]:
                candidate.setdefault("finishReason", "STOP")
        elif "content" in response_json:
            response_json.setdefault("stop_reason", "end_turn")
        elif "output" in response_json or "steps" in response_json:
            response_json.setdefault("status", "completed")
    return SimpleNamespace(usage=usage, response_json=response_json)


def test_chat_usage_uses_prompt_and_completion_and_treats_details_as_advisory() -> None:
    usage = normalize_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 221,
            "total_tokens": 231,
            "input_tokens": 888,
            "output_tokens": 777,
            "completion_tokens_details": {"reasoning_tokens": 172},
        },
        "chat_completions",
    )

    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 221
    assert usage["total_tokens"] == 231
    assert usage["answer_tokens"] is None
    assert usage["thinking_tokens"] is None
    assert usage["details_advisory"] == {
        "input_tokens": 888,
        "output_tokens": 777,
        "reasoning_tokens": 172,
        "provider_total_tokens": 231,
    }
    assert usage["errors"] == []


def test_claude_output_is_inclusive_and_not_double_counted() -> None:
    usage = normalize_usage(
        {
            "input_tokens": 10,
            "cache_creation_input_tokens": 5,
            "cache_read_input_tokens": 7,
            "output_tokens": 60,
            "thinking_tokens": 20,
        },
        "claude_messages",
    )

    assert usage["input_tokens"] == 22
    assert usage["answer_tokens"] == 40
    assert usage["thinking_tokens"] == 20
    assert usage["output_tokens"] == 60
    assert usage["total_tokens"] == 82


def test_gemini_output_adds_candidates_and_thoughts_once() -> None:
    usage = normalize_usage(
        {
            "promptTokenCount": 20,
            "candidatesTokenCount": 73,
            "thoughtsTokenCount": 40,
            "totalTokenCount": 133,
        },
        "gemini_generate_content",
    )

    assert usage["answer_tokens"] == 73
    assert usage["thinking_tokens"] == 40
    assert usage["output_tokens"] == 113
    assert usage["total_tokens"] == 133


def test_gemini_interactions_output_adds_thoughts_once() -> None:
    usage = normalize_usage(
        {
            "total_input_tokens": 7,
            "total_output_tokens": 20,
            "total_thought_tokens": 22,
            "total_tokens": 49,
            "total_cached_tokens": 3,
        },
        "gemini_interactions",
    )

    assert usage["input_tokens"] == 7
    assert usage["answer_tokens"] == 20
    assert usage["thinking_tokens"] == 22
    assert usage["output_tokens"] == 42
    assert usage["total_tokens"] == 49
    assert usage["cache_tokens"] == 3


def test_openai_responses_and_image_usage_variants_are_normalized() -> None:
    responses = normalize_usage(
        {
            "input_tokens": 20,
            "output_tokens": 30,
            "total_tokens": 50,
            "input_tokens_details": {"cached_tokens": 5},
            "output_tokens_details": {"reasoning_tokens": 10},
        },
        "openai_responses",
    )
    image = normalize_usage(
        {
            "input_tokens": 11,
            "output_tokens": 272,
            "total_tokens": 283,
            "output_tokens_details": {"image_tokens": 250},
        },
        "image_generation",
    )

    assert responses["cache_tokens"] == 5
    assert responses["thinking_tokens"] == 10
    assert responses["answer_tokens"] == 20
    assert image["image_tokens"] == 250
    assert image["image_token_scope"] == "output"
    assert image["total_tokens"] == 283

    responses_audit = audit_exchange(
        {"model": "m", "input": "hello"},
        _result(
            {"input_tokens": 20, "output_tokens": 2, "total_tokens": 22},
            {
                "object": "response",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "OK"}],
                    }
                ],
            },
        ),
        "openai_responses",
        CONFIG,
        "initial",
    )
    assert responses_audit["output"]["estimated_answer_tokens"] > 0


def test_image_token_subitem_arithmetic_is_strict_but_pixel_count_is_not_a_token_oracle() -> None:
    audit = audit_image_usage(
        {"model": "image-model", "prompt": "draw a square"},
        {"model": "image-model", "data": [{"b64_json": "..."}]},
        {
            "input_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 30,
            "output_tokens_details": {"image_tokens": 21},
        },
        CONFIG,
        provider=None,
        model="image-model",
    )
    exchange = audit["exchanges"][0]

    assert exchange["usage_arithmetic"]["status"] == "fail"
    assert "image tokens exceed output tokens" in exchange["usage_arithmetic"]["errors"]
    assert exchange["output_accuracy"]["status"] == "not_available"


def test_input_image_token_subitem_is_checked_against_input_not_output() -> None:
    audit = audit_image_usage(
        {"model": "image-model", "prompt": "edit the attached image"},
        {"data": [{"b64_json": "..."}]},
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_tokens_details": {"image_tokens": 50},
        },
        CONFIG,
        provider=None,
        model="image-model",
    )

    assert audit["exchanges"][0]["usage_arithmetic"]["status"] == "pass"


def test_fifty_percent_interval_and_absolute_floor_include_boundaries() -> None:
    assert token_range(200, 0.50, 16) == {"min": 100, "max": 300}
    assert token_range(4, 0.50, 8) == {"min": 0, "max": 12}


def test_short_answer_with_high_completion_usage_is_partial() -> None:
    result = _result(
        {"prompt_tokens": 10, "completion_tokens": 50},
        {"choices": [{"message": {"content": "OK"}}]},
    )
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "x"}]},
        result,
        "chat_completions",
        CONFIG,
        "initial",
    )

    assert audit["output"]["short_reply"] is True
    assert audit["output"]["total_status"] == "partial"
    assert audit["output"]["status"] == "partial"


def test_hidden_thinking_is_included_but_only_partially_verifiable() -> None:
    result = _result(
        {"input_tokens": 10, "output_tokens": 60, "thinking_tokens": 20},
        {"content": [{"type": "text", "text": "a" * 160}]},
    )
    audit = audit_exchange(
        {
            "messages": [{"role": "user", "content": "x"}],
            "thinking": {"type": "enabled", "budget_tokens": 1024},
        },
        result,
        "claude_messages",
        CONFIG,
        "initial",
    )

    assert audit["usage_accounting"]["output_tokens"] == 60
    assert audit["usage_accounting"]["answer_tokens"] == 40
    assert audit["output"]["thinking_visibility"] == "none"
    assert audit["output"]["status"] == "partial"


def test_long_visible_output_mismatch_is_fail() -> None:
    result = _result(
        {"prompt_tokens": 10, "completion_tokens": 80},
        {"choices": [{"message": {"content": "a" * 100}}]},
    )
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "x"}]},
        result,
        "chat_completions",
        CONFIG,
        "initial",
    )

    assert audit["output"]["short_reply"] is False
    assert audit["output"]["status"] == "fail"


def test_advisory_reasoning_can_explain_hidden_high_side_without_becoming_authoritative() -> None:
    result = _result(
        {
            "prompt_tokens": 10,
            "completion_tokens": 80,
            "completion_tokens_details": {"reasoning_tokens": 55},
        },
        {"choices": [{"message": {"content": "a" * 100}}]},
    )
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "x"}]},
        result,
        "chat_completions",
        CONFIG,
        "initial",
    )

    assert audit["usage_accounting"]["thinking_tokens"] is None
    assert audit["output"]["advisory_details"]["reasoning_tokens"] == 55
    assert audit["output"]["thinking_detected"] is True
    assert audit["output"]["status"] == "partial"
    summary = summarize_token_audits([{"token_audit": combine_exchange_audits([audit])}])
    assert summary["thinking_tokens"] is None
    assert summary["advisory_thinking_tokens"] == 55


def test_visible_compatible_reasoning_is_partial_when_split_is_only_advisory() -> None:
    result = _result(
        {
            "prompt_tokens": 10,
            "completion_tokens": 50,
            "completion_tokens_details": {"reasoning_tokens": 25},
        },
        {
            "choices": [
                {
                    "message": {
                        "content": "a" * 100,
                        "reasoning_content": "b" * 100,
                    }
                }
            ]
        },
    )
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "x"}]},
        result,
        "chat_completions",
        CONFIG,
        "initial",
    )

    assert audit["output"]["total_status"] == "pass"
    assert audit["output"]["answer_status"] == "not_available"
    assert audit["output"]["thinking_status"] == "not_available"
    assert audit["output"]["status"] == "partial"


def test_stream_final_usage_payload_is_audited_normally() -> None:
    result = _result(
        {"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16},
        {
            "choices": [
                {
                    "message": {"content": "a" * 20, "reasoning_content": ""},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16},
        },
    )
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "x"}], "stream": True},
        result,
        "chat_completions",
        CONFIG,
        "initial",
    )

    assert audit["usage_accounting"]["output_tokens"] == 6
    assert audit["output"]["status"] == "pass"


def test_all_candidates_are_included_in_visible_output_estimate() -> None:
    result = _result(
        {"prompt_tokens": 10, "completion_tokens": 50},
        {
            "choices": [
                {"message": {"content": "a" * 100}},
                {"message": {"content": "b" * 100}},
            ]
        },
    )
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "x"}], "n": 2},
        result,
        "chat_completions",
        CONFIG,
        "initial",
    )

    assert audit["output"]["estimated_answer_tokens"] == 54
    assert audit["output"]["status"] == "pass"


def test_contradictory_thinking_breakdown_is_fail() -> None:
    result = _result(
        {"input_tokens": 10, "output_tokens": 20, "thinking_tokens": 21},
        {"content": [{"type": "text", "text": "answer"}]},
    )
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "x"}]},
        result,
        "claude_messages",
        CONFIG,
        "initial",
    )

    assert audit["output"]["status"] == "fail"
    assert "exceed" in audit["output"]["note"]


def test_missing_usage_is_not_available_and_excluded_from_pass_rate() -> None:
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "x"}]},
        _result({}, {"choices": [{"message": {"content": "answer"}}]}),
        "chat_completions",
        CONFIG,
        "initial",
    )
    result = {"name": "p:m:r:p:run_1", "profile": "p", "token_audit": combine_exchange_audits([audit])}
    summary = summarize_token_audits([result])

    assert audit["status"] == "not_available"
    assert summary["eligible_dimensions"] == 0
    assert summary["pass_rate"] is None
    assert audit["usage_presence"]["status"] == "fail"
    assert audit["validation_pass"] is False
    assert summary["mismatch_count"] == 1
    assert summary["missing_usage_count"] == 1
    assert summary["pass"] is False


def test_usage_rejects_non_integer_token_counts_without_crashing() -> None:
    for invalid in ("10", 10.5, float("inf"), True):
        audit = audit_exchange(
            {"messages": [{"role": "user", "content": "x"}]},
            _result(
                {
                    "prompt_tokens": invalid,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
                {"choices": [{"message": {"content": "answer"}}]},
            ),
            "chat_completions",
            CONFIG,
            "initial",
        )

        assert audit["usage_arithmetic"]["status"] == "fail"
        assert audit["usage_arithmetic"]["errors"] == [
            "usage.prompt_tokens must be a non-negative integer"
        ]


def test_null_optional_usage_detail_is_treated_as_absent() -> None:
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "hello"}]},
        _result(
            {
                "prompt_tokens": 9,
                "completion_tokens": 2,
                "total_tokens": 11,
                "prompt_tokens_details": {"multimodal_tokens": None},
            },
            {"choices": [{"message": {"content": "OK"}}]},
        ),
        "chat_completions",
        CONFIG,
        "initial",
    )

    assert audit["usage_arithmetic"]["status"] == "pass"
    assert audit["validation_pass"] is True


def test_summary_and_flat_report_keep_initial_and_followup_separate() -> None:
    initial = {
        "exchange": "initial",
        "status": "pass",
        "input": {"status": "pass"},
        "output": {"status": "pass"},
        "usage_accounting": {
            "input_tokens": 10,
            "answer_tokens": 40,
            "thinking_tokens": 20,
            "output_tokens": 60,
            "total_tokens": 70,
        },
    }
    followup = {
        "exchange": "followup",
        "status": "partial",
        "input": {"status": "pass"},
        "output": {"status": "partial"},
        "usage_accounting": {
            "input_tokens": 20,
            "answer_tokens": 10,
            "thinking_tokens": 0,
            "output_tokens": 10,
            "total_tokens": 30,
        },
    }
    result = {
        "name": "p:m:r:p:run_1",
        "profile": "p",
        "run_index": 1,
        "token_audit": combine_exchange_audits([initial, followup]),
    }

    summary = summarize_token_audits([result])
    flat = flatten_token_audits([result])

    assert [row["exchange"] for row in flat] == ["initial", "followup"]
    assert summary["exchange_count"] == 2
    assert summary["thinking_tokens"] == 20
    assert summary["output_tokens"] == 70
    assert summary["thinking_share"] == 20 / 70
    assert summary["partial_dimensions"] == 1


def test_exact_independent_counts_gate_zero_delta(monkeypatch) -> None:
    monkeypatch.setattr(
        "lib.token_audit.count_semantic_tokens",
        lambda *_args, **_kwargs: {
            "source": "test-tokenizer",
            "kind": "tokenizer_json",
            "input": {"tokens": 10, "evidence_level": "exact", "note": None},
            "output": {"tokens": 6, "evidence_level": "exact", "note": None},
        },
    )
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "hello"}]},
        _result(
            {"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16},
            {"choices": [{"message": {"content": "answer"}}]},
        ),
        "chat_completions",
        CONFIG,
        "initial",
    )

    assert audit["schema_version"] == 4
    assert audit["input_accuracy"]["status"] == "pass"
    assert audit["output_accuracy"]["status"] == "pass"
    assert audit["status"] == "pass"


def test_exact_mismatch_and_usage_arithmetic_block_token_gate(monkeypatch) -> None:
    monkeypatch.setattr(
        "lib.token_audit.count_semantic_tokens",
        lambda *_args, **_kwargs: {
            "source": "test-tokenizer",
            "kind": "tokenizer_json",
            "input": {"tokens": 9, "evidence_level": "exact", "note": None},
            "output": {"tokens": 6, "evidence_level": "exact", "note": None},
        },
    )
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "hello"}]},
        _result(
            {"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 99},
            {"choices": [{"message": {"content": "answer"}}]},
        ),
        "chat_completions",
        CONFIG,
        "initial",
    )
    summary = summarize_token_audits(
        [{"token_audit": combine_exchange_audits([audit])}]
    )

    assert audit["input_accuracy"]["delta"] == 1
    assert audit["input_accuracy"]["status"] == "fail"
    assert audit["usage_arithmetic"]["status"] == "fail"
    assert summary["pass"] is False


def test_xai_chat_reasoning_outside_completion_explains_total_gap() -> None:
    """xAI Chat: total = prompt + completion + reasoning_tokens (reasoning not in completion)."""
    usage = normalize_usage(
        {
            "prompt_tokens": 208,
            "completion_tokens": 1,
            "total_tokens": 224,
            "completion_tokens_details": {"reasoning_tokens": 15},
        },
        "chat_completions",
        additive_chat_reasoning=True,
    )
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "Say OK"}]},
        _result(
            {
                "prompt_tokens": 208,
                "completion_tokens": 1,
                "total_tokens": 224,
                "completion_tokens_details": {"reasoning_tokens": 15},
            },
            {"choices": [{"message": {"content": "OK"}}]},
        ),
        "chat_completions",
        CONFIG,
        "initial",
        accounting_source_id="xai",
        accounting_contract_id="grok_chat_completions",
    )
    assert usage["details_advisory"]["reasoning_tokens"] == 15
    assert usage["output_tokens"] == 16
    assert usage["thinking_tokens"] == 15
    assert audit["usage_arithmetic"]["status"] == "pass"
    assert audit["usage_arithmetic"]["errors"] == []


def test_additive_chat_reasoning_cannot_be_claimed_without_xai_mpdb_source() -> None:
    raw_usage = {
        "prompt_tokens": 2,
        "completion_tokens": 1,
        "total_tokens": 1_000_003,
        "completion_tokens_details": {"reasoning_tokens": 1_000_000},
    }
    result = _result(
        raw_usage,
        {"choices": [{"message": {"content": "OK"}}]},
    )
    request = {
        "messages": [{"role": "user", "content": "Say OK"}],
        "max_tokens": 16,
    }

    unbound = audit_exchange(
        request, result, "chat_completions", CONFIG, "initial"
    )
    xai_bound = audit_exchange(
        request,
        result,
        "chat_completions",
        CONFIG,
        "initial",
        accounting_source_id="xai",
        accounting_contract_id="grok_chat_completions",
    )

    assert unbound["usage_arithmetic"]["status"] == "fail"
    assert unbound["validation_pass"] is False
    assert xai_bound["usage_accounting"]["output_tokens"] == 1_000_001
    assert xai_bound["gross_plausibility"]["output"]["status"] == "fail"
    assert xai_bound["validation_pass"] is False


def test_estimate_is_displayed_but_never_produces_accuracy_pass(monkeypatch) -> None:
    monkeypatch.setattr(
        "lib.token_audit.count_semantic_tokens",
        lambda *_args, **_kwargs: {
            "source": "approx",
            "kind": "test",
            "input": {"tokens": 10, "evidence_level": "estimate", "note": "approx"},
            "output": {"tokens": 6, "evidence_level": "estimate", "note": "approx"},
        },
    )
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "hello"}]},
        _result(
            {"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16},
            {"choices": [{"message": {"content": "answer"}}]},
        ),
        "chat_completions",
        CONFIG,
        "initial",
    )

    assert audit["input_accuracy"]["independent_tokens"] == 10
    assert audit["input_accuracy"]["status"] == "not_available"
    assert audit["output_accuracy"]["status"] == "not_available"
    assert audit["status"] == "partial"


def test_grossly_wrong_but_self_consistent_usage_blocks_validation() -> None:
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 16},
        _result(
            {
                "prompt_tokens": 1_000_000,
                "completion_tokens": 1_000_000,
                "total_tokens": 2_000_000,
            },
            {"choices": [{"message": {"content": "OK"}}]},
        ),
        "chat_completions",
        CONFIG,
        "initial",
    )
    summary = summarize_token_audits(
        [{"status_code": 200, "token_audit": combine_exchange_audits([audit])}]
    )

    assert audit["usage_arithmetic"]["status"] == "pass"
    assert audit["gross_plausibility"]["status"] == "fail"
    assert audit["validation_pass"] is False
    assert summary["gross_failure_count"] == 1
    assert summary["pass"] is False


@pytest.mark.parametrize(
    ("transport", "body", "usage", "response"),
    [
        (
            "openai_responses",
            {"input": "hello", "max_output_tokens": 16},
            {
                "input_tokens": 1_000_000,
                "output_tokens": 1_000_000,
                "total_tokens": 2_000_000,
            },
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "OK"}],
                    }
                ]
            },
        ),
        (
            "claude_messages",
            {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 16},
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
            {"content": [{"type": "text", "text": "OK"}]},
        ),
        (
            "gemini_generate_content",
            {
                "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
                "generationConfig": {"maxOutputTokens": 16},
            },
            {
                "promptTokenCount": 1_000_000,
                "candidatesTokenCount": 1_000_000,
                "totalTokenCount": 2_000_000,
            },
            {
                "candidates": [
                    {"content": {"parts": [{"text": "OK"}]}}
                ]
            },
        ),
        (
            "gemini_interactions",
            {"input": "hello", "generation_config": {"max_output_tokens": 16}},
            {
                "total_input_tokens": 1_000_000,
                "total_output_tokens": 1_000_000,
                "total_thought_tokens": 0,
                "total_tokens": 2_000_000,
            },
            {"steps": [{"type": "model_output", "content": "OK"}]},
        ),
        (
            "fim_completions",
            {"prompt": "hel", "suffix": "lo", "max_tokens": 16},
            {
                "prompt_tokens": 1_000_000,
                "completion_tokens": 1_000_000,
                "total_tokens": 2_000_000,
            },
            {"choices": [{"text": "OK"}]},
        ),
    ],
)
def test_every_text_transport_blocks_grossly_wrong_usage(
    transport, body, usage, response
) -> None:
    audit = audit_exchange(
        body,
        _result(usage, response),
        transport,
        CONFIG,
        "initial",
    )

    assert audit["gross_plausibility"]["status"] == "fail"
    assert audit["validation_pass"] is False


def test_non_successful_exchange_explicitly_exempts_missing_usage() -> None:
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "hello"}]},
        _result({}, {"error": {"message": "unsupported"}}),
        "chat_completions",
        CONFIG,
        "initial",
        usage_required=False,
    )
    summary = summarize_token_audits(
        [{"status_code": 400, "token_audit": combine_exchange_audits([audit])}]
    )

    assert audit["usage_presence"]["status"] == "not_applicable"
    assert audit["validation_pass"] is True
    assert summary["validation_status"] == "not_applicable"
    assert summary["pass"] is True


def test_disabled_audit_fails_closed_for_successful_exchange() -> None:
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "hello"}]},
        _result(
            {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            {"choices": [{"message": {"content": "OK"}}]},
        ),
        "chat_completions",
        {"test_cases": {"token_accuracy": {"enabled": False}}},
        "initial",
    )

    assert audit["validation_status"] == "fail"
    assert audit["validation_pass"] is False


def test_success_result_with_empty_audit_is_not_silently_accepted() -> None:
    combined = combine_exchange_audits([])
    summary = summarize_token_audits(
        [
            {
                "status_code": 200,
                "status": "pass",
                "token_audit": combined,
            }
        ]
    )

    assert combined["validation_pass"] is False
    assert summary["missing_audit_result_count"] == 1
    assert summary["validation_status"] == "fail"
    assert summary["pass"] is False


def test_combined_audit_rejects_unversioned_exchange() -> None:
    combined = combine_exchange_audits(
        [{"validation_status": "pass", "validation_pass": True}]
    )

    assert combined["validation_status"] == "fail"
    assert combined["validation_pass"] is False


def test_summary_rejects_unversioned_extra_exchange() -> None:
    current = audit_exchange(
        {"messages": [{"role": "user", "content": "hello"}]},
        _result(
            {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            {"choices": [{"message": {"content": "OK"}}]},
        ),
        "chat_completions",
        CONFIG,
        "initial",
    )
    combined = combine_exchange_audits(
        [current, {"validation_status": "pass", "validation_pass": True}]
    )
    summary = summarize_token_audits(
        [{"status_code": 200, "token_audit": combined}]
    )

    assert summary["invalid_audit_result_count"] == 1
    assert summary["validation_status"] == "fail"
    assert summary["pass"] is False


def test_nonempty_visible_output_cannot_report_zero_tokens() -> None:
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "hello"}]},
        _result(
            {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
            {"choices": [{"message": {"content": "visible answer"}}]},
        ),
        "chat_completions",
        CONFIG,
        "initial",
    )

    assert audit["gross_plausibility"]["output"]["status"] == "fail"
    assert audit["validation_pass"] is False


def test_nontrivial_input_cannot_be_underreported_by_more_than_gross_ratio() -> None:
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "a" * 400}]},
        _result(
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            {"choices": [{"message": {"content": "OK"}}]},
        ),
        "chat_completions",
        CONFIG,
        "initial",
    )

    input_check = audit["gross_plausibility"]["input"]
    assert input_check["estimated_tokens"] >= 100
    assert input_check["minimum_plausible_tokens"] >= 3
    assert input_check["status"] == "fail"
    assert audit["validation_pass"] is False


def test_opaque_previous_response_context_is_unverified_and_cannot_pass() -> None:
    audit = audit_exchange(
        {
            "input": "continue",
            "previous_response_id": "resp_opaque",
            "max_output_tokens": 64,
        },
        _result(
            {"input_tokens": 50_000, "output_tokens": 2, "total_tokens": 50_002},
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "OK"}],
                    }
                ]
            },
        ),
        "openai_responses",
        CONFIG,
        "initial",
    )

    assert audit["gross_plausibility"]["input"]["status"] == "partial"
    assert audit["validation_status"] == "fail"
    assert audit["validation_pass"] is False


def test_request_output_limit_still_rejects_absurd_hidden_reasoning_total() -> None:
    audit = audit_exchange(
        {
            "input": "answer briefly",
            "max_output_tokens": 16,
            "reasoning": {"effort": "minimal"},
        },
        _result(
            {"input_tokens": 10, "output_tokens": 1_000_000, "total_tokens": 1_000_010},
            {
                "output": [
                    {"type": "reasoning", "encrypted_content": "opaque"},
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "OK"}],
                    },
                ]
            },
        ),
        "openai_responses",
        CONFIG,
        "initial",
    )

    assert audit["output"]["thinking_visibility"] == "hidden"
    assert audit["output"]["estimated_visible_thinking_tokens"] == 0
    assert audit["gross_plausibility"]["output"]["status"] == "fail"
    assert audit["validation_pass"] is False


def test_visible_output_cannot_excuse_an_ignored_request_limit() -> None:
    visible = "word " * 700
    audit = audit_exchange(
        {
            "messages": [{"role": "user", "content": "write a long answer"}],
            "max_tokens": 16,
        },
        _result(
            {"prompt_tokens": 12, "completion_tokens": 700, "total_tokens": 712},
            {"choices": [{"message": {"content": visible}}]},
        ),
        "chat_completions",
        CONFIG,
        "initial",
    )

    assert audit["gross_plausibility"]["output"]["request_limit_maximum_tokens"] == 16
    assert audit["gross_plausibility"]["output"]["status"] == "fail"
    assert audit["validation_pass"] is False


def test_generate_content_keeps_tool_use_tokens_separate_from_total() -> None:
    usage = normalize_usage(
        {
            "promptTokenCount": 20,
            "candidatesTokenCount": 10,
            "thoughtsTokenCount": 5,
            "toolUsePromptTokenCount": 100,
            "totalTokenCount": 35,
        },
        "gemini_generate_content",
    )

    assert usage["tool_use_tokens"] == 100
    assert usage["output_tokens"] == 15
    assert usage["total_tokens"] == 35
    assert usage["errors"] == []


def test_interactions_function_call_contributes_visible_output_estimate() -> None:
    audit = audit_exchange(
        {"input": "call weather", "generation_config": {"max_output_tokens": 64}},
        _result(
            {
                "total_input_tokens": 8,
                "total_output_tokens": 12,
                "total_thought_tokens": 0,
                "total_tokens": 20,
            },
            {
                "steps": [
                    {
                        "type": "function_call",
                        "name": "get_weather",
                        "arguments": {"city": "Shanghai"},
                    }
                ]
            },
        ),
        "gemini_interactions",
        CONFIG,
        "initial",
    )

    assert audit["output"]["estimated_answer_tokens"] > 0


def test_interactions_modality_details_are_preserved_and_bounded() -> None:
    usage = normalize_usage(
        {
            "total_input_tokens": 12,
            "total_output_tokens": 20,
            "total_thought_tokens": 2,
            "total_tokens": 34,
            "input_tokens_by_modality": [
                {"modality": "text", "tokens": 4},
                {"modality": "image", "tokens": 8},
            ],
            "output_tokens_by_modality": [
                {"modality": "text", "tokens": 3},
                {"modality": "image", "tokens": 17},
            ],
        },
        "gemini_interactions",
    )
    audit = audit_exchange(
        {"input": "draw"},
        _result(usage["raw_usage"], {"steps": []}),
        "gemini_interactions",
        CONFIG,
        "initial",
        output_plausibility_supported=False,
    )

    assert usage["input_image_tokens"] == 8
    assert usage["output_image_tokens"] == 17
    assert audit["usage_arithmetic"]["status"] == "pass"


def test_interactions_modality_detail_cannot_exceed_parent() -> None:
    audit = audit_exchange(
        {"input": "draw"},
        _result(
            {
                "total_input_tokens": 12,
                "total_output_tokens": 20,
                "total_thought_tokens": 0,
                "total_tokens": 32,
                "output_tokens_by_modality": [
                    {"modality": "image", "tokens": 21}
                ],
            },
            {"steps": []},
        ),
        "gemini_interactions",
        CONFIG,
        "initial",
        output_plausibility_supported=False,
    )

    assert audit["usage_arithmetic"]["status"] == "fail"
    assert audit["validation_pass"] is False


def test_image_usage_preserves_and_validates_both_detail_sides() -> None:
    audit = audit_image_usage(
        {"model": "image-model", "prompt": "edit image"},
        {"data": [{"b64_json": "..."}]},
        {
            "input_tokens": 100,
            "output_tokens": 200,
            "total_tokens": 300,
            "input_tokens_details": {"text_tokens": 20, "image_tokens": 80},
            "output_tokens_details": {"text_tokens": 10, "image_tokens": 190},
        },
        CONFIG,
        provider=None,
        model="image-model",
    )
    exchange = audit["exchanges"][0]

    assert exchange["reported"]["input_image_tokens"] == 80
    assert exchange["reported"]["output_image_tokens"] == 190
    assert exchange["usage_arithmetic"]["status"] == "pass"
    assert exchange["gross_plausibility"]["output"]["status"] == "not_available"
    assert exchange["validation_pass"] is False


def test_successful_image_without_input_output_usage_fails_validation() -> None:
    audit = audit_image_usage(
        {"model": "image-model", "prompt": "draw image"},
        {"data": [{"b64_json": "..."}]},
        {"cost_in_usd_ticks": 1},
        CONFIG,
        provider=None,
        model="image-model",
    )

    assert audit["validation_pass"] is False
    assert audit["exchanges"][0]["usage_presence"]["status"] == "fail"


def test_nonempty_image_cannot_report_zero_output_tokens() -> None:
    audit = audit_image_usage(
        {"model": "image-model", "prompt": "draw image"},
        {"data": [{"b64_json": "aW1hZ2U="}]},
        {"input_tokens": 3, "output_tokens": 0, "total_tokens": 3},
        CONFIG,
        provider=None,
        model="image-model",
    )

    exchange = audit["exchanges"][0]
    assert exchange["gross_plausibility"]["output"]["status"] == "fail"
    assert exchange["validation_pass"] is False
    assert audit["validation_pass"] is False


def test_chat_image_url_output_cannot_report_zero_output_tokens() -> None:
    audit = audit_image_usage(
        {"model": "image-model", "messages": [{"role": "user", "content": "draw"}]},
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "images": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64,aW1hZ2U="
                                },
                            }
                        ],
                    }
                }
            ]
        },
        {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
        CONFIG,
        provider=None,
        model="image-model",
        transport="chat_completions",
    )

    exchange = audit["exchanges"][0]
    assert exchange["gross_plausibility"]["output"]["status"] == "fail"
    assert audit["validation_pass"] is False


def test_empty_token_audit_summary_fails_closed() -> None:
    summary = summarize_token_audits([])

    assert summary["exchange_count"] == 0
    assert summary["validation_status"] == "fail"
    assert summary["validation_failure_count"] == 1
    assert summary["pass"] is False


def test_nonfinite_or_unbounded_threshold_config_falls_back_safely() -> None:
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "hello"}]},
        _result(
            {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            {"choices": [{"message": {"content": "OK"}}]},
        ),
        "chat_completions",
        {
            "test_cases": {
                "token_accuracy": {
                    "enabled": True,
                    "relative_tolerance": float("inf"),
                    "gross_input_ratio": 1_000_000,
                    "gross_output_absolute_tolerance": 10_000_000,
                }
            }
        },
        "initial",
    )

    assert audit["settings"]["relative_tolerance"] == 0.5
    assert audit["settings"]["gross_input_ratio"] == 2.0
    assert audit["settings"]["gross_output_absolute_tolerance"] == 16
    assert audit["validation_pass"] is True


@pytest.mark.parametrize(
    ("transport", "body", "usage", "response"),
    [
        ("chat_completions", {"messages": [{"role": "user", "content": "hello"}]}, {"prompt_tokens": 10, "completion_tokens": 2}, {"choices": [{"message": {"content": "OK"}, "finish_reason": "length"}]}),
        ("fim_completions", {"prompt": "hello"}, {"prompt_tokens": 2, "completion_tokens": 2}, {"choices": [{"text": "OK", "finish_reason": "length"}]}),
        ("claude_messages", {"messages": [{"role": "user", "content": "hello"}]}, {"input_tokens": 10, "output_tokens": 2}, {"content": [{"type": "text", "text": "OK"}], "stop_reason": "max_tokens"}),
        ("gemini_generate_content", {"contents": [{"parts": [{"text": "hello"}]}]}, {"promptTokenCount": 10, "candidatesTokenCount": 2}, {"candidates": [{"content": {"parts": [{"text": "OK"}]}, "finishReason": "MAX_TOKENS"}]}),
        ("openai_responses", {"input": "hello"}, {"input_tokens": 10, "output_tokens": 2}, {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}]}),
        ("gemini_interactions", {"input": "hello"}, {"total_input_tokens": 10, "total_output_tokens": 2}, {"status": "incomplete", "steps": [{"type": "model_output", "content": [{"type": "text", "text": "OK"}]}]}),
    ],
)
def test_every_text_api_rejects_truncated_output_even_with_plausible_tokens(transport, body, usage, response) -> None:
    audit = audit_exchange(body, _result(usage, response), transport, CONFIG, "initial")
    assert audit["usage_arithmetic"]["status"] == "pass"
    assert audit["output_completion"]["status"] == "fail"
    assert audit["validation_pass"] is False


@pytest.mark.parametrize("reason", [None, "length", "content_filter"])
def test_all_choices_require_normal_completion(reason) -> None:
    response = {"choices": [
        {"message": {"content": "A"}, "finish_reason": "stop"},
        {"message": {"content": "B"}, "finish_reason": reason},
    ]}
    result = _result({"prompt_tokens": 10, "completion_tokens": 4}, response, complete=False)
    result.finish_reason = "stop"
    audit = audit_exchange({"messages": [{"role": "user", "content": "hello"}], "n": 2}, result, "chat_completions", CONFIG, "initial")
    assert audit["output_completion"]["status"] == "fail"
    assert audit["validation_pass"] is False


def test_stop_text_without_terminal_evidence_or_with_stream_error_cannot_pass() -> None:
    result = _result({"prompt_tokens": 10, "completion_tokens": 2}, {"choices": [{"message": {"content": "OK"}}]}, complete=False)
    body = {"messages": [{"role": "user", "content": "hello"}], "stream": True}
    missing = audit_exchange(body, result, "chat_completions", CONFIG, "initial")
    assert missing["validation_pass"] is False
    result.response_json["choices"][0]["finish_reason"] = "stop"
    result.error_type = "stream_missing_done"
    broken = audit_exchange(body, result, "chat_completions", CONFIG, "initial")
    assert broken["output_completion"]["status"] == "fail"
    assert broken["validation_pass"] is False


def test_reasonable_quantity_variation_passes_but_moderate_input_injection_does_not() -> None:
    body = {"messages": [{"role": "user", "content": "Explain the word blue. " * 50}]}
    response = {"choices": [{"message": {"content": "Blue is a color."}}]}
    normal = audit_exchange(body, _result({"prompt_tokens": 300, "completion_tokens": 5}, response), "chat_completions", CONFIG, "initial")
    injected = audit_exchange(body, _result({"prompt_tokens": 1200, "completion_tokens": 5}, response), "chat_completions", CONFIG, "initial")
    assert normal["validation_pass"] is True
    assert normal["input_accuracy"]["status"] == "not_available"
    assert normal["evidence_level"] != "exact"
    assert injected["gross_plausibility"]["input"]["status"] == "fail"
    assert injected["validation_pass"] is False


def test_exact_complete_media_input_count_resolves_opaque_input() -> None:
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": "describe"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]}
    result = _result({"prompt_tokens": 500, "completion_tokens": 2}, {"choices": [{"message": {"content": "OK"}}]})
    unverified = audit_exchange(body, result, "chat_completions", CONFIG, "initial")
    verified = audit_exchange(body, result, "chat_completions", CONFIG, "initial", independent_input_count={"tokens": 500, "evidence_level": "exact", "source": "official-count-endpoint"})
    assert unverified["validation_pass"] is False
    assert verified["input_accuracy"]["status"] == "pass"
    assert verified["gross_plausibility"]["input"]["status"] == "pass"
    assert verified["validation_pass"] is True
    assert verified["evidence_level"] == "mixed"


def test_independent_count_without_declared_exact_evidence_cannot_claim_accuracy() -> None:
    audit = audit_exchange({"messages": [{"role": "user", "content": "hello"}]}, _result({"prompt_tokens": 10, "completion_tokens": 2}, {"choices": [{"message": {"content": "OK"}}]}), "chat_completions", CONFIG, "initial", independent_input_count={"tokens": 10})
    assert audit["input_accuracy"]["status"] == "not_available"
    assert audit["input_accuracy"]["evidence_level"] == "provider_count"


def test_inclusive_reasoning_and_rejected_predictions_require_exact_openai_binding() -> None:
    body = {"messages": [{"role": "user", "content": "Say OK"}], "max_completion_tokens": 256}
    result = _result({"prompt_tokens": 10, "completion_tokens": 62, "total_tokens": 72, "completion_tokens_details": {"reasoning_tokens": 40, "rejected_prediction_tokens": 20}}, {"choices": [{"message": {"content": "OK"}}]})
    unbound = audit_exchange(body, result, "chat_completions", CONFIG, "initial")
    bound = audit_exchange(body, result, "chat_completions", CONFIG, "initial", accounting_source_id="openai", accounting_contract_id="openai_gpt6_astra_chat")
    assert unbound["validation_pass"] is False
    assert bound["usage_accounting"]["answer_tokens"] == 2
    assert bound["usage_accounting"]["rejected_prediction_tokens"] == 20
    assert bound["gross_plausibility"]["output"]["compared_component"] == "answer"
    assert bound["validation_pass"] is True
    assert bound["output_accuracy"]["status"] == "not_available"


def test_image_formula_range_is_independent_of_base64_length() -> None:
    expectation = {"min": 1100, "max": 1140, "source": "https://official.example/image-tokens", "evidence_level": "official_range", "component": "image"}
    for image_bytes in ("AAAA", "AAAA" * 10000):
        audit = audit_image_usage(
            {"messages": [{"role": "user", "content": "draw a square"}]},
            {"choices": [{"message": {"content": f"![image](data:image/png;base64,{image_bytes})"}, "finish_reason": "stop"}]},
            {"prompt_tokens": 10, "completion_tokens": 1120}, CONFIG,
            provider=None, model="image-model", transport="chat_completions", image_output_expectation=expectation,
        )
        assert audit["validation_pass"] is True
        exchange = audit["exchanges"][0]
        assert exchange["output"]["estimated_answer_tokens"] == 0
        assert exchange["gross_plausibility"]["output"]["maximum_plausible_tokens"] == 1140
        assert exchange["output_accuracy"]["status"] == "not_available"


@pytest.mark.parametrize("reported, passed", [(1120, True), (1400, True), (3000, False), (0, False)])
def test_image_output_without_details_uses_official_approximate_estimate(reported, passed) -> None:
    audit = audit_image_usage({"prompt": "draw a square"}, {"data": [{"b64_json": "AAAA"}]}, {"input_tokens": 10, "output_tokens": reported, "total_tokens": reported + 10}, CONFIG, provider=None, model="image-model", image_output_expectation={"min": 1100, "max": 1140, "source": "https://official.example/image-tokens", "evidence_level": "official_range"})
    assert audit["validation_pass"] is passed


def test_image_input_modality_breakdown_cannot_hide_unexpected_extra_input() -> None:
    audit = audit_image_usage({"prompt": "draw a square"}, {"data": [{"b64_json": "AAAA"}]}, {"input_tokens": 510, "input_tokens_details": {"text_tokens": 10, "image_tokens": 500}, "output_tokens": 1120}, CONFIG, provider=None, model="image-model", image_output_expectation={"min": 1100, "max": 1140, "source": "https://official.example/image-tokens", "evidence_level": "official_range"})
    assert audit["exchanges"][0]["gross_plausibility"]["input"]["status"] == "fail"
    assert audit["validation_pass"] is False


def test_summary_counts_completion_failures_and_unverified_exchanges() -> None:
    body = {"input": "hello"}
    response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}]}
    usage = {"input_tokens": 10, "output_tokens": 2}
    good = audit_exchange(body, _result(usage, response), "openai_responses", CONFIG, "initial")
    missing = audit_exchange(body, _result(usage, response, complete=False), "openai_responses", CONFIG, "followup")
    bad = audit_exchange(body, _result(usage, {**response, "status": "incomplete"}), "openai_responses", CONFIG, "initial")
    combined = combine_exchange_audits([good, missing, bad])
    summary = summarize_token_audits([{"status_code": 200, "token_audit": combined}])
    for audit in (combined, summary):
        assert audit["completion_check_count"] == 3
        assert audit["completion_failure_count"] == 1
        assert audit["completion_unverified_count"] == 1
    assert summary["pass"] is False


@pytest.mark.parametrize("reported, passed", [(2000, True), (3000, False)])
def test_image_modality_residual_is_checked_against_approximate_output_envelope(reported, passed) -> None:
    audit = audit_image_usage(
        {"prompt": "draw a square"}, {"data": [{"b64_json": "AAAA"}]},
        {"input_tokens": 10, "output_tokens": reported, "output_tokens_details": {"image_tokens": 1120, "text_tokens": 0}},
        CONFIG, provider=None, model="image-model",
        image_output_expectation={"min": 1100, "max": 1140, "source": "https://official.example/image-tokens", "evidence_level": "official_range"},
    )
    quantity = audit["exchanges"][0]["gross_plausibility"]["output"]
    assert quantity["status"] == ("pass" if passed else "fail")
    assert quantity["validation_scope"] == "official_image_token_estimate"
    assert quantity["exact_output_count_verified"] is False
    assert audit["validation_pass"] is passed


@pytest.mark.parametrize("missing", ["output_completion", "gross_plausibility", "usage_presence", "usage_arithmetic"])
def test_current_schema_label_cannot_replace_required_validation_evidence(missing) -> None:
    current = audit_exchange(
        {"messages": [{"role": "user", "content": "hello"}]},
        _result({"prompt_tokens": 10, "completion_tokens": 2}, {"choices": [{"message": {"content": "OK"}}]}),
        "chat_completions", CONFIG, "initial",
    )
    assert current["validation_pass"] is True
    del current[missing]
    combined = combine_exchange_audits([current])
    assert combined["validation_pass"] is False
    # A report cannot bypass the check by forging its already-combined flag.
    combined["validation_pass"] = True
    summary = summarize_token_audits([{"status_code": 200, "token_audit": combined}])
    assert summary["validated_exchange_count"] == 0
    assert summary["pass"] is False


@pytest.mark.parametrize("reported, passed", [(500, True), (530, True), (700, False)])
def test_official_complete_input_count_allows_small_variation_without_claiming_exactness(reported, passed) -> None:
    audit = audit_exchange(
        {"input": "describe", "previous_response_id": "previous"},
        _result({"input_tokens": reported, "output_tokens": 2}, {"output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}]}),
        "openai_responses", CONFIG, "initial",
        independent_input_count={"tokens": 500, "kind": "provider_count", "source": "official countTokens", "evidence_level": "official_count", "covers_full_input": True},
    )
    assert audit["validation_pass"] is passed
    assert audit["gross_plausibility"]["input"]["covers_full_input"] is True
    assert audit["input_accuracy"]["status"] == "not_available"
    assert audit["input_accuracy"]["evidence_level"] == "official_count"


def test_output_image_schema_and_tool_parameter_names_are_not_input_media() -> None:
    body = {"input": "hello", "tools": [{"type": "function", "name": "record", "parameters": {"type": "object", "properties": {"image_url": {"type": "string"}}}}], "response_format": {"type": "image"}}
    audit = audit_exchange(body, _result({"total_input_tokens": 50, "total_output_tokens": 2}, {"steps": [{"type": "model_output", "content": [{"type": "text", "text": "OK"}]}]}), "gemini_interactions", CONFIG, "initial")
    assert audit["gross_plausibility"]["input"]["opaque_high_side"] is False
    assert audit["gross_plausibility"]["input"]["status"] == "pass"
    assert audit["validation_pass"] is False  # requested image output is absent


def test_native_external_tool_context_is_not_mistaken_for_visible_only_input() -> None:
    body = {"contents": [{"parts": [{"text": "look it up"}]}], "tools": [{"googleSearch": {}}]}
    audit = audit_exchange(body, _result({"promptTokenCount": 20, "candidatesTokenCount": 2}, {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "OK"}]}}]}), "gemini_generate_content", CONFIG, "initial")
    assert audit["gross_plausibility"]["input"]["opaque_high_side"] is True
    assert audit["validation_pass"] is False


def test_mutated_independent_count_request_fails_even_when_usage_is_plausible() -> None:
    audit = audit_exchange(
        {"messages": [{"role": "user", "content": "hello"}]},
        _result({"prompt_tokens": 10, "completion_tokens": 2}, {"choices": [{"message": {"content": "OK"}}]}),
        "chat_completions", CONFIG, "initial",
        independent_input_count={"tokens": None, "evidence_level": "unavailable", "request_integrity": "fail"},
    )
    assert audit["gross_plausibility"]["input"]["status"] == "pass"
    assert audit["validation_pass"] is False
    assert "request-integrity" in " ".join(audit["validation_failures"])
