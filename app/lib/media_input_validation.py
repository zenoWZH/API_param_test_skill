"""Protocol, semantic and reported-usage checks for media-to-text probes.

This validator does not certify exact media token counts. The workflow retains
the shared token audit independently and cannot promote missing count evidence.
"""
from __future__ import annotations

import re
import unicodedata

from lib.token_audit import normalize_usage


def _words(text: str) -> list[str]:
    # Only punctuation/spacing may be discarded. Dropping arbitrary non-Latin
    # words would accept e.g. "red; 我无法识别图片" as a successful color answer.
    if any(not (char.isascii() and char.isalnum()) and not char.isspace()
           and not unicodedata.category(char).startswith("P") for char in text):
        return []
    aliases = {"7": "seven", "9": "nine"}
    return [aliases.get(word, word) for word in re.findall(r"[a-z0-9]+", text.casefold())]


def _usage_shape_errors(usage: object, mode: str) -> list[str]:
    """Validate known usage containers and counters, leaving metadata alone."""
    if not isinstance(usage, dict):
        return ["usage_not_object"]
    counters = {
        "prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
        "thinking_tokens", "reasoning_tokens", "cached_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens", "image_tokens", "audio_tokens", "text_tokens", "video_tokens",
        "promptTokenCount", "candidatesTokenCount", "totalTokenCount", "thoughtsTokenCount",
        "cachedContentTokenCount", "toolUsePromptTokenCount", "prompt_token_count",
        "candidates_token_count", "total_token_count", "thoughts_token_count",
        "cached_content_token_count", "tool_use_prompt_token_count",
        "accepted_prediction_tokens", "rejected_prediction_tokens",
        "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens",
    }
    errors = []

    def check_counters(value, path):
        for name, counter in value.items():
            if name in counters and counter is not None and (type(counter) is not int or counter < 0):
                errors.append(path + "." + name)

    check_counters(usage, "usage")
    details = ["input_tokens_details", "output_tokens_details", "completion_tokens_details", "cache_creation"]
    if mode != "gemini_generate_content":
        details.append("prompt_tokens_details")
    for name in details:
        value = usage.get(name)
        if value is None:
            continue
        if not isinstance(value, dict):
            errors.append("usage." + name)
        else:
            check_counters(value, "usage." + name)
    if mode == "gemini_generate_content":
        for name in ("promptTokensDetails", "candidatesTokensDetails", "cacheTokensDetails", "toolUsePromptTokensDetails",
                     "prompt_tokens_details", "candidates_tokens_details", "cache_tokens_details", "tool_use_prompt_tokens_details"):
            value = usage.get(name)
            if value is None:
                continue
            if not isinstance(value, list):
                errors.append("usage." + name)
                continue
            for index, row in enumerate(value):
                path = f"usage.{name}[{index}]"
                if not isinstance(row, dict):
                    errors.append(path)
                    continue
                if not isinstance(row.get("modality"), str) or not row["modality"]:
                    errors.append(path + ".modality")
                count_fields = [key for key in ("tokenCount", "token_count") if key in row]
                if not count_fields or any(type(row[key]) is not int or row[key] < 0 for key in count_fields):
                    errors.append(path + ".tokenCount")
    return errors


