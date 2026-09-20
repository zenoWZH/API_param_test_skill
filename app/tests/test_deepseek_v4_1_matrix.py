"""Guard the official V4.1 request scope and evidence coverage ledger."""
from __future__ import annotations

import base64
import copy
import json
import re
import struct
import sys
import zlib
from collections import Counter
from pathlib import Path

import pytest

from lib.deepseek_v4_1_matrix import (
    ANTHROPIC,
    CHAT,
    FIM,
    MODEL,
    PATHS,
    PREFIX,
    PROFILE_ID,
    RESPONSES,
    build_cases,
    documented_parameters,
    parameter_coverage,
)


def test_smoke_is_a_complete_five_form_two_transport_suite():
    smoke = [case for case in build_cases() if case["smoke"]]
    assert len(smoke) == 10
    assert {(case["api_form"], case["body"]["stream"]) for case in smoke} == {
        (form, stream) for form in PATHS for stream in (False, True)
    }
    assert all(case["expected"]["kind"] != "error" for case in smoke)


def test_official_identity_paths_and_bounded_output_are_enforced():
    cases = build_cases()
    assert len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        body = case["body"]
        assert body["model"] == MODEL == "deepseek-flash"
        assert type(body["stream"]) is bool
        cap = body.get("max_tokens", body.get("max_output_tokens"))
        assert type(cap) is int and 256 <= cap <= 4096
        assert not any(field in body for field in ("max_completion_tokens", "file_id"))
        assert case["path"] == PATHS[case["api_form"]] or (
            case["id"] == "chat_tool_strict_stream" and case["path"] == "/beta/chat/completions"
        )
        assert "http" not in case["path"]
        thinking = (
            body.get("thinking", {}).get("type") == "enabled"
            or body.get("reasoning", {}).get("effort") in ("low", "high", "max")
            or body.get("reasoning_effort") in ("low", "high", "max")
        )
        if thinking:
            assert cap >= 2048
        if case["api_form"] == FIM:
            assert not {"thinking", "reasoning_effort", "tools", "messages", "response_format"}.intersection(body)


def test_matrix_and_coverage_do_not_mutate_across_calls():
    original = build_cases()
    corrupted = build_cases()
    corrupted[0]["body"]["messages"][0]["content"] = "not the original prompt"
    corrupted[0]["target_parameters"].append("bogus")
    corrupted[0]["expected"]["exact_text"] = "bogus"
    assert build_cases() == original
    coverage = parameter_coverage()
    saved = copy.deepcopy(coverage)
    coverage[0]["case_ids"].clear()
    assert parameter_coverage() == saved
    assert json.loads(json.dumps(original)) == original


def test_coverage_matches_every_current_profile_contract_entry():
    package = Path(__file__).resolve().parents[1] / "packages" / "model-profile-db"
    sys.path.insert(0, str(package))
    try:
        from model_profile_db import load_catalog

        catalog = load_catalog()
        profile = catalog.get_profile(PROFILE_ID)
        required = {
            (interface["api_form"], parameter)
            for interface in profile["interfaces"]
            for parameter in catalog.get_contract(interface["default_contract_id"])["parameter_capabilities"]
        }
    finally:
        sys.path.remove(str(package))
    coverage = parameter_coverage()
    assert len(coverage) == len(required) == 186
    assert {(row["api_form"], row["parameter"]) for row in coverage} == required
    assert Counter(row["api_form"] for row in coverage) == {
        CHAT: 33, PREFIX: 35, FIM: 19, RESPONSES: 51, ANTHROPIC: 48,
    }


def test_coverage_links_only_actual_scoped_cases_and_never_claims_a_pass():
    cases = {case["id"]: case for case in build_cases()}
    source = documented_parameters()
    coverage = parameter_coverage()
    for row in coverage:
        assert row["live_verified"] is False
        assert row["status"] in {
            "planned_probe", "planned_acceptance_probe", "documented_not_probed", "unknown_not_probed",
        }
        assert row["reason"]
        assert row["documentation_state"] == source[row["api_form"]][row["parameter"]]["state"]
        assert row["official_sources"]
        for case_id in row["case_ids"]:
            assert cases[case_id]["api_form"] == row["api_form"]
            assert row["parameter"] in cases[case_id]["target_parameters"]
        if row["documentation_state"] == "unknown":
            assert row["status"] == "unknown_not_probed"
            assert row["case_ids"] == []
        if row["status"].startswith("planned"):
            assert row["case_ids"]
        else:
            assert row["case_ids"] == []
    for case in cases.values():
        assert set(case["target_parameters"]).issubset(source[case["api_form"]])


