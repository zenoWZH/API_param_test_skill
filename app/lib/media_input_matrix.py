"""Official, exact-model image/video/audio input cases for text-output APIs.

Capability declarations, executable cases and live observations are separate.
No credentials, configuration overlays, HTTP or model-family guesses are used.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from lib.media_input_fixtures import audio_fixture, image_fixture, photo_fixture, video_fixture, remote_video_fixture

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_FILES = tuple(f"references/media_input/{name}.json" for name in ("google", "western", "china"))
API_FORMS = frozenset({"openai_chat_completions", "openai_responses", "anthropic_messages", "gemini_generate_content"})
OUTPUT_TOKENS = 4096
_IMAGE_PROMPT = "Name the dominant color of the square in the image. Reply with only the English color name."
_MULTI_PROMPT = "Name the dominant color in each image, in input order. Reply only with comma-separated English color names."
_AUDIO_PROMPT = "Transcribe the spoken words in this audio. Reply with only the English transcript."
_PHOTO_PROMPT = "Identify the celestial body shown in this photograph. Reply with only its standard English name."
_VIDEO_PROMPT = "Name the celestial bodies in this video in the order they first appear. Reply only with their English names separated by a comma."
_FORMATS = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif",
            "wav": "audio/wav", "mp3": "audio/mp3", "flac": "audio/flac", "ogg": "audio/ogg",
            "m4a": "audio/m4a", "aac": "audio/aac", "aiff": "audio/aiff"}
_AUDIO_ALIASES = {"audio/mpeg": "mp3", "audio/mpga": "mp3", "audio/mp4": "m4a", "audio/x-aac": "aac",
                  "audio/x-wav": "wav", "audio/x-aiff": "aiff"}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def load_reference() -> dict:
    result = {"sources": [], "models": [], "protocols": []}
    seen = {key: set() for key in result}
    for filename in REFERENCE_FILES:
        document = json.loads((ROOT / filename).read_text(encoding="utf-8"))
        for section in result:
            for row in document[section]:
                key = row["id"] if section == "sources" else (
                    (row["source_id"], row["model"], row["api_form"]) if section == "models"
                    else (row["source_id"], row["api_form"]))
                if key in seen[section]:
                    raise ValueError(f"Duplicate official media {section} identity: {key}")
                seen[section].add(key)
                result[section].append(copy.deepcopy(row))
    for row in result["sources"]:
        if not row.get("url", "").startswith("https://") or not row.get("checked_at") or not row.get("facts"):
            raise ValueError("Media reference requires a retrieved official source and specific facts")
    for row in result["models"]:
        if row["api_form"] not in API_FORMS:
            raise ValueError("Media reference uses an unregistered API form")
        for modality in ("image", "audio", "video"):
            state = row[modality]
            if state["status"] not in {"supported", "unsupported", "unknown"}:
                raise ValueError("Invalid input modality state")
            if not state.get("evidence") or not set(state["evidence"]) <= seen["sources"]:
                raise ValueError("Model input modality lacks closed official evidence")
        if (row["source_id"], row["api_form"]) not in seen["protocols"]:
            raise ValueError("Model input modality lacks exact source/API protocol evidence")
    for protocol in result["protocols"]:
        if not protocol.get("evidence") or not set(protocol["evidence"]) <= seen["sources"]:
            raise ValueError("Protocol evidence is incomplete")
    return result


def find_reference(source_id: str, model: str, api_form: str) -> dict:
    rows = [row for row in load_reference()["models"]
            if (row["source_id"], row["model"], row["api_form"]) == (source_id, model, api_form)]
    if len(rows) != 1:
        raise ValueError(f"No exact documented media input binding: {source_id}/{model}/{api_form}")
    return rows[0]


def source_digest() -> str:
    names = [*REFERENCE_FILES, "lib/media_input_matrix.py", "lib/media_input_fixtures.py"]
    names.extend(str(path.relative_to(ROOT)) for path in sorted((ROOT / "fixtures/media_input").rglob("*"))
                 if path.is_file() and "__pycache__" not in path.parts)
    return hashlib.sha256(_canonical({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names})).hexdigest()


def _format_names(values: list[str]) -> list[str]:
    result = []
    for value in values:
        name = _AUDIO_ALIASES.get(value, value.split("/")[-1]).lower()
        name = "jpeg" if name == "jpg" else name
        if name in _FORMATS and name not in result:
            result.append(name)
    return result


def _part(fixture: dict, api_form: str, *, detail=None, video_sources=()) -> dict:
    mime = fixture["mime_type"]
    if fixture.get("external_url"):
        if not mime.startswith("video/") or api_form != "openai_chat_completions" or "url" not in video_sources:
            raise ValueError("External video URL has no source-documented API carrier")
        return {"type": "video_url", "video_url": {"url": fixture["external_url"]}}
    data = fixture["data_base64"]
    image = mime.startswith("image/")
    if api_form == "gemini_generate_content":
        return {"inlineData": {"mimeType": mime, "data": data}}
    if mime.startswith("video/"):
        if api_form != "openai_chat_completions" or "base64" not in video_sources:
            raise ValueError("No source-documented inline video encoding for this model/API")
        return {"type": "video_url", "video_url": {"url": f"data:{mime};base64,{data}"}}
    if api_form == "anthropic_messages":
        if not image:
            raise ValueError("No documented Anthropic audio content encoding")
        return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
    if api_form == "openai_responses":
        if not image:
            raise ValueError("No documented Responses audio content encoding for these text models")
        return {"type": "input_image", "image_url": f"data:{mime};base64,{data}", **({"detail": detail} if detail else {})}
    if api_form == "openai_chat_completions":
        if image:
            return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}", **({"detail": detail} if detail else {})}}
        return {"type": "input_audio", "input_audio": {"data": data, "format": _format_names([mime])[0]}}
    raise ValueError("Unregistered media input API form")


def _body(row: dict, prompt: str, fixtures: list[dict], *, detail=None, text_first=False) -> dict:
    form, model = row["api_form"], row["model"]
    text = {"text": prompt} if form == "gemini_generate_content" else {
        "type": "input_text" if form == "openai_responses" else "text", "text": prompt}
    video_sources = ((row.get("video") or {}).get("parameters") or {}).get("sources", [])
    media = [_part(fixture, form, detail=detail, video_sources=video_sources) for fixture in fixtures]
    content = [text, *media] if text_first else [*media, text]
    if form == "gemini_generate_content":
        return {"contents": [{"role": "user", "parts": content}], "generationConfig": {"maxOutputTokens": OUTPUT_TOKENS}}
    if form == "openai_responses":
        return {"model": model, "input": [{"role": "user", "content": content}], "max_output_tokens": OUTPUT_TOKENS, "stream": False}
    body = {"model": model, "messages": [{"role": "user", "content": content}], "stream": False}
    limit = "max_completion_tokens" if row["source_id"] == "openai" and not model.startswith("gpt-4o") else "max_tokens"
    body[limit] = OUTPUT_TOKENS
    return body


def _fields(api_form: str, modality: str, detail=None) -> list[str]:
    if api_form == "gemini_generate_content":
        return ["contents[].parts[].inlineData.mimeType", "contents[].parts[].inlineData.data", "contents[].parts[].text"]
    if api_form == "anthropic_messages":
        return ["messages[].content[].source.type", "messages[].content[].source.media_type", "messages[].content[].source.data"]
    if api_form == "openai_responses":
        return ["input[].content[].type", "input[].content[].image_url", *(["input[].content[].detail"] if detail else [])]
    if modality == "audio":
        return ["messages[].content[].input_audio.data", "messages[].content[].input_audio.format"]
    if modality == "video":
        return ["messages[].content[].type", "messages[].content[].video_url.url"]
    return ["messages[].content[].image_url.url", *(["messages[].content[].image_url.detail"] if detail else [])]


def _corrupt_base64(body: dict, form: str) -> None:
    if form == "gemini_generate_content":
        body["contents"][0]["parts"][0]["inlineData"]["data"] = "not-valid-base64!"
    else:
        content = body["input" if form == "openai_responses" else "messages"][0]["content"][0]
        if form == "anthropic_messages":
            content["source"]["data"] = "not-valid-base64!"
        elif content["type"] == "input_audio":
            content["input_audio"]["data"] = "not-valid-base64!"
        elif form == "openai_responses":
            content["image_url"] = content["image_url"].split(",")[0] + ",not-valid-base64!"
        else:
            content["image_url"]["url"] = content["image_url"]["url"].split(",")[0] + ",not-valid-base64!"


def cases_for(source_id: str, model: str, api_form: str) -> list[dict]:
    reference = load_reference()
    rows = [row for row in reference["models"] if (row["source_id"], row["model"], row["api_form"]) == (source_id, model, api_form)]
    if len(rows) != 1:
        raise ValueError(f"No exact documented media input binding: {source_id}/{model}/{api_form}")
    row = rows[0]
    if (row.get("lifecycle") or {}).get("status") in {"retired", "discontinued"}:
        return []
    protocol = next(p for p in reference["protocols"] if (p["source_id"], p["api_form"]) == (source_id, api_form))
    cases = []

    def add(name, group, prompt, fixtures, expected, *, detail=None, text_first=False, negative=False, depends=(), variants=()):
        modalities = ("image", "audio") if group == "mixed" else (group,)
        evidence = list(dict.fromkeys([*protocol["evidence"], *(s for modality in modalities for s in row[modality]["evidence"]),
                                       *(s for fixture in fixtures for s in fixture.get("evidence", []))]))
        fields = list(dict.fromkeys(field for modality in modalities for field in _fields(api_form, modality, detail)))
        body = _body(row, prompt, fixtures, detail=detail, text_first=text_first)
        if negative:
            _corrupt_base64(body, api_form)
        cases.append({"id": name, "name": name, "group": group, "body": body, "source_id": source_id,
                      "expectation": "rejected" if negative else "accepted", "expected_text": expected,
                      "target_fields": fields, "evidence": evidence,
                      "fixtures": [{key: f[key] for key in ("sha256", "mime_type", "byte_length")} for f in fixtures],
                      "depends_on": list(depends), "negative_kind": "invalid_base64" if negative else None,
                      **({"external_media": [{key: f[key] for key in ("external_url", "sha256", "mime_type", "byte_length")}
                                             for f in fixtures if f.get("external_url")]} if any(f.get("external_url") for f in fixtures) else {}),
                      **({"expected_text_variants": list(variants)} if variants else {})})

    if row["image"]["status"] == "supported":
        formats = _format_names(row["image"].get("formats", protocol.get("image_formats", [])))
        if "png" not in formats:
            raise ValueError("Supported image protocol lacks the baseline PNG encoding")
        if "jpeg" not in formats:
            raise ValueError("Real photograph cases require documented JPEG input support")
        red, blue = image_fixture("red", "png"), image_fixture("blue", "png")
        for format_name in formats:
            if format_name not in {"png", "jpeg", "webp", "gif"}:
                continue
            add("image_" + format_name, "image", _IMAGE_PROMPT, [image_fixture("red", format_name)], "red")
        add("image_control_blue", "image", _IMAGE_PROMPT, [blue], "blue")
        add("real_photo_earth", "image", _PHOTO_PROMPT, [photo_fixture("earth")], "earth",
            variants=("the earth", "planet earth", "the planet earth"))
        add("real_photo_moon", "image", _PHOTO_PROMPT, [photo_fixture("moon")], "moon",
            variants=("the moon", "earth's moon", "the earth's moon", "luna"))
        add("image_text_first", "image", _IMAGE_PROMPT, [red], "red", text_first=True)
        add("image_multi", "image", _MULTI_PROMPT, [red, blue], "red,blue")
        add("image_multi_reversed", "image", _MULTI_PROMPT, [blue, red], "blue,red")
        details = (row["image"].get("parameters") or {}).get("detail", [])
        if details and api_form not in {"openai_chat_completions", "openai_responses"}:
            raise ValueError("Detail enum declared without an installed API encoding")
        for detail in details:
            if detail not in {"auto", "low", "default", "high", "original", "xhigh"}:
                raise ValueError("Unreviewed image detail value")
            add("image_detail_" + detail, "image", _IMAGE_PROMPT, [red], "red", detail=detail)
        add("image_reject_invalid_base64", "image", _IMAGE_PROMPT, [red], "", negative=True, depends=("image_png",))
    if row["audio"]["status"] == "supported":
        declared_formats = row["audio"].get("formats", protocol.get("audio_formats", []))
        formats = _format_names(declared_formats)
        if "wav" not in formats:
            raise ValueError("Supported audio protocol lacks the baseline WAV encoding")
        for format_name in formats:
            if format_name not in {"wav", "mp3", "flac", "ogg", "m4a", "aac", "aiff"}:
                continue
            fixture = audio_fixture("green_seven", format_name)
            if api_form == "gemini_generate_content" and fixture["mime_type"] not in declared_formats:
                # File extensions are not API MIME values: Vertex, for example,
                # documents audio/x-aac while AI Studio documents audio/aac.
                matches = [mime for mime in declared_formats if mime.startswith("audio/") and _format_names([mime]) == [format_name]]
                if not matches:
                    raise ValueError("Audio fixture has no source-documented MIME encoding")
                fixture = {**fixture, "mime_type": matches[0]}
            add("audio_" + format_name, "audio", _AUDIO_PROMPT, [fixture], fixture["expected_text"])
        control = audio_fixture("blue_nine", "wav")
        add("audio_control_blue_nine", "audio", _AUDIO_PROMPT, [control], control["expected_text"])
        human = audio_fixture("nasa_eagle", "wav")
        add("real_audio_nasa_eagle", "audio", _AUDIO_PROMPT, [human], human["expected_text"],
            variants=("Houston, uh, Tranquility Base here. The Eagle has landed.",))
        wav = audio_fixture("green_seven", "wav")
        add("audio_text_first", "audio", _AUDIO_PROMPT, [wav], wav["expected_text"], text_first=True)
        add("audio_reject_invalid_base64", "audio", _AUDIO_PROMPT, [wav], "", negative=True, depends=("audio_wav",))
    video = row.get("video") or {}
    video_sources = (video.get("parameters") or {}).get("sources", protocol.get("video_sources", []))
    if video.get("status") == "supported" and "base64" in video_sources:
        row = copy.deepcopy(row)
        row["video"].setdefault("parameters", {})["sources"] = list(video_sources)
        for variant, expected in (("earth_moon", "earth,moon"), ("moon_earth", "moon,earth")):
            first, second = expected.split(",")
            add("real_video_" + variant, "video", _VIDEO_PROMPT, [video_fixture(variant)], expected,
                variants=(f"the {first},the {second}", f"{first},the {second}", f"the {first},{second}"))
    if source_id == "zhipu" and video.get("status") == "supported" and "url" in video_sources:
        row = copy.deepcopy(row)
        row["video"].setdefault("parameters", {})["sources"] = list(video_sources)
        external = remote_video_fixture("zhipu_elephants")
        add("real_video_official_url", "video", "Name the main animal shown in this video. Reply with only the plural English animal name.",
            [external], external["expected_text"], variants=("elephants", "three elephants", "elephant"))
    return cases
