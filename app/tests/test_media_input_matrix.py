"""The official modality inventory is the scope of executable media cases."""
import base64
import copy
import hashlib
import json

import pytest

from lib.media_input_matrix import API_FORMS, cases_for, find_reference, load_reference, source_digest
from lib.media_input_validation import validate_response
from lib.media_input_fixtures import audio_fixture, photo_fixture, remote_video_fixture, video_fixture


REFERENCE = load_reference()


@pytest.mark.parametrize("row", REFERENCE["models"], ids=lambda row: "/".join(row[k] for k in ("source_id", "model", "api_form")))
def test_every_exact_documented_target_has_only_applicable_cases(row):
    cases = cases_for(row["source_id"], row["model"], row["api_form"])
    retired = (row.get("lifecycle") or {}).get("status") in {"retired", "discontinued"}
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))
    for modality in ("image", "audio", "video"):
        selected = [case for case in cases if case["group"] == modality]
        assert bool(selected) == (not retired and row[modality]["status"] == "supported")
    for case in cases:
        assert set(case["depends_on"]) <= set(ids)
        assert case["target_fields"] and case["evidence"]
        assert case["fixtures"] and all(len(f["sha256"]) == 64 for f in case["fixtures"])
        body = case["body"]
        cap = body.get("max_tokens", body.get("max_completion_tokens", body.get("max_output_tokens", body.get("generationConfig", {}).get("maxOutputTokens"))))
        assert cap >= 256
        assert "tools" not in body
        assert body.get("model", row["model"]) == row["model"]


def test_exact_source_model_api_matching_never_falls_back_to_family():
    for selection in [("openai", "gpt-audio", "openai_chat_completions"),
                      ("google_ai_studio", "gemini-3.7-flash", "gemini_interactions"),
                      ("moonshot", "kimi-k3-unregistered", "openai_chat_completions")]:
        with pytest.raises(ValueError, match="exact documented"):
            cases_for(*selection)
    assert all(row["api_form"] in API_FORMS for row in REFERENCE["models"])
    assert len(source_digest()) == 64


def test_model_and_source_differences_are_preserved():
    assert cases_for("deepseek", "deepseek-v4-pro", "openai_chat_completions") == []
    assert cases_for("moonshot", "kimi-k2.5", "openai_chat_completions") == []
    assert find_reference("moonshot", "kimi-k2.5", "openai_chat_completions")["image"]["status"] == "supported"
    minimax = cases_for("minimax", "MiniMax-M3", "openai_chat_completions")
    assert {row["id"] for row in minimax} >= {"image_detail_low", "image_detail_default", "image_detail_high"}
    assert {row["id"] for row in minimax} >= {"video_detail_low", "video_detail_default", "video_detail_high"}
    assert not any(row["id"].startswith("audio_") for row in minimax)
    kimi = cases_for("moonshot", "kimi-k3", "openai_chat_completions")
    assert not any("detail" in row["id"] for row in kimi)


def test_minimax_selects_documented_split_format_without_relaxing_final_answer():
    cases = cases_for("minimax", "MiniMax-M3", "openai_chat_completions")
    assert all(case["body"]["reasoning_split"] is True for case in cases)
    assert all("extra_body" not in case["body"] for case in cases)
    assert all("cn_minimax_reasoning_split_20260920" in case["evidence"] for case in cases)
    case = next(c for c in cases if c["id"] == "real_photo_earth")
    response = receipt_for("chat_completions", "MiniMax-M3", "Earth")
    response["response"]["choices"][0]["message"]["reasoning_content"] = "Internal reasoning may mention another object."
    assert validate_response(case, response, model="MiniMax-M3", transport="chat_completions")["semantic_pass"]
    response["response"]["choices"][0]["message"]["content"] = "Moon"
    response["response"]["choices"][0]["message"]["reasoning_content"] = "Earth"
    assert not validate_response(case, response, model="MiniMax-M3", transport="chat_completions")["semantic_pass"]
    for source, model in (("openai", "gpt-4o"), ("moonshot", "kimi-k3")):
        assert all("reasoning_split" not in c["body"] for c in cases_for(source, model, "openai_chat_completions"))