def test_sampling_and_ignored_fields_do_not_claim_unobservable_semantics():
    coverage = parameter_coverage()
    for row in coverage:
        if row["case_ids"] and row["parameter"] in ("top_p", "temperature"):
            assert "statistical evidence" in row["reason"]
        if row["documentation_state"] == "ignored" and row["case_ids"]:
            assert row["status"] == "planned_acceptance_probe"
            assert "not empirically proven" in row["reason"]


def test_all_primary_reasoning_efforts_have_explicit_positive_cases():
    cases = [case for case in build_cases() if case["expected"]["kind"] != "error"]
    assert {case["body"].get("reasoning_effort") for case in cases if case["api_form"] == CHAT} >= {"none", "low", "high", "max"}
    assert {case["body"].get("reasoning", {}).get("effort") for case in cases if case["api_form"] == RESPONSES} >= {"none", "low", "high", "max"}
    assert {case["body"].get("output_config", {}).get("effort") for case in cases if case["api_form"] == ANTHROPIC} >= {"low", "high", "max"}
    none_case = next(case for case in cases if case["id"] == "chat_effort_none")
    assert "thinking" not in none_case["body"]


@pytest.mark.parametrize("form", [CHAT, RESPONSES, ANTHROPIC])
def test_each_main_form_requires_a_real_tool_call_with_validated_arguments(form):
    cases = [case for case in build_cases() if case["api_form"] == form and case["expected"]["kind"] == "tool"]
    assert cases
    assert any(case["body"]["stream"] for case in cases)
    assert any(case["expected"].get("reasoning_contract") == "schema" for case in cases)
    for case in cases:
        expected = case["expected"]
        assert expected["tool_name"] == "add_numbers"
        assert expected["tool_arguments"] == {"a": 2, "b": 3}
        tool = case["body"]["tools"][0]
        if form == CHAT:
            tool = tool["function"]
        schema = tool["input_schema" if form == ANTHROPIC else "parameters"]
        assert tool["name"] == expected["tool_name"]
        assert set(schema["required"]) == set(expected["tool_arguments"])
        assert schema["additionalProperties"] is False


def test_negative_cases_require_parameter_attribution_and_keep_unknowns_unprobed():
    negatives = [case for case in build_cases() if case["expected"]["kind"] == "error"]
    assert len(negatives) >= 7
    for case in negatives:
        assert case["expected"]["status_codes"] == [400]
        assert case["expected"]["error_fields"]
        assert not case["smoke"]
        assert not case["acceptance_only"]
    # The Chat-only thinking/required rejection must not leak to compat forms.
    assert {case["api_form"] for case in negatives if "thinking" in case["id"]} == {CHAT}


def test_inline_image_fixture_is_valid_and_identical_across_supported_forms():
    fixtures = []
    cases = [case for case in build_cases() if case["expected"]["kind"] == "image"]
    assert {case["api_form"] for case in cases} == {CHAT, RESPONSES, ANTHROPIC}
    for case in cases:
        body = case["body"]
        if case["api_form"] == RESPONSES:
            encoded = body["input"][0]["content"][1]["image_url"].split(",", 1)[1]
        elif case["api_form"] == CHAT:
            encoded = body["messages"][0]["content"][1]["image_url"]["url"].split(",", 1)[1]
        else:
            encoded = body["messages"][0]["content"][1]["source"]["data"]
        fixture = base64.b64decode(encoded, validate=True)
        fixtures.append(fixture)
        assert fixture.startswith(b"\x89PNG\r\n\x1a\n")
        offset = 8
        image_data = b""
        while offset < len(fixture):
            length = struct.unpack(">I", fixture[offset:offset + 4])[0]
            name = fixture[offset + 4:offset + 8]
            data = fixture[offset + 8:offset + 8 + length]
            checksum = struct.unpack(">I", fixture[offset + 8 + length:offset + 12 + length])[0]
            assert checksum == zlib.crc32(name + data)
            if name == b"IHDR":
                assert struct.unpack(">IIBBBBB", data) == (128, 128, 8, 2, 0, 0, 0)
            if name == b"IDAT":
                image_data += data
            offset += length + 12
        assert zlib.decompress(image_data) == (b"\x00" + b"\xff\x00\x00" * 128) * 128
        assert case["expected"]["contains"] == "red"
    assert fixtures[0] == fixtures[1] == fixtures[2]


