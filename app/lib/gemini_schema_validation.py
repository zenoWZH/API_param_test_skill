"""Bounded validation of the exact Gemini 3.7 AI Studio wire schema.

Official references checked 2026-09-08:
https://ai.google.dev/api/generate-content#Schema
https://ai.google.dev/api/generate-content#GenerationConfig
https://ai.google.dev/gemini-api/docs/generate-content/structured-output

This checks the documented structural subset used by the approved fixtures. An
unimplemented schema keyword is unverified, never silently counted as enforced.
It neither certifies a provider nor proves that a schema caused the output.
"""
from __future__ import annotations

import json
import math
from urllib.parse import urlsplit

CONTRACT = "gemini_3_7_flash_generate_content"
MODEL = "gemini-3.7-flash"
SCHEMA_PROFILES = {"gemini_native_response_schema", "gemini_native_response_json_schema"}
_TYPES = {"string", "number", "integer", "boolean", "object", "array", "null"}
_KEYS = {"type", "properties", "required", "items", "additionalProperties", "enum",
         "minimum", "maximum", "minItems", "maxItems", "anyOf", "oneOf",
         "title", "description"}


class _Unverified(ValueError):
    pass


def _finite_number(value):
    return type(value) in (int, float) and (type(value) is int or math.isfinite(value))


def _normalize(schema, *, native, depth=0):
    if depth > 24 or not isinstance(schema, dict) or not schema:
        raise _Unverified("schema shape or depth")
    allowed = _KEYS | ({"nullable", "default", "example"} if native else set())
    if set(schema) - allowed or (native and {"oneOf", "additionalProperties"} & set(schema)):
        raise _Unverified("schema keyword")
    result = dict(schema)
    types = schema.get("type")
    if types is not None:
        values = types if isinstance(types, list) and not native else [types]
        if not values or any(not isinstance(value, str) for value in values):
            raise _Unverified("schema type")
        values = [value.lower() if native else value for value in values]
        if any(value not in _TYPES for value in values):
            raise _Unverified("schema type")
        result["type"] = values
    elif native and "anyOf" not in schema:
        raise _Unverified("native schema type")
    if "nullable" in schema and type(schema["nullable"]) is not bool:
        raise _Unverified("nullable")
    for key in ("properties",):
        if key in schema:
            if not isinstance(schema[key], dict) or any(not isinstance(k, str) for k in schema[key]):
                raise _Unverified(key)
            result[key] = {k: _normalize(v, native=native, depth=depth + 1) for k, v in schema[key].items()}
    for key in ("required",):
        if key in schema and (not isinstance(schema[key], list)
                or any(not isinstance(v, str) for v in schema[key])
                or len(schema[key]) != len(set(schema[key]))):
            raise _Unverified(key)
    for key in ("items", "additionalProperties"):
        if key in schema:
            if key == "additionalProperties" and type(schema[key]) is bool:
                continue
            result[key] = _normalize(schema[key], native=native, depth=depth + 1)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            if not isinstance(schema[key], list) or not schema[key]:
                raise _Unverified(key)
            result[key] = [_normalize(v, native=native, depth=depth + 1) for v in schema[key]]
    for key in ("minItems", "maxItems"):
        if key in schema:
            value = schema[key]
            # Google's OpenAPI Schema encodes int64 constraints as strings.
            if native and isinstance(value, str) and value.isascii() and value.isdigit():
                value = int(value)
            if type(value) is not int or value < 0:
                raise _Unverified(key)
            result[key] = value
    for key in ("minimum", "maximum"):
        if key in schema and not _finite_number(schema[key]):
            raise _Unverified(key)
    if "enum" in schema:
        if (not isinstance(schema["enum"], list) or not schema["enum"]
                or any(not isinstance(v, str) and not _finite_number(v) for v in schema["enum"])
                or (native and any(not isinstance(v, str) for v in schema["enum"]))):
            raise _Unverified("enum")
    for low, high in (("minimum", "maximum"), ("minItems", "maxItems")):
        if low in result and high in result and result[low] > result[high]:
            raise _Unverified("inverted bounds")
    return result


def _matches_type(value, kind):
    return {"null": value is None, "boolean": type(value) is bool,
            "object": isinstance(value, dict), "array": isinstance(value, list),
            "string": isinstance(value, str), "number": _finite_number(value),
            "integer": _finite_number(value) and (type(value) is int or value.is_integer())}[kind]


