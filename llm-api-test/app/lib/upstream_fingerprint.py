from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import requests

from lib.config import (
    get_api_key,
    get_provider_config,
    get_provider_interface,
)

ID_PREFIX_RE = re.compile(r"^([A-Za-z]{2,}(?:[-_][A-Za-z0-9]+)?[-_])")
NODE_HASH_RE = re.compile(r"([0-9a-f]{7})[A-Za-z0-9]{8}$")
GENERIC_HEX7_RE = re.compile(r"(?<![0-9a-f])([0-9a-f]{7})(?![0-9a-f])")
REQUEST_ID_TEXT_RE = re.compile(r"request id: ([A-Za-z0-9]+)")
HEADER_KEYS = (
    "server",
    "via",
    "cf-ray",
    "x-request-id",
    "x-oneapi-request-id",
    "x-new-api-version",
    "x-ratelimit-limit-requests",
    "openai-organization",
    "openai-version",
)
CORPUS_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "upstream_fingerprints.json"

DIMENSION_WEIGHTS = {
    "node_hash": 3.0,
    "id_prefix": 1.0,
    "usage_keys": 1.0,
    "stream_structure": 1.5,
    "error_structure": 1.0,
    "error_message": 1.5,
    "header_signature": 1.0,
}


def _chat_endpoint(config: dict[str, Any], provider: str) -> tuple[str, str]:
    provider_cfg = get_provider_config(config, provider)
    base_url = str(provider_cfg.get("base_url") or "").rstrip("/")
    try:
        interface = get_provider_interface(config, "chat_completions", provider)
        path = str(interface.get("path") or "/chat/completions")
    except Exception:
        path = "/chat/completions"
    return base_url, path


def _id_prefix(value: str | None) -> str | None:
    if not value:
        return None
    match = ID_PREFIX_RE.match(str(value))
    return match.group(1) if match else None


def _node_hashes(*values: Any) -> list[str]:
    found: list[str] = []
    for value in values:
        if not value:
            continue
        text = str(value)
        match = NODE_HASH_RE.search(text)
        if match:
            found.append(match.group(1))
            continue
        tail = text.split(":")[-1] if ":" in text else text
        match = NODE_HASH_RE.search(tail)
        if match:
            found.append(match.group(1))
    return sorted(set(found))


def _ordered_keys(value: Any) -> list[str]:
    return list(value.keys()) if isinstance(value, dict) else []


def _header_signature(headers: requests.structures.CaseInsensitiveDict) -> dict[str, str]:
    return {key: str(headers.get(key)) for key in HEADER_KEYS if headers.get(key) is not None}


def _summarize_body(body_text: str) -> dict[str, Any]:
    summary: dict[str, Any] = {"raw_head": body_text[:300]}
    try:
        data = json.loads(body_text)
    except json.JSONDecodeError:
        summary["parseable"] = False
        return summary
    summary["parseable"] = True
    summary["top_keys"] = _ordered_keys(data)
    usage = data.get("usage")
    if isinstance(usage, dict):
        summary["usage_keys"] = _ordered_keys(usage)
        for nested in ("prompt_tokens_details", "completion_tokens_details"):
            if isinstance(usage.get(nested), dict):
                summary[f"{nested}_keys"] = _ordered_keys(usage[nested])
    message = ((data.get("choices") or [{}])[0] or {}).get("message")
    if isinstance(message, dict):
        summary["message_keys"] = _ordered_keys(message)
    error = data.get("error")
    if isinstance(error, dict):
        summary["error_keys"] = _ordered_keys(error)
        summary["error_code"] = error.get("code")
        summary["error_type"] = error.get("type")
        message_text = str(error.get("message") or "")
        summary["error_message"] = message_text[:300]
        nested = None
        try:
            nested = json.loads(message_text)
        except (json.JSONDecodeError, TypeError):
            pass
        summary["error_message_nested_json"] = isinstance(nested, dict)
        request_id = REQUEST_ID_TEXT_RE.search(message_text)
        if request_id:
            summary["error_request_id"] = request_id.group(1)
    return summary


def _probe_nonstream(url: str, headers: dict[str, str], body: dict[str, Any], timeout: int) -> dict[str, Any]:
    response = requests.post(url, headers=headers, json=body, timeout=timeout)
    summary = _summarize_body(response.text)
    summary["status"] = response.status_code
    summary["headers"] = _header_signature(response.headers)
    if summary.get("parseable"):
        data = json.loads(response.text)
        summary["id"] = data.get("id")
        summary["id_prefix"] = _id_prefix(data.get("id"))
        summary["model_field"] = data.get("model")
    return summary