def test_native_audio_and_chat_audio_have_distinct_real_wire_encodings():
    native = {row["id"]: row for row in cases_for("google_ai_studio", "gemini-3.6-flash", "gemini_generate_content")}
    chat = {row["id"]: row for row in cases_for("google_ai_studio", "gemini-3.6-flash", "openai_chat_completions")}
    assert "audio_mp3" in native and "audio_mp3" not in chat
    inline = native["audio_wav"]["body"]["contents"][0]["parts"][0]["inlineData"]
    audio = chat["audio_wav"]["body"]["messages"][0]["content"][0]["input_audio"]
    assert inline["mimeType"] == "audio/wav" and audio["format"] == "wav"
    assert base64.b64decode(audio["data"], validate=True).startswith(b"RIFF")
    assert inline["data"] == audio["data"]
    for case in (native["audio_wav"], native["audio_control_blue_nine"]):
        prompt = case["body"]["contents"][0]["parts"][-1]["text"]
        assert "green" not in prompt and "seven" not in prompt and "blue" not in prompt and "nine" not in prompt
    assert native["audio_wav"]["fixtures"] != native["audio_control_blue_nine"]["fixtures"]


def test_vertex_aac_preserves_its_source_specific_documented_mime():
    protocol = next(p for p in REFERENCE["protocols"] if p["source_id"] == "google_vertex")
    cases = cases_for("google_vertex", "gemini-3.7-flash", "gemini_generate_content")
    case = next(case for case in cases if case["id"] == "audio_aac")
    inline = case["body"]["contents"][0]["parts"][0]["inlineData"]
    assert inline["mimeType"] == "audio/x-aac"
    assert inline["mimeType"] in protocol["audio_formats"]
    assert case["fixtures"][0]["mime_type"] == inline["mimeType"]


@pytest.mark.parametrize("source,model,form", [
    ("google_ai_studio", "gemini-3.7-flash", "gemini_generate_content"),
    ("moonshot", "kimi-k3", "openai_chat_completions"),
    ("minimax", "MiniMax-M3", "openai_chat_completions"),
])
@pytest.mark.parametrize("variant", ["earth_moon", "moon_earth"])
def test_real_mp4_bytes_have_the_documented_protocol_specific_carrier(source, model, form, variant):
    case = next(case for case in cases_for(source, model, form) if case["id"] == "real_video_" + variant)
    fixture = video_fixture(variant)
    if form == "gemini_generate_content":
        parts = case["body"]["contents"][0]["parts"]
        assert parts[0]["inlineData"]["mimeType"] == "video/mp4"
        encoded = parts[0]["inlineData"]["data"]
        prompt = parts[-1]["text"]
    else:
        parts = case["body"]["messages"][0]["content"]
        assert parts[0]["type"] == "video_url"
        uri = parts[0]["video_url"]["url"]
        assert uri.startswith("data:video/mp4;base64,")
        encoded = uri.split(",", 1)[1]
        prompt = parts[-1]["text"]
    raw = base64.b64decode(encoded, validate=True)
    assert raw[4:8] == b"ftyp" and b"moov" in raw and b"mdat" in raw
    assert len(raw) == fixture["byte_length"] > 100_000
    assert hashlib.sha256(raw).hexdigest() == fixture["sha256"] == case["fixtures"][0]["sha256"]
    assert case["expected_text"] == fixture["expected_text"]
    assert "earth" not in prompt.casefold() and "moon" not in prompt.casefold()


def test_minimax_video_detail_enum_is_encoded_in_the_documented_video_url_carrier():
    cases = {case["id"]: case for case in cases_for("minimax", "MiniMax-M3", "openai_chat_completions")}
    fixture = video_fixture("earth_moon")
    for value in ("low", "default", "high"):
        case = cases["video_detail_" + value]
        content = case["body"]["messages"][0]["content"]
        video = next(part for part in content if part["type"] == "video_url")
        assert video["video_url"]["detail"] == value
        assert video["video_url"]["url"].startswith("data:video/mp4;base64,")
        assert "messages[].content[].video_url.detail" in case["target_fields"]
        assert case["expected_text"] == "earth,moon"
        assert case["fixtures"][0]["sha256"] == fixture["sha256"]
    baseline = next(part for part in cases["real_video_earth_moon"]["body"]["messages"][0]["content"]
                    if part["type"] == "video_url")
    assert "detail" not in baseline["video_url"]


