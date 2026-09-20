from __future__ import annotations

import copy

import pytest

from lib.config import load_config
from lib.deepseek_params import _build_openai_responses_body, build_request


CONTRACT = "deepseek_v4_pro_0813_responses"


@pytest.mark.parametrize("profile,field,value", [
    ("deepseek0813_responses_temperature", "temperature", 0.4),
    ("deepseek0813_responses_top_p", "top_p", 0.7),
    ("deepseek0813_responses_top_logprobs", "top_logprobs", 3),
    ("deepseek0813_responses_background_accepted_ignored", "background", True),
    ("deepseek0813_responses_include_accepted_ignored", "include", ["reasoning.encrypted_content"]),
    ("deepseek0813_responses_prompt_accepted_ignored", "responses_prompt", {"id": "offline-template", "version": "1"}),
])
def test_contract_probe_reaches_wire(profile, field, value):
    request = build_request(
        load_config(), "compatibility_profiles", profile,
        overrides={"model": "deepseek-v4-pro", field: value},
        model_family_override="deepseek", api_form_override="openai_responses",
        route_profile_override="vendor_direct", reference_source=CONTRACT,
    )
    assert request.body["prompt" if field == "responses_prompt" else field] == value


def test_ignored_fields_do_not_leak_into_another_source_contract():
    settings = {"input": "hello", "background": True, "responses_prompt": {"id": "offline-template"}}
    before = copy.deepcopy(settings)
    body = _build_openai_responses_body(load_config(), settings, "gpt-5.6-sol",
                                        reference_source="openai_responses")
    assert "background" not in body and "prompt" not in body
    assert settings == before


@pytest.mark.parametrize("field,value", [("temperature", True), ("temperature", 3),
                                        ("top_p", -1), ("top_logprobs", 1.5)])
def test_numeric_shape_is_checked(field, value):
    with pytest.raises(ValueError):
        _build_openai_responses_body(load_config(), {"input": "hello", field: value},
                                     "deepseek-v4-pro", reference_source=CONTRACT)
