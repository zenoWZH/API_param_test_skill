from __future__ import annotations

import copy
import json
import os
import random
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lib.cache_suite import _random_trajectory, run_cache_suite
from lib.config import load_config
from lib.deepseek_params import cache_tokens_from_usage


class FakeCacheClient:
    def __init__(self) -> None:
        self.bodies: list[dict[str, object]] = []

    def chat_completion(self, body: dict[str, object]) -> SimpleNamespace:
        self.bodies.append(copy.deepcopy(body))
        index = len(self.bodies)
        if index == 1:
            usage = {
                "prompt_tokens": 5000,
                "completion_tokens": 10,
                "total_tokens": 5010,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 5000,
            }
        else:
            usage = {
                "prompt_tokens": 5200,
                "completion_tokens": 10,
                "total_tokens": 5210,
                "prompt_cache_hit_tokens": 4900,
                "prompt_cache_miss_tokens": 300,
            }
        return SimpleNamespace(
            timestamp=time.time(),
            success=True,
            status_code=200,
            latency_ms=100,
            ttft_ms=None,
            text=f"cache answer {index}",
            response_length=100,
            finish_reason="stop",
            usage=usage,
            error_type=None,
            failure_classification=None,
            cache_headers={},
        )


class FakeGrowingCacheClient:
    def __init__(self) -> None:
        self.bodies: list[dict[str, object]] = []
        self.seen: dict[str, int] = {}
        self.usages = [
            {
                "prompt_tokens": 5000,
                "completion_tokens": 10,
                "total_tokens": 5010,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 5000,
            },
            {
                "prompt_tokens": 5200,
                "completion_tokens": 10,
                "total_tokens": 5210,
                "prompt_cache_hit_tokens": 4900,
                "prompt_cache_miss_tokens": 300,
            },
            {
                "prompt_tokens": 5450,
                "completion_tokens": 10,
                "total_tokens": 5460,
                "prompt_cache_hit_tokens": 5100,
                "prompt_cache_miss_tokens": 350,
            },
            {
                "prompt_tokens": 5700,
                "completion_tokens": 10,
                "total_tokens": 5710,
                "prompt_cache_hit_tokens": 5400,
                "prompt_cache_miss_tokens": 300,
            },
        ]

    def chat_completion(self, body: dict[str, object]) -> SimpleNamespace:
        self.bodies.append(copy.deepcopy(body))
        index = len(self.bodies)
        if index <= len(self.usages):
            usage = self.usages[index - 1]
        else:
            key = json.dumps(body, ensure_ascii=False, sort_keys=True)
            seen = self.seen.get(key, 0)
            self.seen[key] = seen + 1
            system = str((body.get("messages") or [{}])[0].get("content") or "")
            hit = 4900 if system.startswith("positive-control") and seen else 0
            usage = {
                "prompt_tokens": 5000,
                "completion_tokens": 10,
                "total_tokens": 5010,
                "prompt_cache_hit_tokens": hit,
                "prompt_cache_miss_tokens": 5000 - hit,
            }
        return SimpleNamespace(
            timestamp=time.time(),
            success=True,
            status_code=200,
            latency_ms=100,
            ttft_ms=None,
            text=f"assistant response {index}",
            response_length=100,
            finish_reason="stop",
            usage=usage,
            error_type=None,
            failure_classification=None,
            cache_headers={},
        )


class FakeKilocodeCacheClient:
    def __init__(self) -> None:
        self.bodies: list[dict[str, object]] = []
        self.seen: dict[str, int] = {}
        self.prev_messages: list[object] = []
        self.prev_prompt_tokens = 0

    def chat_completion(self, body: dict[str, object]) -> SimpleNamespace:
        self.bodies.append(copy.deepcopy(body))
        messages = body.get("messages") or []
        system = str(messages[0].get("content") or "") if messages else ""
        key = json.dumps(body, ensure_ascii=False, sort_keys=True)
        seen = self.seen.get(key, 0)
        self.seen[key] = seen + 1
        if system.startswith("positive-control"):
            prompt_tokens, hit_tokens = 5000, 4900 if seen else 0
        elif system.startswith("negative-control"):
            prompt_tokens, hit_tokens = 5000, 0
        else:
            prompt_tokens = 5000 + 150 * max(len(messages) - 2, 0)
            if self.prev_messages and messages[: len(self.prev_messages)] == self.prev_messages:
                hit_tokens = self.prev_prompt_tokens
            else:
                hit_tokens = 0
            self.prev_messages = messages
            self.prev_prompt_tokens = prompt_tokens
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 10,
            "total_tokens": prompt_tokens + 10,
            "prompt_cache_hit_tokens": hit_tokens,
            "prompt_cache_miss_tokens": prompt_tokens - hit_tokens,
        }
        return SimpleNamespace(
            timestamp=time.time(),
            success=True,
            status_code=200,
            latency_ms=100 if not seen else 50,
            ttft_ms=None,
            text="ok",
            response_json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": usage},
            response_length=100,
            finish_reason="stop",
            usage=usage,
            error_type=None,
            failure_classification=None,
            cache_headers={},
        )


