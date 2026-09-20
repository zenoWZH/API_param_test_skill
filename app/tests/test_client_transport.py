from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

from lib.client import OpenAICompatibleClient
from scripts.smoke_test import _send_transport_request


class FakeResponse:
    status_code = 200
    content = b"{}"
    headers: dict[str, str] = {}
    text = "{}"

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self) -> dict:
        return self.payload


class ClientTransportTest(unittest.TestCase):
    def test_smoke_dispatches_all_registry_transports(self) -> None:
        client = Mock()
        client.chat_completion.return_value = "chat"
        client.claude_messages.return_value = "claude"
        client.gemini_generate_content.return_value = "gemini"

        self.assertEqual(
            _send_transport_request(client, "chat_completions", "m", {"a": 1}),
            "chat",
        )
        self.assertEqual(
            _send_transport_request(client, "claude_messages", "m", {"b": 2}),
            "claude",
        )
        self.assertEqual(
            _send_transport_request(
                client, "gemini_generate_content", "m", {"c": 3}
            ),
            "gemini",
        )
        client.gemini_generate_content.assert_called_once_with("m", {"c": 3})

    def setUp(self) -> None:
        self.client = OpenAICompatibleClient(
            "https://fallback.example/v1",
            "secret",
            api_interfaces={
                "chat_completions": {
                    "base_url": "https://chat.example/v1",
                    "path": "/chat/completions",
                    "auth": "bearer",
                },
                "claude_messages": {
                    "base_url": "https://claude.example/v1",
                    "path": "/messages",
                    "auth": "anthropic",
                },
                "gemini_generate_content": {
                    "base_url": "https://gemini.example/v1beta",
                    "path": "/models/{model}:generateContent",
                    "auth": "google_api_key",
                },
                "token_count": {
                    "base_url": "https://chat.example/v1",
                    "path": "/token-count",
                    "auth": "bearer",
                    "transports": ["chat_completions"],
                    "request_wrapper": "request",
                    "response_field": "usage.input_tokens",
                },
            },
        )

    def test_configured_token_count_interface_returns_complete_independent_count(self) -> None:
        self.client.session.post = Mock(
            return_value=FakeResponse({"usage": {"input_tokens": 42}})
        )

        result = self.client.count_tokens(
            "chat_completions", "model-a", {"messages": []}
        )

        self.assertEqual(result["tokens"], 42)
        self.assertEqual(result["evidence_level"], "official_count")
        self.assertIs(result["covers_full_input"], True)
        call = self.client.session.post.call_args
        self.assertEqual(call.args[0], "https://chat.example/v1/token-count")
        self.assertEqual(call.kwargs["json"], {"request": {"messages": []}})

    def test_count_wrapper_includes_model_and_full_input_without_mutating_it(self) -> None:
        interface = self.client.api_interfaces["token_count"]
        interface.update(request_wrapper="generateContentRequest", request_model_field="model")
        body = {"contents": [{"parts": [{"text": "Hello"}]}], "systemInstruction": {"parts": [{"text": "Reply OK"}]}}
        self.client.session.post = Mock(return_value=FakeResponse({"usage": {"input_tokens": 12}}))
        counted = self.client.count_tokens("chat_completions", "model-a", body)
        sent = self.client.session.post.call_args.kwargs["json"]["generateContentRequest"]
        self.assertEqual(sent, {**body, "model": "models/model-a"})
        self.assertNotIn("model", body)
        self.assertTrue(counted["covers_full_input"])

    def test_count_dispatch_mutation_cannot_be_used_as_complete_input_evidence(self) -> None:
        for raises in (False, True):
            with self.subTest(raises=raises):
                def changed(*args, **kwargs):
                    kwargs["json"]["request"]["messages"].append({"role": "system", "content": "extra"})
                    if raises:
                        raise RuntimeError("counter aborted after changing input")
                    return FakeResponse({"usage": {"input_tokens": 42}})
                self.client.session.post = Mock(side_effect=changed)
                body = {"messages": [{"role": "user", "content": "Hello"}]}
                counted = self.client.count_tokens("chat_completions", "model-a", body)
                self.assertEqual(counted["request_integrity"], "fail")
                self.assertIsNone(counted["tokens"])
                self.assertEqual(len(body["messages"]), 1)

    def test_configured_token_count_rejects_non_integer_or_negative_values(self) -> None:
        for invalid in (True, 1.5, "42", -1):
            with self.subTest(invalid=invalid):
                self.client.session.post = Mock(
                    return_value=FakeResponse({"usage": {"input_tokens": invalid}})
                )

                self.assertIsNone(
                    self.client.count_tokens(
                        "chat_completions", "model-a", {"messages": []}
                    )
                )

    def test_claude_uses_persistent_session_and_anthropic_interface(self) -> None:
        self.client.session.post = Mock(
            return_value=FakeResponse(
                {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {}}
            )
        )
        result = self.client.claude_messages(
            {"model": "claude-test", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertTrue(result.success)
        call = self.client.session.post.call_args
        self.assertEqual(call.args[0], "https://claude.example/v1/messages")
        self.assertEqual(call.kwargs["headers"]["x-api-key"], "secret")
        self.assertNotIn("Authorization", call.kwargs["headers"])
        self.assertFalse(call.kwargs["allow_redirects"])

    def test_gemini_uses_native_resource_path_and_google_auth(self) -> None:
        self.client.session.post = Mock(
            return_value=FakeResponse(
                {
                    "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
                    "usageMetadata": {},
                }
            )
        )
        result = self.client.gemini_generate_content("gemini test", {"contents": []})
        self.assertTrue(result.success)
        call = self.client.session.post.call_args
        self.assertEqual(
            call.args[0],
            "https://gemini.example/v1beta/models/gemini%20test:generateContent",
        )
        self.assertEqual(call.kwargs["headers"]["x-goog-api-key"], "secret")

    def test_gemini_merges_optional_request_headers(self) -> None:
        self.client.session.post = Mock(
            return_value=FakeResponse(
                {
                    "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"trafficType": "ON_DEMAND"},
                }
            )
        )
        result = self.client.gemini_generate_content(
            "gemini-test",
            {"contents": []},
            headers={"X-Vertex-AI-LLM-Request-Type": "shared"},
        )
        self.assertTrue(result.success)
        headers = self.client.session.post.call_args.kwargs["headers"]
        self.assertEqual(headers["x-goog-api-key"], "secret")
        self.assertEqual(headers["X-Vertex-AI-LLM-Request-Type"], "shared")

    def test_optional_headers_cannot_override_auth_case_insensitively(self) -> None:
        self.client.session.post = Mock()
        with self.assertRaisesRegex(ValueError, "not allowed"):
            self.client.gemini_generate_content(
                "gemini-test",
                {"contents": []},
                headers={"X-Goog-Api-Key": "attacker-key"},
            )
        self.client.session.post.assert_not_called()

    def test_mutated_interface_cannot_send_key_to_unbound_origin(self) -> None:
        self.client.api_interfaces["chat_completions"]["base_url"] = "https://attacker.example/v1"
        self.client.session.post = Mock()
        with self.assertRaisesRegex(ValueError, "unbound origin"):
            self.client.chat_completion({"model": "m", "messages": []})
        self.client.session.post.assert_not_called()

    def test_upstream_key_echo_is_redacted_from_result(self) -> None:
        key = "super-secret-api-key-123"
        client = OpenAICompatibleClient("https://chat.example/v1", key)
        client.session.post = Mock(
            return_value=FakeResponse({"error": {"message": f"bad credential {key}"}})
        )
        result = client.chat_completion({"model": "m", "messages": []})
        self.assertNotIn(key, json.dumps(result.response_json))
        self.assertNotIn(key, result.raw_text)


if __name__ == "__main__":
    unittest.main()