def _envelope(payload: dict, transport: str) -> tuple[bool, bool, str]:
    """Extract final text only: thought blocks never satisfy a media assertion."""
    if transport in {"chat_completions", "openai_chat_completions"}:
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            return False, False, ""
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            return False, False, ""
        text = message.get("content")
        valid = bool(isinstance(payload.get("id"), str) and payload["id"] and
                     message.get("role") == "assistant" and isinstance(text, str) and
                     not message.get("tool_calls") and not message.get("refusal"))
        valid &= payload.get("object", "chat.completion") == "chat.completion"
        valid &= all(message.get(key) is None for key in ("audio", "images", "image", "video", "function_call"))
        valid &= message.get("tool_calls") is None or message.get("tool_calls") == []
        valid &= message.get("refusal") is None or message.get("refusal") == ""
        valid &= message.get("reasoning_content") is None or isinstance(message.get("reasoning_content"), str)
        valid &= "index" not in choice or (type(choice["index"]) is int and choice["index"] == 0)
        return valid, choice.get("finish_reason") == "stop", text if isinstance(text, str) else ""
    if transport == "openai_responses":
        output = payload.get("output")
        if not isinstance(output, list) or not output or not all(isinstance(row, dict) for row in output):
            return False, False, ""
        messages = [row for row in output if row.get("type") == "message"]
        valid = bool(isinstance(payload.get("id"), str) and payload["id"] and messages and
                     all(isinstance(row.get("type"), str) and row["type"] in {"message", "reasoning"} for row in output))
        valid &= payload.get("object", "response") == "response"
        text = []
        for message in messages:
            blocks = message.get("content")
            valid &= message.get("role") == "assistant" and message.get("status", "completed") == "completed"
            if not isinstance(blocks, list) or not blocks:
                valid = False
                continue
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "output_text" or not isinstance(block.get("text"), str):
                    valid = False
                else:
                    text.append(block["text"])
        return bool(valid), payload.get("status") == "completed" and payload.get("incomplete_details") is None, "".join(text)
    if transport in {"claude_messages", "anthropic_messages"}:
        blocks = payload.get("content")
        if not isinstance(blocks, list) or not blocks:
            return False, False, ""
        valid = bool(isinstance(payload.get("id"), str) and payload["id"] and payload.get("type") == "message"
                     and payload.get("role") == "assistant")
        text = []
        for block in blocks:
            if not isinstance(block, dict):
                valid = False
            elif block.get("type") == "text" and isinstance(block.get("text"), str):
                text.append(block["text"])
            elif block.get("type") == "thinking":
                valid &= isinstance(block.get("thinking"), str)
                valid &= "signature" not in block or isinstance(block["signature"], str)
            elif block.get("type") == "redacted_thinking":
                valid &= isinstance(block.get("data"), str)
            else:
                valid = False
        return bool(valid), payload.get("stop_reason") == "end_turn", "".join(text)
    if transport == "gemini_generate_content":
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], dict):
            return False, False, ""
        candidate = candidates[0]
        content = candidate.get("content")
        if not isinstance(content, dict) or not isinstance(content.get("parts"), list) or not content["parts"]:
            return False, False, ""
        feedback = payload.get("promptFeedback")
        if feedback is not None and not isinstance(feedback, dict):
            return False, False, ""
        valid = content.get("role") == "model" and not (feedback or {}).get("blockReason")
        if feedback is not None and "blockReason" in feedback:
            valid &= feedback["blockReason"] is None or isinstance(feedback["blockReason"], str)
        data_fields = {"text", "inlineData", "fileData", "functionCall", "functionResponse", "executableCode",
                       "codeExecutionResult", "toolCall", "toolResponse"}
        text = []
        for part in content["parts"]:
            if not isinstance(part, dict):
                valid = False
            elif set(part).intersection(data_fields) != {"text"} or not isinstance(part.get("text"), str):
                valid = False
            elif "thought" in part and type(part["thought"]) is not bool:
                valid = False
            elif "thoughtSignature" in part and not isinstance(part["thoughtSignature"], str):
                valid = False
            elif part.get("thought") is True:
                continue
            else:
                text.append(part["text"])
        return bool(valid), candidate.get("finishReason") == "STOP", "".join(text)
    raise ValueError("Unregistered media response transport")


def _attributed_rejection(case: dict, status, payload: dict) -> bool:
    if type(status) is not int or status not in {400, 422} or not isinstance(payload.get("error"), dict):
        return False
    error = payload["error"]
    if not isinstance(error.get("message"), str):
        return False
    if any(error.get(key) is not None and not isinstance(error[key], str) for key in ("type", "param", "status")):
        return False
    if error.get("code") is not None and type(error["code"]) not in (str, int):
        return False
    description = " ".join(str(error.get(key) or "") for key in ("message", "type", "code", "param", "status"))
    if re.search(r"authenticat|permission|quota|billing|overload|rate.?limit|model.?not.?found", description, re.I):
        return False
    media = "audio" if case["group"] == "audio" else "image"
    # A rejection needs actual media/data attribution, not any arbitrary 4xx.
    return bool(re.search(r"base.?64|inline.?data|input_audio|image_url|media.?type|mime|" + media, description, re.I))


