from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from lib.config import load_config
from lib.deepseek_params import (
    build_claude_tool_followup_request,
    build_native_tool_followup_request,
    build_request,
    build_tool_followup_request,
)
from lib.profile_validation import (
    validate_profile_response,
    validate_tool_followup_response,
)


def result(success: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        success=success,
        failure_classification=None,
        error_type=None,
    )


class ToolValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()

    def test_openai_tool_call_validates_structure_name_and_arguments(self) -> None:
        request = build_request(
            self.config,
            "compatibility_profiles",
            "gemini_tools",
            model_family_override="gemini",
        )
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_weather",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": json.dumps({"city": "Beijing"}),
                                },
                            }
                        ],
                    }
                }
            ]
        }

        self.assertIsNone(
            validate_profile_response(
                "gemini_tools",
                response,
                result(),
                request_body=request.body,
                transport="chat_completions",
            )
        )
        invalid = copy.deepcopy(response)
        invalid["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{"
        self.assertEqual(
            validate_profile_response(
                "gemini_tools",
                invalid,
                result(),
                request_body=request.body,
                transport="chat_completions",
            ),
            "tool_call_arguments_invalid",
        )
        self.assertEqual(
            validate_profile_response(
                "gemini_tools",
                response,
                result(),
                request_body=request.body,
                transport="chat_completions",
                tool_validation_mode="gemini_native",
            ),
            "native_function_call_missing",
        )

    def test_openai_followup_preserves_provider_message_extensions(self) -> None:
        request = build_request(
            self.config,
            "compatibility_profiles",
            "gemini_tool_choice_auto",
            model_family_override="gemini",
        )
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "extra_content": {
                            "google": {"thought_signature": "signature-value"}
                        },
                        "tool_calls": [
                            {
                                "id": "call_weather",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": "{\"city\":\"Beijing\"}",
                                },
                            }
                        ],
                    }
                }
            ]
        }

        followup = build_tool_followup_request(request.body, response)
        assistant = followup["messages"][-2]
        tool = followup["messages"][-1]
        self.assertEqual(
            assistant["extra_content"]["google"]["thought_signature"],
            "signature-value",
        )
        self.assertEqual(tool["tool_call_id"], "call_weather")
        self.assertEqual(followup["tool_choice"], "none")

    def test_native_function_call_and_followup_preserve_signature(self) -> None:
        request = build_request(
            self.config,
            "compatibility_profiles",
            "gemini_native_tools",
            model_family_override="gemini",
        )
        response = {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "id": "native_weather",
                                    "name": "get_weather",
                                    "args": {"city": "Beijing"},
                                },
                                "thoughtSignature": "native-signature",
                            }
                        ],
                    }
                }
            ]
        }

        self.assertIsNone(
            validate_profile_response(
                "gemini_native_tools",
                response,
                result(),
                request_body=request.body,
                transport="gemini_generate_content",
            )
        )
        followup = build_native_tool_followup_request(request.body, response)
        model_part = followup["contents"][-2]["parts"][0]
        function_response = followup["contents"][-1]["parts"][0]["functionResponse"]
        self.assertEqual(model_part["thoughtSignature"], "native-signature")
        self.assertEqual(function_response["id"], "native_weather")
        self.assertEqual(function_response["name"], "get_weather")

    def test_gemini_native_thinking_requires_marked_thought_summary(self) -> None:
        response = {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "thought": True,
                                "text": "Compare the quantities before answering.",
                            },
                            {"text": "9.8 is larger."},
                        ],
                    }
                }
            ]
        }
        for profile in (
            "gemini_native_thinking_medium",
            "gemini_native_thinking_high",
        ):
            with self.subTest(profile=profile):
                self.assertIsNone(
                    validate_profile_response(
                        profile,
                        response,
                        result(),
                        transport="gemini_generate_content",
                        reference_source="gemini_native_generate_content",
                    )
                )
                missing = copy.deepcopy(response)
                missing["candidates"][0]["content"]["parts"][0]["thought"] = False
                self.assertEqual(
                    validate_profile_response(
                        profile,
                        missing,
                        result(),
                        transport="gemini_generate_content",
                        reference_source="gemini_native_generate_content",
                    ),
                    "thought_summary_missing",
                )

    def test_gemini_candidate_count_validates_response_cardinality(self) -> None:
        chat_request = build_request(
            self.config,
            "compatibility_profiles",
            "gemini_n",
            model_family_override="gemini",
        )
        one_choice = {
            "choices": [{"message": {"role": "assistant", "content": "one"}}]
        }
        self.assertEqual(
            validate_profile_response(
                "gemini_n",
                one_choice,
                result(),
                request_body=chat_request.body,
            ),
            "n_choices_mismatch",
        )
        two_choices = copy.deepcopy(one_choice)
        two_choices["choices"].append(
            {"message": {"role": "assistant", "content": "two"}}
        )
        self.assertIsNone(
            validate_profile_response(
                "gemini_n",
                two_choices,
                result(),
                request_body=chat_request.body,
            )
        )
        chat_alias_request = build_request(
            self.config,
            "compatibility_profiles",
            "gemini_chat_candidate_count",
            model_family_override="gemini",
        )
        self.assertEqual(
            validate_profile_response(
                "gemini_chat_candidate_count",
                one_choice,
                result(),
                request_body=chat_alias_request.body,
            ),
            "n_choices_mismatch",
        )
        self.assertIsNone(
            validate_profile_response(
                "gemini_chat_candidate_count",
                two_choices,
                result(),
                request_body=chat_alias_request.body,
            )
        )

        native_request = build_request(
            self.config,
            "compatibility_profiles",
            "gemini_native_candidate_count",
            model_family_override="gemini",
        )
        one_candidate = {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "one"}]}}
            ]
        }
        self.assertEqual(
            validate_profile_response(
                "gemini_native_candidate_count",
                one_candidate,
                result(),
                request_body=native_request.body,
                transport="gemini_generate_content",
            ),
            "n_choices_mismatch",
        )
        two_candidates = copy.deepcopy(one_candidate)
        two_candidates["candidates"].append(
            {"content": {"role": "model", "parts": [{"text": "two"}]}}
        )
        self.assertIsNone(
            validate_profile_response(
                "gemini_native_candidate_count",
                two_candidates,
                result(),
                request_body=native_request.body,
                transport="gemini_generate_content",
            )
        )

    def test_followup_requires_final_text_and_no_pending_calls(self) -> None:
        final = {
            "choices": [
                {"message": {"role": "assistant", "content": "北京当前天气晴朗。"}}
            ]
        }
        self.assertIsNone(
            validate_tool_followup_response(
                final,
                result(),
                transport="chat_completions",
            )
        )
        self.assertEqual(
            validate_tool_followup_response(
                {"choices": [{"message": {"role": "assistant", "content": ""}}]},
                result(),
                transport="chat_completions",
            ),
            "tool_followup_content_missing",
        )

    def test_claude_tool_profile_validates_openai_compat_calls(self) -> None:
        request = build_request(
            self.config,
            "compatibility_profiles",
            "claude_tools",
            model_family_override="claude",
        )
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_weather",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": json.dumps({"city": "Beijing"}),
                                },
                            }
                        ],
                    }
                }
            ]
        }

        self.assertIn("tools", request.body)
        self.assertIsNone(
            validate_profile_response(
                "claude_tools",
                response,
                result(),
                request_body=request.body,
                transport="chat_completions",
            )
        )
        followup = build_tool_followup_request(request.body, response)
        self.assertEqual(followup["messages"][-1]["role"], "tool")
        self.assertIsNone(
            validate_tool_followup_response(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "北京当前天气晴朗。",
                            }
                        }
                    ]
                },
                result(),
                transport="chat_completions",
            )
        )

    def test_claude_native_tool_profile_validates_tool_use_blocks(self) -> None:
        request = build_request(
            self.config,
            "compatibility_profiles",
            "claude_native_tools",
            model_family_override="claude",
        )
        response = {
            "content": [
                {
                    "id": "toolu_weather",
                    "type": "tool_use",
                    "name": "get_weather",
                    "input": {"city": "Beijing"},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 12, "output_tokens": 8},
        }

        self.assertEqual(request.metadata["transport"], "claude_messages")
        self.assertIn("tools", request.body)
        self.assertEqual(request.body["tools"][0]["name"], "get_weather")
        self.assertIsNone(
            validate_profile_response(
                "claude_native_tools",
                response,
                result(),
                request_body=request.body,
                transport="claude_messages",
            )
        )
        followup = build_claude_tool_followup_request(request.body, response)
        self.assertEqual(followup["messages"][-2]["role"], "assistant")
        self.assertEqual(followup["messages"][-1]["role"], "user")
        self.assertEqual(followup["messages"][-1]["content"][0]["type"], "tool_result")
        self.assertIsNone(
            validate_tool_followup_response(
                {
                    "content": [{"type": "text", "text": "北京当前天气晴朗。"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 18, "output_tokens": 6},
                },
                result(),
                transport="claude_messages",
            )
        )

    def test_gemini_vertex_fingerprint_requires_traffic_type(self) -> None:
        vertex_response = {
            "candidates": [{"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 2, "trafficType": "ON_DEMAND"},
        }
        studio_response = {
            "candidates": [{"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 2, "serviceTier": "standard"},
        }

        self.assertIsNone(
            validate_profile_response(
                "gemini_vertex_traffic_type",
                vertex_response,
                result(),
                transport="gemini_generate_content",
            )
        )
        self.assertEqual(
            validate_profile_response(
                "gemini_vertex_traffic_type",
                studio_response,
                result(),
                transport="gemini_generate_content",
            ),
            "vertex_traffic_type_missing",
        )
        self.assertEqual(
            validate_profile_response(
                "gemini_vertex_service_tier_body",
                {
                    "candidates": [{"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}],
                    "usageMetadata": {
                        "trafficType": "ON_DEMAND",
                        "serviceTier": "standard",
                    },
                },
                result(),
                transport="gemini_generate_content",
            ),
            "vertex_service_tier_unexpected",
        )

    def test_kimi_k3_dynamic_tools_validate_message_declaration(self) -> None:
        request = build_request(
            self.config,
            "compatibility_profiles",
            "kimi_k3_dynamic_tools",
            overrides={"model": "kimi-k3"},
            model_family_override="gpt",
            enforce_model_capabilities=False,
        )
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "I should use the weather tool.",
                        "tool_calls": [
                            {
                                "id": "call_weather",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": json.dumps({"city": "Beijing"}),
                                },
                            }
                        ],
                    }
                }
            ]
        }
        self.assertIsNone(
            validate_profile_response(
                "kimi_k3_dynamic_tools",
                response,
                result(),
                request_body=request.body,
                transport="chat_completions",
            )
        )
        wrong_name = copy.deepcopy(response)
        wrong_name["choices"][0]["message"]["tool_calls"][0]["function"][
            "name"
        ] = "undeclared_tool"
        self.assertEqual(
            validate_profile_response(
                "kimi_k3_dynamic_tools",
                wrong_name,
                result(),
                request_body=request.body,
                transport="chat_completions",
            ),
            "tool_call_unknown_function",
        )

    def test_kimi_k3_requires_reasoning_and_preserved_history_semantics(self) -> None:
        missing_reasoning = {
            "choices": [{"message": {"role": "assistant", "content": "OK"}}]
        }
        self.assertEqual(
            validate_profile_response(
                "kimi_k3_reasoning_high",
                missing_reasoning,
                result(),
            ),
            "reasoning_content_missing",
        )
        preserved = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "The hidden pair was preserved.",
                        "content": "215, 222",
                    }
                }
            ]
        }
        self.assertIsNone(
            validate_profile_response(
                "kimi_k3_preserved_thinking",
                preserved,
                result(),
            )
        )
        preserved["choices"][0]["message"]["content"] = "I do not know."
        self.assertEqual(
            validate_profile_response(
                "kimi_k3_preserved_thinking",
                preserved,
                result(),
            ),
            "preserved_thinking_mismatch",
        )

    def test_deepseek_thinking_profiles_validate_reasoning_semantics(self) -> None:
        missing_reasoning = {
            "choices": [{"message": {"role": "assistant", "content": "OK"}}]
        }
        self.assertEqual(
            validate_profile_response(
                "thinking_enabled",
                missing_reasoning,
                result(),
                reference_source="deepseek_chat",
            ),
            "reasoning_content_missing",
        )
        self.assertIsNone(
            validate_profile_response(
                "thinking_enabled",
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "reasoning_content": "I should answer concisely.",
                                "content": "OK",
                            }
                        }
                    ]
                },
                result(),
                reference_source="deepseek_chat",
            )
        )
        self.assertIsNone(
            validate_profile_response(
                "thinking_disabled",
                missing_reasoning,
                result(),
                reference_source="deepseek_chat",
            )
        )
        self.assertEqual(
            validate_profile_response(
                "thinking_disabled",
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "reasoning_content": "Thinking was not disabled.",
                                "content": "OK",
                            }
                        }
                    ]
                },
                result(),
                reference_source="deepseek_chat",
            ),
            "reasoning_content_unexpected",
        )

    def test_deepseek_reasoning_validation_is_source_specific(self) -> None:
        response = {
            "choices": [{"message": {"role": "assistant", "content": "OK"}}]
        }
        self.assertIsNone(
            validate_profile_response(
                "thinking_enabled",
                response,
                result(),
                reference_source="gemini_openai_compat",
            )
        )

    def test_glm_reasoning_levels_validate_enabled_and_disabled_semantics(self) -> None:
        no_reasoning = {
            "choices": [{"message": {"role": "assistant", "content": "9.8 is larger."}}]
        }
        with_reasoning = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "Align the decimal places before comparing.",
                        "content": "9.8 is larger.",
                    }
                }
            ]
        }
        for profile in (
            "glm_thinking_enabled",
            "glm_reasoning_low",
            "glm_reasoning_medium",
            "glm_reasoning_high",
            "glm_reasoning_xhigh",
            "glm_reasoning_max",
        ):
            with self.subTest(profile=profile):
                self.assertEqual(
                    validate_profile_response(
                        profile,
                        no_reasoning,
                        result(),
                        reference_source="glm_openai_compat",
                    ),
                    "reasoning_content_missing",
                )
                self.assertIsNone(
                    validate_profile_response(
                        profile,
                        with_reasoning,
                        result(),
                        reference_source="glm_openai_compat",
                    )
                )

        for profile in (
            "glm_thinking_disabled",
            "glm_reasoning_none",
            "glm_reasoning_minimal",
        ):
            with self.subTest(profile=profile):
                self.assertIsNone(
                    validate_profile_response(
                        profile,
                        no_reasoning,
                        result(),
                        reference_source="glm_openai_compat",
                    )
                )
                self.assertEqual(
                    validate_profile_response(
                        profile,
                        with_reasoning,
                        result(),
                        reference_source="glm_openai_compat",
                    ),
                    "reasoning_content_unexpected",
                )

        request = build_request(
            self.config,
            "compatibility_profiles",
            "glm_tool_calls_thinking",
            overrides={"model": "glm-5.2"},
            model_family_override="glm",
            enforce_model_capabilities=False,
        )
        tool_response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "I need the weather tool.",
                        "tool_calls": [
                            {
                                "id": "call_weather",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": json.dumps({"city": "Beijing"}),
                                },
                            }
                        ],
                    }
                }
            ]
        }
        self.assertIsNone(
            validate_profile_response(
                "glm_tool_calls_thinking",
                tool_response,
                result(),
                request_body=request.body,
                reference_source="glm_openai_compat",
            )
        )
        followup = build_tool_followup_request(
            request.body,
            tool_response,
            pass_reasoning_content=True,
        )
        assistant = followup["messages"][-2]
        self.assertEqual(
            assistant["reasoning_content"],
            "I need the weather tool.",
        )

    def test_qwen_reasoning_and_preserved_history_semantics(self) -> None:
        no_reasoning = {
            "choices": [{"message": {"role": "assistant", "content": "9.8 is larger."}}]
        }
        with_reasoning = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "Align the decimal places before comparing.",
                        "content": "9.8 is larger.",
                    }
                }
            ]
        }
        for profile in ("qwen_thinking_enabled", "qwen_thinking_budget"):
            with self.subTest(profile=profile):
                self.assertEqual(
                    validate_profile_response(
                        profile,
                        no_reasoning,
                        result(),
                        reference_source="qwen_openai_compat",
                    ),
                    "reasoning_content_missing",
                )
                self.assertIsNone(
                    validate_profile_response(
                        profile,
                        with_reasoning,
                        result(),
                        reference_source="qwen_openai_compat",
                    )
                )

        self.assertIsNone(
            validate_profile_response(
                "qwen_thinking_disabled",
                no_reasoning,
                result(),
                reference_source="qwen_openai_compat",
            )
        )
        self.assertEqual(
            validate_profile_response(
                "qwen_thinking_disabled",
                with_reasoning,
                result(),
                reference_source="qwen_openai_compat",
            ),
            "reasoning_content_unexpected",
        )
        preserved = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "The hidden pair was retained.",
                        "content": "215, 222",
                    }
                }
            ]
        }
        self.assertIsNone(
            validate_profile_response(
                "qwen_preserve_thinking",
                preserved,
                result(),
                reference_source="qwen_openai_compat",
            )
        )
        preserved["choices"][0]["message"]["content"] = "I do not know."
        self.assertEqual(
            validate_profile_response(
                "qwen_preserve_thinking",
                preserved,
                result(),
                reference_source="qwen_openai_compat",
            ),
            "preserved_thinking_mismatch",
        )

    def test_openai_reasoning_context_requires_effective_context_echo(self) -> None:
        request = {
            "reasoning": {
                "effort": "medium",
                "context": "all_turns",
            }
        }
        response = {
            "reasoning": {
                "effort": "medium",
                "context": "all_turns",
            },
            "output": [],
        }
        self.assertIsNone(
            validate_profile_response(
                "openai_responses_reasoning_context_all_turns",
                response,
                result(),
                request_body=request,
                transport="openai_responses",
                reference_source="openai_gpt56_responses",
            )
        )
        missing = copy.deepcopy(response)
        missing["reasoning"].pop("context")
        self.assertEqual(
            validate_profile_response(
                "openai_responses_reasoning_context_all_turns",
                missing,
                result(),
                request_body=request,
                transport="openai_responses",
                reference_source="openai_gpt56_responses",
            ),
            "reasoning_context_mismatch",
        )
        wrong = copy.deepcopy(response)
        wrong["reasoning"]["context"] = "current_turn"
        self.assertEqual(
            validate_profile_response(
                "openai_responses_reasoning_context_all_turns",
                wrong,
                result(),
                request_body=request,
                transport="openai_responses",
                reference_source="openai_gpt56_responses",
            ),
            "reasoning_context_mismatch",
        )

if __name__ == "__main__":
    unittest.main()
