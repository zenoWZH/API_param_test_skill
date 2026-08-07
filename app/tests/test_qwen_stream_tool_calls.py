from __future__ import annotations

import json
import unittest

from lib.client import (
    _is_openai_tool_stream_body,
    _is_qwen_tool_stream_body,
    _merge_openai_stream_tool_calls,
)
from lib.profile_validation import validate_profile_response


def _ok_result():
    from types import SimpleNamespace

    return SimpleNamespace(success=True, failure_classification=None, error_type=None)


class OpenAIToolStreamMergeTest(unittest.TestCase):
    def test_is_openai_tool_stream_body_requires_flag_only(self) -> None:
        self.assertTrue(
            _is_openai_tool_stream_body(
                {"model": "qwen3.7-max", "tool_stream": True, "stream": True}
            )
        )
        self.assertTrue(
            _is_openai_tool_stream_body(
                {"model": "glm-5.2", "tool_stream": True, "stream": True}
            )
        )
        self.assertTrue(
            _is_openai_tool_stream_body(
                {"model": "GLM5.2", "tool_stream": True, "stream": True}
            )
        )
        self.assertFalse(
            _is_openai_tool_stream_body(
                {"model": "qwen3.7-max", "tool_stream": False, "stream": True}
            )
        )
        self.assertFalse(
            _is_openai_tool_stream_body(
                {"model": "deepseek-v4-pro", "stream": True}
            )
        )
        # Alias kept for older imports.
        self.assertTrue(
            _is_qwen_tool_stream_body(
                {"model": "glm-5.2", "tool_stream": True, "stream": True}
            )
        )

    def test_merge_qwen_tool_stream_fragments_into_valid_arguments(self) -> None:
        # Fragments captured from xinglian qwen3.7-max qwen_tool_stream failure.
        deltas = [
            {
                "index": 0,
                "id": "call_9488ce65d2b74438babf4bb6",
                "type": "function",
                "function": {"name": "get_weather", "arguments": ""},
            },
            {"index": 0, "id": "", "type": "function", "function": {"arguments": ""}},
            {
                "index": 0,
                "id": "",
                "type": "function",
                "function": {"arguments": '{"city": '},
            },
            {
                "index": 0,
                "id": "",
                "type": "function",
                "function": {"arguments": '"杭州'},
            },
            {"index": 0, "id": "", "type": "function", "function": {"arguments": '"'}},
            {
                "index": 0,
                "id": "",
                "type": "function",
                "function": {"arguments": ', "unit": '},
            },
            {
                "index": 0,
                "id": "",
                "type": "function",
                "function": {"arguments": '"celsius'},
            },
            {"index": 0, "id": "", "type": "function", "function": {"arguments": '"'}},
            {"index": 0, "id": "", "type": "function", "function": {"arguments": "}"}},
            {"index": 0, "id": "", "type": "function", "function": {"arguments": ""}},
        ]

        tool_calls: list[dict] = []
        for delta in deltas:
            _merge_openai_stream_tool_calls(tool_calls, [delta])

        self.assertEqual(len(tool_calls), 1)
        call = tool_calls[0]
        self.assertEqual(call["id"], "call_9488ce65d2b74438babf4bb6")
        self.assertEqual(call["type"], "function")
        self.assertEqual(call["function"]["name"], "get_weather")
        parsed = json.loads(call["function"]["arguments"])
        self.assertEqual(parsed, {"city": "杭州", "unit": "celsius"})

        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": tool_calls,
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        request_body = {
            "model": "qwen3.7-max",
            "stream": True,
            "tool_stream": True,
            "tool_choice": "auto",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get current weather for a city.",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        }
        self.assertIsNone(
            validate_profile_response(
                "qwen_tool_stream",
                response,
                _ok_result(),
                request_body=request_body,
                transport="chat_completions",
            )
        )

    def test_merge_glm_tool_stream_fragments_into_valid_arguments(self) -> None:
        # Fragments captured from neurospark GLM5.2 glm_tool_stream failure.
        deltas = [
            {
                "index": 0,
                "id": "call_38445c91331b46bdb1032f01",
                "type": "function",
                "function": {"name": "get_weather", "arguments": ""},
            },
            {"index": 0, "type": "function", "function": {"arguments": "{"}},
            {"index": 0, "type": "function", "function": {"arguments": '"city": "Sh'}},
            {"index": 0, "type": "function", "function": {"arguments": 'anghai", '}},
            {"index": 0, "type": "function", "function": {"arguments": '"unit": '}},
            {"index": 0, "type": "function", "function": {"arguments": '"celsius"'}},
            {"index": 0, "type": "function", "function": {"arguments": "}"}},
        ]

        tool_calls: list[dict] = []
        self.assertTrue(
            _is_openai_tool_stream_body(
                {"model": "GLM5.2", "tool_stream": True, "stream": True}
            )
        )
        for delta in deltas:
            _merge_openai_stream_tool_calls(tool_calls, [delta])

        self.assertEqual(len(tool_calls), 1)
        call = tool_calls[0]
        self.assertEqual(call["id"], "call_38445c91331b46bdb1032f01")
        self.assertEqual(call["function"]["name"], "get_weather")
        parsed = json.loads(call["function"]["arguments"])
        self.assertEqual(parsed, {"city": "Shanghai", "unit": "celsius"})

        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": tool_calls,
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        request_body = {
            "model": "GLM5.2",
            "stream": True,
            "tool_stream": True,
            "tool_choice": "auto",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get current weather for a city.",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        }
        self.assertIsNone(
            validate_profile_response(
                "glm_tool_stream",
                response,
                _ok_result(),
                request_body=request_body,
                transport="chat_completions",
            )
        )

    def test_non_tool_stream_path_keeps_extend_semantics(self) -> None:
        """Bodies without tool_stream=true do not opt into merge; extend stays caller-side."""
        body = {"model": "glm-5.2", "stream": True}
        self.assertFalse(_is_openai_tool_stream_body(body))

        tool_calls: list[dict] = []
        fragments = [
            {"index": 0, "id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": ""}},
            {"index": 0, "id": "", "type": "function", "function": {"arguments": '{"city":"Hangzhou"}'}},
        ]
        for fragment in fragments:
            tool_calls.extend([fragment])
        self.assertEqual(len(tool_calls), 2)
        self.assertEqual(tool_calls[0]["function"]["arguments"], "")
        self.assertEqual(tool_calls[1]["function"]["arguments"], '{"city":"Hangzhou"}')


if __name__ == "__main__":
    unittest.main()
