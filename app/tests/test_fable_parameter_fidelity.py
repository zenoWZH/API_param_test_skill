"""Exact Fable parameter bodies survive the actual config/client path."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import yaml
from lib.client import OpenAICompatibleClient
from lib.deepseek_params import build_request

MODEL = "claude-fable-5"


@pytest.fixture
def config(monkeypatch):
    monkeypatch.delenv("LOADTEST_PROVIDER", raising=False)
    monkeypatch.delenv("LOADTEST_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-fable-parameter-key")
    value = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text())
    value["active_provider"] = "anthropic_official"
    return value


def request(config, fields, *, parameter_test=True, preserve=False, group="compatibility_profiles", profile="claude_sampling"):
    return build_request(config, group, profile,
        overrides={"model": MODEL, "max_tokens": 8192, "preserve_rejected_params": preserve, **fields},
        model_family_override="claude_fable", api_form_override="openai_chat_completions",
        route_profile_override="vendor_compat", reference_source="claude_fable_openai_compat",
        enforce_model_capabilities=False, parameter_test=parameter_test)


@pytest.mark.parametrize("field,value", [("temperature", 0.5), ("temperature", 2), ("top_p", 0.9), ("top_p", 0.995),
    ("thinking", {"type": "disabled"}), ("thinking", {"type": "adaptive"})])
def test_parameter_values_are_sent_unchanged_through_real_from_config(config, field, value):
    fields = {field: value}
    if field != "thinking": fields["omit_params"] = ["thinking"]
    built = request(config, fields)
    assert built.body[field] == value and built.body["max_tokens"] == 8192
    assert "output_config" not in built.body
    if field != "thinking": assert "thinking" not in built.body
    client = OpenAICompatibleClient.from_config(config, provider="anthropic_official")
    payload = {"error": {"type": "invalid_request_error", "message": "Offline fixture; no network"}}
    raw = json.dumps(payload).encode()
    response = SimpleNamespace(status_code=400, headers={"content-type": "application/json"}, content=raw,
                               text=raw.decode(), json=lambda: payload)
    client.session.post = Mock(return_value=response)
    client.chat_completion(built.body)
    args, kwargs = client.session.post.call_args
    assert args == ("https://api.anthropic.com/v1/chat/completions",)
    assert kwargs["json"] == built.body and kwargs["json"][field] == value
    assert kwargs["allow_redirects"] is False


def test_explicit_fidelity_flag_preserves_same_case_without_parameter_test_switch(config):
    fields = {"top_p": 0.9, "thinking": {"type": "disabled"}}
    built = request(config, fields, parameter_test=False, preserve=True)
    assert all(built.body[key] == value for key, value in fields.items())
    assert "output_config" not in built.body


def test_ordinary_compatibility_call_omits_rejected_native_defaults(config):
    built = request(config, {"top_p": 0.9, "thinking": {"type": "disabled"}}, parameter_test=False, preserve=False)
    assert "top_p" not in built.body and "thinking" not in built.body
    assert "output_config" not in built.body


@pytest.mark.parametrize("group,profile", [("throughput_profiles", "baseline_short"), ("cache_profiles", "cache_long_context")])
def test_load_cache_defaults_keep_existing_sampling_omission_even_with_probe_flags(config, group, profile):
    built = request(config, {"temperature": 0.5, "top_p": 0.9, "thinking": {"type": "disabled"}},
                    parameter_test=True, preserve=True, group=group, profile=profile)
    assert "temperature" not in built.body and "top_p" not in built.body
    assert built.body["thinking"] == {"type": "adaptive"} and built.body["output_config"] == {"effort": "medium"}


def test_ordinary_compatibility_body_matches_saved_successful_baseline(config):
    root=Path(__file__).resolve().parents[1]
    if root.name=='app':root=root.parent
    config={**config,'_parameter_test_exact_input':True}  # Isolate normalization from optional input padding.
    batch=root/'reports/approved_live_20260907/fable_direct_compat_20260908T222003Z_884dfba4'
    if not (batch/'request_package.json').is_file():pytest.skip('P1M raw baseline unavailable')
    package=json.loads((batch/'request_package.json').read_text())
    case=package['cases'][1]
    built=request(config,{**copy.deepcopy(case['body']), 'omit_params':['temperature','top_p','thinking','output_config']},parameter_test=False)
    assert built.body==case['body']
    client=OpenAICompatibleClient.from_config(config,provider='anthropic_official')
    payload=json.loads((batch/'case_02_response.json').read_text());raw=json.dumps(payload).encode()
    response=SimpleNamespace(status_code=200,headers={'content-type':'application/json'},content=raw,text=raw.decode(),json=lambda:payload)
    client.session.post=Mock(return_value=response)
    client.chat_completion(built.body)
    args,kwargs=client.session.post.call_args
    assert args==('https://api.anthropic.com/v1/chat/completions',) and kwargs['json']==case['body']

@pytest.mark.parametrize("profile,value", [
    ("claude_native_top_p", 1),
    ("claude_native_top_p_compat", 0.995),
])
@pytest.mark.parametrize("status,expected_pass", [(400, True), (200, False)])
def test_native_top_p_deprecation_reaches_parameter_runner(config, profile, value, status, expected_pass):
    from lib.client import ChatResult
    from lib.reference_specs import load_model_capability_profile, resolve_profile_expectation
    from scripts.param_test import run_one_profile

    contract = "claude_fable_native_messages"
    capability = load_model_capability_profile(
        "text", "claude_fable", MODEL, api_form="anthropic_messages",
        route_profile="vendor_direct", reference_source=contract,
    )
    assert resolve_profile_expectation(
        "text", "claude_fable", MODEL, profile,
        capability_profile=capability, reference_source=contract,
    ) == "unsupported"
    assert resolve_profile_expectation(
        "text", "claude_fable", MODEL, "claude_native_temperature",
        capability_profile=capability, reference_source=contract,
    ) == "supported"
    sent = []
    payload = (
        {"error": {"type": "invalid_request_error", "message": "`top_p` is deprecated for this model."}}
        if status == 400 else
        {"id": "offline-top-p", "type": "message", "role": "assistant", "model": MODEL,
         "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn",
         "usage": {"input_tokens": 1, "output_tokens": 1}}
    )

    class OfflineClient:
        def claude_messages(self, body):
            sent.append(copy.deepcopy(body))
            return ChatResult(
                success=status == 200, status_code=status, latency_ms=1, timestamp=0,
                response_json=payload, raw_text=json.dumps(payload),
                error_type="http_error" if status == 400 else None,
            )

        def count_tokens(self, *args, **kwargs):
            return None

    result = run_one_profile(
        config, OfflineClient(), "anthropic_official", MODEL, "claude_fable",
        contract, "claude_fable", profile, 1, {"id": "offline", "prompt": "Reply OK."},
        capability_profile=capability,
    )
    assert len(sent) == 1 and sent[0]["top_p"] == value
    assert result["expectation"] == "unsupported"
    assert result["pass"] is expected_pass