class FakeKilocodeNativeCacheClient:
    def __init__(self, transport: str) -> None:
        self.transport = transport
        self.bodies: list[dict[str, object]] = []
        self.prev_items: list[object] = []
        self.prev_prompt_tokens = 0

    def _chain(self, items: list[object]) -> tuple[int, int]:
        prompt_tokens = 5000 + 150 * max(len(items) - 1, 0)
        if self.prev_items and items[: len(self.prev_items)] == self.prev_items:
            hit_tokens = self.prev_prompt_tokens
        else:
            hit_tokens = 0
        self.prev_items = items
        self.prev_prompt_tokens = prompt_tokens
        return prompt_tokens, hit_tokens

    def claude_messages(self, body: dict[str, object]) -> SimpleNamespace:
        self.bodies.append(copy.deepcopy(body))
        prompt_tokens, hit_tokens = self._chain(body.get("messages") or [])
        usage = {
            "input_tokens": prompt_tokens - hit_tokens,
            "cache_read_input_tokens": hit_tokens,
            "cache_creation_input_tokens": 0,
            "output_tokens": 10,
        }
        response_json = {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": usage,
        }
        return self._result(response_json, usage)

    def gemini_generate_content(self, model: str, body: dict[str, object]) -> SimpleNamespace:
        self.bodies.append(copy.deepcopy(body))
        prompt_tokens, hit_tokens = self._chain(body.get("contents") or [])
        usage = {
            "promptTokenCount": prompt_tokens,
            "cachedContentTokenCount": hit_tokens,
            "candidatesTokenCount": 10,
            "totalTokenCount": prompt_tokens + 10,
        }
        response_json = {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "ok"}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": usage,
        }
        return self._result(response_json, usage)

    @staticmethod
    def _result(response_json: dict[str, object], usage: dict[str, object]) -> SimpleNamespace:
        return SimpleNamespace(
            timestamp=time.time(),
            success=True,
            status_code=200,
            latency_ms=100,
            ttft_ms=None,
            text="ok",
            response_json=response_json,
            response_length=100,
            finish_reason="stop",
            usage=usage,
            error_type=None,
            failure_classification=None,
            cache_headers={},
        )


class FakeProgressiveCacheClient:
    def __init__(
        self,
        *,
        emit_tool_calls: bool = True,
        unexpected_tool_calls: bool = False,
    ) -> None:
        self.emit_tool_calls = emit_tool_calls
        self.unexpected_tool_calls = unexpected_tool_calls
        self.bodies: list[dict[str, object]] = []

    def chat_completion(self, body: dict[str, object]) -> SimpleNamespace:
        snapshot = copy.deepcopy(body)
        self.bodies.append(snapshot)
        messages = body.get("messages") or []
        latest = messages[-1] if messages else {}
        latest_content = str(latest.get("content") or "") if isinstance(latest, dict) else ""
        is_tool_request = (
            self.emit_tool_calls
            and bool(body.get("tools"))
            and isinstance(latest, dict)
            and latest.get("role") == "user"
            and (self.unexpected_tool_calls or "get_weather" in latest_content)
        )
        prompt_tokens = len(messages) * 100
        hit_tokens = 0 if len(messages) <= 2 else max(prompt_tokens - 100, 0)
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 10,
            "total_tokens": prompt_tokens + 10,
            "prompt_cache_hit_tokens": hit_tokens,
            "prompt_cache_miss_tokens": prompt_tokens - hit_tokens,
        }
        message: dict[str, object]
        if is_tool_request:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{len(self.bodies)}",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city":"杭州"}',
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        else:
            message = {
                "role": "assistant",
                "content": f"真实助手回复 {len(self.bodies)}",
            }
            finish_reason = "stop"
        response_json = {
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": usage,
        }
        return SimpleNamespace(
            timestamp=time.time(),
            success=True,
            status_code=200,
            latency_ms=100,
            ttft_ms=None,
            text=str(message.get("content") or ""),
            response_json=response_json,
            response_length=100,
            finish_reason=finish_reason,
            usage=usage,
            error_type=None,
            failure_classification=None,
            cache_headers={},
        )


