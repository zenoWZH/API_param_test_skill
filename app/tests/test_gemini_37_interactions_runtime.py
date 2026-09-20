"""Offline App coverage for the bounded Gemini 3.7 Interactions transport.

The shared MPDB enables the bounded 18-profile parameter selection; fake-client
construction remains independent from live proof and never sends provider traffic.
"""
from __future__ import annotations


import copy


import json


from typing import Any


from unittest.mock import Mock


import pytest


from lib.client import ChatResult, OpenAICompatibleClient


from lib.config import load_config


from lib.deepseek_params import (
    GEMINI_37_GENERATE_CONTENT_SAFE_PROFILES,
    GEMINI_37_INTERACTIONS_PROFILES,
    _build_gemini_interactions_body,
    build_request,
)


from lib.profile_validation import validate_profile_response


from lib.model_profile_catalog import (
    database_snapshot,
    get_model_profile_catalog,
    resolve_runtime_profile_binding,
)


from lib.reference_specs import (
    capability_profile_snapshot,
    test_profiles_for_reference as reference_test_profiles,
)


from lib.token_audit import (
    audit_exchange,
    combine_exchange_audits,
    normalize_usage,
    summarize_token_audits,
)


from scripts.param_test import (
    _input_group_for_profile,
    run_identity_probe,
    run_one_profile,
    run_param_tests,
)


MODEL = "gemini-3.7-flash"


GENERATE_SOURCE = "gemini_3_7_flash_generate_content"


INTERACTIONS_SOURCE = "gemini_3_7_flash_interactions"


def _usage() -> dict[str, int]:
    return {
        "total_input_tokens": 8,
        "total_output_tokens": 2,
        "total_thought_tokens": 0,
        "total_tokens": 10,
        "total_tool_use_tokens": 0,
        "total_cached_tokens": 0,
    }


def _text_payload(text: str = "OK") -> dict[str, Any]:
    return {
        "id": "v1_interaction_test",
        "object": "interaction",
        "model": MODEL,
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "content": [{"type": "text", "text": text}],
            }
        ],
        "usage": _usage(),
    }


def _tool_payload() -> dict[str, Any]:
    payload = _text_payload()
    payload["status"] = "requires_action"
    payload["steps"] = [
        {
            "id": "call_weather_1",
            "type": "function_call",
            "name": "get_weather",
            "arguments": {"city": "Beijing"},
        }
    ]
    return payload


def _result(payload: dict[str, Any]) -> ChatResult:
    status = str(payload["status"])
    return ChatResult(
        success=True,
        status_code=200,
        latency_ms=5.0,
        timestamp=0.0,
        response_json=payload,
        text="OK" if status == "completed" else "",
        tool_calls=[payload["steps"][0]] if status == "requires_action" else [],
        finish_reason=status,
        usage=payload["usage"],
        ttft_ms=1.0,
    )


def _build(profile: str, source: str, api_form: str):
    return build_request(
        load_config(),
        "compatibility_profiles",
        profile,
        overrides={"model": MODEL, "prompt": "Reply briefly."},
        model_family_override="gemini",
        api_form_override=api_form,
        route_profile_override="google_ai_studio",
        reference_source=source,
        enforce_model_capabilities=False,
    )


def test_contract_profile_ids_build_with_exact_safe_transports() -> None:
    generate_profiles = reference_test_profiles(GENERATE_SOURCE)
    interaction_profiles = reference_test_profiles(INTERACTIONS_SOURCE)
    assert len(generate_profiles) == 26
    assert set(generate_profiles) == GEMINI_37_GENERATE_CONTENT_SAFE_PROFILES
    assert "gemini_native_safety_settings" not in generate_profiles
    assert len(interaction_profiles) == 18
    assert set(interaction_profiles) == GEMINI_37_INTERACTIONS_PROFILES

    for profile in generate_profiles:
        built = _build(profile, GENERATE_SOURCE, "gemini_generate_content")
        assert built.metadata["transport"] == "gemini_generate_content"
        assert "cachedContent" not in built.body
        generation = built.body.get("generationConfig") or {}
        assert "imageConfig" not in generation
        assert "IMAGE" not in generation.get("responseModalities", [])
        assert all(
            setting.get("threshold") != "BLOCK_NONE"
            for setting in built.body.get("safetySettings") or []
        )

    for profile in interaction_profiles:
        built = _build(profile, INTERACTIONS_SOURCE, "gemini_interactions")
        assert built.metadata["transport"] == "gemini_interactions"
        assert built.metadata["multi_turn"] is False
        assert built.body["model"] == MODEL
        assert built.body["store"] is False
        assert isinstance(built.body["input"], str)
        assert not {
            "background",
            "previous_interaction_id",
            "safety_settings",
        }.intersection(built.body)


