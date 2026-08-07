from __future__ import annotations

from types import SimpleNamespace

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


def _result(usage: dict, response_json: dict) -> SimpleNamespace:
    return SimpleNamespace(usage=usage, response_json=response_json)


def test_chat_usage_uses_prompt_and_completion_and_treats_details_as_advisory() -> None:
    usage = normalize_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 221,
            "total_tokens": 999,
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
        "provider_total_tokens": 999,
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
    assert summary["mismatch_count"] == 0


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

    assert audit["schema_version"] == 2
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
    )
    assert usage["details_advisory"]["reasoning_tokens"] == 15
    assert audit["usage_arithmetic"]["status"] == "pass"
    assert audit["usage_arithmetic"]["errors"] == []


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
