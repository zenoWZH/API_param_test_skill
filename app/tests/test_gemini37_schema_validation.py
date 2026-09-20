"""The same offline fixtures run against each consumer's own public config."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from lib.deepseek_params import build_request
from lib.gemini_schema_validation import CONTRACT, MODEL
from lib.profile_validation import validate_profile_response

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = {"requested_model": MODEL,
           "request_url": f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"}
VALID = {"summary": "A short summary", "items": ["one", "two"]}


def request(profile="gemini_native_response_json_schema"):
    config = yaml.safe_load((ROOT / "config.yaml").read_text())
    return build_request(config, "compatibility_profiles", profile,
        overrides={"model": MODEL}, model_family_override="gemini",
        api_form_override="gemini_generate_content", route_profile_override="google_ai_studio",
        reference_source=CONTRACT).body


def response(value=VALID):
    return {"modelVersion": MODEL, "candidates": [{"finishReason": "STOP",
        "content": {"role": "model", "parts": [{"text": json.dumps(value)}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}}


def validate(payload=None, *, body=None, profile="gemini_native_response_json_schema", context=CONTEXT,
             contract=CONTRACT, transport="gemini_generate_content", success=True):
    return validate_profile_response(profile, response() if payload is None else payload,
        SimpleNamespace(success=success, usage={}, failure_classification="http_error", error_type="http_error"),
        request_body=request(profile) if body is None else body, transport=transport,
        reference_source=contract, request_context=context)


@pytest.mark.parametrize("profile", ["gemini_native_response_schema", "gemini_native_response_json_schema"])
def test_actual_public_wire_schema_accepts_only_matching_required_types(profile):
    body = request(profile)
    before = copy.deepcopy(body)
    field = "responseSchema" if profile == "gemini_native_response_schema" else "responseJsonSchema"
    assert body["generationConfig"][field]["required"] == ["summary", "items"]
    assert validate(body=body, profile=profile) is None
    for wrong in ({"wrong_field": 42}, {"summary": 42, "items": []}, {"summary": "ok"},
                  {"summary": "ok", "items": "one"}, {"summary": "ok", "items": [False]}):
        assert validate(response(wrong), body=body, profile=profile) == "json_schema_mismatch"
    assert body == before


def test_additional_properties_follow_actual_schema_not_profile_name():
    extra = {**VALID, "extra": 4}
    assert validate(response(extra)) == "json_schema_mismatch"
    body = request()
    body["generationConfig"]["responseJsonSchema"]["additionalProperties"] = True
    assert validate(response(extra), body=body) is None
    assert validate(response(extra), profile="gemini_native_response_schema") is None
    # A schema remains binding even when a caller uses an unrelated profile label.
    assert validate(response({"wrong_field": 42}), body=body, profile="gemini_native_temperature") == "json_schema_mismatch"


@pytest.mark.parametrize("value,expected", [(2, None), (2.0, None), (True, "json_schema_mismatch"),
    (1, "json_schema_mismatch"), (4, "json_schema_mismatch"), (2.5, "json_schema_mismatch")])
def test_numeric_type_enum_and_bounds(value, expected):
    body = request()
    body["generationConfig"]["responseJsonSchema"] = {"type": "integer", "enum": [1, 2, 3, 4], "minimum": 2, "maximum": 3}
    assert validate(response(value), body=body) == expected


@pytest.mark.parametrize("value,expected", [(["ok"], None), ([], "json_schema_mismatch"),
    (["a", "b", "c"], "json_schema_mismatch"), ([1], "json_schema_mismatch")])
def test_native_array_constraints_and_top_level_non_object(value, expected):
    body = request("gemini_native_response_schema")
    body["generationConfig"]["responseSchema"] = {"type": "ARRAY", "items": {"type": "STRING"}, "minItems": "1", "maxItems": "2"}
    assert validate(response(value), body=body, profile="gemini_native_response_schema") == expected


def test_nested_properties_nullable_and_google_oneof_semantics():
    body = request()
    body["generationConfig"]["responseJsonSchema"] = {"type": "object", "additionalProperties": {"type": ["string", "null"]},
        "properties": {"n": {"oneOf": [{"type": "number"}, {"type": "integer"}]}}, "required": ["n"]}
    assert validate(response({"n": 2, "optional": None}), body=body) is None
    assert validate(response({"n": 2, "optional": 5}), body=body) == "json_schema_mismatch"
    native = request("gemini_native_response_schema")
    native["generationConfig"]["responseSchema"] = {"type": "STRING", "nullable": True}
    assert validate(response(None), body=native, profile="gemini_native_response_schema") is None


@pytest.mark.parametrize("patch", [{"pattern": "^a"}, {"$ref": "https://example.invalid/schema"},
    {"prefixItems": [{"type": "string"}]}, {"format": "date"}, {"type": "unknown"},
    {"minItems": True}, {"minimum": True}, {"propertyOrdering": ["summary", "items"]}])
def test_unimplemented_or_malformed_schema_never_passes(patch):
    body = request()
    body["generationConfig"]["responseJsonSchema"].update(patch)
    assert validate(body=body) == "gemini_schema_unverified"


@pytest.mark.parametrize("fault", ["missing", "both", "mime", "deprecated", "response_format"])
def test_schema_request_shape_must_be_unambiguous(fault):
    body = request()
    config = body["generationConfig"]
    if fault == "missing": config.pop("responseJsonSchema")
    elif fault == "both": config["responseSchema"] = {"type": "OBJECT"}
    elif fault == "mime": config["responseMimeType"] = "text/plain"
    elif fault == "deprecated": config["_responseJsonSchema"] = {}
    else: config["responseFormat"] = {"text": {"mimeType": "application/json"}}
    assert validate(body=body) == "gemini_schema_request_invalid"


@pytest.mark.parametrize("context", [None, {}, {**CONTEXT, "requested_model": "gemini-3.6-flash"},
    {**CONTEXT, "request_url": CONTEXT["request_url"].replace("v1beta", "v1")},
    {**CONTEXT, "request_url": CONTEXT["request_url"].replace("googleapis.com", "example.invalid")},
    {**CONTEXT, "request_url": CONTEXT["request_url"].replace("https:", "http:")},
    {**CONTEXT, "request_url": CONTEXT["request_url"] + "?key=not-a-real-key"},
    {**CONTEXT, "request_url": CONTEXT["request_url"] + "#fragment"},
    {**CONTEXT, "request_url": 42}])
def test_source_model_and_actual_version_cannot_be_inferred_from_reply(context):
    assert validate(context=context) == "gemini_schema_scope_unverified"


@pytest.mark.parametrize("contract,transport", [("gemini_native_generate_content", "gemini_generate_content"),
    ("gemini_3_7_flash_vertex_generate_content_reference", "gemini_generate_content"),
    (CONTRACT, "chat_completions")])
def test_other_contracts_and_transports_keep_existing_behavior(contract, transport):
    assert validate(response({"wrong_field": 42}), contract=contract, transport=transport) is None


def test_non_schema_json_and_failed_requests_keep_existing_behavior():
    body = request("gemini_native_response_mime_type")
    assert validate(response({"anything": 42}), body=body, profile="gemini_native_response_mime_type", context=None) is None
    assert validate(response([]), body=body, profile="gemini_native_response_mime_type", context=None) == "json_not_object"
    assert validate(context=None, success=False) == "http_error"


@pytest.mark.parametrize("text", ['{"summary":"first","summary":"second","items":[]}',
    '{"summary":"ok","items":[],"x":NaN}', '{"summary":"ok","items":[],"x":1e999}',
    '{"summary":', '```json\n{}\n```'])
def test_duplicate_nonfinite_truncated_or_fenced_json_does_not_pass(text):
    payload = response()
    payload["candidates"][0]["content"]["parts"][0]["text"] = text
    assert validate(payload) == "json_parse"


@pytest.mark.parametrize("fault", ["model", "candidate", "finish", "parts", "role", "thought_type", "error", "blocked", "feedback"])
def test_native_response_must_be_complete_and_match_model(fault):
    payload = response()
    if fault == "model": payload["modelVersion"] = "gemini-3.6-flash"
    elif fault == "candidate": payload["candidates"] = []
    elif fault == "finish": payload["candidates"][0]["finishReason"] = "MAX_TOKENS"
    elif fault == "parts": payload["candidates"][0]["content"]["parts"] = [{"inlineData": {}}]
    elif fault == "role": payload["candidates"][0]["content"]["role"] = "user"
    elif fault == "error": payload["error"] = {"message": "failure"}
    elif fault == "blocked": payload["promptFeedback"] = {"blockReason": "SAFETY"}
    elif fault == "feedback": payload["promptFeedback"] = "invalid"
    else: payload["candidates"][0]["content"]["parts"][0]["thought"] = "false"
    assert validate(payload) in {"gemini_schema_model_mismatch", "gemini_schema_response_invalid", "gemini_schema_response_incomplete"}


def test_all_candidates_and_only_non_thought_text_are_validated():
    body = request()
    body["generationConfig"]["candidateCount"] = 2
    payload = response()
    payload["candidates"].append(copy.deepcopy(payload["candidates"][0]))
    payload["candidates"][0]["content"]["parts"].insert(0, {"thought": True, "text": "private reasoning is not schema output"})
    assert validate(payload, body=body) is None
    payload["candidates"][1]["content"]["parts"][0]["text"] = '{"wrong": 42}'
    assert validate(payload, body=body) == "json_schema_mismatch"


@pytest.mark.parametrize("valid", [True, False])
def test_full_runner_passes_client_url_and_wire_body_to_validator(valid):
    from lib.client import ChatResult, OpenAICompatibleClient
    from scripts.param_test import run_one_profile
    config = yaml.safe_load((ROOT / "config.yaml").read_text())
    class OfflineClient(OpenAICompatibleClient):
        def __init__(self):
            self.base_url = "https://generativelanguage.googleapis.com/v1beta"
            self.api_interfaces = {"gemini_generate_content": {"base_url": self.base_url, "path": "/models/{model}:generateContent"}}
            self.calls = []
        def gemini_generate_content(self, model, body, headers=None):
            self.calls.append(copy.deepcopy(body))
            payload = response(VALID if valid else {"wrong_field": 42})
            return ChatResult(success=True, status_code=200, latency_ms=1, timestamp=0,
                response_json=payload, text=payload["candidates"][0]["content"]["parts"][0]["text"],
                usage=payload["usageMetadata"], finish_reason="STOP")
        def count_tokens(self, *args, **kwargs): return None
    client = OfflineClient()
    result = run_one_profile(config, client, "gemini", MODEL, "gemini", CONTRACT, "gemini",
        "gemini_native_response_json_schema", 1, {"id": "offline", "prompt": "Return JSON."}, expectation="supported")
    assert len(client.calls) == 1
    assert result["pass"] is valid
    if not valid:
        assert result["failure_classification"] == "json_schema_mismatch"