def _probe_stream(url: str, headers: dict[str, str], body: dict[str, Any], timeout: int) -> dict[str, Any]:
    stream_body = dict(body)
    stream_body["stream"] = True
    result: dict[str, Any] = {
        "chunk_count": 0,
        "done_marker": False,
        "usage_in_final_chunk": False,
        "usage_chunk_keys": None,
        "first_chunk_keys": None,
        "delta_keys": None,
        "reasoning_field": None,
        "chunk_id_prefix": None,
    }
    with requests.post(url, headers=headers, json=stream_body, timeout=timeout, stream=True) as response:
        result["status"] = response.status_code
        result["headers"] = _header_signature(response.headers)
        if response.status_code >= 400:
            result.update(_summarize_body(response.text))
            return result
        buffer = ""
        for raw in response.iter_content(chunk_size=4096, decode_unicode=True):
            buffer += raw or ""
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    result["done_marker"] = True
                    continue
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                result["chunk_count"] += 1
                if result["first_chunk_keys"] is None:
                    result["first_chunk_keys"] = _ordered_keys(chunk)
                    result["chunk_id_prefix"] = _id_prefix(chunk.get("id"))
                delta = ((chunk.get("choices") or [{}])[0] or {}).get("delta")
                if isinstance(delta, dict) and result["delta_keys"] is None and delta:
                    result["delta_keys"] = _ordered_keys(delta)
                if isinstance(delta, dict):
                    for field in ("reasoning_content", "reasoning", "thinking"):
                        if delta.get(field) and not result["reasoning_field"]:
                            result["reasoning_field"] = field
                if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
                    result["usage_in_final_chunk"] = True
                    result["usage_chunk_keys"] = _ordered_keys(chunk["usage"])
                if result["chunk_count"] >= 400:
                    break
            if result["chunk_count"] >= 400:
                break
    return result


def collect_fingerprint(
    config: dict[str, Any],
    provider: str,
    model: str,
    *,
    timeout: int = 120,
) -> dict[str, Any]:
    base_url, path = _chat_endpoint(config, provider)
    url = f"{base_url}{path}"
    api_key = get_api_key(config, provider)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    basic_body = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 8,
    }
    started = time.time()
    nonstream = _probe_nonstream(url, headers, dict(basic_body), timeout)
    stream = _probe_stream(url, headers, dict(basic_body), timeout)
    invalid_temperature = _probe_nonstream(
        url, headers, {**basic_body, "temperature": -1}, timeout
    )
    invalid_model = _probe_nonstream(
        url, headers, {**basic_body, "model": "__fingerprint_probe_nonexistent__"}, timeout
    )
    node_hashes = _node_hashes(
        nonstream.get("id"),
        stream.get("id") if isinstance(stream.get("id"), str) else None,
        (nonstream.get("headers") or {}).get("x-oneapi-request-id"),
        (nonstream.get("headers") or {}).get("x-request-id"),
        nonstream.get("error_request_id"),
        invalid_temperature.get("error_request_id"),
        invalid_model.get("error_request_id"),
    )
    header_keys_seen = sorted(
        set(nonstream.get("headers") or {}) | set(stream.get("headers") or {})
    )
    return {
        "schema_version": 1,
        "provider": provider,
        "model": model,
        "endpoint": url,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_sec": round(time.time() - started, 2),
        "node_hashes": node_hashes,
        "id_prefix": nonstream.get("id_prefix") or stream.get("chunk_id_prefix"),
        "model_field": nonstream.get("model_field"),
        "header_keys_seen": header_keys_seen,
        "nonstream": nonstream,
        "stream": stream,
        "error_invalid_temperature": invalid_temperature,
        "error_invalid_model": invalid_model,
    }


def _message_tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return set(re.split(r"[^a-z0-9]+", text.lower())) - {""}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _stream_signature(fp_side: dict[str, Any]) -> tuple:
    return (
        tuple(fp_side.get("first_chunk_keys") or ()),
        bool(fp_side.get("done_marker")),
        bool(fp_side.get("usage_in_final_chunk")),
        fp_side.get("reasoning_field"),
    )


def _error_signature(fp_side: dict[str, Any]) -> tuple:
    return (
        tuple(fp_side.get("top_keys") or ()),
        tuple(fp_side.get("error_keys") or ()),
        fp_side.get("error_type"),
        bool(fp_side.get("error_message_nested_json")),
    )