def _matches(value, schema):
    if value is None and schema.get("nullable") is True:
        return True
    if "type" in schema and not any(_matches_type(value, kind) for kind in schema["type"]):
        return False
    if "enum" in schema and not any(
            value == member and (type(value) is type(member) or (_finite_number(value) and _finite_number(member)))
            for member in schema["enum"]):
        return False
    # Google explicitly interprets oneOf as anyOf, not exclusive-one matching.
    for key in ("anyOf", "oneOf"):
        if key in schema and not any(_matches(value, child) for child in schema[key]):
            return False
    if _finite_number(value):
        if "minimum" in schema and value < schema["minimum"]:
            return False
        if "maximum" in schema and value > schema["maximum"]:
            return False
    if isinstance(value, dict):
        if any(key not in value for key in schema.get("required", [])):
            return False
        properties = schema.get("properties", {})
        for key, member in value.items():
            if key in properties:
                if not _matches(member, properties[key]):
                    return False
            elif schema.get("additionalProperties") is False:
                return False
            elif isinstance(schema.get("additionalProperties"), dict):
                if not _matches(member, schema["additionalProperties"]):
                    return False
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            return False
        if "items" in schema and not all(_matches(member, schema["items"]) for member in value):
            return False
    return True


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("nonfinite JSON value")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite JSON number")
    return number


def validate_gemini37_schema(profile, response, *, body, transport, reference_source, request_context=None):
    """Return (handled, error); other contracts and non-schema requests fall through.

    Context comes from the same client URL builder used for this request. The
    model is URL-bound, so neither response identity nor a catalog default can
    stand in for this context. No credentials, reports, or catalog are read.
    """
    if reference_source != CONTRACT or transport != "gemini_generate_content":
        return False, None
    config = body.get("generationConfig", {}) if isinstance(body, dict) else {}
    fields = [key for key in ("responseSchema", "responseJsonSchema") if key in config] if isinstance(config, dict) else []
    if not fields and profile not in SCHEMA_PROFILES:
        return False, None
    if not isinstance(request_context, dict):
        return True, "gemini_schema_scope_unverified"
    try:
        url = urlsplit(request_context.get("request_url", ""))
        exact = (request_context.get("requested_model") == MODEL
                 and url.scheme == "https" and url.hostname == "generativelanguage.googleapis.com"
                 and url.port in (None, 443) and url.username is None and url.password is None
                 and not url.query and not url.fragment
                 and url.path == f"/v1beta/models/{MODEL}:generateContent")
    except (ValueError, TypeError, AttributeError):
        exact = False
    if not exact:
        return True, "gemini_schema_scope_unverified"
    if (len(fields) != 1 or config.get("responseMimeType") != "application/json"
            or "_responseJsonSchema" in config or "responseFormat" in config):
        return True, "gemini_schema_request_invalid"
    try:
        schema = _normalize(config[fields[0]], native=fields[0] == "responseSchema")
    except (ValueError, TypeError, RecursionError):
        return True, "gemini_schema_unverified"
    if not isinstance(response, dict) or response.get("modelVersion") != MODEL:
        return True, "gemini_schema_model_mismatch"
    feedback = response.get("promptFeedback")
    if (response.get("error") or (feedback is not None and not isinstance(feedback, dict))
            or (isinstance(feedback, dict) and feedback.get("blockReason"))):
        return True, "gemini_schema_response_invalid"
    candidates = response.get("candidates")
    count = config.get("candidateCount", 1)
    if type(count) is not int or count < 1 or not isinstance(candidates, list) or len(candidates) != count:
        return True, "gemini_schema_response_invalid"
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("finishReason") != "STOP":
            return True, "gemini_schema_response_incomplete"
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list) or not parts or content.get("role", "model") != "model":
            return True, "gemini_schema_response_invalid"
        texts = []
        for part in parts:
            if (not isinstance(part, dict) or not isinstance(part.get("text"), str)
                    or ("thought" in part and type(part["thought"]) is not bool)):
                return True, "gemini_schema_response_invalid"
            if part.get("thought") is not True:
                texts.append(part["text"])
        try:
            value = json.loads("".join(texts), object_pairs_hook=_unique_object,
                               parse_constant=_reject_constant, parse_float=_finite_float)
            if not _matches(value, schema):
                return True, "json_schema_mismatch"
        except (ValueError, TypeError, RecursionError):
            return True, "json_parse"
    return True, None
