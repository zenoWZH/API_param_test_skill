from __future__ import annotations

import tempfile
import unittest

from lib.threshold import check_cache, check_staircase


class StaircaseThresholdTest(unittest.TestCase):
    def test_tpm_goal_is_enforced_when_configured(self) -> None:
        config = {
            "thresholds": {
                "staircase": {
                    "target_business_rpm_min": 500,
                    "target_total_tpm_min": 100_000,
                    "success_rate_min": 0.99,
                    "error_429_max_ratio": 0.01,
                    "error_5xx_max_ratio": 0.01,
                }
            }
        }
        steps = [
            {
                "step": 1,
                "business_record_count": 100,
                "business_rpm": 600,
                "total_tpm": 80_000,
                "success_rate": 1.0,
                "error_429_ratio": 0,
                "error_5xx_ratio": 0,
            }
        ]

        with tempfile.TemporaryDirectory() as output_dir:
            verdict = check_staircase(steps, config, output_dir)

        self.assertFalse(verdict["pass"])
        self.assertEqual(verdict["peak_total_tpm"], 80_000)
        self.assertEqual(verdict["failures"][0]["metric"], "peak_total_tpm")

    def test_later_saturation_does_not_invalidate_qualified_step(self) -> None:
        config = {
            "thresholds": {
                "staircase": {
                    "target_business_rpm_min": 500,
                    "target_total_tpm_min": 0,
                    "success_rate_min": 0.99,
                    "p95_latency_max_ms": 30000,
                    "error_429_max_ratio": 0.01,
                    "error_5xx_max_ratio": 0.01,
                }
            }
        }
        steps = [
            {
                "step": 1,
                "users": 20,
                "business_record_count": 100,
                "business_rpm": 550,
                "total_tpm": 10000,
                "success_rate": 1.0,
                "p95_latency_ms": 1000,
                "error_429_ratio": 0,
                "error_5xx_ratio": 0,
            },
            {
                "step": 2,
                "users": 40,
                "business_record_count": 100,
                "business_rpm": 650,
                "total_tpm": 12000,
                "success_rate": 0.80,
                "p95_latency_ms": 40000,
                "error_429_ratio": 0.20,
                "error_5xx_ratio": 0,
            },
        ]
        with tempfile.TemporaryDirectory() as output_dir:
            verdict = check_staircase(steps, config, output_dir)
        self.assertTrue(verdict["pass"])
        self.assertEqual(verdict["highest_passing_step"]["step"], 1)
        self.assertEqual(verdict["first_failing_step"]["step"], 2)


class CacheThresholdTest(unittest.TestCase):
    def test_confirmed_cached_token_mismatch_fails_even_in_observe_mode(self) -> None:
        config = {
            "thresholds": {
                "cache": {"mode": "observe", "require_usage_fields": False}
            }
        }
        summary = {
            "cache_usage_accuracy_status": "fail",
            "cache_usage_accuracy_pass": False,
            "cache_usage_accuracy_failures": ["cached tokens exceed input tokens"],
        }

        with tempfile.TemporaryDirectory() as output_dir:
            verdict = check_cache({"summary": summary}, config, output_dir)

        self.assertFalse(verdict["pass"])
        self.assertEqual(verdict["failures"][0]["metric"], "cache_usage_accuracy")

    def test_latency_never_substitutes_for_missing_official_usage(self) -> None:
        config = {"thresholds": {"cache": {"mode": "observe", "require_usage_fields": True}}}
        with tempfile.TemporaryDirectory() as output_dir:
            verdict = check_cache(
                {"summary": {"cached_input_token_ratio": None}, "latency_speedup_ratio": 0.9},
                config,
                output_dir,
            )
        self.assertTrue(verdict["pass"])
        self.assertFalse(verdict["threshold_pass"])
        self.assertEqual(verdict["failures"][0]["metric"], "official_cache_usage")

    def test_gate_requires_explicit_customer_and_control_thresholds(self) -> None:
        config = {"thresholds": {"cache": {"mode": "gate", "require_usage_fields": True}}}
        summary = {
            "cached_input_token_ratio": 0.4,
            "cache_measurement_coverage": 1.0,
            "cache_control_metrics": {
                "positive_long_prefix": {"cached_input_token_ratio": 0.8},
                "negative_unique_prefix": {"cached_input_token_ratio": 0.0},
            },
        }
        with tempfile.TemporaryDirectory() as output_dir:
            verdict = check_cache({"summary": summary}, config, output_dir)
        self.assertFalse(verdict["pass"])
        self.assertEqual(verdict["failures"][0]["metric"], "cache_gate_thresholds")


if __name__ == "__main__":
    unittest.main()