class FakeNativeCustomerCacheClient:
    def __init__(self, transport: str) -> None:
        self.transport = transport
        self.bodies: list[dict[str, object]] = []

    def claude_messages(self, body: dict[str, object]) -> SimpleNamespace:
        self.bodies.append(copy.deepcopy(body))
        messages = body.get("messages") or []
        latest = messages[-1].get("content") if messages else ""
        latest_text = latest if isinstance(latest, str) else json.dumps(latest, ensure_ascii=False)
        initial_tool = bool(body.get("tools")) and "get_weather" in latest_text
        followup = isinstance(latest, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in latest
        )
        response_json = {
            "content": (
                [
                    {
                        "type": "tool_use",
                        "id": f"toolu_{len(self.bodies)}",
                        "name": "get_weather",
                        "input": {"city": "杭州", "unit": "celsius"},
                    }
                ]
                if initial_tool
                else [{"type": "text", "text": "ok"}]
            ),
            "stop_reason": "tool_use" if initial_tool else "end_turn",
        }
        usage = {
            "input_tokens": 50 if followup else 200,
            "cache_read_input_tokens": 150 if followup else 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 10,
        }
        response_json["usage"] = usage
        return self._result(response_json, usage)

    def gemini_generate_content(self, model: str, body: dict[str, object]) -> SimpleNamespace:
        self.bodies.append(copy.deepcopy(body))
        contents = body.get("contents") or []
        latest_parts = contents[-1].get("parts") if contents else []
        latest_text = "".join(
            str(part.get("text") or "")
            for part in latest_parts or []
            if isinstance(part, dict)
        )
        initial_tool = bool(body.get("tools")) and "get_weather" in latest_text
        followup = any(
            isinstance(part, dict) and part.get("functionResponse")
            for part in latest_parts or []
        )
        response_json = {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": (
                            [
                                {
                                    "functionCall": {
                                        "name": "get_weather",
                                        "args": {"city": "杭州", "unit": "celsius"},
                                    }
                                }
                            ]
                            if initial_tool
                            else [{"text": "ok"}]
                        ),
                    },
                    "finishReason": "STOP",
                }
            ]
        }
        usage = {
            "promptTokenCount": 350 if followup else 200,
            "cachedContentTokenCount": 150 if followup else 0,
            "candidatesTokenCount": 10,
            "totalTokenCount": 360 if followup else 210,
        }
        response_json["usageMetadata"] = usage
        return self._result(response_json, usage)

    @staticmethod
    def _result(response_json: dict[str, object], usage: dict[str, object]) -> SimpleNamespace:
        return SimpleNamespace(
            timestamp=time.time(),
            success=True,
            status_code=200,
            latency_ms=100,
            ttft_ms=None,
            text="ok",
            response_json=response_json,
            response_length=100,
            finish_reason="stop",
            usage=usage,
            error_type=None,
            failure_classification=None,
            cache_headers={},
        )