def test_glm_video_uses_only_the_pinned_official_public_url():
    cases = cases_for("zhipu", "glm-5.3-flash", "openai_chat_completions")
    videos = [case for case in cases if case["group"] == "video"]
    assert [case["id"] for case in videos] == ["real_video_official_url"]
    case, public = videos[0], remote_video_fixture("zhipu_elephants")
    content = case["body"]["messages"][0]["content"]
    assert content[0] == {"type": "video_url", "video_url": {"url": public["external_url"]}}
    assert "data:" not in json.dumps(case["body"]) and "elephant" not in content[-1]["text"].casefold()
    assert case["external_media"] == [{key: public[key] for key in ("external_url", "sha256", "mime_type", "byte_length")}]


def test_gemini_chat_video_is_not_inferred_from_native_support():
    native = cases_for("google_ai_studio", "gemini-3.6-flash", "gemini_generate_content")
    chat = cases_for("google_ai_studio", "gemini-3.6-flash", "openai_chat_completions")
    assert any(case["group"] == "video" for case in native)
    assert not any(case["group"] == "video" for case in chat)


def test_real_photos_and_human_audio_keep_payload_answers_out_of_text_prompts():
    cases = {case["id"]: case for case in cases_for("google_ai_studio", "gemini-3.7-flash", "gemini_generate_content")}
    for subject in ("earth", "moon"):
        case = cases["real_photo_" + subject]
        parts = case["body"]["contents"][0]["parts"]
        assert parts[0]["inlineData"]["data"] == photo_fixture(subject)["data_base64"]
        assert "earth" not in parts[-1]["text"].casefold() and "moon" not in parts[-1]["text"].casefold()
    case = cases["real_audio_nasa_eagle"]
    parts = case["body"]["contents"][0]["parts"]
    assert base64.b64decode(parts[0]["inlineData"]["data"], validate=True).startswith(b"RIFF")
    assert parts[0]["inlineData"]["data"] == audio_fixture("nasa_eagle")["data_base64"]
    assert all(word not in parts[-1]["text"].casefold() for word in ("houston", "tranquility", "eagle", "landed"))


def case_for(transport="chat_completions", *, audio=False):
    target = {"chat_completions": ("openai", "gpt-4o", "openai_chat_completions"),
              "openai_responses": ("openai", "gpt-4o", "openai_responses"),
              "claude_messages": ("anthropic", "claude-opus-4-6", "anthropic_messages"),
              "gemini_generate_content": ("google_ai_studio", "gemini-3.7-flash", "gemini_generate_content")}[transport]
    return next(c for c in cases_for(*target) if c["id"] == ("audio_wav" if audio else "image_png")), target[1]