def test_prefix_and_fim_have_semantic_assertions_beyond_http_success():
    for case in build_cases():
        if case["api_form"] == PREFIX:
            last_message = case["body"]["messages"][-1]
            assert last_message["role"] == "assistant"
            assert last_message["prefix"] is True
            if case["expected"]["kind"] == "error":
                assert case["body"]["response_format"] == {"type": "json_object"}
                assert case["expected"]["status_codes"] == [400]
                continue
            assert case["expected"]["prefix"] == last_message["content"]
            if "python_function_name" in case["expected"]:
                assert case["expected"]["python_function_name"] == "quick_sort"
                assert case["expected"]["python_arguments"] == ["arr"]
                assert case["body"]["stop"] == ["```"]
            else:
                assert case["expected"]["json_contains"] == {"answer": "BLUE"}
                if "stop" in case["body"]:
                    assert case["body"]["stop"] == ["<END>"]
                    assert "<END>" in case["body"]["messages"][0]["content"]
                    assert all(stop not in '{"answer":"BLUE"}' for stop in case["body"]["stop"])
                    assert case["expected"]["forbid_contains"] == "<END>"
        if case["api_form"] == FIM and case["expected"]["kind"] != "error":
            assert case["expected"]["contains"] == "Paris"
            assert case["expected"]["reconstructed_pattern"]
            if case["body"].get("echo"):
                assert case["expected"]["echo_prefix"] == case["body"]["prompt"]


def test_fim_few_shot_fixture_preserves_strict_semantic_reconstruction():
    cases = [case for case in build_cases() if case["api_form"] == FIM and case["expected"]["kind"] != "error"]
    assert len({case["body"]["prompt"] for case in cases}) == 1
    for case in cases:
        prompt = case["body"]["prompt"]
        assert prompt.endswith("Japan -> Tokyo\nFrance -> ")
        assert "Germany -> Berlin\nItaly -> Rome\n" in prompt
        suffix = case["body"].get("suffix", "")
        pattern = case["expected"]["reconstructed_pattern"]
        assert re.fullmatch(pattern, prompt + "Paris" + suffix)
        assert not re.fullmatch(pattern, prompt + "1054 km from the capital of Germany" + suffix)
        assert not re.fullmatch(pattern, prompt + "Rome" + suffix)
        assert not re.fullmatch(pattern, prompt + "Paris and also Rome" + suffix)


def test_followup_diagnostics_preserve_failed_dimensions_and_smoke_membership():
    cases = {case["id"]: case for case in build_cases()}
    assert len(cases) == 61
    assert {case["id"] for case in cases.values() if case["smoke"]} == {
        "chat_text", "chat_stream", "responses_text", "responses_stream", "anthropic_text",
        "anthropic_stream", "prefix_text", "prefix_stream", "fim_text", "fim_suffix_stream",
    }
    assert "prefix_thinking" in cases
    prefilled = cases["prefix_thinking"]
    assert prefilled["body"]["thinking"] == {"type": "enabled"}
    assert prefilled["body"]["messages"][-1]["reasoning_content"]
    assert "require_reasoning" not in prefilled["expected"]
    assert prefilled["expected"]["json_contains"] == {"answer": "BLUE"}
    assert prefilled["notes"].startswith("prefilled reasoning accepted; nullable returned reasoning does not prove a new reasoning chain")
    reasoning_coverage = next(row for row in parameter_coverage() if row["api_form"] == PREFIX and row["parameter"] == "messages[-1].reasoning_content")
    assert reasoning_coverage["notes"] == prefilled["notes"]
    code_default = cases["prefix_code_default_thinking"]
    code_disabled = cases["prefix_code_nonthinking"]
    assert "thinking" not in code_default["body"]
    assert "reasoning_effort" not in code_default["body"]
    assert code_default["body"]["max_tokens"] == 4096
    assert code_disabled["body"]["thinking"] == {"type": "disabled"}
    assert "temperature" not in code_default["body"]
    for case in cases.values():
        if case["api_form"] == PREFIX and case["body"].get("thinking", {}).get("type") == "disabled":
            assert case["body"]["temperature"] == 0
            assert "temperature" in case["target_parameters"]
    assert code_default["body"]["messages"] == code_disabled["body"]["messages"]
    no_stop = cases["prefix_json_no_stop"]
    assert "stop" not in no_stop["body"]
    assert "<END>" not in json.dumps(no_stop)
    assert no_stop["expected"]["json_contains"] == {"answer": "BLUE"}
    max_effort = cases["responses_effort_max"]
    assert "17 * 23 + 41 * 7" in max_effort["body"]["input"]
    assert max_effort["expected"] == {"kind": "text", "exact_text": "678", "reasoning_contract": "schema"}
    assert cases["fim_logprobs_top_p"]["body"]["temperature"] == 0
    assert cases["fim_logprobs_top_p"]["body"]["top_p"] == 0.8


