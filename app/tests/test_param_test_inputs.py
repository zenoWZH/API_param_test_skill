from __future__ import annotations

import random
import unittest
from unittest.mock import patch

from scripts.param_test import (
    _select_reference_source,
    _input_group_for_profile,
    _profile_uses_openai_json_object,
    _sample_inputs_for_profile,
)


class ParamTestInputGroupingTests(unittest.TestCase):
    def test_model_comparison_forces_provider_independent_reference_source(self) -> None:
        config = {
            "providers": {
                "test": {
                    "models": {
                        "reference_sources": {
                            "deepseek-v4-pro": "aliyun_deepseek_v4_openai_compat",
                        }
                    }
                }
            }
        }
        with patch.dict(
            "os.environ",
            {"LOADTEST_MODEL_COMPARISON": "1"},
            clear=True,
        ):
            self.assertEqual(
                _select_reference_source(
                    config,
                    "deepseek",
                    "deepseek-v4-pro",
                    "test",
                    route_profile="vendor_direct",
                    api_form="openai_chat_completions",
                ),
                "deepseek_chat",
            )

    def test_model_comparison_rejects_conflicting_explicit_source(self) -> None:
        config = {"providers": {"test": {"models": {}}}}
        with patch.dict(
            "os.environ",
            {
                "LOADTEST_MODEL_COMPARISON": "1",
                "LOADTEST_REFERENCE_SOURCE": "aliyun_deepseek_v4_openai_compat",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "conflicts"):
                _select_reference_source(
                    config,
                    "deepseek",
                    "deepseek-v4-pro",
                    "test",
                    route_profile="vendor_direct",
                    api_form="openai_chat_completions",
                )

    def test_openai_compat_json_object_profiles_use_json_input_pool(self) -> None:
        config = {
            "compatibility_profiles": {
                "qwen_response_format": {
                    "stream": False,
                    "enable_thinking": False,
                    "response_format": {"type": "json_object"},
                },
                "json_output": {
                    "stream": False,
                    "response_format": {"type": "json_object"},
                },
                "basic_stream": {"stream": True},
            },
            "param_test_inputs": {
                "general": [
                    {"id": "general_only", "prompt": "用两句话解释 API 压测。"},
                ],
                "json_output": [
                    {
                        "id": "json_ok",
                        "prompt": "请只返回一个合法 JSON object，字段包含 summary。",
                    },
                ],
            },
        }

        self.assertTrue(
            _profile_uses_openai_json_object(
                config["compatibility_profiles"]["qwen_response_format"]
            )
        )
        self.assertEqual(_input_group_for_profile(config, "qwen_response_format"), "json_output")
        self.assertEqual(_input_group_for_profile(config, "json_output"), "json_output")
        self.assertEqual(_input_group_for_profile(config, "basic_stream"), "general")

        samples = _sample_inputs_for_profile(config, "qwen_response_format", 1, random.Random(0))
        self.assertEqual(len(samples), 1)
        self.assertIn("json", samples[0]["prompt"].casefold())
        self.assertEqual(samples[0]["id"], "json_ok")

    def test_json_object_profile_rejects_pool_without_json_word(self) -> None:
        config = {
            "compatibility_profiles": {
                "qwen_response_format": {
                    "response_format": {"type": "json_object"},
                }
            },
            "param_test_inputs": {
                "json_output": [
                    {"id": "bad", "prompt": "请返回一个对象，不要提那个词。"},
                ]
            },
        }
        with self.assertRaisesRegex(ValueError, "contain the word JSON"):
            _sample_inputs_for_profile(config, "qwen_response_format", 1, random.Random(0))

    def test_reasoning_profiles_use_reasoning_input_pool(self) -> None:
        config = {
            "compatibility_profiles": {
                "glm_reasoning_max": {
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": "max",
                },
                "qwen_thinking_enabled": {
                    "enable_thinking": True,
                },
                "qwen_thinking_budget": {
                    "enable_thinking": True,
                    "thinking_budget": 64,
                },
                "gemini_reasoning_high": {
                    "reasoning_effort": "high",
                },
                "gemini_native_thinking_high": {
                    "transport": "gemini_generate_content",
                    "native_generation_config": {
                        "thinkingConfig": {"thinkingLevel": "HIGH"},
                    },
                },
            },
            "param_test_inputs": {
                "general": [{"id": "general", "prompt": "介绍你自己。"}],
                "reasoning": [
                    {
                        "id": "decimal",
                        "prompt": "请分析 9.11 和 9.8 哪个更大。",
                    }
                ],
            },
        }

        self.assertEqual(
            _input_group_for_profile(config, "glm_reasoning_max"),
            "reasoning",
        )
        samples = _sample_inputs_for_profile(
            config,
            "glm_reasoning_max",
            1,
            random.Random(0),
        )
        self.assertEqual(samples[0]["id"], "decimal")
        for profile in ("qwen_thinking_enabled", "qwen_thinking_budget"):
            with self.subTest(profile=profile):
                self.assertEqual(
                    _input_group_for_profile(config, profile),
                    "reasoning",
                )
                qwen_samples = _sample_inputs_for_profile(
                    config,
                    profile,
                    1,
                    random.Random(0),
                )
                self.assertEqual(qwen_samples[0]["id"], "decimal")
        for profile in ("gemini_reasoning_high", "gemini_native_thinking_high"):
            with self.subTest(profile=profile):
                self.assertEqual(
                    _input_group_for_profile(config, profile),
                    "reasoning",
                )


if __name__ == "__main__":
    unittest.main()
