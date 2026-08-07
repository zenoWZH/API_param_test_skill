from __future__ import annotations

from lib.client import ChatResult
from scripts.param_test import _performance_metrics, _performance_summary


def test_streaming_performance_metrics_use_ttft_for_tpot() -> None:
    result = ChatResult(
        success=True,
        status_code=200,
        latency_ms=1200.0,
        timestamp=0.0,
        usage={"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16},
        ttft_ms=200.0,
        response_length=128,
    )

    metrics = _performance_metrics(result)

    assert metrics["input_tokens"] == 10
    assert metrics["output_tokens"] == 6
    assert metrics["total_tokens"] == 16
    assert metrics["ttft_ms"] == 200.0
    assert metrics["tpot_ms"] == 200.0
    assert metrics["tpot_basis"] == "stream_latency_after_ttft_per_output_token"
    assert metrics["throughput_output_tokens_per_sec"] == 5.0
    assert metrics["throughput_total_tokens_per_sec"] == 13.333
    assert metrics["response_bytes"] == 128


def test_non_streaming_performance_metrics_use_end_to_end_tpot() -> None:
    result = ChatResult(
        success=True,
        status_code=200,
        latency_ms=1000.0,
        timestamp=0.0,
        usage={"input_tokens": 8, "output_tokens": 4},
    )

    metrics = _performance_metrics(result)

    assert "ttft_ms" not in metrics
    assert metrics["tpot_ms"] == 250.0
    assert metrics["tpot_basis"] == "end_to_end_latency_per_output_token"
    assert metrics["throughput_output_tokens_per_sec"] == 4.0
    assert metrics["throughput_total_tokens_per_sec"] == 12.0


def test_chat_performance_ignores_compatible_output_and_detail_fields() -> None:
    result = ChatResult(
        success=True,
        status_code=200,
        latency_ms=1000.0,
        timestamp=0.0,
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 221,
            "total_tokens": 999,
            "output_tokens": 393,
            "completion_tokens_details": {"reasoning_tokens": 172},
        },
    )

    metrics = _performance_metrics(result, "chat_completions")

    assert metrics["input_tokens"] == 10
    assert metrics["output_tokens"] == 221
    assert metrics["total_tokens"] == 231
    assert "answer_tokens" not in metrics
    assert "thinking_tokens" not in metrics


def test_gemini_performance_includes_thoughts_in_output_throughput() -> None:
    result = ChatResult(
        success=True,
        status_code=200,
        latency_ms=1000.0,
        timestamp=0.0,
        usage={
            "promptTokenCount": 20,
            "candidatesTokenCount": 73,
            "thoughtsTokenCount": 40,
            "totalTokenCount": 133,
        },
    )

    metrics = _performance_metrics(result, "gemini_generate_content")

    assert metrics["answer_tokens"] == 73
    assert metrics["thinking_tokens"] == 40
    assert metrics["output_tokens"] == 113
    assert metrics["total_tokens"] == 133
    assert metrics["thinking_share"] == 0.354
    assert metrics["throughput_output_tokens_per_sec"] == 113.0


def test_performance_summary_uses_successful_samples() -> None:
    results = [
        {
            "pass": True,
            "performance_metrics": {
                "latency_ms": 100.0,
                "ttft_ms": 20.0,
                "tpot_ms": 10.0,
                "throughput_output_tokens_per_sec": 5.0,
                "throughput_total_tokens_per_sec": 8.0,
                "total_tokens": 4,
            },
        },
        {
            "pass": False,
            "performance_metrics": {
                "latency_ms": 900.0,
                "ttft_ms": 800.0,
                "tpot_ms": 100.0,
                "throughput_output_tokens_per_sec": 1.0,
                "throughput_total_tokens_per_sec": 2.0,
                "total_tokens": 2,
            },
        },
        {
            "pass": True,
            "performance_metrics": {
                "latency_ms": 300.0,
                "ttft_ms": 40.0,
                "tpot_ms": 20.0,
                "throughput_output_tokens_per_sec": 10.0,
                "throughput_total_tokens_per_sec": 16.0,
                "total_tokens": 6,
            },
        },
    ]

    summary = _performance_summary(results)

    assert summary["sample_count"] == 3
    assert summary["success_sample_count"] == 2
    assert summary["latency_ms"]["p50"] == 200.0
    assert summary["ttft_ms"]["count"] == 2
    assert summary["ttft_ms"]["p50"] == 30.0
    assert summary["tpot_ms"]["avg"] == 15.0
    assert summary["throughput_output_tokens_per_sec"]["p95"] == 9.75
    assert summary["token_usage_sample_count"] == 2
    assert summary["ttft_coverage"] == 1.0
