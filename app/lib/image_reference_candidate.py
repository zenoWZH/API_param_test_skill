"""Read-only, exact-scope admission for official image reference observations.

Candidates grant access only to the checked-in case factory. They neither change
MPDB execution policy nor supply arbitrary HTTP bodies or endpoints.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .credential_security import redact_secrets


def image_exchange_evidence(
    *, endpoint: str, request_body: dict[str, Any], response_payload: dict[str, Any],
    request_started_at: str | None,
) -> dict[str, Any]:
    """Capture new exchange provenance without retaining embedded image data.

    Digests cover the complete canonical JSON objects before redaction. The
    response excerpt is deliberately limited to identity, usage and completion.
    This helper does not reconstruct evidence for earlier report records.
    """
    def digest(value: Any) -> str | None:
        try:
            raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            return None
        return hashlib.sha256(raw).hexdigest()

    def scalar(value: Any) -> Any:
        if isinstance(value, str):
            # Error strings occasionally echo invalid input. Never persist an
            # embedded image or a long base64 token in the safe excerpt.
            value = re.sub(r"data:image/[^\s]+", "[IMAGE_DATA_OMITTED]", value)
            value = re.sub(r"[A-Za-z0-9+/]{128,}={0,2}", "[BASE64_OMITTED]", value)
            return value[:1000]
        return value if value is None or isinstance(value, (bool, int, float)) else None

    def usage_excerpt(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:64]:
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                result[str(key)] = item
            elif key in {"promptTokensDetails", "candidatesTokensDetails", "cacheTokensDetails", "toolUsePromptTokensDetails"} and isinstance(item, list):
                result[key] = [
                    {field: scalar(entry[field]) for field in ("modality", "tokenCount") if field in entry}
                    for entry in item[:16] if isinstance(entry, dict)
                ]
        return result

    excerpt = {
        key: scalar(response_payload[key])
        for key in ("modelVersion", "model", "responseId", "id", "status")
        if key in response_payload
    }
    for key in ("usageMetadata", "usage"):
        if key in response_payload:
            excerpt[key] = usage_excerpt(response_payload[key])
    candidates = response_payload.get("candidates")
    if isinstance(candidates, list):
        excerpt["finishReasons"] = [
            scalar(item.get("finishReason")) for item in candidates[:16]
            if isinstance(item, dict)
        ]
    error = response_payload.get("error")
    if isinstance(error, dict):
        excerpt["error"] = {
            key: scalar(error[key]) for key in ("code", "status", "type", "param", "message") if key in error
        }
    elif error is not None:
        excerpt["error"] = scalar(error)
    return {
        "evidence_schema_version": 1,
        "request_started_at": request_started_at,
        "actual_request_endpoint": endpoint,
        "request_sha256": digest(request_body),
        "response_sha256": digest(response_payload),
        "response_evidence": redact_secrets(excerpt),
    }



def load_image_reference_candidate(
    path: str,
    *,
    model: str,
    family: str,
    route_profile: str,
    api_form: str,
    api_version: str | None,
    endpoint: str,
    capability: dict[str, Any],
    case_ids: list[str],
    case_definitions: list[dict[str, Any]],
) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    candidate = json.loads(raw)
    expected = {
        "source_id": "google_ai_studio",
        "api_form": "gemini_generate_content",
        "api_version": "v1beta",
        "request_field": "generationConfig.imageConfig",
    }
    if not isinstance(candidate, dict) or any(candidate.get(k) != v for k, v in expected.items()):
        raise ValueError("Image reference candidate has a mismatched source, API, version, or request field.")
    if (family, route_profile, api_form, api_version) != (
        "banana", "google_ai_studio", "gemini_generate_content", "v1beta"
    ):
        raise ValueError("Image reference candidates require Banana AI Studio GenerateContent v1beta.")
    url = urlsplit(endpoint)
    if (url.scheme, url.netloc, url.path, url.query, url.fragment) != (
        "https", "generativelanguage.googleapis.com",
        f"/v1beta/models/{model}:generateContent", "", ""
    ):
        raise ValueError("Image reference candidate is not bound to the exact official model endpoint.")
    models = candidate.get("models")
    entry = models.get(model) if isinstance(models, dict) else None
    if not isinstance(entry, dict):
        raise ValueError("Image reference candidate does not include the selected exact model.")
    for field in ("profile_id", "interface_id"):
        if not entry.get(field) or entry[field] != capability.get(field):
            raise ValueError(f"Image reference candidate {field} does not match the resolved MPDB identity.")
    contract = capability.get("reference_contract_id") or capability.get("default_reference_source")
    if not entry.get("contract_id") or entry["contract_id"] != contract:
        raise ValueError("Image reference candidate contract does not match the resolved MPDB identity.")
    declared = entry.get("case_ids")
    if not isinstance(declared, list) or len(declared) != len(set(declared)) or declared != case_ids:
        raise ValueError("Image reference candidate case allowlist differs from the official case factory.")
    matrix_digest = hashlib.sha256(json.dumps(
        case_definitions, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    if entry.get("case_definitions_sha256") != matrix_digest:
        raise ValueError("Image reference candidate payload or expectations differ from the reviewed matrix digest.")
    return {
        "candidate_id": candidate.get("candidate_id"),
        "sha256": hashlib.sha256(raw).hexdigest(),
        **expected,
        "model": model,
        "profile_id": entry["profile_id"],
        "interface_id": entry["interface_id"],
        "contract_id": entry["contract_id"],
        "case_ids": declared,
        "case_definitions_sha256": matrix_digest,
        "certified": False,
    }


def diagnostic_summary(results: list[dict[str, Any]], planned_count: int) -> dict[str, Any]:
    completed = sum(item.get("diagnostic_pass") is True for item in results)
    return {
        "planned_case_count": planned_count,
        "executed_case_count": len(results),
        "completed_observation_count": completed,
        "accepted_count": sum(item.get("status") == "observed_acceptance" for item in results),
        "parameter_rejected_count": sum(item.get("status") == "observed_parameter_rejection" for item in results),
        "unresolved_count": sum(item.get("diagnostic_pass") is not True for item in results),
        "not_executed_count": planned_count - len(results),
        "dimension_mismatch_count": sum(item.get("dimension_validation_pass") is False for item in results),
        "aspect_ratio_mismatch_count": sum(item.get("aspect_ratio_validation_pass") is False for item in results),
        "complete": len(results) == planned_count and completed == planned_count,
        "certified": False,
    }


def separate_image_observations(summary: dict[str, Any], results: list[dict[str, Any]]) -> None:
    """Keep embedded diagnostic cells outside ordinary compatibility counts."""
    observations = [item for item in results if (item.get("metadata") or {}).get("banana_gc_candidate") is True]
    if not observations:
        return
    contractual = [item for item in results if item not in observations]
    failures = [item for item in contractual if item.get("overall_pass", item.get("pass")) is not True]
    diagnostic = diagnostic_summary(observations, len(observations))
    summary.update({
        "pass": False,
        "certified_route_contract_pass": False,
        "diagnostic_summary": diagnostic,
        "diagnostic_pass": diagnostic["complete"],
        "observed_case_count": len(observations),
        "contractual_case_count": len(contractual),
        "pass_count": len(contractual) - len(failures),
        "compatibility_pass": bool(contractual) and not any(not item.get("pass") for item in contractual),
        "compatibility_failure_count": sum(not item.get("pass") for item in contractual),
        "compatibility_failed_cases": [item.get("case") for item in contractual if not item.get("pass")],
        "failure_count": len(failures) + diagnostic["unresolved_count"],
        "failed_cases": [item.get("case") for item in failures] + [
            item.get("case") for item in observations if item.get("diagnostic_pass") is not True
        ],
    })
