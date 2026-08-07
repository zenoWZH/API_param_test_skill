from __future__ import annotations

import unittest

from lib.metrics import (
    RequestRecord,
    build_time_series,
    classify_failure,
    summarize_records,
)


def record(
    timestamp: float,
    *,
    success: bool,
    usage: dict[str, object],
    latency_ms: float = 100,
    ttft_ms: float | None = None,
    users: int = 10,
    step: int = 1,
    extra: dict[str, object] | None = None,
) -> RequestRecord:
    return RequestRecord(
        timestamp=timestamp,
        task_name="chat:throughput_profiles:baseline_short",
        group="throughput_profiles",
        profile="baseline_short",
        method="POST",
        path="/v1/chat/completions",
        success=success,
        status_code=200 if success else 500,
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
        usage=usage,
        extra={"configured_users": users, "staircase_step": step, **(extra or {})},
    )


class MetricsTest(unittest.TestCase):
    def test_http_status_takes_precedence_over_response_parse_error(self) -> None:
        self.assertEqual(classify_failure(413, error_type="json_parse"), "http_4xx")
        self.assertEqual(classify_failure(429, error_type="json_parse"), "http_429")
        self.assertEqual(classify_failure(502, error_type="json_parse"), "http_5xx")
        self.assertEqual(classify_failure(200, error_type="json_parse"), "json_parse")

    def test_streaming_latency_percentiles_use_successful_requests(self) -> None:
        records = [
            record(100, success=True, usage={}, latency_ms=100, ttft_ms=10),
            record(101, success=True, usage={}, latency_ms=200, ttft_ms=20),
            record(102, success=True, usage={}, latency_ms=300, ttft_ms=None),
            record(103, success=True, usage={}, latency_ms=400, ttft_ms=40),
            record(104, success=False, usage={}, latency_ms=1, ttft_ms=1),
        ]

        summary = summarize_records(records, duration_sec=60)

        self.assertEqual(summary["e2e_latency_sample_count"], 4)
        self.assertEqual(summary["e2e_latency_p50_ms"], 250)
        self.assertEqual(summary["e2e_latency_p90_ms"], 370)
        self.assertEqual(summary["e2e_latency_p95_ms"], 385)
        self.assertEqual(summary["e2e_latency_p99_ms"], 397)
        self.assertEqual(summary["ttft_sample_count"], 3)
        self.assertEqual(summary["ttft_coverage"], 0.75)
        self.assertEqual(summary["ttft_p50_ms"], 20)
        self.assertEqual(summary["ttft_p90_ms"], 36)
        self.assertEqual(summary["ttft_p95_ms"], 38)
        self.assertAlmostEqual(summary["ttft_p99_ms"], 39.6)

    def test_summary_reports_attempted_rpm_and_token_rates(self) -> None:
        records = [
            record(
                100,
                success=True,
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            ),
            record(
                160,
                success=False,
                usage={"input_tokens": 20, "output_tokens": 7},
            ),
        ]

        summary = summarize_records(records, duration_sec=60)

        self.assertEqual(summary["business_rpm"], 1)
        self.assertEqual(summary["attempted_business_rpm"], 2)
        # Chat Completions ignores compatible-layer input/output aliases.
        self.assertEqual(summary["input_tpm"], 10)
        self.assertEqual(summary["output_tpm"], 5)
        self.assertEqual(summary["total_tpm"], 15)
        self.assertEqual(summary["token_usage_coverage"], 0.5)
        self.assertEqual(summary["success_rate"], 0.5)

    def test_tpm_uses_completion_inclusive_of_thinking_without_double_counting_details(self) -> None:
        records = [
            record(
                100,
                success=True,
                usage={
                    "prompt_tokens": 10,
                    "completion_tokens": 221,
                    "total_tokens": 403,
                    "output_tokens": 393,
                    "completion_tokens_details": {"reasoning_tokens": 172},
                },
            )
        ]

        summary = summarize_records(records, duration_sec=60)

        self.assertEqual(summary["input_tpm"], 10)
        self.assertEqual(summary["output_tpm"], 221)
        self.assertEqual(summary["total_tpm"], 231)

    def test_claude_messages_tpm_uses_inclusive_output_tokens(self) -> None:
        records = [
            record(
                100,
                success=True,
                usage={"input_tokens": 10, "output_tokens": 60, "thinking_tokens": 20},
                extra={"transport": "claude_messages"},
            )
        ]

        summary = summarize_records(records, duration_sec=60)

        self.assertEqual(summary["input_tpm"], 10)
        self.assertEqual(summary["output_tpm"], 60)
        self.assertEqual(summary["total_tpm"], 70)

    def test_progressive_cache_metrics_require_official_usage_and_ignore_legacy_token_floor(self) -> None:
        seed = RequestRecord(
            timestamp=100,
            task_name="cache:progressive:seed",
            group="cache_profiles",
            profile="progressive_seed",
            method="POST",
            path="/v1/chat/completions",
            success=True,
            status_code=200,
            latency_ms=500,
            usage={
                "prompt_tokens": 100,
                "prompt_cache_hit_tokens": 10,
                "prompt_cache_miss_tokens": 90,
            },
            extra={
                "cache_scenario": "progressive_customer_session",
                "cache_stage": "seed",
                "session_index": 1,
                "transport": "chat_completions",
            },
        )
        growth = RequestRecord(
            timestamp=101,
            task_name="cache:progressive:final_growth",
            group="cache_profiles",
            profile="progressive_final_growth",
            method="POST",
            path="/v1/chat/completions",
            success=True,
            status_code=200,
            latency_ms=10,
            usage={},
            extra={
                "cache_scenario": "progressive_customer_session",
                "cache_stage": "final_growth",
                "session_index": 1,
                "strict_prefix_extension": True,
                "reusable_prefix_tokens": 100,
                "session_completed": True,
                "transport": "chat_completions",
            },
        )

        summary = summarize_records(
            [seed, growth],
            business_prefix="cache:",
            business_group="cache_profiles",
            cache_min_prompt_tokens=4000,
            duration_sec=60,
        )

        self.assertEqual(summary["cached_input_token_ratio"], 0.1)
        self.assertEqual(summary["cache_measurement_coverage"], 0.5)
        self.assertIsNone(summary["progressive_prefix_reuse_rate"])
        self.assertIsNone(summary["structural_hit_rate_ceiling"])
        self.assertIsNone(summary["cache_efficiency"])
        self.assertEqual(summary["cache_efficiency_status"], "unavailable")
        self.assertEqual(summary["session_completion_ratio"], 1.0)

        seed.usage = {}
        no_usage = summarize_records(
            [seed, growth],
            business_prefix="cache:",
            business_group="cache_profiles",
            cache_min_prompt_tokens=4000,
            duration_sec=60,
        )
        self.assertIsNone(no_usage["cached_input_token_ratio"])
        self.assertEqual(no_usage["cache_measurement_coverage"], 0.0)

    def test_progressive_structural_ceiling_actual_hit_rate_and_efficiency(self) -> None:
        structure_probe = RequestRecord(
            timestamp=99,
            task_name="cache:structure_probe",
            group="cache_profiles",
            profile="progressive_structure_probe",
            method="POST",
            path="/v1/chat/completions",
            success=True,
            status_code=200,
            usage={"prompt_tokens": 20},
            extra={
                "cache_scenario": "progressive_customer_session",
                "cache_stage": "structure_probe",
                "cache_structure_probe": "stable_system_and_tools",
                "transport": "chat_completions",
            },
        )
        seed = RequestRecord(
            timestamp=100,
            task_name="cache:progressive:seed",
            group="cache_profiles",
            profile="progressive_seed",
            method="POST",
            path="/v1/chat/completions",
            success=True,
            status_code=200,
            usage={
                "prompt_tokens": 100,
                "prompt_cache_hit_tokens": 10,
                "prompt_cache_miss_tokens": 90,
            },
            extra={
                "cache_scenario": "progressive_customer_session",
                "cache_stage": "seed",
                "session_index": 1,
                "transport": "chat_completions",
            },
        )
        growth = RequestRecord(
            timestamp=101,
            task_name="cache:progressive:final_growth",
            group="cache_profiles",
            profile="progressive_final_growth",
            method="POST",
            path="/v1/chat/completions",
            success=True,
            status_code=200,
            usage={
                "prompt_tokens": 200,
                "prompt_cache_hit_tokens": 60,
                "prompt_cache_miss_tokens": 140,
            },
            extra={
                "cache_scenario": "progressive_customer_session",
                "cache_stage": "final_growth",
                "session_index": 1,
                "strict_prefix_extension": True,
                "reusable_prefix_tokens": 100,
                "session_completed": True,
                "transport": "chat_completions",
            },
        )

        summary = summarize_records(
            [structure_probe, seed, growth],
            business_prefix="cache:",
            business_group="cache_profiles",
            duration_sec=60,
        )

        self.assertAlmostEqual(summary["structural_hit_rate_ceiling"], 120 / 300)
        self.assertAlmostEqual(summary["actual_cache_hit_rate"], 70 / 300)
        self.assertAlmostEqual(summary["cache_efficiency"], 7 / 12)
        self.assertEqual(summary["cache_efficiency_status"], "measured")
        self.assertEqual(summary["structure_probe_input_tokens"], 20)
        self.assertEqual(summary["structure_ceiling_measurement_coverage"], 1.0)
        self.assertAlmostEqual(
            summary["cache_stage_metrics"]["final_growth"]["structural_hit_rate_ceiling"],
            0.5,
        )
        self.assertAlmostEqual(
            summary["cache_stage_metrics"]["final_growth"]["cache_efficiency"],
            0.6,
        )

    def test_time_series_uses_fixed_windows_and_load_context(self) -> None:
        records = [
            record(100, success=True, usage={"total_tokens": 10}),
            record(105, success=False, usage={"total_tokens": 20}),
            record(111, success=True, usage={"total_tokens": 30}, users=30, step=2),
        ]

        points = build_time_series(records, bucket_sec=10, now=115)

        self.assertEqual(len(points), 2)
        self.assertEqual(points[0]["business_rpm"], 6)
        self.assertEqual(points[0]["attempted_business_rpm"], 12)
        self.assertEqual(points[0]["total_tpm"], 180)
        self.assertEqual(points[0]["success_rate"], 0.5)
        self.assertEqual(points[0]["configured_users"], 10)
        self.assertEqual(points[0]["staircase_step"], 1)
        self.assertEqual(points[1]["business_rpm"], 12)
        self.assertEqual(points[1]["total_tpm"], 360)
        self.assertEqual(points[1]["configured_users"], 30)
        self.assertEqual(points[1]["staircase_step"], 2)

    def test_time_series_marks_tpm_unavailable_without_usage(self) -> None:
        points = build_time_series(
            [record(100, success=True, usage={})],
            bucket_sec=10,
            now=105,
        )

        self.assertIsNone(points[0]["total_tpm"])
        self.assertEqual(points[0]["token_usage_coverage"], 0)

    def test_adaptive_token_length_metrics_report_distribution_and_status(self) -> None:
        records = [
            record(
                100 + index,
                success=True,
                usage={"prompt_tokens": 900, "completion_tokens": 100, "total_tokens": 1000},
                extra={
                    "target_tokens_per_request": 1000,
                    "adaptive_band": ("short", "target", "long")[index % 3],
                    "context_window_tokens": 131072,
                    "context_window_source": "fallback",
                    "context_clamped": index == 0,
                },
            )
            for index in range(20)
        ]

        summary = summarize_records(records, duration_sec=60)

        self.assertEqual(summary["avg_tokens_per_request"], 1000)
        self.assertEqual(summary["p50_tokens_per_request"], 1000)
        self.assertEqual(summary["p95_tokens_per_request"], 1000)
        self.assertEqual(summary["tokens_per_request_deviation_ratio"], 0)
        self.assertEqual(summary["adaptive_controller_status"], "on_target")
        self.assertEqual(summary["adaptive_context_clamped_count"], 1)
        self.assertEqual(sum(summary["adaptive_band_counts"].values()), 20)


if __name__ == "__main__":
    unittest.main()