def compare_fingerprints(
    fingerprint: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    details: dict[str, Any] = {}
    score = 0.0
    max_score = 0.0

    weight = DIMENSION_WEIGHTS["node_hash"]
    max_score += weight
    mine = set(fingerprint.get("node_hashes") or [])
    theirs = set(candidate.get("node_hashes") or [])
    matched = sorted(mine & theirs)
    if mine and theirs:
        ratio = len(matched) / max(len(mine), len(theirs))
        score += weight * ratio
        details["node_hash"] = {"mine": sorted(mine), "theirs": sorted(theirs), "matched": matched, "score": ratio}
    else:
        details["node_hash"] = {"mine": sorted(mine), "theirs": sorted(theirs), "matched": matched, "score": None}

    weight = DIMENSION_WEIGHTS["id_prefix"]
    max_score += weight
    match = fingerprint.get("id_prefix") and fingerprint.get("id_prefix") == candidate.get("id_prefix")
    if match:
        score += weight
    details["id_prefix"] = {"mine": fingerprint.get("id_prefix"), "theirs": candidate.get("id_prefix"), "match": bool(match)}

    weight = DIMENSION_WEIGHTS["usage_keys"]
    max_score += weight
    my_usage = fingerprint.get("nonstream", {}).get("usage_keys") or fingerprint.get("stream", {}).get("usage_chunk_keys")
    their_usage = candidate.get("nonstream", {}).get("usage_keys") or candidate.get("stream", {}).get("usage_chunk_keys")
    match = bool(my_usage) and my_usage == their_usage
    if match:
        score += weight
    details["usage_keys"] = {"mine": my_usage, "theirs": their_usage, "match": match}

    weight = DIMENSION_WEIGHTS["stream_structure"]
    max_score += weight
    my_stream = _stream_signature(fingerprint.get("stream") or {})
    their_stream = _stream_signature(candidate.get("stream") or {})
    if my_stream[0] and their_stream[0]:
        dims = sum(1 for a, b in zip(my_stream, their_stream) if a == b)
        ratio = dims / len(my_stream)
        score += weight * ratio
        details["stream_structure"] = {"mine": my_stream, "theirs": their_stream, "score": ratio}
    else:
        details["stream_structure"] = {"mine": my_stream, "theirs": their_stream, "score": None}

    weight = DIMENSION_WEIGHTS["error_structure"]
    max_score += weight
    my_error = _error_signature(fingerprint.get("error_invalid_model") or {})
    their_error = _error_signature(candidate.get("error_invalid_model") or {})
    if my_error[0] and their_error[0]:
        dims = sum(1 for a, b in zip(my_error, their_error) if a == b)
        ratio = dims / len(my_error)
        score += weight * ratio
        details["error_structure"] = {"mine": my_error, "theirs": their_error, "score": ratio}
    else:
        details["error_structure"] = {"mine": my_error, "theirs": their_error, "score": None}

    weight = DIMENSION_WEIGHTS["error_message"]
    max_score += weight
    for probe in ("error_invalid_temperature", "error_invalid_model"):
        my_text = (fingerprint.get(probe) or {}).get("error_message")
        their_text = (candidate.get(probe) or {}).get("error_message")
        similarity = _jaccard(_message_tokens(my_text), _message_tokens(their_text))
        score += weight / 2 * similarity
        details.setdefault("error_message", {})[probe] = {
            "mine": (my_text or "")[:120],
            "theirs": (their_text or "")[:120],
            "similarity": round(similarity, 3),
        }

    weight = DIMENSION_WEIGHTS["header_signature"]
    max_score += weight
    my_headers = set(fingerprint.get("header_keys_seen") or [])
    their_headers = set(candidate.get("header_keys_seen") or [])
    similarity = _jaccard(my_headers, their_headers)
    score += weight * similarity
    details["header_signature"] = {
        "mine": sorted(my_headers),
        "theirs": sorted(their_headers),
        "similarity": round(similarity, 3),
    }

    return {
        "candidate_id": candidate.get("entry_id") or candidate.get("provider"),
        "candidate_label": candidate.get("label"),
        "score": round(score / max_score, 4) if max_score else 0.0,
        "details": details,
    }


def load_corpus(path: str | Path | None = None) -> list[dict[str, Any]]:
    corpus_path = Path(path) if path else CORPUS_PATH
    if not corpus_path.exists():
        return []
    data = json.loads(corpus_path.read_text(encoding="utf-8"))
    entries = data.get("entries") if isinstance(data, dict) else data
    return [entry for entry in entries if isinstance(entry, dict)]


def append_to_corpus(
    entry_id: str,
    label: str,
    fingerprint: dict[str, Any],
    *,
    notes: str = "",
    path: str | Path | None = None,
) -> Path:
    corpus_path = Path(path) if path else CORPUS_PATH
    corpus_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"schema_version": 1, "entries": []}
    if corpus_path.exists():
        data = json.loads(corpus_path.read_text(encoding="utf-8"))
    entries = [e for e in data.get("entries", []) if e.get("entry_id") != entry_id]
    entries.append(
        {
            "entry_id": entry_id,
            "label": label,
            "notes": notes,
            "fingerprint": fingerprint,
        }
    )
    data["entries"] = entries
    corpus_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return corpus_path


def compare_against_corpus(
    fingerprint: dict[str, Any], path: str | Path | None = None
) -> list[dict[str, Any]]:
    results = []
    for entry in load_corpus(path):
        candidate = dict(entry.get("fingerprint") or {})
        candidate["entry_id"] = entry.get("entry_id")
        candidate["label"] = entry.get("label")
        results.append(compare_fingerprints(fingerprint, candidate))
    results.sort(key=lambda item: item["score"], reverse=True)
    return results