def test_json_prefix_shape_cases_preserve_modes_and_strict_expectations():
    cases = {case["id"]: case for case in build_cases()}
    for variant in ("quote", "mode"):
        for stream in (False, True):
            case = cases["prefix_json_" + variant + ("_stream" if stream else "")]
            body = case["body"]
            assert not case["smoke"]
            assert body["stream"] is stream
            assert body["max_tokens"] == 512
            assert body["thinking"] == {"type": "disabled"}
            assert body["temperature"] == 0
            assert "stop" not in body
            if variant == "quote":
                assert body["messages"][0]["role"] == "system"
                assert "JSON Unicode escape sequences" in body["messages"][0]["content"]
            prefix = '{"answer":"B' if variant == "quote" else '{"answer":'
            assert body["messages"][-1] == {"role": "assistant", "content": prefix, "prefix": True}
            assert case["notes"]
            if variant == "quote":
                assert case["expected"] == {"kind": "prefix", "prefix": prefix, "json_contains": {"answer": "BLUE"}, "exact_json": True}
                assert body["response_format"] == {"type": "text"}
                assert json.loads(prefix + 'LUE"}') == {"answer": "BLUE"}
            else:
                assert case["expected"] == {"kind": "error", "status_codes": [400], "error_fields": ["response_format", "prefix"]}
                assert body["response_format"] == {"type": "json_object"}
                assert "response_format" in case["target_parameters"]
                assert "reports/deepseek_v41/prefix_shapes_20260917_01" in case["notes"]
                assert "incompatible combination" in case["notes"]


def test_open_quote_json_prefix_requires_the_full_value_and_preserves_partial_word_probe():
    cases = {case["id"]: case for case in build_cases()}
    for stream in (False, True):
        suffix = "_stream" if stream else ""
        case = cases["prefix_json_open_quote" + suffix]
        body = case["body"]
        prefix = '{"answer": "'
        assert body["messages"][-1] == {"role": "assistant", "content": prefix, "prefix": True}
        assert body["messages"][0] == cases["prefix_json_mode"]["body"]["messages"][0]
        assert body["stream"] is stream
        assert body["thinking"] == {"type": "disabled"}
        assert body["temperature"] == 0
        assert body["max_tokens"] == 512
        assert "response_format" not in body and "stop" not in body
        assert case["expected"] == {"kind": "prefix", "prefix": prefix, "json_contains": {"answer": "BLUE"}, "exact_json": True}
        assert json.loads(prefix + 'BLUE"}') == {"answer": "BLUE"}
        assert cases["prefix_json_quote" + suffix]["expected"]["json_contains"] == {"answer": "BLUE"}
        assert not case["smoke"]
    coverage = next(row for row in parameter_coverage() if row["api_form"] == PREFIX and row["parameter"] == "response_format")
    assert "incompatible combination" in coverage["notes"]
    assert "reports/deepseek_v41/prefix_shapes_20260917_01" in coverage["notes"]


def test_primary_prefix_fixtures_generate_the_complete_value_and_keep_failed_shapes():
    cases = {case["id"]: case for case in build_cases()}
    for case_id in ("prefix_text", "prefix_stream", "prefix_thinking"):
        case = cases[case_id]
        prefix = '{"answer": "'
        assert case["body"]["messages"][-1]["content"] == prefix
        assert case["expected"]["prefix"] == prefix
        assert case["expected"]["json_contains"] == {"answer": "BLUE"}
        assert case["body"]["stop"] == ["<END>"]
        assert json.loads(prefix + 'BLUE"}') == {"answer": "BLUE"}
        assert "reports/deepseek_v41/prefix_open_quote_20260917_01" in case["notes"]
        assert "Historical bare-colon and partial-word failures" in case["notes"]
    assert cases["prefix_json_no_stop"]["body"]["messages"][-1]["content"] == '{"answer":'
    assert cases["prefix_json_no_stop"]["expected"]["json_contains"] == {"answer": "BLUE"}
    for suffix in ("", "_stream"):
        assert cases["prefix_json_quote" + suffix]["body"]["messages"][-1]["content"] == '{"answer":"B'
        assert cases["prefix_json_quote" + suffix]["expected"]["json_contains"] == {"answer": "BLUE"}
        assert cases["prefix_json_mode" + suffix]["body"]["messages"][-1]["content"] == '{"answer":'
        assert cases["prefix_json_mode" + suffix]["expected"]["kind"] == "error"


