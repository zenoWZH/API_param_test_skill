from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib.client import ChatResult
from scripts import smoke_test


@pytest.mark.parametrize("mode,passed", [
    ("normal", True), ("extra_input", False), ("truncated", False),
    ("mutated_request", False), ("missing_usage", False),
])
def test_compatibility_smoke_enforces_quantity_and_complete_output(monkeypatch, mode, passed):
    body = {"model": "offline", "messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 16}
    built = SimpleNamespace(body=body, metadata={"transport": "chat_completions", "requested_model": "offline"}, warnings=[])
    def build(*args, **kwargs):
        assert kwargs["parameter_test"] is True
        return built
    monkeypatch.setattr(smoke_test, "build_request", build)
    monkeypatch.setattr(smoke_test, "get_reference_source", lambda _: {"params": {"max_tokens": {"supported": True}}})
    monkeypatch.setattr(smoke_test, "parameter_label_for_profile", lambda *_: "temperature")
    monkeypatch.setattr(smoke_test, "validate_profile_response", lambda *args, **kwargs: None)

    class Client:
        provider = "offline"
        def chat_completion(self, outbound):
            assert outbound["max_tokens"] == 256
            assert outbound["messages"] == [{"role": "user", "content": "Reply with OK."}]
            if mode == "mutated_request":
                outbound["messages"].insert(0, {"role": "system", "content": "hidden extra input"})
            usage = {"prompt_tokens": 4096 if mode == "extra_input" else 12,
                     "completion_tokens": 1, "total_tokens": 4097 if mode == "extra_input" else 13}
            if mode == "missing_usage":
                usage = {}
            finish = "length" if mode == "truncated" else "stop"
            return ChatResult(success=True, status_code=200, latency_ms=1, timestamp=0,
                usage=usage, finish_reason=finish, text="OK",
                response_json={"choices": [{"message": {"content": "OK"}, "finish_reason": finish}], "usage": usage})

    result = smoke_test.run_profile_smoke(Client(), {}, "compatibility_profiles", "probe", reference_source="offline")
    assert result["pass"] is passed
    assert result["token_validation_pass"] is passed
