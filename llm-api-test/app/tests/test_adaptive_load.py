from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from lib.adaptive_load import (
    AdaptiveLengthController,
    estimate_prompt_token_units,
    filter_context_unsafe_profiles,
    rebalance_band_targets,
    resolve_context_window,
)
from lib.config import load_config
from lib.deepseek_params import weighted_workload_profiles


class AdaptiveLoadTest(unittest.TestCase):
    def test_band_rebalancing_preserves_requested_weighted_mean(self) -> None:
        bands = [
            {"name": "short", "ratio": 0.5, "weight": 25},
            {"name": "target", "ratio": 1.0, "weight": 50},
            {"name": "long", "ratio": 1.5, "weight": 25},
        ]
        values, unreachable = rebalance_band_targets(1000, bands, 100, 1200)
        weighted_mean = sum(
            value * band["weight"] for value, band in zip(values, bands)
        ) / 100
        self.assertFalse(unreachable)
        self.assertAlmostEqual(weighted_mean, 1000)
        self.assertLessEqual(max(values), 1200)

    def test_unreachable_target_is_safely_clamped(self) -> None:
        bands = [
            {"name": "short", "ratio": 0.5, "weight": 25},
            {"name": "target", "ratio": 1.0, "weight": 50},
            {"name": "long", "ratio": 1.5, "weight": 25},
        ]
        values, unreachable = rebalance_band_targets(5000, bands, 100, 1200)
        self.assertTrue(unreachable)
        self.assertEqual(values, [1200, 1200, 1200])

    def test_context_window_uses_model_mapping_then_fallback(self) -> None:
        config = {"adaptive_load": {"fallback_context_window_tokens": 131072}}
        provider = {
            "name": "test",
            "models": {"context_windows": {"model-a": 32768}},
        }
        self.assertEqual(resolve_context_window(config, provider, "model-a")[0], 32768)
        value, source = resolve_context_window(config, provider, "model-b")
        self.assertEqual(value, 131072)
        self.assertEqual(source, "adaptive_load.fallback_context_window_tokens")

    def test_context_unsafe_static_profiles_are_filtered(self) -> None:
        config = load_config()
        entries = weighted_workload_profiles(config, "throughput_tpm")
        allowed, skipped = filter_context_unsafe_profiles(
            config,
            {
                "name": "test",
                "models": {"context_windows": {"model-a": 131_072}},
            },
            "model-a",
            entries,
        )

        self.assertIn("context_128k_chars", {item[1] for item in allowed})
        self.assertEqual(
            {item["profile"] for item in skipped},
            {"context_512k_chars", "half_million_context"},
        )

    def test_controller_generates_mixed_lengths_and_calibrates_from_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            corpus = Path(temp_dir) / "corpus.txt"
            corpus.write_text("中文负载语料 " * 3000, encoding="utf-8")
            config = {
                "adaptive_load": {
                    "corpus_fixtures": [str(corpus)],
                    "fallback_context_window_tokens": 4096,
                    "context_safety_ratio": 0.95,
                    "initial_completion_tokens": 64,
                    "bands": [
                        {"name": "short", "ratio": 0.5, "weight": 25},
                        {"name": "target", "ratio": 1.0, "weight": 50},
                        {"name": "long", "ratio": 1.5, "weight": 25},
                    ],
                }
            }
            controller = AdaptiveLengthController(
                config,
                {"name": "test", "models": {}},
                "model-a",
                1000,
            )
            template = {
                "model": "model-a",
                "messages": [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "placeholder"},
                ],
                "max_tokens": 128,
            }
            plans = []
            lengths = []
            prompts = []
            for _ in range(3):
                body = copy.deepcopy(template)
                plan = controller.apply_to_body(body)
                plans.append(plan)
                lengths.append(estimate_prompt_token_units(body))
                prompts.append(body["messages"][1]["content"])

            self.assertEqual([plan.band for plan in plans], ["short", "target", "long"])
            self.assertLess(lengths[0], lengths[1])
            self.assertLess(lengths[1], lengths[2])
            self.assertTrue(all("nonce=" in prompt for prompt in prompts))
            self.assertEqual(len(set(prompts)), 3)

            controller.feedback(
                plans[0],
                {"prompt_tokens": plans[0].estimated_prompt_tokens * 2, "completion_tokens": 32},
            )
            snapshot = controller.snapshot()
            self.assertGreater(snapshot["prompt_tokens_per_estimated_unit"], 1.0)
            self.assertLess(snapshot["predicted_completion_tokens"], 64)
            self.assertEqual(snapshot["status"], "learning")


if __name__ == "__main__":
    unittest.main()