class CacheSuiteTest(unittest.TestCase):
    def test_cache_usage_parsers_cover_claude_and_gemini(self) -> None:
        self.assertEqual(
            cache_tokens_from_usage(
                {
                    "input_tokens": 100,
                    "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 4900,
                }
            ),
            (4900, 300),
        )
        self.assertEqual(
            cache_tokens_from_usage(
                {"promptTokenCount": 5200, "cachedContentTokenCount": 4900}
            ),
            (4900, 300),
        )
        self.assertEqual(
            cache_tokens_from_usage(
                {
                    "prompt_tokens": 100,
                    "prompt_tokens_details": {"cached_tokens": 120},
                }
            ),
            (120, 0),
        )

    def test_measured_requests_use_unique_suffix_and_shared_prefix_stats(self) -> None:
        with patch.dict(os.environ, {"LOADTEST_PROVIDER": "yibu", "LOADTEST_MODEL": "deepseek-v4-pro"}):
            config = load_config()
        config["active_provider"] = "yibu"
        config["providers"]["yibu"]["models"]["default"] = "deepseek-v4-pro"
        config["cache_test"] = {
            **(config.get("cache_test") or {}),
            "scenario": "shared_prefix",
            "warmup_requests": 2,
            "wait_after_warmup_sec": 0,
            "controls": {
                "mode": "custom",
                "positive_long_prefix_pairs": 1,
                "negative_unique_prefix_requests": 1,
            },
        }
        client = FakeCacheClient()

        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_cache_suite(
                config,
                client,
                Path(temp_dir),
                measured_requests=3,
            )
            record_rows = [
                json.loads(line)
                for line in (Path(temp_dir) / "request_records.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        marker = "请求唯一随机串（仅用于区分请求，不参与共享前缀命中率统计）："
        main_bodies = [
            body
            for body in client.bodies
            if marker in str(body["messages"][-1].get("content") or "")
        ]
        suffixes = [
            str(body["messages"][-1]["content"]).split(marker, 1)[1]
            for body in main_bodies
        ]
        self.assertEqual(len(client.bodies), 8)
        self.assertEqual(len(main_bodies), 5)
        self.assertEqual(suffixes[0], "")
        self.assertTrue(all(len(suffix) == 200 and suffix.isdigit() for suffix in suffixes[1:]))
        self.assertEqual(len(set(suffixes[1:])), 4)
        warmup_audits = [
            row["cache_token_audit"]
            for row in record_rows
            if row.get("phase") == "cache_warmup"
        ]
        self.assertEqual(
            [audit["expected_reusable_tokens"] for audit in warmup_audits],
            [0, 5000],
        )

        summary = result["summary"]
        self.assertEqual(summary["business_record_count"], 3)
        self.assertEqual(summary["cache_eligible_record_count"], 3)
        self.assertEqual(summary["cache_shared_prefix_record_count"], 3)
        self.assertEqual(summary["cache_shared_prefix_tokens"], 15000)
        self.assertEqual(summary["cache_hit_tokens"], 14700)
        self.assertEqual(summary["cache_miss_tokens"], 300)
        self.assertEqual(summary["cache_hit_rate"], 0.98)

    def test_growing_conversation_uses_previous_prompt_tokens_as_denominator(self) -> None:
        with patch.dict(os.environ, {"LOADTEST_PROVIDER": "yibu", "LOADTEST_MODEL": "deepseek-v4-pro"}):
            config = load_config()
        config["active_provider"] = "yibu"
        config["providers"]["yibu"]["models"]["default"] = "deepseek-v4-pro"
        config["cache_test"] = {
            **(config.get("cache_test") or {}),
            "scenario": "growing_conversation",
            "warmup_requests": 1,
            "wait_after_warmup_sec": 0,
            "assistant_history_max_chars": 1000,
            "controls": {
                "mode": "custom",
                "positive_long_prefix_pairs": 1,
                "negative_unique_prefix_requests": 1,
            },
        }
        client = FakeGrowingCacheClient()

        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_cache_suite(
                config,
                client,
                Path(temp_dir),
                measured_requests=3,
            )

        main_bodies = [
            body
            for body in client.bodies
            if str(body["messages"][0].get("content") or "").startswith("cache-run-")
        ]
        self.assertEqual(len(client.bodies), 7)
        self.assertEqual(len(main_bodies), 4)
        self.assertEqual(
            [len(body["messages"]) for body in main_bodies],
            [2, 4, 6, 8],
        )
        self.assertFalse(
            any(
                "请求唯一随机串" in str(message.get("content") or "")
                for body in main_bodies
                for message in body["messages"]
                if isinstance(message, dict)
            )
        )

        summary = result["summary"]
        self.assertEqual(summary["business_record_count"], 3)
        self.assertEqual(summary["cache_eligible_record_count"], 3)
        self.assertEqual(summary["cache_shared_prefix_record_count"], 3)
        self.assertEqual(summary["cache_shared_prefix_tokens"], 15650)
        self.assertEqual(summary["cache_hit_tokens"], 15400)
        self.assertEqual(summary["cache_miss_tokens"], 250)
        self.assertAlmostEqual(summary["cache_hit_rate"], 15400 / 15650)

    def test_progressive_customer_session_grows_real_conversations_and_reports_v10_metrics(self) -> None:
        with patch.dict(os.environ, {"LOADTEST_PROVIDER": "yibu", "LOADTEST_MODEL": "deepseek-v4-pro"}):
            config = load_config()
        config["active_provider"] = "yibu"
        config["providers"]["yibu"]["models"]["default"] = "deepseek-v4-pro"
        config["cache_test"] = {
            **(config.get("cache_test") or {}),
            "scenario": "progressive_customer_session",
            "sessions": 2,
            "rounds_per_session": 4,
            "content_profile": "custom",
            "resolved_content_ranges": {
                "user_chars": {"min": 220, "max": 220},
                "tool_result_chars": {"min": 600, "max": 600},
            },
            "tool_stage": {"enabled": True, "round": 3},
            "controls": {
                "mode": "custom",
                "positive_long_prefix_pairs": 1,
                "negative_unique_prefix_requests": 1,
            },
            "wait_after_seed_sec": 0,
            "max_run_seconds": 60,
            "consecutive_failure_limit": 3,
            "seed": 42,
            "evidence_mode": "official_usage",
            "estimated_request_count": 14,
        }
        client = FakeProgressiveCacheClient()

        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_cache_suite(config, client, Path(temp_dir))

        self.assertEqual(result["schema_version"], 10)
        self.assertEqual(result["actual_request_count"], 14)
        self.assertEqual(len(client.bodies), 14)
        main_bodies = [
            body
            for body in client.bodies
            if str(body["messages"][0].get("content") or "").startswith("cache-run-")
        ]
        customer_bodies = main_bodies[:-1]
        self.assertEqual(main_bodies[-1]["messages"][-1]["content"], "x")
        self.assertEqual(main_bodies[-1]["max_tokens"], 1)
        stable_system = config["cache_test"]["stable_system"]
        self.assertTrue(
            all(body["messages"][0]["content"].endswith(stable_system) for body in main_bodies)
        )
        self.assertTrue(all(body.get("tools") == main_bodies[0].get("tools") for body in main_bodies))

        newly_sent_users = [
            str(body["messages"][-1]["content"])
            for body in customer_bodies
            if body["messages"][-1].get("role") == "user"
        ]
        self.assertEqual(len(newly_sent_users), 8)
        self.assertEqual(len(newly_sent_users), len(set(newly_sent_users)))
        self.assertTrue(all(len(value) == 220 for value in newly_sent_users))

        session_one = [main_bodies[index] for index in (0, 2, 4, 5, 8)]
        for previous, current in zip(session_one, session_one[1:]):
            self.assertEqual(
                current["messages"][: len(previous["messages"])],
                previous["messages"],
            )
        tool_results = [
            str(body["messages"][-1]["content"])
            for body in main_bodies
            if body["messages"][-1].get("role") == "tool"
        ]
        self.assertEqual(len(tool_results), 2)
        self.assertEqual(len(set(tool_results)), 2)
        self.assertTrue(all(len(value) == 600 for value in tool_results))

        summary = result["summary"]
        self.assertAlmostEqual(summary["cached_input_token_ratio"], 0.8)
        self.assertAlmostEqual(summary["actual_cache_hit_rate"], 0.8)
        self.assertAlmostEqual(summary["structural_hit_rate_ceiling"], 4400 / 6000)
        self.assertAlmostEqual(summary["cache_efficiency"], 12 / 11)
        self.assertEqual(summary["cache_efficiency_status"], "exceeds_structure")
        self.assertEqual(summary["structure_probe_input_tokens"], 200)
        self.assertAlmostEqual(summary["progressive_prefix_reuse_rate"], 1.0)
        self.assertAlmostEqual(summary["tool_followup_reuse_rate"], 1.0)
        self.assertAlmostEqual(summary["cache_hit_request_ratio"], 0.8)
        self.assertEqual(summary["session_completion_ratio"], 1.0)
        self.assertEqual(summary["tool_flow_supported_session_ratio"], 1.0)
        self.assertEqual(summary["cache_stage_metrics"]["tool_followup"]["request_count"], 2)

    def test_progressive_tool_unsupported_does_not_create_a_fake_followup_request(self) -> None:
        with patch.dict(os.environ, {"LOADTEST_PROVIDER": "yibu", "LOADTEST_MODEL": "deepseek-v4-pro"}):
            config = load_config()
        config["active_provider"] = "yibu"
        config["providers"]["yibu"]["models"]["default"] = "deepseek-v4-pro"
        config["cache_test"] = {
            **(config.get("cache_test") or {}),
            "scenario": "progressive_customer_session",
            "sessions": 1,
            "rounds_per_session": 3,
            "resolved_content_ranges": {
                "user_chars": {"min": 200, "max": 200},
                "tool_result_chars": {"min": 500, "max": 500},
            },
            "tool_stage": {"enabled": True, "round": 2},
            "controls": {
                "mode": "custom",
                "positive_long_prefix_pairs": 1,
                "negative_unique_prefix_requests": 1,
            },
            "wait_after_seed_sec": 0,
            "max_run_seconds": 60,
            "consecutive_failure_limit": 3,
            "estimated_request_count": 8,
        }
        client = FakeProgressiveCacheClient(emit_tool_calls=False)

        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_cache_suite(config, client, Path(temp_dir))
            request_rows = [
                json.loads(line)
                for line in (Path(temp_dir) / "request_records.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(result["actual_request_count"], 6)
        self.assertEqual(result["summary"]["cache_stage_metrics"]["tool_followup"]["request_count"], 0)
        self.assertEqual(result["summary"]["tool_flow_unsupported_session_count"], 1)
        self.assertEqual(result["summary"]["tool_flow_supported_session_ratio"], 0.0)
        self.assertEqual(result["session_outcomes"][0]["stop_reason"], "tool_flow_unsupported")
        self.assertTrue(all(record["success"] for record in request_rows))

    def test_progressive_unexpected_tool_call_stops_only_that_session(self) -> None:
        with patch.dict(os.environ, {"LOADTEST_PROVIDER": "yibu", "LOADTEST_MODEL": "deepseek-v4-pro"}):
            config = load_config()
        config["active_provider"] = "yibu"
        config["providers"]["yibu"]["models"]["default"] = "deepseek-v4-pro"
        config["cache_test"] = {
            **(config.get("cache_test") or {}),
            "scenario": "progressive_customer_session",
            "sessions": 1,
            "rounds_per_session": 3,
            "resolved_content_ranges": {
                "user_chars": {"min": 200, "max": 200},
                "tool_result_chars": {"min": 500, "max": 500},
            },
            "tool_stage": {"enabled": True, "round": 3},
            "controls": {
                "mode": "custom",
                "positive_long_prefix_pairs": 1,
                "negative_unique_prefix_requests": 1,
            },
            "wait_after_seed_sec": 0,
            "max_run_seconds": 60,
            "consecutive_failure_limit": 3,
            "estimated_request_count": 8,
        }
        client = FakeProgressiveCacheClient(unexpected_tool_calls=True)

        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_cache_suite(config, client, Path(temp_dir))

        self.assertEqual(result["actual_request_count"], 5)
        self.assertEqual(result["session_outcomes"][0]["stop_reason"], "unexpected_tool_call")
        self.assertEqual(result["summary"]["session_completion_ratio"], 0.0)

    def test_kilocode_agent_session_append_only_monotonic_hits_and_metrics(self) -> None:
        with patch.dict(os.environ, {"LOADTEST_PROVIDER": "yibu", "LOADTEST_MODEL": "deepseek-v4-pro"}):
            config = load_config()
        config["active_provider"] = "yibu"
        config["providers"]["yibu"]["models"]["default"] = "deepseek-v4-pro"
        config["cache_test"] = {
            **(config.get("cache_test") or {}),
            "scenario": "kilocode_agent_session",
            "steps": 4,
            "trajectory_mode": "scripted",
            "warmup_requests": 1,
            "controls": {
                "positive_long_prefix_pairs": 1,
                "negative_unique_prefix_requests": 1,
            },
            "wait_after_seed_sec": 0,
            "max_run_seconds": 60,
            "consecutive_failure_limit": 3,
            "seed": 42,
            "evidence_mode": "official_usage",
            "estimated_request_count": 8,
        }
        client = FakeKilocodeCacheClient()

        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_cache_suite(config, client, Path(temp_dir))

        self.assertEqual(result["schema_version"], 11)
        self.assertEqual(result["scenario"], "kilocode_agent_session")
        self.assertEqual(result["actual_request_count"], 8)
        self.assertEqual(len(client.bodies), 8)

        session_bodies = client.bodies[:5]
        self.assertEqual(len(session_bodies[0]["messages"]), 2)
        self.assertEqual(len(session_bodies[0]["tools"]), 10)
        for previous, current in zip(session_bodies, session_bodies[1:]):
            self.assertEqual(
                current["messages"][: len(previous["messages"])],
                previous["messages"],
            )
            self.assertEqual(current["tools"], session_bodies[0]["tools"])
        self.assertEqual(
            [len(body["messages"]) for body in session_bodies],
            [2, 5, 8, 11, 14],
        )
        self.assertTrue(
            all(body["messages"][0]["role"] == "system" for body in session_bodies)
        )
        self.assertTrue(
            all(
                body["messages"][0]["content"] == session_bodies[0]["messages"][0]["content"]
                for body in session_bodies
            )
        )
        step_records = result["step_records"]
        self.assertEqual(len(step_records), 4)
        step_hits = [record["cache_hit_tokens"] for record in step_records]
        self.assertEqual(step_hits, sorted(step_hits))
        self.assertGreater(step_hits[0], 0)
        self.assertEqual(
            [record["prompt_tokens"] for record in step_records],
            [5450, 5900, 6350, 6800],
        )

        summary = result["summary"]
        self.assertAlmostEqual(summary["cached_input_token_ratio"], 22700 / 24500)
        self.assertEqual(summary["cache_hit_rate_semantics"], "cached_input_tokens/input_tokens")
        self.assertEqual(summary["kilocode_step_count"], 4)
        self.assertEqual(summary["kilocode_step_success_count"], 4)
        self.assertEqual(summary["cache_hit_request_ratio"], 1.0)
        self.assertEqual(summary["cache_measurement_coverage"], 1.0)
        step_metrics = summary["kilocode_step_metrics"]
        self.assertEqual(sorted(step_metrics), ["step_1", "step_2", "step_3", "step_4"])
        self.assertEqual(step_metrics["step_1"]["prompt_tokens"], 5450)
        self.assertEqual(step_metrics["step_1"]["cache_hit_tokens"], 5000)
        self.assertAlmostEqual(
            step_metrics["step_1"]["cached_input_token_ratio"], 5000 / 5450
        )
        self.assertEqual(step_metrics["step_4"]["prompt_tokens"], 6800)
        self.assertEqual(step_metrics["step_4"]["cache_hit_tokens"], 6350)
        self.assertAlmostEqual(
            summary["cache_control_metrics"]["positive_long_prefix"]["cached_input_token_ratio"],
            4900 / 5000,
        )
        self.assertEqual(
            summary["cache_control_metrics"]["negative_unique_prefix"]["cached_input_token_ratio"],
            0,
        )
        self.assertNotIn("cache_case_metrics", summary)
        self.assertNotIn("session_completion_ratio", summary)

    def test_kilocode_agent_session_supports_claude_and_gemini_native_transports(self) -> None:
        for family, model, transport in (
            ("claude", "claude-sonnet-4-6", "claude_messages"),
            ("gemini", "gemini-2.5-flash", "gemini_generate_content"),
        ):
            with self.subTest(transport=transport), patch.dict(
                os.environ,
                {"LOADTEST_PROVIDER": "yibu", "LOADTEST_MODEL": model},
            ):
                config = load_config()
                config["active_provider"] = "yibu"
                models = config["providers"]["yibu"]["models"]
                models["default"] = model
                models["candidates"] = list(models.get("candidates") or []) + [model]
                models["families"][model] = family
                models["transports"][model] = transport
                config["providers"]["yibu"]["api_interfaces"][transport] = {
                    "base_url": "https://example.invalid/v1",
                    "path": "/messages" if transport == "claude_messages" else "/models/{model}:generateContent",
                    "auth": "anthropic" if transport == "claude_messages" else "google_api_key",
                }
                config["cache_test"] = {
                    **(config.get("cache_test") or {}),
                    "scenario": "kilocode_agent_session",
                    "steps": 2,
                    "trajectory_mode": "scripted",
                    "warmup_requests": 1,
                    "controls": {
                        "positive_long_prefix_pairs": 1,
                        "negative_unique_prefix_requests": 1,
                    },
                    "wait_after_seed_sec": 0,
                    "max_run_seconds": 60,
                    "consecutive_failure_limit": 3,
                    "seed": 7,
                    "evidence_mode": "official_usage",
                    "estimated_request_count": 6,
                }
                client = FakeKilocodeNativeCacheClient(transport)
                with tempfile.TemporaryDirectory() as temp_dir:
                    result = run_cache_suite(config, client, Path(temp_dir))

                self.assertEqual(len(client.bodies), 6)
                conversation_key = "contents" if transport == "gemini_generate_content" else "messages"
                main_bodies = client.bodies[:3]
                for previous, current in zip(main_bodies, main_bodies[1:]):
                    self.assertEqual(
                        current[conversation_key][: len(previous[conversation_key])],
                        previous[conversation_key],
                    )
                step_body = main_bodies[1]
                if transport == "claude_messages":
                    assistant = step_body["messages"][-2]
                    self.assertEqual(assistant["role"], "assistant")
                    self.assertEqual(assistant["content"][0]["type"], "tool_use")
                    tool_result = step_body["messages"][-1]
                    self.assertEqual(tool_result["content"][0]["type"], "tool_result")
                    self.assertEqual(
                        tool_result["content"][0]["tool_use_id"],
                        assistant["content"][0]["id"],
                    )
                    self.assertTrue(step_body["tools"][0]["input_schema"])
                else:
                    model_turn = step_body["contents"][-2]
                    self.assertEqual(model_turn["role"], "model")
                    self.assertIn("functionCall", model_turn["parts"][0])
                    response_turn = step_body["contents"][-1]
                    self.assertIn("functionResponse", response_turn["parts"][0])
                    declarations = step_body["tools"][0]["functionDeclarations"]
                    self.assertEqual(len(declarations), 10)
                    self.assertEqual(
                        declarations[0]["parameters"]["type"], "OBJECT"
                    )

                summary = result["summary"]
                self.assertEqual(summary["kilocode_step_count"], 2)
                self.assertEqual(summary["kilocode_step_success_count"], 2)
                self.assertIsNotNone(summary["cached_input_token_ratio"])
                self.assertEqual(
                    sorted(summary["kilocode_step_metrics"]), ["step_1", "step_2"]
                )

    def test_kilocode_random_trajectory_is_deterministic_per_seed(self) -> None:
        first = _random_trajectory(12, random.Random(7))
        second = _random_trajectory(12, random.Random(7))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 12)
        self.assertTrue(
            all(step["tool"] in {"read", "grep", "bash", "glob", "edit"} for step in first)
        )
        other = _random_trajectory(12, random.Random(8))
        self.assertNotEqual(first, other)

    def test_progressive_customer_session_supports_native_transports(self) -> None:
        for family, model, transport in (
            ("claude", "claude-sonnet-4-6", "claude_messages"),
            ("gemini", "gemini-2.5-flash", "gemini_generate_content"),
        ):
            with self.subTest(transport=transport), patch.dict(
                os.environ,
                {"LOADTEST_PROVIDER": "yibu", "LOADTEST_MODEL": model},
            ):
                config = load_config()
                config["active_provider"] = "yibu"
                models = config["providers"]["yibu"]["models"]
                models["default"] = model
                models["candidates"] = list(models.get("candidates") or []) + [model]
                models["families"][model] = family
                models["transports"][model] = transport
                config["providers"]["yibu"]["api_interfaces"][transport] = {
                    "base_url": "https://example.invalid/v1",
                    "path": "/messages" if transport == "claude_messages" else "/models/{model}:generateContent",
                    "auth": "anthropic" if transport == "claude_messages" else "google_api_key",
                }
                config["cache_test"] = {
                    **(config.get("cache_test") or {}),
                    "scenario": "progressive_customer_session",
                    "sessions": 1,
                    "rounds_per_session": 3,
                    "resolved_content_ranges": {
                        "user_chars": {"min": 200, "max": 200},
                        "tool_result_chars": {"min": 500, "max": 500},
                    },
                    "tool_stage": {"enabled": True, "round": 2},
                    "controls": {
                        "mode": "custom",
                        "positive_long_prefix_pairs": 1,
                        "negative_unique_prefix_requests": 1,
                    },
                    "wait_after_seed_sec": 0,
                    "max_run_seconds": 60,
                    "consecutive_failure_limit": 3,
                    "seed": 9,
                    "estimated_request_count": 8,
                }
                client = FakeNativeCustomerCacheClient(transport)
                with tempfile.TemporaryDirectory() as temp_dir:
                    result = run_cache_suite(config, client, Path(temp_dir))

                self.assertEqual(result["schema_version"], 10)
                self.assertEqual(len(client.bodies), 8)
                main_bodies = client.bodies[:5]
                structure_probe = main_bodies[-1]
                if transport == "claude_messages":
                    self.assertEqual(structure_probe["messages"][-1]["content"], "x")
                    self.assertEqual(structure_probe["max_tokens"], 1)
                else:
                    self.assertEqual(
                        structure_probe["contents"][-1]["parts"][-1]["text"],
                        "x",
                    )
                    self.assertEqual(
                        structure_probe["generationConfig"]["maxOutputTokens"],
                        1,
                    )
                self.assertEqual(
                    structure_probe.get("tools"),
                    main_bodies[0].get("tools"),
                )
                self.assertIsNotNone(result["summary"]["structural_hit_rate_ceiling"])
                self.assertIsNotNone(result["summary"]["cache_efficiency"])
                self.assertEqual(result["summary"]["session_completion_ratio"], 1.0)
                self.assertEqual(result["summary"]["tool_flow_supported_session_ratio"], 1.0)
                self.assertEqual(
                    result["summary"]["cache_stage_metrics"]["tool_followup"]["request_count"],
                    1,
                )


if __name__ == "__main__":
    unittest.main()
