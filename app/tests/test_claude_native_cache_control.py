"""Explicit automatic-cache fields retain their exact native request meaning."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from lib import model_profile_catalog as mpdb
from lib.client import OpenAICompatibleClient
from lib.deepseek_params import _build_claude_messages_body, build_request

MODELS = ("claude-haiku-4-5-20251001", "claude-opus-4-5-20251101", "claude-opus-4-6",
          "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-sonnet-4-5-20250929",
          "claude-sonnet-4-6", "claude-sonnet-5")


@pytest.fixture
def config(monkeypatch):
    for name in ("LOADTEST_PROVIDER", "LOADTEST_MODEL", "LOADTEST_ROUTE_PROFILE", "LOADTEST_API_FORM"):
        monkeypatch.delenv(name, raising=False)
    # Public configuration only: no overlay, dotenv or credential loading.
    value = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text())
    value["active_provider"] = "anthropic_official"
    return value


def request(config, value=None, *, supplied=True, model=MODELS[-1], preserve=False,
            reference="claude_native_messages", route=None):
    overrides = {"model": model, "max_tokens": 2048, "preserve_rejected_params": preserve}
    if supplied:
        overrides["cache_control"] = value
    return build_request(config, "compatibility_profiles", "claude_native_max_tokens",
        overrides=overrides, reference_source=reference, route_profile_override=route,
        enforce_model_capabilities=False)


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("value", [{"type": "ephemeral"}, {"type": "ephemeral", "ttl": "5m"},
                                 {"type": "ephemeral", "ttl": "1h"}])
def test_explicit_current_contract_values_survive_build_request(config, model, value):
    built = request(config, value, model=model)
    assert built.body["cache_control"] == value
    assert built.body["cache_control"] is not value
    assert built.body["max_tokens"] == 2048
    assert built.metadata["transport"] == "claude_messages"
    assert built.metadata["reference_source"] == "claude_native_messages"
    baseline = request(config, supplied=False, model=model)
    without = copy.deepcopy(built.body)
    without.pop("cache_control")
    assert without == baseline.body


def test_supported_explicit_field_reaches_actual_client_wire(config):
    value = {"type": "ephemeral"}
    built = request(config, value)
    sent = []
    payload = {"type": "message", "role": "assistant", "model": MODELS[-1],
        "id": "offline-cache", "content": [{"type": "text", "text": "OK"}],
        "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}

    def post(url, **kwargs):
        sent.append((url, copy.deepcopy(kwargs)))
        return SimpleNamespace(status_code=200, headers={}, content=json.dumps(payload).encode(),
                               text=json.dumps(payload), json=lambda: payload)

    client = object.__new__(OpenAICompatibleClient)
    client.timeout_sec = 1
    client.session = SimpleNamespace(post=post)
    client._transport_url = lambda _: "https://api.anthropic.com/v1/messages"
    client._auth_headers = lambda *_: {}
    client.claude_messages(built.body)
    assert len(sent) == 1
    assert sent[0][0] == "https://api.anthropic.com/v1/messages"
    assert sent[0][1]["json"] == built.body
    assert sent[0][1]["json"]["cache_control"] == value
    assert sent[0][1]["allow_redirects"] is False


def test_missing_explicit_setting_preserves_legacy_builder_without_source_dependency():
    body = _build_claude_messages_body({}, {"messages": [{"role": "user", "content": "OK"}],
        "max_tokens": 2048}, MODELS[-1])
    assert "cache_control" not in body


def test_default_reference_resolves_current_native_contract(config):
    assert request(config, {"type": "ephemeral"}, reference=None).body["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize("value", [None, 17, True, "ephemeral", [], {}, {"ttl": "5m"},
    {"type": "persistent"}, {"type": "ephemeral", "ttl": "2h"},
    {"type": "ephemeral", "unknown": 1}, {"type": "ephemeral", "ttl": None}])
def test_invalid_values_are_rejected_or_kept_for_explicit_negative_probe(config, value):
    with pytest.raises(ValueError, match="cache_control"):
        request(config, value)
    assert request(config, value, preserve=True).body["cache_control"] == value


@pytest.mark.parametrize("reference", ["claude_openai_compat", "claude_fable_native_messages",
    "deepseek_v4_pro_0813_anthropic", "bedrock_mantle_claude_native_messages", "unknown"])
def test_wrong_contract_is_not_an_anthropic_cache_capability_even_for_negative_probe(config, reference):
    with pytest.raises(ValueError, match="cache_control"):
        request(config, {"type": "ephemeral"}, reference=reference, preserve=True)


@pytest.mark.parametrize("source,url", [
    ("aws_bedrock", "https://bedrock-mantle.us-east-1.api.aws/anthropic/v1"),
    ("deepseek", "https://api.deepseek.com/anthropic/v1"),
    ("unknown", "https://api.anthropic.com/v1"),
])
def test_wrong_runtime_source_cannot_borrow_native_field(config, source, url):
    provider = config["providers"]["anthropic_official"]
    provider["reference_source_id"] = source
    provider["models"].setdefault("reference_source_ids", {})[MODELS[-1]] = source
    provider["base_url"] = url
    with pytest.raises(ValueError, match="cache_control"):
        request(config, {"type": "ephemeral"}, preserve=True)


@pytest.mark.parametrize("alias", [False, True])
def test_adapter_wire_uses_runtime_endpoint_and_model_without_origin_relabeling(config, alias):
    from lib.reference_specs import load_model_capability_profile
    config, model = adapter_config(config, alias=alias)
    built = request(config, {"type": "ephemeral"}, model=model, route="dynamic_aggregator")
    capability = load_model_capability_profile("text", "claude", MODELS[-1],
        api_form="anthropic_messages", route_profile="dynamic_aggregator", reference_source="claude_native_messages")
    assert capability["certification_scope"] == "adapter_only"
    assert capability["execution_target"]["route_profile"] == "dynamic_aggregator"
    sent = []
    payload = {"content": [{"type": "text", "text": "OK"}], "model": model,
               "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}
    def post(url, **kwargs):
        sent.append((url, copy.deepcopy(kwargs["json"])))
        return SimpleNamespace(status_code=200, headers={}, content=b"{}", text="{}",
                               json=lambda: payload)
    provider = config["providers"]["offline_adapter"]
    client = object.__new__(OpenAICompatibleClient)
    client.base_url = provider["base_url"]
    client.api_interfaces = copy.deepcopy(provider["api_interfaces"])
    client.timeout_sec = 1
    client.session = SimpleNamespace(post=post)
    client._auth_headers = lambda *_: {}
    client.claude_messages(built.body)
    assert sent == [("https://adapter.example/v1/messages", built.body)]
    assert sent[0][1]["model"] == model
    assert sent[0][1]["cache_control"] == {"type": "ephemeral"}



def adapter_config(config, *, alias=False):
    model = "adapter-claude-sonnet" if alias else MODELS[-1]
    provider = copy.deepcopy(config["providers"]["anthropic_official"])
    provider["base_url"] = "https://adapter.example/v1"
    provider["models"] = {"default": model, "candidates": [model], "families": {model: "claude"},
        "transports": {model: "claude_messages"}, "reference_source_ids": {model: "anthropic"},
        "reference_model_ids": {model: MODELS[-1]},
        "routes": {model: {"dynamic_aggregator": {"api_forms": {"anthropic_messages": {}}}}},
        "default_routes": {model: "dynamic_aggregator"},
        "default_api_forms": {model: {"dynamic_aggregator": "anthropic_messages"}}}
    config["providers"]["offline_adapter"] = provider
    config["active_provider"] = "offline_adapter"
    return config, model


@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("value", [{"type": "ephemeral"}, {"type": "ephemeral", "ttl": "1h"}])
def test_valid_adapter_mapping_keeps_wire_identity_and_execution_target(config, alias, value):
    config, model = adapter_config(config, alias=alias)
    selected = mpdb.resolve_runtime_parameter_config(config, "offline_adapter", model, "claude",
        "dynamic_aggregator", "anthropic_messages", contract_id="claude_native_messages")
    target = {"provider_id": "offline_adapter", "request_model_id": model,
              "route_profile": "dynamic_aggregator", "api_form": "anthropic_messages"}
    assert selected["source_id"] == "anthropic" and selected["contract_id"] == "claude_native_messages"
    assert selected["profile"]["model_slug"] == MODELS[-1]
    assert selected["execution_target"] == target
    snapshot_target = selected["model_profile_database"]["execution_target"]
    assert {key: snapshot_target[key] for key in target} == target
    assert snapshot_target.get("transport", "claude_messages") == "claude_messages"
    built = request(config, value, model=model, route="dynamic_aggregator")
    assert built.body["model"] == model and built.body["cache_control"] == value
    assert built.metadata["provider"] == "offline_adapter"
    assert built.metadata["requested_model"] == model
    assert built.metadata["route_profile"] == "dynamic_aggregator"
    assert built.metadata["reference_source"] == "claude_native_messages"
    assert request(config, value, model=model, route="dynamic_aggregator", reference=None).body == built.body


def test_explicit_execution_route_overrides_provider_default(config):
    config, model = adapter_config(config, alias=True)
    models = config["providers"]["offline_adapter"]["models"]
    models["routes"][model]["vendor_compat"] = {"api_forms": {"openai_chat_completions": {}}}
    models["default_routes"][model] = "vendor_compat"
    built = request(config, {"type": "ephemeral"}, model=model, route="dynamic_aggregator")
    assert built.metadata["route_profile"] == "dynamic_aggregator"
    assert built.body["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize("alias,mutation", [
    (False, None), (True, None), (True, "unmapped_alias"),
    (False, "wrong_source"), (True, "wrong_source"),
])
def test_parameter_runner_checks_execution_binding_before_cache_dispatch(config, monkeypatch, alias, mutation):
    from scripts.param_test import run_one_profile

    config, model = adapter_config(config, alias=alias)
    provider = config["providers"]["offline_adapter"]
    if mutation == "unmapped_alias":
        provider["models"]["reference_model_ids"].pop(model)
    elif mutation == "wrong_source":
        provider["models"]["reference_source_ids"][model] = "deepseek"
    value = {"type": "ephemeral", "ttl": "1h"}
    config["compatibility_profiles"]["claude_native_max_tokens"].update(
        cache_control=value, preserve_rejected_params=True
    )
    sent = []
    payload = {"type": "message", "role": "assistant", "model": model,
        "id": "offline-cache-runner", "content": [{"type": "text", "text": "OK"}],
        "stop_reason": "end_turn", "usage": {"input_tokens": 8, "output_tokens": 1}}

    def post(url, **kwargs):
        sent.append((url, copy.deepcopy(kwargs["json"])))
        raw = json.dumps(payload)
        return SimpleNamespace(status_code=200, headers={}, content=raw.encode(), text=raw,
                               json=lambda: payload)

    client = OpenAICompatibleClient(provider["base_url"], "offline-cache-control",
        timeout_sec=1, provider="offline_adapter", api_interfaces=provider["api_interfaces"])
    monkeypatch.setattr(client.session, "post", post)
    monkeypatch.setattr(client, "count_tokens", lambda *args, **kwargs: None)
    result = run_one_profile(config, client, "offline_adapter", model, "claude",
        "claude_native_messages", "claude", "claude_native_max_tokens", 1,
        {"id": "offline", "prompt": "Reply OK."}, expectation="supported")

    assert result["route_profile"] == "dynamic_aggregator"
    assert result["reference_route_profile"] == "vendor_direct"
    if mutation:
        assert not sent
        assert result["pass"] is False
        assert "cache_control" in result["message"]
    else:
        assert sent == [("https://adapter.example/v1/messages", result["request_body"])]
        assert sent[0][1]["model"] == model
        assert sent[0][1]["cache_control"] == value
        assert result["status_code"] == 200
        assert result["pass"] is True


@pytest.mark.parametrize("mutation", ["unmapped_alias", "wrong_source", "wrong_model", "wrong_form", "wrong_interface"])
def test_invalid_adapter_mappings_cannot_use_negative_probe_escape(config, mutation):
    config, model = adapter_config(config, alias=True)
    models = config["providers"]["offline_adapter"]["models"]
    if mutation == "unmapped_alias":
        models["reference_model_ids"].pop(model)
    elif mutation == "wrong_source":
        models["reference_source_ids"][model] = "deepseek"
    elif mutation == "wrong_model":
        models["reference_model_ids"][model] = "deepseek-v4-pro"
    elif mutation == "wrong_form":
        models["routes"][model]["dynamic_aggregator"]["api_forms"] = {"openai_chat_completions": {}}
    else:
        models["reference_bindings"] = {model: {"profile_id": "text/deepseek/deepseek/deepseek-v4-pro-0813",
            "interface_id": "text/deepseek/deepseek/deepseek-v4-pro-0813#anthropic-messages-default"}}
    with pytest.raises((ValueError, KeyError)):
        request(config, {"type": "invalid"}, model=model, route="dynamic_aggregator", preserve=True)


def test_existing_deepseek_nested_cache_control_stays_on_its_own_contract(config):
    config["active_provider"] = "deepseek_official"
    built = build_request(config, "compatibility_profiles", "deepseek0813_anthropic_tools_cache_control_accepted_ignored",
        overrides={"model": "deepseek-v4-pro"}, reference_source="deepseek_v4_pro_0813_anthropic",
        enforce_model_capabilities=False)
    assert built.body["tools"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in built.body
    with pytest.raises(ValueError, match="cache_control"):
        build_request(config, "compatibility_profiles", "deepseek0813_anthropic_tools_cache_control_accepted_ignored",
            overrides={"model": "deepseek-v4-pro", "cache_control": {"type": "ephemeral"}, "preserve_rejected_params": True},
            reference_source="deepseek_v4_pro_0813_anthropic", enforce_model_capabilities=False)


def test_wrong_route_fails_with_explicit_contract(config):
    with pytest.raises(ValueError, match="cache_control|route profile"):
        request(config, {"type": "ephemeral"}, route="dynamic_aggregator", preserve=True)
    with pytest.raises(ValueError, match="cache_control"):
        _build_claude_messages_body(config, {"messages": [{"role": "user", "content": "OK"}],
            "cache_control": {"type": "ephemeral"}}, MODELS[-1],
            reference_source="claude_native_messages", runtime_route_profile="dynamic_aggregator")


def test_fable_does_not_inherit_ordinary_claude_cache_reference(config):
    with pytest.raises(ValueError, match="cache_control"):
        request(config, {"type": "ephemeral"}, model="claude-fable-5")


@pytest.mark.parametrize("where", ["contract", "interface", "request_ids", "disabled"])
def test_current_unsupported_capability_stops_body_construction(config, monkeypatch, where):
    original = mpdb.get_model_profile_catalog()
    class Altered:
        def __getattr__(self, name):
            return getattr(original, name)
        def resolve_request_model(self, *args, **kwargs):
            resolved = original.resolve_request_model(*args, **kwargs)
            return self.get_interface(resolved["interface_id"])
        def get_contract(self, identifier):
            value = copy.deepcopy(original.get_contract(identifier))
            if where == "contract":
                value["parameter_capabilities"]["cache_control"] = {"state": "unsupported"}
            return value
        def get_interface(self, identifier):
            value = copy.deepcopy(original.get_interface(identifier))
            if where == "interface":
                value.setdefault("parameter_capabilities", {})["cache_control"] = {"state": "unsupported"}
            if where == "request_ids":
                value["request_model_ids"] = ["other-model"]
            if where == "disabled":
                value["executable"] = False
            return value
        def resolve_parameter_config(self, **kwargs):
            value = copy.deepcopy(original.resolve_parameter_config(**kwargs))
            value["contract"] = self.get_contract(value["contract_id"])
            value["interface"] = self.get_interface(value["interface_id"])
            return value
    monkeypatch.setattr(mpdb, "get_model_profile_catalog", lambda: Altered())
    with pytest.raises(ValueError, match="cache_control"):
        request(config, {"type": "ephemeral"}, preserve=True)