def validate_response(case: dict, receipt: dict, *, model: str, transport: str,
                      allowed_response_models=()) -> dict:
    result = {"compatibility_pass": False, "semantic_pass": False, "usage_pass": False,
              "identity_pass": False, "envelope_pass": False, "completion_pass": False,
              "failures": [], "text": "", "proof_scope": "media_input_protocol_semantics_and_reported_usage",
              "exact_media_token_count_verified": False}
    if not isinstance(receipt, dict):
        result["failures"].append("receipt_not_object")
        return result
    status = receipt.get("http_status", receipt.get("status_code"))
    payload = receipt.get("response")
    if not isinstance(payload, dict):
        result["failures"].append("response_not_object")
        return result
    if receipt.get("failure") or receipt.get("response_complete") is not True:
        result["failures"].append("transport_incomplete")
        return result
    if case["expectation"] == "rejected":
        accepted = _attributed_rejection(case, status, payload)
        result.update(compatibility_pass=accepted, rejection_attributed=accepted,
                      proof_scope="attributed_invalid_media_rejection", semantic_status="not_applicable",
                      usage_status="not_applicable", identity_status="not_applicable")
        if not accepted:
            result["failures"].append("media_rejection_not_attributed")
        return result
    if type(status) is not int or status != 200 or payload.get("error") is not None:
        result["failures"].append("http_or_api_error")
        return result
    valid, complete, text = _envelope(payload, transport)
    result.update(envelope_pass=valid, completion_pass=complete, text=text)
    expected_texts = [case["expected_text"], *case.get("expected_text_variants", [])]
    if any(not isinstance(value, str) or not value.strip() for value in expected_texts):
        raise ValueError("Media semantic expectations must be nonempty frozen strings")
    observed_words = _words(text)
    matches = [value for value in expected_texts if observed_words and observed_words == _words(value)]
    result["semantic_pass"] = bool(matches)
    result["semantic_assertion"] = {"mode": "frozen_exact_text_variants", "matched_expected_text": matches[0] if matches else None,
                                    "primary_text_match": bool(observed_words) and observed_words == _words(case["expected_text"])}
    returned = payload.get("modelVersion") if transport == "gemini_generate_content" else payload.get("model")
    result["returned_model"] = returned
    result["identity_pass"] = isinstance(returned, str) and returned in {model, *allowed_response_models}
    usage = payload.get("usageMetadata") if transport == "gemini_generate_content" else payload.get("usage")
    mode = {"openai_chat_completions": "chat_completions", "anthropic_messages": "claude_messages"}.get(transport, transport)
    usage_errors = _usage_shape_errors(usage, mode)
    result["usage_schema_errors"] = usage_errors
    normalization_input = dict(usage) if not usage_errors else {}
    if mode != "gemini_generate_content":
        # The shared normalizer recognizes legacy Gemini-shaped usage even for
        # other modes. Extra diagnostics must not override this exact protocol's
        # required counters and hide contradictions in their arithmetic.
        for field in ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount", "toolUsePromptTokenCount", "totalTokenCount"):
            normalization_input.pop(field, None)
    normalized = normalize_usage(normalization_input, mode,
                                 additive_chat_reasoning=(case.get("source_id") == "xai" and mode == "chat_completions"))
    if isinstance(usage, dict):
        normalized["raw_usage"] = usage
    counts = [normalized.get("input_tokens"), normalized.get("output_tokens")]
    result["usage_pass"] = bool(isinstance(usage, dict) and all(type(n) is int and n > 0 for n in counts)
                                and not normalized.get("errors") and not usage_errors)
    # Booleans and numeric strings are invalid counters, even if coercible.
    if isinstance(usage, dict):
        expected_fields = {"chat_completions": ("prompt_tokens", "completion_tokens", "total_tokens"),
                           "openai_responses": ("input_tokens", "output_tokens", "total_tokens"),
                           "claude_messages": ("input_tokens", "output_tokens"),
                           "gemini_generate_content": ("promptTokenCount", "candidatesTokenCount", "totalTokenCount")}[mode]
        result["usage_pass"] &= all(type(usage.get(field)) is int and usage[field] >= 0 for field in expected_fields)
    body = case["body"]
    cap = (body.get("generationConfig") or {}).get("maxOutputTokens", body.get("max_output_tokens", body.get("max_completion_tokens", body.get("max_tokens"))))
    result["usage_pass"] &= type(cap) is int and cap >= 256 and type(counts[1]) is int and counts[1] <= cap
    result["usage"] = normalized
    for field in ("envelope", "completion", "identity", "usage", "semantic"):
        if not result[field + "_pass"]:
            result["failures"].append(field + "_mismatch")
    result["compatibility_pass"] = not result["failures"]
    return result
