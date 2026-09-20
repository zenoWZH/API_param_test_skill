"""Offline regression for the exact source/API forms approved in items 16-18."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from banana_gc_fixtures import historical_image_capability
from lib.client import ChatResult, OpenAICompatibleClient
from lib.deepseek_params import build_request
from lib.image_validation import ImageInfo, _matches_aspect_ratio, evaluate_case, gemini_flash_31_lite_image_profile_cases
from scripts.param_test import _input_group_for_profile, _validate_deepseek0813_profile_response, run_one_profile

APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parent
RESPONSES = "deepseek_v4_pro_0813_responses"


@pytest.fixture
def public_config():
    # Never load providers.local.yaml or credentials for an offline test.
    return yaml.safe_load((APP_ROOT / "config.yaml").read_text())


def response_result(**changes):
    usage = {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10,
             "output_tokens_details": {"reasoning_tokens": 0}}
    payload = {"model": "deepseek-v4-pro", "store": False, "parallel_tool_calls": True,
               "previous_response_id": None, "usage": usage,
               "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}]}
    payload.update(changes)
    return ChatResult(success=True, status_code=200, latency_ms=1, timestamp=0,
                      response_json=payload, text="OK", usage=payload.get("usage", {}))


def validate_deepseek(profile, result, body=None, contract=RESPONSES, transport="openai_responses"):
    return _validate_deepseek0813_profile_response(profile, result.response_json, result,
        request_body=body or {"reasoning": {"effort": "none"}}, transport=transport,
        tool_validation_mode="auto", reference_source=contract)


@pytest.mark.parametrize("field,value,error", [
    ("output", [], "responses_output_missing"),
    ("usage", {}, "responses_usage_missing"),
    ("store", True, "responses_fixed_field_mismatch"),
    ("parallel_tool_calls", False, "responses_fixed_field_mismatch"),
    ("previous_response_id", "unexpected", "responses_fixed_field_mismatch"),
])
def test_http_200_is_not_sufficient_for_deepseek_responses(field, value, error):
    assert validate_deepseek("deepseek0813_responses_basic", response_result(**{field: value})) == error


def test_reasoning_json_and_tools_semantics():
    result = response_result()
    assert validate_deepseek("deepseek0813_responses_reasoning_none", result) is None
    assert validate_deepseek("deepseek0813_responses_reasoning_high", result,
                            {"reasoning": {"effort": "high"}}) == "responses_reasoning_missing"
    result.response_json["output"].insert(0, {"type": "reasoning", "summary": [{"text": "unexpected"}]})
    assert validate_deepseek("deepseek0813_responses_reasoning_none", result) == "responses_reasoning_none_mismatch"
    assert validate_deepseek("deepseek0813_responses_reasoning_summary_accepted_ignored", result,
                            {"reasoning": {"effort": "high"}}) == "responses_reasoning_summary_unexpected"
    assert validate_deepseek("deepseek0813_responses_top_logprobs", response_result()) == "responses_logprobs_missing"
    assert validate_deepseek("deepseek0813_responses_text_json", response_result()) == "json_parse"
    result = response_result(output=[{"type": "function_call", "call_id": "call1", "name": "weather", "arguments": "{}"}])
    assert validate_deepseek("deepseek0813_responses_tool_choice_none", result) == "tool_calls_unexpected"


def test_deepseek_validator_rejects_cross_contract_use():
    assert validate_deepseek("deepseek0813_responses_basic", response_result(),
                            contract="openai_responses") == "deepseek0813_contract_mismatch"


@pytest.mark.parametrize("payload,expected", [({}, "anthropic_content_missing"),
    ({"content": [{"type": "text", "text": "OK"}]}, "anthropic_usage_missing")])
def test_anthropic_envelope_requires_content_and_usage(payload, expected):
    result = SimpleNamespace(response_json=payload, usage={})
    assert validate_deepseek("deepseek0813_anthropic_basic", result,
        contract="deepseek_v4_pro_0813_anthropic", transport="claude_messages") == expected


def test_runner_applies_deepseek_semantic_validator(public_config):
    class OfflineClient:
        def openai_responses(self, body):
            return response_result(store=True)
    result = run_one_profile(public_config, OfflineClient(), "deepseek_official", "deepseek-v4-pro",
        "deepseek", RESPONSES, "deepseek", "deepseek0813_responses_basic",
        1, {"id": "offline", "prompt": "Reply OK."}, expectation="supported")
    assert not result["pass"]
    assert result["failure_classification"] == "responses_fixed_field_mismatch"


def test_deepseek_profile_input_groups(public_config):
    assert _input_group_for_profile(public_config, "deepseek0813_responses_tools") == "tool_calls"
    assert _input_group_for_profile(public_config, "deepseek0813_anthropic_tools_named") == "tool_calls"
    assert _input_group_for_profile(public_config, "deepseek0813_responses_reasoning_high") == "reasoning"


@pytest.mark.parametrize("terminal", ["incomplete", "failed"])
def test_responses_sse_keeps_terminal_usage_and_identity(terminal):
    payload = response_result().response_json
    payload.update({"id": "resp1", "status": terminal})
    if terminal == "failed":
        payload["error"] = {"message": "upstream failure"}
    event = {"type": "response." + terminal, "response": payload}
    class Response:
        status_code = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def iter_lines(self, **kwargs):
            yield "data: " + json.dumps(event)
    class Session:
        def post(self, *args, **kwargs): return Response()
    client = OpenAICompatibleClient(api_key="offline-test", base_url="https://example.invalid")
    client.session = Session()
    result = client.openai_responses({"model": "deepseek-v4-pro", "input": "OK", "stream": True})
    assert result.usage == payload["usage"]
    assert result.response_json["model"] == "deepseek-v4-pro"
    assert result.response_json["id"] == "resp1"
    assert result.finish_reason == terminal
    if terminal == "failed": assert not result.success


@pytest.mark.parametrize("profile", ["gemini_native_response_mime_type", "gemini_native_response_schema",
    "gemini_native_response_json_schema", "gemini_native_response_format"])
def test_gemini37_json_retry_budget_is_exact_contract_scoped(public_config, profile):
    kwargs = dict(model_family_override="gemini", api_form_override="gemini_generate_content",
                  route_profile_override="google_ai_studio", enforce_model_capabilities=False)
    request = build_request(public_config, "compatibility_profiles", profile,
        overrides={"model": "gemini-3.7-flash"}, reference_source="gemini_3_7_flash_generate_content", **kwargs)
    assert request.body["generationConfig"]["maxOutputTokens"] == 2048
    other = build_request(public_config, "compatibility_profiles", profile,
        overrides={"model": "gemini-3.6-flash"}, reference_source="gemini_native_generate_content", **kwargs)
    assert other.body["generationConfig"]["maxOutputTokens"] == 128


@pytest.mark.parametrize("overrides", [
    {"model": "gemini-3.6-flash"}, {"native_cached_content": "cachedContents/unsupported"},
    {"native_generation_config": {"responseModalities": ["TEXT", "IMAGE"]}},
    {"native_generation_config": {"imageConfig": {"imageSize": "1K"}}},
    {"native_safety_settings": [{"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}]},
    {"native_safety_settings": [{"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"}]},
])
def test_gemini37_safe_contract_stays_bounded(public_config, overrides):
    with pytest.raises(ValueError):
        build_request(public_config, "compatibility_profiles", "gemini_native_temperature",
            overrides={"model": "gemini-3.7-flash", **overrides}, model_family_override="gemini",
            api_form_override="gemini_generate_content", route_profile_override="google_ai_studio",
            reference_source="gemini_3_7_flash_generate_content", enforce_model_capabilities=False)


def test_lite_existing_native_images_pass_exact_model_alignment_tolerance():
    from scripts.image_param_test import _with_output_options
    report = REPO_ROOT / "reports/image_param/gemini_official_lite_generate_content_full_20260824/case_results.json"
    if not report.exists(): pytest.skip("historical ignored media report is not shipped")
    # This is an offline regression over the archived August contract, not a
    # request to apply today's v1beta capability to historical image evidence.
    cases = {case.name: case for case in gemini_flash_31_lite_image_profile_cases(
        "full", api_form="gemini_generate_content", include_4k=True,
        capability_profile=historical_image_capability("gemini-3.1-flash-lite-image"),
    )}
    rows = json.loads(report.read_text())
    checked = 0
    for row in rows:
        if "__aspect_" not in row["case"]: continue
        info = row["actual_images"][0]
        image = ImageInfo(format=info["format"], width=info["width"], height=info["height"],
                          byte_length=info["byte_length"], sha256=info["sha256"])
        case = _with_output_options(cases[row["case"]], "low", "png", transport="gemini-generate-content")
        result = evaluate_case(case, status_code=200, images=[image], usage=row.get("usage"))
        assert result["pass"], (row["case"], result["failures"])
        checked += 1
    assert checked == 14
    # Generic image probes retain their stricter tolerance.
    assert not _matches_aspect_ratio(352, 2928, "1:8")


def test_console_exposes_only_verified_new_source_forms(public_config):
    from scripts.web_console import _capability_registry_payload
    public_config["providers"] = {key: value for key, value in public_config["providers"].items()
                                  if key in {"deepseek_official", "gemini"}}
    registry = _capability_registry_payload(public_config)
    forms = registry["text"]["deepseek_official"]["deepseek-v4-pro"]["routes"]["vendor_direct"]["api_forms"]
    assert set(forms) == {"openai_chat_completions", "openai_responses", "anthropic_messages", "openai_fim_completions_beta", "deepseek_beta_chat_prefix"}
    assert all(row["parameter_test_enabled"] is True for row in forms.values())
    assert forms["deepseek_beta_chat_prefix"]["pressure_test_enabled"] is False
    forms = registry["text"]["gemini"]["gemini-3.7-flash"]["routes"]["google_ai_studio"]["api_forms"]
    assert set(forms) == {"gemini_generate_content", "gemini_interactions"}
    assert forms["gemini_generate_content"]["parameter_test_enabled"]
    # The user keeps the ordinary Interactions entry disabled even though its
    # earlier bounded acceptance evidence is retained in the catalog.
    assert forms["gemini_interactions"]["parameter_test_enabled"] is False
    assert forms["gemini_interactions"]["pressure_test_enabled"] is False
    assert registry["text"]["gemini"]["gemini-3.7-flash"]["default_api_form"] == "gemini_generate_content"
    image = registry["image"]["gemini"]["gemini-3.1-flash-lite-image"]
    assert image["default_api_form"] == "gemini_generate_content"
    forms = image["routes"]["google_ai_studio"]["api_forms"]
    assert forms["gemini_generate_content"]["parameter_test_enabled"]
    assert not forms["gemini_interactions"]["parameter_test_enabled"]


@pytest.mark.parametrize("parameter_test", [False, True])
def test_requests_are_cwd_independent_with_the_bundled_public_config(parameter_test):
    script = '''
import json,yaml,sys
from pathlib import Path
from lib.deepseek_params import build_request
from lib.reference_specs import test_profiles_for_reference
from lib.config import PROJECT_ROOT
c=yaml.safe_load((PROJECT_ROOT/'config.yaml').read_text());result={}
for contract,family,model,form,route in [
('deepseek_v4_pro_0813_chat','deepseek','deepseek-v4-pro','openai_chat_completions','vendor_direct'),
('deepseek_v4_pro_0813_responses','deepseek','deepseek-v4-pro','openai_responses','vendor_direct'),
('deepseek_v4_pro_0813_anthropic','deepseek','deepseek-v4-pro','anthropic_messages','vendor_direct'),
('deepseek_v4_pro_0813_fim_beta','deepseek','deepseek-v4-pro','openai_fim_completions_beta','vendor_direct'),
('gemini_3_7_flash_generate_content','gemini','gemini-3.7-flash','gemini_generate_content','google_ai_studio')]:
 for profile in test_profiles_for_reference(contract):
  overrides={'model':model}
  if sys.argv[1]=='true':overrides['prompt']='Public JSON fixture: return exactly {"answer":2}.'
  req=build_request(c,'compatibility_profiles',profile,overrides=overrides,model_family_override=family,api_form_override=form,route_profile_override=route,reference_source=contract,parameter_test=sys.argv[1]=='true')
  if sys.argv[1]=='true':assert '10000 10001' not in json.dumps(req.body)
  result[contract+'/'+profile]={'body':req.body,'transport':req.metadata['transport']}
print(json.dumps(result,sort_keys=True))
'''
    results = [json.loads(subprocess.check_output([sys.executable, "-c", script, "true" if parameter_test else "false"], cwd=root, text=True))
               for root in [REPO_ROOT, APP_ROOT]]
    assert len(results[0]) == 124
    assert results[0] == results[1]