def receipt_for(transport, model, text="red"):
    if transport == "chat_completions":
        payload = {"id": "chatcmpl_fixture", "model": model, "object": "chat.completion", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}
    elif transport == "openai_responses":
        payload = {"id": "resp_fixture", "model": model, "status": "completed", "output": [
            {"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}
    elif transport == "claude_messages":
        payload = {"id": "msg_fixture", "model": model, "type": "message", "role": "assistant", "stop_reason": "end_turn",
                   "content": [{"type": "text", "text": text}], "usage": {"input_tokens": 100, "output_tokens": 10}}
    else:
        payload = {"modelVersion": model, "candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [{"text": text}]}}],
                   "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 10, "totalTokenCount": 110}}
    return {"http_status": 200, "response_complete": True, "response": payload}


@pytest.mark.parametrize("case_id,answer,expected", [
    ("real_photo_earth", "The Earth", True),
    ("real_photo_earth", "planet Earth", True),
    ("real_photo_earth", "Moon", False),
    ("real_photo_moon", "the Moon", True),
    ("real_photo_moon", "Earth", False),
    ("real_photo_earth", "The Earth. I cannot view the image.", False),
    ("real_photo_earth", "The Earth；我无法查看图片", False),
    ("real_photo_earth", "our planet Earth", False),
    ("real_video_earth_moon", "Earth, the Moon", True),
    ("real_video_earth_moon", "Moon, Earth", False),
    ("real_video_moon_earth", "The Moon, the Earth", True),
    ("real_video_moon_earth", "Earth, Moon", False),
    ("real_video_earth_moon", "Earth, Moon. I guessed.", False),
    ("real_audio_nasa_eagle", "Houston, Tranquility Base here. The Eagle has landed.", True),
    ("real_audio_nasa_eagle", "Houston, uh, Tranquility Base here. The Eagle has landed.", True),
    ("real_audio_nasa_eagle", "Houston, um, Tranquility Base here. The Eagle has landed.", False),
    ("real_audio_nasa_eagle", "The lantern is green. The number is seven.", False),
    ("real_audio_nasa_eagle", "Houston, Tranquility Base here. The Eagle has landed. I could not hear it.", False),
])
def test_real_media_semantics_allow_only_frozen_variants_and_reject_wrong_content(case_id, answer, expected):
    model = "gemini-3.7-flash"
    case = next(case for case in cases_for("google_ai_studio", model, "gemini_generate_content") if case["id"] == case_id)
    verdict = validate_response(case, receipt_for("gemini_generate_content", model, answer), model=model, transport="gemini_generate_content")
    assert verdict["semantic_pass"] is expected and verdict["compatibility_pass"] is expected
    assert verdict["semantic_assertion"]["mode"] == "frozen_exact_text_variants"
    if expected:
        assert verdict["semantic_assertion"]["matched_expected_text"] in [case["expected_text"], *case.get("expected_text_variants", [])]
    else:
        assert verdict["semantic_assertion"]["matched_expected_text"] is None


@pytest.mark.parametrize("transport", ["chat_completions", "openai_responses", "claude_messages", "gemini_generate_content"])
def test_media_success_requires_semantics_identity_usage_and_complete_response(transport):
    case, model = case_for(transport)
    original = receipt_for(transport, model)
    good = validate_response(case, original, model=model, transport=transport)
    assert good["compatibility_pass"] and good["semantic_pass"] and good["usage_pass"]
    assert good["exact_media_token_count_verified"] is False
    wrong = receipt_for(transport, model, "blue")
    assert not validate_response(case, wrong, model=model, transport=transport)["semantic_pass"]
    for mutation in ("identity", "usage", "complete"):
        changed = copy.deepcopy(original)
        if mutation == "identity":
            changed["response"]["modelVersion" if transport == "gemini_generate_content" else "model"] = "unregistered-model"
        elif mutation == "usage":
            changed["response"].pop("usageMetadata" if transport == "gemini_generate_content" else "usage")
        else:
            changed["response_complete"] = False
        assert not validate_response(case, changed, model=model, transport=transport)["compatibility_pass"]


def test_usage_arithmetic_and_boolean_counters_are_not_accepted():
    case, model = case_for()
    for usage in ({"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 999},
                  {"prompt_tokens": True, "completion_tokens": 10, "total_tokens": 11}):
        receipt = receipt_for("chat_completions", model)
        receipt["response"]["usage"] = usage
        assert not validate_response(case, receipt, model=model, transport="chat_completions")["usage_pass"]


def test_audio_semantics_ignore_punctuation_but_not_wrong_utterance():
    case, model = case_for("gemini_generate_content", audio=True)
    good = receipt_for("gemini_generate_content", model, "The lantern is green, the number is 7.")
    assert validate_response(case, good, model=model, transport="gemini_generate_content")["semantic_pass"]
    wrong = receipt_for("gemini_generate_content", model, "The lantern is blue. The number is nine.")
    assert not validate_response(case, wrong, model=model, transport="gemini_generate_content")["semantic_pass"]


def test_thought_only_answer_does_not_count_as_media_understanding():
    case, model = case_for("gemini_generate_content")
    receipt = receipt_for("gemini_generate_content", model)
    receipt["response"]["candidates"][0]["content"]["parts"][0]["thought"] = True
    assert not validate_response(case, receipt, model=model, transport="gemini_generate_content")["semantic_pass"]


def test_negative_requires_media_attribution_and_does_not_accept_access_failures():
    case = next(c for c in cases_for("openai", "gpt-4o", "openai_chat_completions") if c["expectation"] == "rejected")
    assert case["depends_on"] == ["image_png"]
    for status, message, expected in [(400, "Invalid base64 image data", True), (422, "image_url is invalid", True),
                                      (400, "max_tokens is invalid", False), (400, "Image quota exceeded", False),
                                      (401, "Invalid image data", False), (200, "Invalid image data", False)]:
        receipt = {"http_status": status, "response_complete": True, "response": {"error": {"message": message}}}
        assert validate_response(case, receipt, model="gpt-4o", transport="chat_completions")["compatibility_pass"] is expected


@pytest.mark.parametrize("feedback", ["blocked", ["blocked"], 1, True, {"blockReason": []}])
def test_malformed_gemini_feedback_fails_without_raising(feedback):
    case, model = case_for("gemini_generate_content")
    receipt = receipt_for("gemini_generate_content", model)
    receipt["response"]["promptFeedback"] = feedback
    verdict = validate_response(case, receipt, model=model, transport="gemini_generate_content")
    assert verdict["compatibility_pass"] is False and verdict["envelope_pass"] is False


@pytest.mark.parametrize("extra", [
    {"inlineData": {"mimeType": "image/png", "data": "AAAA"}},
    {"fileData": {"mimeType": "audio/wav", "fileUri": "gs://example/audio.wav"}},
    {"functionCall": {"name": "unexpected_tool", "args": {}}},
    {"functionResponse": {}}, {"executableCode": {}}, {"codeExecutionResult": {}},
    {"toolCall": {}}, {"toolResponse": {}}, {"thought": "true"}, {"thought": 1},
    {"thoughtSignature": ["invalid"]},
])
def test_gemini_final_part_has_one_text_value_and_valid_thought_metadata(extra):
    case, model = case_for("gemini_generate_content")
    receipt = receipt_for("gemini_generate_content", model)
    receipt["response"]["candidates"][0]["content"]["parts"][0].update(extra)
    verdict = validate_response(case, receipt, model=model, transport="gemini_generate_content")
    assert verdict["compatibility_pass"] is False and verdict["envelope_pass"] is False


@pytest.mark.parametrize("extra", [{"audio": {"data": "AAAA"}}, {"images": ["image"]}, {"video": {}},
                                   {"function_call": {"name": "unexpected"}}, {"tool_calls": {}},
                                   {"refusal": []}, {"reasoning_content": {"text": "red"}}])
def test_chat_rejects_unrequested_media_tools_and_malformed_known_fields(extra):
    case, model = case_for()
    receipt = receipt_for("chat_completions", model)
    receipt["response"]["choices"][0]["message"].update(extra)
    verdict = validate_response(case, receipt, model=model, transport="chat_completions")
    assert verdict["compatibility_pass"] is False and verdict["envelope_pass"] is False


@pytest.mark.parametrize("transport,field,value", [
    ("chat_completions", "completion_tokens_details", ["wrong-container"]),
    ("chat_completions", "completion_tokens_details", {"reasoning_tokens": -1}),
    ("chat_completions", "completion_tokens_details", {"reasoning_tokens": True}),
    ("chat_completions", "prompt_tokens_details", {"audio_tokens": "100"}),
    ("openai_responses", "input_tokens_details", "wrong-container"),
    ("openai_responses", "output_tokens_details", {"reasoning_tokens": 1.5}),
    ("claude_messages", "cache_creation", {"ephemeral_5m_input_tokens": -1}),
    ("claude_messages", "cache_read_input_tokens", True),
    ("gemini_generate_content", "thoughtsTokenCount", -1),
    ("gemini_generate_content", "promptTokensDetails", "wrong-container"),
    ("gemini_generate_content", "candidatesTokensDetails", ["wrong-entry"]),
    ("gemini_generate_content", "cacheTokensDetails", [{"modality": "AUDIO", "tokenCount": True}]),
    ("gemini_generate_content", "toolUsePromptTokensDetails", [{"modality": "AUDIO", "tokenCount": -1}]),
    ("gemini_generate_content", "promptTokensDetails", [{"modality": [], "tokenCount": 100}]),
    ("gemini_generate_content", "promptTokensDetails", [{"modality": "AUDIO"}]),
])
def test_known_usage_details_require_valid_containers_and_nonnegative_integer_counts(transport, field, value):
    case, model = case_for(transport)
    receipt = receipt_for(transport, model)
    receipt["response"]["usageMetadata" if transport == "gemini_generate_content" else "usage"][field] = value
    verdict = validate_response(case, receipt, model=model, transport=transport)
    assert verdict["compatibility_pass"] is False and verdict["usage_pass"] is False
    assert verdict["usage_schema_errors"]


@pytest.mark.parametrize("transport", ["chat_completions", "openai_responses", "claude_messages"])
def test_foreign_usage_diagnostics_cannot_mask_protocol_arithmetic_errors(transport):
    case, model = case_for(transport)
    receipt = receipt_for(transport, model)
    usage = receipt["response"]["usage"]
    usage.update(total_tokens=999, promptTokenCount=100, candidatesTokenCount=10, totalTokenCount=110)
    verdict = validate_response(case, receipt, model=model, transport=transport)
    assert verdict["usage_pass"] is False and verdict["compatibility_pass"] is False


@pytest.mark.parametrize("incomplete", [{}, [], "", False])
def test_responses_cannot_claim_completion_with_malformed_incomplete_details(incomplete):
    case, model = case_for("openai_responses")
    receipt = receipt_for("openai_responses", model)
    receipt["response"]["incomplete_details"] = incomplete
    verdict = validate_response(case, receipt, model=model, transport="openai_responses")
    assert verdict["completion_pass"] is False and verdict["compatibility_pass"] is False


@pytest.mark.parametrize("transport", ["chat_completions", "openai_responses", "claude_messages", "gemini_generate_content"])
def test_documented_text_thinking_and_unknown_metadata_are_not_confused_with_final_media(transport):
    case, model = case_for(transport)
    receipt = receipt_for(transport, model)
    response = receipt["response"]
    response["vendor_metadata"] = {"diagnostic": ["unchanged"]}
    usage = response["usageMetadata" if transport == "gemini_generate_content" else "usage"]
    usage["vendor_metadata"] = {"optional": True, "opaque_value": "preserved"}
    if transport == "chat_completions":
        response["choices"][0]["message"].update(reasoning_content="An internal thought.", audio=None, tool_calls=[])
        usage["prompt_tokens_details"] = {"cached_tokens": 0, "vendor_metadata": {"anything": True}}
    elif transport == "openai_responses":
        response["output"].insert(0, {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Thinking."}]})
        usage["output_tokens_details"] = {"reasoning_tokens": 0, "vendor_metadata": ["anything"]}
    elif transport == "claude_messages":
        response["content"].insert(0, {"type": "thinking", "thinking": "Thinking.", "signature": "opaque"})
        response["content"].insert(1, {"type": "redacted_thinking", "data": "opaque"})
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}
    else:
        response["candidates"][0]["content"]["parts"].insert(0, {"text": "Thinking.", "thought": True, "thoughtSignature": "opaque"})
        usage["promptTokensDetails"] = [{"modality": "IMAGE", "tokenCount": 100, "vendor_metadata": True}]
        response["promptFeedback"] = None
    verdict = validate_response(case, receipt, model=model, transport=transport)
    assert verdict["compatibility_pass"] is True and verdict["text"] == "red"


@pytest.mark.parametrize("text", ["red；我无法识别图片", "red 我只是猜测", "red 🚫"])
def test_semantic_check_does_not_discard_non_english_claims_or_symbols(text):
    case, model = case_for()
    verdict = validate_response(case, receipt_for("chat_completions", model, text), model=model, transport="chat_completions")
    assert verdict["semantic_pass"] is False and verdict["compatibility_pass"] is False


@pytest.mark.parametrize("transport,malformed", [
    ("openai_responses", {"type": [], "content": []}),
    ("openai_responses", {"type": "function_call", "name": "unrequested", "arguments": "{}"}),
    ("claude_messages", {"type": [], "text": "red"}),
    ("claude_messages", {"type": "thinking", "thinking": {"text": "red"}}),
    ("claude_messages", {"type": "redacted_thinking", "data": []}),
])
def test_malformed_or_unrequested_output_blocks_fail_without_raising(transport, malformed):
    case, model = case_for(transport)
    receipt = receipt_for(transport, model)
    receipt["response"]["output" if transport == "openai_responses" else "content"].append(malformed)
    verdict = validate_response(case, receipt, model=model, transport=transport)
    assert verdict["compatibility_pass"] is False and verdict["envelope_pass"] is False


@pytest.mark.parametrize("status", [[400], {"status": 400}, 400.0, True])
def test_malformed_rejection_status_does_not_crash_or_pass(status):
    case = next(c for c in cases_for("openai", "gpt-4o", "openai_chat_completions") if c["expectation"] == "rejected")
    receipt = {"http_status": status, "response_complete": True, "response": {"error": {"message": "Invalid image base64"}}}
    verdict = validate_response(case, receipt, model="gpt-4o", transport="chat_completions")
    assert verdict["compatibility_pass"] is False


@pytest.mark.parametrize("error", [{"message": ["Invalid image base64"]}, {"message": "Invalid image base64", "code": []}])
def test_malformed_rejection_error_is_not_field_attribution(error):
    case = next(c for c in cases_for("openai", "gpt-4o", "openai_chat_completions") if c["expectation"] == "rejected")
    receipt = {"http_status": 400, "response_complete": True, "response": {"error": error}}
    assert not validate_response(case, receipt, model="gpt-4o", transport="chat_completions")["compatibility_pass"]