def test_generate_content_model_contract_rejects_unsafe_profiles() -> None:
    with pytest.raises(ValueError, match="outside the Gemini 3.7 Flash"):
        _build(
            "gemini_native_safety_settings",
            GENERATE_SOURCE,
            "gemini_generate_content",
        )
    with pytest.raises(ValueError, match="outside the Gemini 3.7 Flash"):
        _build(
            "gemini_native_safety_block_none_nsfw",
            GENERATE_SOURCE,
            "gemini_generate_content",
        )
    with pytest.raises(ValueError, match="outside the Gemini 3.7 Flash"):
        _build(
            "gemini_native_cached_content",
            GENERATE_SOURCE,
            "gemini_generate_content",
        )


def test_interactions_builder_blocks_stateful_and_non_text_controls() -> None:
    config = load_config()
    base = {
        "prompt": "safe prompt",
        "interaction_store": False,
        "interaction_generation_config": {"max_output_tokens": 32},
    }
    assert _build_gemini_interactions_body(config, base, MODEL)["store"] is False

    for update, match in (
        ({"interaction_store": True}, "store=false"),
        ({"store": True}, "store=true"),
        ({"previous_interaction_id": "v1_old"}, "stateless"),
        ({"background": True}, "synchronous"),
        ({"safety_settings": []}, "not enabled"),
        (
            {"interaction_response_format": {"type": "image", "mime_type": "image/jpeg"}},
            "type=text",
        ),
    ):
        settings = copy.deepcopy(base)
        settings.update(update)
        with pytest.raises(ValueError, match=match):
            _build_gemini_interactions_body(config, settings, MODEL)


def test_interactions_profile_wire_shapes_and_input_groups() -> None:
    config = load_config()
    basic = _build(
        "gemini_3_7_flash_interactions_basic",
        INTERACTIONS_SOURCE,
        "gemini_interactions",
    ).body
    assert basic["generation_config"]["max_output_tokens"] == 2048
    assert _build(
        "gemini_3_7_flash_interactions_max_output_tokens",
        INTERACTIONS_SOURCE,
        "gemini_interactions",
    ).body["generation_config"]["max_output_tokens"] == 768

    json_body = _build(
        "gemini_3_7_flash_interactions_response_format_json",
        INTERACTIONS_SOURCE,
        "gemini_interactions",
    ).body
    assert json_body["response_format"]["type"] == "text"
    assert json_body["response_format"]["mime_type"] == "application/json"
    assert json_body["response_format"]["schema"]["type"] == "object"

    for suffix, mode in (
        ("auto", "auto"),
        ("any", "any"),
        ("none", "none"),
        ("validated", "validated"),
    ):
        profile = f"gemini_3_7_flash_interactions_tools_{suffix}"
        body = _build(profile, INTERACTIONS_SOURCE, "gemini_interactions").body
        assert body["tools"][0]["type"] == "function"
        assert body["generation_config"]["tool_choice"] == mode

    assert (
        _input_group_for_profile(
            config, "gemini_3_7_flash_interactions_response_format_json"
        )
        == "json_output"
    )
    assert (
        _input_group_for_profile(config, "gemini_3_7_flash_interactions_tools_any")
        == "tool_calls"
    )
    assert (
        _input_group_for_profile(config, "gemini_3_7_flash_interactions_tools_none")
        == "general"
    )


class _FakeResponse:
    status_code = 200
    headers: dict[str, str] = {"content-type": "application/json"}

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self) -> dict[str, Any]:
        return self.payload


class _FakeStreamResponse:
    status_code = 200
    encoding = "utf-8"
    headers: dict[str, str] = {"content-type": "text/event-stream"}
    content = b""
    text = ""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def iter_lines(self, decode_unicode: bool = True):
        for line in self.lines:
            yield line
            if line.startswith("data:"):
                yield ""  # Each synthetic event is a complete SSE frame.

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _client() -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        "https://fallback.example/v1",
        "test-google-key",
        provider="gemini-test",
        api_interfaces={
            "gemini_interactions": {
                "base_url": "https://generativelanguage.googleapis.com",
                "path": "/v1beta/interactions",
                "auth": "google_api_key",
            }
        },
    )