def test_fixed_text_and_json_fixtures_require_exact_semantics():
    for case in build_cases():
        expected = case["expected"]
        if expected["kind"] == "text":
            assert expected["exact_text"] in {"READY", "678"}
            assert "contains" not in expected
        if "json_contains" in expected:
            assert expected["exact_json"] is True
            assert expected["json_contains"] == {"answer": "BLUE"}


def test_thinking_cases_validate_nullable_reasoning_schema_without_claiming_nonempty_output():
    cases = {case["id"]: case for case in build_cases()}
    schema_cases = {
        "chat_thinking_top_p", "chat_effort_high", "chat_effort_max", "chat_tool_thinking_auto",
        "responses_thinking_top_p", "responses_effort_high", "responses_effort_max",
        "responses_tool_thinking_auto", "anthropic_thinking_top_p", "anthropic_effort_high",
        "anthropic_effort_max", "anthropic_tool_thinking_stream", "anthropic_ignored_fields",
    }
    assert {case_id for case_id, case in cases.items()
            if case["expected"].get("reasoning_contract") == "schema"} == schema_cases
    assert all("require_reasoning" not in case["expected"] for case in cases.values())
    for case_id in schema_cases:
        case = cases[case_id]
        body = case["body"]
        if case["api_form"] == RESPONSES:
            assert body["reasoning"]["effort"] in {"low", "high", "max"}
        elif case_id.startswith("chat_effort_"):
            assert "thinking" not in body
            assert body["reasoning_effort"] in {"high", "max"}
        else:
            assert body["thinking"] == ({"type": "enabled", "budget_tokens": 1024}
                                         if case_id == "anthropic_ignored_fields" else {"type": "enabled"})
        if case["expected"]["kind"] == "tool":
            assert case["expected"]["tool_name"] == "add_numbers"
            assert case["expected"]["tool_arguments"] == {"a": 2, "b": 3}
        else:
            assert case["expected"]["exact_text"] == ("678" if case_id == "responses_effort_max" else "READY")


@pytest.mark.parametrize("case_id,prefix,stream", [
    ("prefix_json_no_stop", '{"answer":', False),
    ("prefix_json_quote", '{"answer":"B', False),
    ("prefix_json_quote_stream", '{"answer":"B', True),
])
def test_failed_prefix_repairs_keep_original_boundary_and_strict_complete_target(case_id, prefix, stream):
    case = next(case for case in build_cases() if case["id"] == case_id)
    body = case["body"]
    assert case["path"] == "/beta/chat/completions"
    assert not case["smoke"] and not case["acceptance_only"]
    assert body["model"] == "deepseek-flash"
    assert body["stream"] is stream
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0
    assert body["max_tokens"] == 512
    assert "stop" not in body
    assert body["response_format"] == {"type": "text"}
    assert body["reasoning_effort"] == "none"
    assert len(body["messages"]) == 3
    assert body["messages"][-1] == {"role": "assistant", "content": prefix, "prefix": True}
    assert body["messages"][0]["role"] == "system"
    system = body["messages"][0]["content"]
    baseline_system = (
        "Finish the current assistant message as one complete JSON object with exactly one key, answer. "
        "Preserve its existing opening. The answer value is the concatenation of the uppercase characters "
        "supplied by the user, in their listed order. "
        "Do not add any other fields, markdown, or explanations."
    )
    if prefix.endswith("B"):
        assert "JSON Unicode escape sequences exactly" in system
        assert "do not double-escape" in system
        assert body["messages"][1] == {"role": "user", "content": (
            r'The target serialized JSON document is {"answer":"B\u004c\u0055\u0045"}. '
            "Complete the assistant message to exactly this document."
        )}
        serialized_target = r'{"answer":"B\u004c\u0055\u0045"}'
        assert serialized_target.startswith(prefix)
        assert json.loads(serialized_target) == {"answer": "BLUE"}
    else:
        assert system == baseline_system
        assert body["messages"][1] == {
        "role": "user",
        "content": "The characters for the answer string are: B, L, U, E. "
                   "Use exactly these four characters in this order.",
        }
    serialized_messages = json.dumps(body["messages"])
    assert "BLUE" not in serialized_messages
    assert "LUE" not in serialized_messages
    assert "remaining" not in serialized_messages and "suffix" not in serialized_messages
    assert case["expected"] == {
        "kind": "prefix", "prefix": prefix, "json_contains": {"answer": "BLUE"}, "exact_json": True,
    }
    assert set(case["target_parameters"]) == {
        "model", "messages", "max_tokens", "stream", "thinking", "temperature", "reasoning_effort", "response_format",
        "messages[-1].role", "messages[-1].content", "messages[-1].prefix",
    }
