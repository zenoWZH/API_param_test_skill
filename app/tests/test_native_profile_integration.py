from __future__ import annotations

from unittest.mock import Mock

import pytest

from lib.config import load_config
from lib.deepseek_params import build_request
from lib.reference_specs import test_profiles_for_reference as reference_profiles
from scripts.param_test import run_one_profile
from scripts.smoke_test import run_profile_smoke


def _build_anthropic(profile: str, overrides: dict | None = None):
    return build_request(
        load_config(),
        "compatibility_profiles",
        profile,
        overrides={"model": "deepseek-v4-pro", **(overrides or {})},
        model_family_override="deepseek",
        api_form_override="anthropic_messages",
        route_profile_override="vendor_direct",
        reference_source="deepseek_v4_pro_0813_anthropic",
    )


def test_declared_deepseek_anthropic_cases_build_without_transport_rewriting() -> None:
    profiles = reference_profiles("deepseek_v4_pro_0813_anthropic")
    assert len(profiles) == 23
    for profile in profiles:
        request = _build_anthropic(profile)
        assert request.body["model"] == "deepseek-v4-pro"
        assert request.metadata["api_form"] == "anthropic_messages"
        assert request.metadata["transport"] == "claude_messages"
        assert request.metadata["model_family"] == "deepseek"
        assert request.metadata["reference_source"] == "deepseek_v4_pro_0813_anthropic"
        assert request.body["messages"]


@pytest.mark.parametrize(
    ("profile", "field"),
    [
        ("deepseek0813_anthropic_container_accepted_ignored", "container"),
        ("deepseek0813_anthropic_mcp_servers_accepted_ignored", "mcp_servers"),
        ("deepseek0813_anthropic_service_tier_accepted_ignored", "service_tier"),
        ("deepseek0813_anthropic_metadata_other_accepted_ignored", "metadata"),
        ("deepseek0813_anthropic_output_config_other_accepted_ignored", "output_config"),
    ],
)
def test_accepted_ignored_probes_keep_their_declared_wire_field(
    profile: str, field: str
) -> None:
    config = load_config()
    declared = config["compatibility_profiles"][profile][field]
    assert _build_anthropic(profile).body[field] == declared


def test_anthropic_validation_is_family_scoped_and_preserves_negative_probes() -> None:
    supported = _build_anthropic("deepseek0813_anthropic_basic", {"temperature": 1.5})
    assert supported.body["temperature"] == 1.5
    rejected = _build_anthropic("deepseek0813_anthropic_reject_temperature")
    assert rejected.body["temperature"] > 2
    with pytest.raises(ValueError, match="temperature must be in"):
        _build_anthropic("deepseek0813_anthropic_basic", {"temperature": 2.1})
    with pytest.raises(ValueError, match="thinking must be"):
        _build_anthropic("deepseek0813_anthropic_basic", {"thinking": {"type": "adaptive"}})
    with pytest.raises(ValueError, match="output_config.effort"):
        _build_anthropic("deepseek0813_anthropic_basic", {"output_config": {"effort": "low"}})
    with pytest.raises(ValueError, match="temperature must be in"):
        build_request(
            load_config(), "compatibility_profiles", "claude_native_temperature",
            overrides={"temperature": 1.5}, model_family_override="claude",
        )
    with pytest.raises(ValueError, match="reference family"):
        build_request(
            load_config(), "compatibility_profiles", "deepseek0813_anthropic_basic",
            model_family_override="gpt", api_form_override="anthropic_messages",
        )


@pytest.mark.parametrize("profile", ["gpt6_chat_unknown_profile", "gpt6_responses_unknown_profile"])
def test_unknown_cases_fail_before_any_parameter_or_smoke_send(profile: str) -> None:
    config = load_config()
    assert profile not in config["compatibility_profiles"]
    client = Mock()
    parameter_result = run_one_profile(
        config, client, "yibu", "deepseek-v4-flash", "deepseek", "deepseek_chat",
        "deepseek", profile, 1, {"id": "offline", "prompt": "offline"},
        expectation="supported",
    )
    assert parameter_result["pass"] is False
    assert "not found" in parameter_result["failure_detail"]["actual"]
    smoke_result = run_profile_smoke(
        client, config, "compatibility_profiles", profile,
        reference_source="deepseek_chat",
    )
    assert smoke_result["pass"] is False
    assert "not found" in smoke_result["message"]
    assert client.mock_calls == []