def test_interactions_json_client_uses_v1beta_google_auth_and_extracts_contract() -> None:
    client = _client()
    client.session.post = Mock(return_value=_FakeResponse(_text_payload()))
    result = client.gemini_interactions(
        {"model": MODEL, "input": "hi", "store": False}
    )
    assert result.success is True
    assert result.text == "OK"
    assert result.finish_reason == "completed"
    assert result.usage == _usage()
    call = client.session.post.call_args
    assert call.args[0] == "https://generativelanguage.googleapis.com/v1beta/interactions"
    assert call.kwargs["headers"]["x-goog-api-key"] == "test-google-key"
    assert call.kwargs["allow_redirects"] is False


def test_default_interactions_interface_normalizes_openai_compat_base_to_v1beta_origin() -> None:
    client = OpenAICompatibleClient(
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "test-google-key",
    )
    client.session.post = Mock(return_value=_FakeResponse(_text_payload()))
    assert client.gemini_interactions({"model": MODEL, "input": "hi"}).success
    assert (
        client.session.post.call_args.args[0]
        == "https://generativelanguage.googleapis.com/v1beta/interactions"
    )


def test_interactions_sse_client_reconstructs_steps_status_model_and_usage() -> None:
    usage = _usage()
    lines = [
        'event: interaction.created',
        'data: {"interaction":{"id":"v1_stream","status":"in_progress","object":"interaction","model":"gemini-3.7-flash"},"event_type":"interaction.created"}',
        'event: step.start',
        'data: {"index":0,"step":{"type":"thought"},"event_type":"step.start"}',
        'event: step.delta',
        'data: {"index":0,"delta":{"content":{"type":"text","text":"brief thought"},"type":"thought_summary"},"event_type":"step.delta"}',
        'event: step.stop',
        'data: {"index":0,"event_type":"step.stop"}',
        'event: step.start',
        'data: {"index":1,"step":{"type":"model_output"},"event_type":"step.start"}',
        'event: step.delta',
        'data: {"index":1,"delta":{"type":"text","text":"O"},"event_type":"step.delta"}',
        'event: step.delta',
        'data: {"index":1,"delta":{"type":"text","text":"K"},"event_type":"step.delta"}',
        'event: step.stop',
        'data: {"index":1,"event_type":"step.stop"}',
        "event: interaction.completed",
        "data: "
        + json.dumps(
            {
                "interaction": {
                    "id": "v1_stream",
                    "status": "completed",
                    "model": MODEL,
                    "usage": usage,
                },
                "event_type": "interaction.completed",
            }
        ),
        "event: done",
        "data: [DONE]",
    ]
    client = _client()
    client.session.post = Mock(return_value=_FakeStreamResponse(lines))
    result = client.gemini_interactions(
        {"model": MODEL, "input": "hi", "store": False, "stream": True}
    )
    assert result.success is True
    assert result.text == "OK"
    assert result.response_json["id"] == "v1_stream"
    assert result.response_json["model"] == MODEL
    assert result.response_json["status"] == "completed"
    assert result.response_json["steps"][0]["summary"][0]["text"] == "brief thought"
    assert result.usage == usage
    assert result.ttft_ms is not None
    assert client.session.post.call_args.kwargs["stream"] is True


def test_interactions_sse_client_reconstructs_function_call_arguments() -> None:
    lines = [
        'data: {"interaction":{"id":"v1_tool","status":"in_progress","model":"gemini-3.7-flash"},"event_type":"interaction.created"}',
        'data: {"index":0,"step":{"id":"call_1","type":"function_call","name":"get_weather","arguments":{}},"event_type":"step.start"}',
        'data: {"index":0,"delta":{"type":"arguments_delta","arguments":"{\\"city\\":\\"Beijing\\"}"},"event_type":"step.delta"}',
        'data: {"index":0,"event_type":"step.stop"}',
        "data: "
        + json.dumps(
            {
                "interaction": {
                    "id": "v1_tool",
                    "status": "requires_action",
                    "model": MODEL,
                    "usage": _usage(),
                },
                "event_type": "interaction.completed",
            }
        ),
        "data: [DONE]",
    ]
    client = _client()
    client.session.post = Mock(return_value=_FakeStreamResponse(lines))
    result = client.gemini_interactions(
        {"model": MODEL, "input": "weather", "stream": True, "store": False}
    )
    assert result.success is True
    assert result.finish_reason == "requires_action"
    assert result.tool_calls == [
        {
            "id": "call_1",
            "type": "function_call",
            "name": "get_weather",
            "arguments": {"city": "Beijing"},
        }
    ]


