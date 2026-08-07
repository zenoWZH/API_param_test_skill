from __future__ import annotations

import copy
import unittest

from lib.adaptive_load import estimated_text_token_units
from lib.config import load_config
from lib.deepseek_params import (
    apply_request_mode,
    build_request,
    ensure_minimum_prompt_text,
    profile_names,
    _resolve_prompt,
    weighted_workload_profiles,
)


class WorkloadPresetTest(unittest.TestCase):
    def test_mixed_compat_uses_family_transport_source_and_explicit_weights_only(self) -> None:
        config = copy.deepcopy(load_config())
        config["active_provider"] = "test_openai"
        config["providers"] = {
            "test_openai": {
                "label": "test",
                "base_url": "https://example.test/v1",
                "backend": "openai_compatible",
                "default_transport": "chat_completions",
                "api_interfaces": {
                    "chat_completions": {
                        "path": "/chat/completions",
                        "auth": "bearer",
                    }
                },
                "api_key": "test",
                "models": {
                    "default": "gpt-4o",
                    "candidates": ["gpt-4o"],
                    "families": {"gpt-4o": "gpt"},
                    "transports": {"gpt-4o": "chat_completions"},
                },
            }
        }
        config["profile_weights"]["mixed_compat"] = {
            "basic_stream": 2,
            "glm_thinking_enabled": 999,
            "list_models": 1,
        }

        entries = weighted_workload_profiles(config, "mixed_compat")

        self.assertEqual(
            entries,
            [
                ("compatibility_profiles", "basic_stream", 2),
                ("control", "list_models", 1),
            ],
        )

    def test_request_mode_unique_prepends_nonce_for_all_transports(self) -> None:
        bodies = {
            "chat_completions": {"messages": [{"role": "user", "content": "hello"}]},
            "claude_messages": {"messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]},
            "gemini_generate_content": {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
            "openai_responses": {"input": "hello"},
        }
        for transport, body in bodies.items():
            with self.subTest(transport=transport):
                self.assertTrue(apply_request_mode(body, transport, "unique", nonce="abc"))
                self.assertIn("load-request-abc|hello", str(body))
        fixed = {"messages": [{"role": "user", "content": "hello"}]}
        self.assertFalse(apply_request_mode(fixed, "chat_completions", "fixed"))
        self.assertEqual(fixed["messages"][0]["content"], "hello")

    def test_all_generated_test_profiles_have_at_least_100_prompt_tokens(self) -> None:
        config = load_config()
        minimum = int(config["test_cases"]["minimum_prompt_tokens"])

        for group in (
            "throughput_profiles",
            "compatibility_profiles",
            "cache_profiles",
            "qwen_throughput_profiles",
            "qwen_cache_profiles",
        ):
            for profile in profile_names(config, group):
                family = _family_for_profile(profile)
                if group.startswith("qwen_"):
                    family = "qwen"
                with self.subTest(group=group, profile=profile, family=family):
                    request = build_request(
                        config,
                        group,
                        profile,
                        model_family_override=family,
                    )
                    prompt = _user_prompt_text(request.body)
                    self.assertGreaterEqual(
                        estimated_text_token_units(prompt),
                        minimum,
                    )

    def test_short_prompt_override_is_padded_to_minimum(self) -> None:
        config = load_config()
        prompt = ensure_minimum_prompt_text(config, "只输出 pong。")

        self.assertGreaterEqual(
            estimated_text_token_units(prompt),
            config["test_cases"]["minimum_prompt_tokens"],
        )

    def test_streaming_latency_preset_uses_comparable_streaming_requests(self) -> None:
        config = load_config()
        entries = weighted_workload_profiles(config, "throughput_streaming")
        profiles = [entry[1] for entry in entries]
        requests = [
            build_request(
                config,
                "throughput_profiles",
                profile,
                model_family_override="deepseek",
            )
            for profile in profiles
        ]
        prompt_lengths = [
            len(request.body["messages"][-1]["content"])
            for request in requests
        ]

        self.assertEqual(
            set(profiles),
            {
                "streaming_latency_api",
                "streaming_latency_load",
                "streaming_latency_cache",
            },
        )
        self.assertTrue(all(request.body["stream"] for request in requests))
        self.assertTrue(
            all(request.body["stream_options"]["include_usage"] for request in requests)
        )
        self.assertEqual({request.body["max_tokens"] for request in requests}, {160})
        self.assertLessEqual(max(prompt_lengths) - min(prompt_lengths), 4)

    def test_rpm_and_tpm_presets_select_expected_profiles(self) -> None:
        config = load_config()

        rpm_entries = weighted_workload_profiles(config, "throughput_rpm")
        tpm_entries = weighted_workload_profiles(config, "throughput_tpm")

        self.assertEqual({entry[1] for entry in rpm_entries}, {"baseline_short", "baseline_medium"})
        self.assertEqual(
            {entry[1] for entry in tpm_entries},
            {
                "baseline_short",
                "baseline_medium",
                "long_context",
                "context_128k_chars",
                "context_512k_chars",
                "half_million_context",
            },
        )

    def test_fixture_chars_limits_generated_prompt_size(self) -> None:
        prompt = _resolve_prompt(
            {},
            {
                "fixture": "fixtures/half_million_context.txt",
                "fixture_chars": 1_000,
            },
        )

        self.assertGreater(len(prompt), 1_000)
        self.assertLess(len(prompt), 1_100)


def _family_for_profile(profile: str) -> str:
    if profile.startswith(("gpt5_chat_", "openai_responses_")):
        return "gpt"
    for family in ("gemini", "qwen", "glm", "deepseek", "claude", "grok"):
        if profile.startswith(f"{family}_"):
            return family
    return "deepseek"


class ClaudeFamilyWorkloadTest(unittest.TestCase):
    def test_claude_throughput_uses_native_messages_transport(self) -> None:
        config = load_config()

        streaming = build_request(
            config,
            "throughput_profiles",
            "streaming_latency_api",
            model_family_override="claude",
        )
        self.assertEqual(streaming.metadata["transport"], "claude_messages")
        self.assertEqual(streaming.metadata["request_endpoint"], "/messages")
        self.assertTrue(streaming.body["stream"])
        self.assertEqual(streaming.body["max_tokens"], 160)
        self.assertNotIn("stream_options", streaming.body)
        self.assertEqual(streaming.body["thinking"]["type"], "disabled")
        self.assertEqual(streaming.body["temperature"], 0)

    def test_claude_cache_keeps_openai_compat_filtering(self) -> None:
        config = load_config()

        cache = build_request(
            config,
            "cache_profiles",
            "cache_long_context",
            model_family_override="claude",
        )
        self.assertEqual(cache.metadata["transport"], "chat_completions")
        self.assertNotIn("thinking", cache.body)
        self.assertNotIn("user_id", cache.body)
        self.assertEqual(cache.body["max_tokens"], 128)
        self.assertTrue(
            any("user_id is not in claude supported params" in warning for warning in cache.warnings)
        )

    def test_claude_thinking_profiles_validate(self) -> None:
        config = load_config()

        adaptive = build_request(
            config,
            "compatibility_profiles",
            "claude_thinking_adaptive",
            model_family_override="claude",
        )
        self.assertEqual(adaptive.body["thinking"]["type"], "adaptive")

        budget = build_request(
            config,
            "compatibility_profiles",
            "claude_thinking_budget",
            model_family_override="claude",
        )
        self.assertEqual(budget.body["thinking"]["type"], "enabled")
        self.assertEqual(budget.body["thinking"]["budget_tokens"], 1024)

        disabled = build_request(
            config,
            "compatibility_profiles",
            "claude_thinking_disabled",
            model_family_override="claude",
        )
        self.assertNotIn("thinking", disabled.body)
        self.assertEqual(disabled.body["extra_body"]["thinking"]["type"], "disabled")

    def test_claude_native_messages_profiles_validate(self) -> None:
        config = load_config()

        stream = build_request(
            config,
            "compatibility_profiles",
            "claude_native_stream",
            model_family_override="claude",
        )
        self.assertEqual(stream.metadata["transport"], "claude_messages")
        self.assertEqual(stream.metadata["request_endpoint"], "/messages")
        self.assertTrue(stream.body["stream"])
        self.assertIn("system", stream.body)
        self.assertIn("messages", stream.body)

        tools = build_request(
            config,
            "compatibility_profiles",
            "claude_native_tool_choice_auto",
            model_family_override="claude",
        )
        self.assertEqual(tools.body["tool_choice"], {"type": "auto"})
        self.assertEqual(tools.body["tools"][0]["name"], "get_weather")
        self.assertIn("input_schema", tools.body["tools"][0])

        adaptive = build_request(
            config,
            "compatibility_profiles",
            "claude_native_thinking_adaptive",
            model_family_override="claude",
        )
        self.assertEqual(adaptive.body["thinking"]["type"], "adaptive")

        disabled = build_request(
            config,
            "compatibility_profiles",
            "claude_native_thinking_disabled",
            model_family_override="claude",
        )
        self.assertEqual(disabled.body["thinking"]["type"], "disabled")

        budget = build_request(
            config,
            "compatibility_profiles",
            "claude_native_thinking_budget",
            model_family_override="claude",
        )
        self.assertEqual(budget.body["thinking"]["budget_tokens"], 1024)
        self.assertEqual(budget.body["max_tokens"], 1025)

        effort = build_request(
            config,
            "compatibility_profiles",
            "claude_fable_thinking_effort_medium",
            model_family_override="claude",
        )
        self.assertEqual(effort.body["thinking"]["type"], "adaptive")
        self.assertEqual(effort.body["output_config"]["effort"], "medium")
        self.assertNotIn("top_p", effort.body)

    def test_claude_fable_throughput_rewrites_to_adaptive_effort(self) -> None:
        config = load_config()

        streaming = build_request(
            config,
            "throughput_profiles",
            "streaming_latency_api",
            model_family_override="claude_fable",
        )
        self.assertEqual(streaming.metadata["transport"], "claude_messages")
        self.assertEqual(streaming.metadata["request_endpoint"], "/messages")
        self.assertEqual(streaming.body["thinking"]["type"], "adaptive")
        self.assertEqual(streaming.body["output_config"]["effort"], "medium")
        self.assertNotIn("top_p", streaming.body)
        self.assertTrue(streaming.body["stream"])
        self.assertEqual(streaming.body["temperature"], 0)

    def test_claude_fable_cache_uses_messages_adaptive_effort(self) -> None:
        config = load_config()

        cache = build_request(
            config,
            "cache_profiles",
            "cache_long_context",
            model_family_override="claude_fable",
        )
        self.assertEqual(cache.metadata["transport"], "claude_messages")
        self.assertEqual(cache.metadata["request_endpoint"], "/messages")
        self.assertEqual(cache.body["thinking"]["type"], "adaptive")
        self.assertEqual(cache.body["output_config"]["effort"], "medium")
        self.assertNotIn("top_p", cache.body)
        self.assertNotIn("user_id", cache.body)
        self.assertIn("system", cache.body)
        self.assertTrue(cache.body["messages"])


class QwenFamilyWorkloadTest(unittest.TestCase):
    def test_qwen_throughput_uses_native_enable_thinking_profiles(self) -> None:
        config = load_config()

        short = build_request(
            config,
            "throughput_profiles",
            "baseline_short",
            model_family_override="qwen",
        )
        self.assertEqual(short.group, "throughput_profiles")
        self.assertEqual(short.metadata["profile_group"], "qwen_throughput_profiles")
        self.assertEqual(short.metadata["transport"], "chat_completions")
        self.assertEqual(short.body["enable_thinking"], False)
        self.assertNotIn("thinking", short.body)
        self.assertNotIn("user_id", short.body)
        self.assertFalse(short.warnings)

        streaming = build_request(
            config,
            "throughput_profiles",
            "streaming_latency_api",
            model_family_override="qwen",
        )
        self.assertEqual(streaming.metadata["profile_group"], "qwen_throughput_profiles")
        self.assertTrue(streaming.body["stream"])
        self.assertEqual(streaming.body["enable_thinking"], False)
        self.assertEqual(streaming.body["stream_options"]["include_usage"], True)
        self.assertNotIn("thinking", streaming.body)

        long_context = build_request(
            config,
            "throughput_profiles",
            "long_context",
            model_family_override="qwen",
        )
        self.assertEqual(long_context.body["enable_thinking"], False)
        self.assertNotIn("user_id", long_context.body)
        self.assertNotIn("thinking", long_context.body)

    def test_qwen_cache_uses_native_enable_thinking_profiles(self) -> None:
        config = load_config()

        cache = build_request(
            config,
            "cache_profiles",
            "cache_long_context",
            model_family_override="qwen",
        )
        self.assertEqual(cache.group, "cache_profiles")
        self.assertEqual(cache.metadata["profile_group"], "qwen_cache_profiles")
        self.assertEqual(cache.metadata["transport"], "chat_completions")
        self.assertEqual(cache.body["enable_thinking"], False)
        self.assertNotIn("thinking", cache.body)
        self.assertNotIn("user_id", cache.body)
        self.assertEqual(cache.body["max_tokens"], 128)
        self.assertFalse(cache.warnings)

    def test_qwen_compat_rewrites_shared_thinking_dict(self) -> None:
        from lib.deepseek_params import _apply_qwen_compat

        payload = {
            "thinking": {"type": "disabled"},
            "user_id": "loadtest_user",
            "max_tokens": 64,
        }
        _apply_qwen_compat(payload)
        self.assertEqual(payload["enable_thinking"], False)
        self.assertNotIn("thinking", payload)
        self.assertNotIn("user_id", payload)

        enabled = {"thinking": {"type": "enabled", "budget_tokens": 128}}
        _apply_qwen_compat(enabled)
        self.assertEqual(enabled["enable_thinking"], True)
        self.assertEqual(enabled["thinking_budget"], 128)


def _user_prompt_text(body: dict) -> str:
    if "messages" in body:
        return "\n".join(
            str(message.get("content") or "")
            for message in body["messages"]
            if message.get("role") == "user"
        )
    if "input" in body:
        return _responses_input_text(body["input"])
    return "\n".join(
        str(part.get("text") or "")
        for content in body.get("contents") or []
        for part in content.get("parts") or []
        if isinstance(part, dict)
    )


def _responses_input_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            text
            for item in value
            if (text := _responses_input_text(item))
        )
    if isinstance(value, dict):
        if value.get("role") not in (None, "user"):
            return ""
        for key in ("input_text", "text", "content"):
            if key in value:
                return _responses_input_text(value[key])
    return ""


if __name__ == "__main__":
    unittest.main()