def test_interactions_sse_preserves_terminal_steps_without_step_events() -> None:
    payload = _text_payload("terminal text")
    lines = [
        "data: "
        + json.dumps(
            {
                "interaction": payload,
                "event_type": "interaction.completed",
            }
        ),
        "data: [DONE]",
    ]
    client = _client()
    client.session.post = Mock(return_value=_FakeStreamResponse(lines))

    result = client.gemini_interactions(
        {"model": MODEL, "input": "hi", "stream": True, "store": False}
    )

    assert result.success is False
    assert result.text == "terminal text"
    assert result.response_json["steps"] == payload["steps"]


def test_interactions_sse_prefers_authoritative_terminal_steps() -> None:
    payload = _text_payload("terminal text")
    lines = [
        'data: {"index":0,"step":{"type":"model_output","content":[]},"event_type":"step.start"}',
        "data: "
        + json.dumps(
            {
                "interaction": payload,
                "event_type": "interaction.completed",
            }
        ),
        "data: [DONE]",
    ]
    client = _client()
    client.session.post = Mock(return_value=_FakeStreamResponse(lines))

    result = client.gemini_interactions(
        {"model": MODEL, "input": "hi", "stream": True, "store": False}
    )

    assert result.success is False
    assert result.text == "terminal text"
    assert result.response_json["steps"] == payload["steps"]


def test_interactions_sse_requires_done_sentinel() -> None:
    terminal = _text_payload()
    terminal["steps"] = []
    lines = [
        'data: {"event_type":"interaction.created","interaction":{"id":"v1_interaction_test","model":"gemini-3.7-flash","status":"in_progress"}}',
        "data: "
        + json.dumps(
            {
                "interaction": terminal,
                "event_type": "interaction.completed",
            }
        )
    ]
    client = _client()
    client.session.post = Mock(return_value=_FakeStreamResponse(lines))

    result = client.gemini_interactions(
        {"model": MODEL, "input": "hi", "stream": True, "store": False}
    )

    assert result.success is False
    assert result.error_type == "stream_missing_done"


@pytest.mark.parametrize(
    ("profile", "payload"),
    [
        ("gemini_3_7_flash_interactions_tools_auto", _tool_payload()),
        ("gemini_3_7_flash_interactions_tools_any", _tool_payload()),
        ("gemini_3_7_flash_interactions_tools_validated", _tool_payload()),
        ("gemini_3_7_flash_interactions_tools_none", _text_payload()),
        (
            "gemini_3_7_flash_interactions_response_format_json",
            _text_payload('{"summary":"ok","items":[]}'),
        ),
        ("gemini_3_7_flash_interactions_stream", _text_payload()),
    ],
)
def test_interactions_profile_validators(profile: str, payload: dict[str, Any]) -> None:
    body = _build(profile, INTERACTIONS_SOURCE, "gemini_interactions").body
    result = _result(payload)
    assert (
        validate_profile_response(
            profile,
            payload,
            result,
            request_body=body,
            transport="gemini_interactions",
            reference_source=INTERACTIONS_SOURCE,
            request_context={"requested_model": MODEL, "request_url": "https://generativelanguage.googleapis.com/v1beta/interactions"},
        )
        is None
    )


@pytest.mark.parametrize("invalid", ["10", 10.5, float("inf"), True, -1])
def test_interactions_validator_requires_integer_usage_counts(invalid: Any) -> None:
    payload = _text_payload()
    payload["usage"]["total_tokens"] = invalid

    assert (
        validate_profile_response(
            "gemini_3_7_flash_interactions_basic",
            payload,
            _result(payload),
            request_body={"model": MODEL, "input": "hi"},
            transport="gemini_interactions",
        )
        == "interaction_usage_invalid"
    )


class _DispatchClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def gemini_interactions(self, body: dict[str, Any]) -> ChatResult:
        self.calls.append(body)
        return _result(_text_payload())


def test_runner_dispatches_interactions_identity_and_profile_without_followup(monkeypatch) -> None:
    # Exercise dispatch independently from the bounded catalog selection.
    # The separate main() tests below verify rejection of mismatched sources.
    from scripts import param_test as runner
    monkeypatch.setattr(runner, "get_model_api_form", lambda *a, **k: "gemini_interactions")
    config = load_config()
    identity_client = _DispatchClient()
    identity = run_identity_probe(
        config,
        identity_client,
        "gemini",
        MODEL,
        "gemini",
        INTERACTIONS_SOURCE,
        "gemini",
    )
    assert identity["pass"] is True
    assert identity["transport"] == "gemini_interactions"
    assert identity_client.calls[0]["store"] is False
    assert identity_client.calls[0]["stream"] is False

    profile_client = _DispatchClient()
    profile_result = run_one_profile(
        config,
        profile_client,
        "gemini",
        MODEL,
        "gemini",
        INTERACTIONS_SOURCE,
        "gemini",
        "gemini_3_7_flash_interactions_basic",
        1,
        {"id": "offline", "prompt": "Reply briefly."},
        expectation="supported",
    )
    assert profile_result["pass"] is True
    assert profile_result["transport"] == "gemini_interactions"
    assert len(profile_client.calls) == 1




@pytest.mark.parametrize("reference_source", [GENERATE_SOURCE, "gemini_vertex_generate_content"])
def test_current_catalog_selection_rejects_wrong_source_before_client(monkeypatch, reference_source):
    from scripts import param_test as runner
    config = load_config()
    monkeypatch.setattr(runner, "load_config", lambda: copy.deepcopy(config))
    monkeypatch.setattr(runner, "get_active_provider_name", lambda *a, **k: "gemini")
    monkeypatch.setattr(runner, "get_selected_model", lambda *a, **k: MODEL)
    monkeypatch.setattr(runner, "get_model_family", lambda *a, **k: "gemini")
    monkeypatch.setattr(runner, "get_model_route_profile", lambda *a, **k: "google_ai_studio")
    monkeypatch.setattr(runner, "get_model_api_form", lambda *a, **k: "gemini_interactions")
    monkeypatch.setattr(runner, "_select_reference_source", lambda *a, **k: reference_source)
    monkeypatch.setattr(runner, "load_job_spec", lambda *a, **k: None)
    for key in ("LOADTEST_PARAMETER_SUITE", "LOADTEST_PARAM_TEST_RUNS", "LOADTEST_TOOL_VALIDATION_MODE"):
        monkeypatch.delenv(key, raising=False)
    client_factory = Mock(side_effect=AssertionError("wrong source must not create a client"))
    monkeypatch.setattr(runner.DeepSeekClient, "from_config", client_factory)
    with pytest.raises(ValueError):
        runner.main()
    client_factory.assert_not_called()


def test_current_interactions_interface_preserves_disabled_execution():
    interface = get_model_profile_catalog().get_interface(
        "text/google_ai_studio/gemini/gemini-3.7-flash#gemini-interactions-default"
    )
    assert interface["enabled"] is False
    assert interface["executable"] is False
    assert interface["disabled_reason"]
    for binding in interface["test_bindings"]:
        assert binding["parameter_test_enabled"] is False
        assert binding["runner_enabled"] is False
        assert binding["pressure_test_enabled"] is False


@pytest.mark.parametrize("request_url, expected_pass", [
    ("https://generativelanguage.googleapis.com/v1beta/interactions", True),
    ("https://foreign.example/v1beta/interactions", False),
])
def test_runner_passes_actual_transport_origin_to_stateless_validation(monkeypatch, request_url, expected_pass):
    from scripts import param_test as runner
    monkeypatch.setattr(runner, "get_model_api_form", lambda *a, **k: "gemini_interactions")
    class StatelessClient(_DispatchClient):
        def _transport_url(self, transport):
            assert transport == "gemini_interactions"
            return request_url
        def gemini_interactions(self, body):
            self.calls.append(copy.deepcopy(body))
            payload = _text_payload()
            payload.pop("id")
            return _result(payload)
    client = StatelessClient()
    row = run_one_profile(load_config(), client, "gemini", MODEL, "gemini",
        INTERACTIONS_SOURCE, "gemini", "gemini_3_7_flash_interactions_basic", 1,
        {"id": "offline", "prompt": "Reply briefly."}, expectation="supported")
    assert row["pass"] is expected_pass
    assert len(client.calls) == 1
    assert client.calls[0]["store"] is False
    assert client.calls[0]["generation_config"]["max_output_tokens"] >= 256
    if not expected_pass:
        assert row["failure_classification"] == "interaction_id_missing"


def test_tool_observation_stops_after_initial_request_without_state_or_followup(monkeypatch):
    from scripts import param_test as runner
    monkeypatch.setattr(runner, "get_model_api_form", lambda *a, **k: "gemini_interactions")
    class ToolClient(_DispatchClient):
        def gemini_interactions(self, body):
            self.calls.append(copy.deepcopy(body))
            return _result(_tool_payload())
    client = ToolClient()
    row = run_one_profile(load_config(), client, "gemini", MODEL, "gemini",
        INTERACTIONS_SOURCE, "gemini", "gemini_3_7_flash_interactions_tools_any", 1,
        {"id": "offline", "prompt": "Call the weather function for Beijing."}, expectation="supported")
    assert row["pass"] is True
    assert len(client.calls) == 1
    assert client.calls[0]["store"] is False
    assert not {"previous_interaction_id", "background", "agent"}.intersection(client.calls[0])
