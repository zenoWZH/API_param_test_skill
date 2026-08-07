from __future__ import annotations

from typing import Any, Iterable


IDENTITY_STATUSES = ("match", "mismatch", "suspicious", "unverifiable")


def audit_model_identity(
    *,
    requested_model: str,
    result: Any,
    transport: str,
    provider_cfg: dict[str, Any],
    exchange: str,
    request_endpoint: str | None = None,
) -> dict[str, Any]:
    response = getattr(result, "response_json", None)
    response = response if isinstance(response, dict) else {}
    headers = getattr(result, "headers", None)
    headers = headers if isinstance(headers, dict) else {}
    returned_model, returned_source = _returned_model(response, transport)
    allowed = allowed_model_identities(provider_cfg, requested_model)
    protocol = _protocol_fingerprint(response, transport)
    interfaces = provider_cfg.get("api_interfaces") or {}
    interface = interfaces.get(transport) if isinstance(interfaces, dict) else {}
    interface = interface if isinstance(interface, dict) else {}
    evidence: list[dict[str, Any]] = [
        {
            "kind": "request",
            "requested_model": requested_model,
            "transport": transport,
            "endpoint": request_endpoint,
            "backend": provider_cfg.get("backend"),
            "auth": interface.get("auth"),
            "resource_path": interface.get("path"),
        }
    ]
    conflicts: list[str] = []

    if returned_model is not None:
        evidence.append(
            {
                "kind": "response_model",
                "source": returned_source,
                "value": returned_model,
            }
        )
        if returned_model not in allowed:
            conflicts.append(
                f"returned model {returned_model!r} is not allowed for requested model {requested_model!r}"
            )

    system_fingerprint = response.get("system_fingerprint")
    if system_fingerprint not in (None, ""):
        evidence.append(
            {
                "kind": "system_fingerprint",
                "value": str(system_fingerprint),
            }
        )
    auxiliary_model_conflict = False
    for name in ("x-model", "x-upstream-model", "x-request-id", "request-id"):
        value = headers.get(name)
        if value not in (None, ""):
            evidence.append({"kind": "response_header", "name": name, "value": str(value)})
            if name in {"x-model", "x-upstream-model"} and str(value) not in allowed:
                auxiliary_model_conflict = True
                conflicts.append(
                    f"auxiliary header {name} reports unallowed model {str(value)!r}"
                )

    evidence.append({"kind": "protocol_fingerprint", **protocol})
    if protocol["status"] == "conflict":
        conflicts.append(str(protocol["reason"]))

    if returned_model is not None and returned_model not in allowed:
        status = "mismatch"
        confidence = "high"
    elif protocol["status"] == "conflict" or auxiliary_model_conflict:
        status = "suspicious"
        confidence = "medium"
    elif returned_model is not None:
        status = "match"
        confidence = "high"
    else:
        status = "unverifiable"
        confidence = "low"

    return {
        "schema_version": 1,
        "exchange": exchange,
        "requested_model": requested_model,
        "returned_model": returned_model,
        "returned_model_source": returned_source,
        "allowed_identities": allowed,
        "transport": transport,
        "backend": provider_cfg.get("backend"),
        "request_endpoint": request_endpoint,
        "auth_mode": interface.get("auth"),
        "resource_path": interface.get("path"),
        "fingerprint": {
            "system_fingerprint": (
                str(system_fingerprint)
                if system_fingerprint not in (None, "")
                else None
            ),
            "protocol": protocol,
            "response_headers": {
                name: str(headers[name])
                for name in ("x-model", "x-upstream-model", "x-request-id", "request-id")
                if headers.get(name) not in (None, "")
            },
        },
        "status": status,
        "confidence": confidence,
        "evidence": evidence,
        "conflicts": conflicts,
    }


def combine_model_identity_audits(exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    returned_models = sorted(
        {
            str(item["returned_model"])
            for item in exchanges
            if item.get("returned_model") not in (None, "")
        }
    )
    status = _aggregate_identity_status(exchanges)
    conflicts = [
        str(conflict)
        for item in exchanges
        for conflict in item.get("conflicts") or []
        if conflict
    ]
    if len(returned_models) > 1:
        status = "mismatch"
        conflicts.append("multiple conflicting response model identities were observed")
    return {
        "schema_version": 1,
        "status": status,
        "requested_model": next(
            (item.get("requested_model") for item in exchanges if item.get("requested_model")),
            None,
        ),
        "returned_model": returned_models[0] if len(returned_models) == 1 else None,
        "returned_models": returned_models,
        "allowed_identities": sorted(
            {
                str(identity)
                for item in exchanges
                for identity in item.get("allowed_identities") or []
            }
        ),
        "transport": next(
            (item.get("transport") for item in exchanges if item.get("transport")), None
        ),
        "backend": next(
            (item.get("backend") for item in exchanges if item.get("backend")), None
        ),
        "confidence": (
            "high" if status in {"match", "mismatch"} else "medium" if status == "suspicious" else "low"
        ),
        "evidence": [
            evidence
            for item in exchanges
            for evidence in item.get("evidence") or []
        ],
        "conflicts": conflicts,
        "exchanges": exchanges,
    }


def summarize_model_identity_audits(
    results: Iterable[dict[str, Any]],
    identity_probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    exchanges: list[dict[str, Any]] = []
    if isinstance(identity_probe, dict):
        exchanges.extend(_identity_exchanges(identity_probe))
    for result in results:
        exchanges.extend(_identity_exchanges(result))

    counts = {
        status: sum(1 for item in exchanges if item.get("status") == status)
        for status in IDENTITY_STATUSES
    }
    returned_models = sorted(
        {
            str(item["returned_model"])
            for item in exchanges
            if item.get("returned_model") not in (None, "")
        }
    )
    requested_models = sorted(
        {
            str(item["requested_model"])
            for item in exchanges
            if item.get("requested_model") not in (None, "")
        }
    )
    returned_by_requested: dict[str, set[str]] = {}
    for item in exchanges:
        requested = item.get("requested_model")
        returned = item.get("returned_model")
        if requested not in (None, "") and returned not in (None, ""):
            returned_by_requested.setdefault(str(requested), set()).add(str(returned))
    drift = {
        requested: sorted(models)
        for requested, models in returned_by_requested.items()
        if len(models) > 1
    }
    conflicts = [
        str(conflict)
        for item in exchanges
        for conflict in item.get("conflicts") or []
        if conflict
    ]
    status = _aggregate_identity_status(exchanges)
    if drift:
        status = "mismatch"
        conflicts.append(
            f"multiple conflicting response model identities were observed: {drift}"
        )
    return {
        "schema_version": 1,
        "status": status,
        "pass": status != "mismatch",
        "exchange_count": len(exchanges),
        "status_counts": counts,
        "requested_models": requested_models,
        "returned_models": returned_models,
        "conflicts": conflicts,
        "limitations": (
            "Observable response metadata and protocol fingerprints cannot prove the physical "
            "upstream model when a gateway forges all identity signals."
        ),
    }


def allowed_model_identities(
    provider_cfg: dict[str, Any], requested_model: str
) -> list[str]:
    allowed = [str(requested_model)]
    models = provider_cfg.get("models") if isinstance(provider_cfg, dict) else None
    aliases = models.get("identity_aliases") if isinstance(models, dict) else None
    configured = aliases.get(requested_model) if isinstance(aliases, dict) else None
    image = provider_cfg.get("image") if isinstance(provider_cfg, dict) else None
    image_aliases = image.get("identity_aliases") if isinstance(image, dict) else None
    image_configured = (
        image_aliases.get(requested_model) if isinstance(image_aliases, dict) else None
    )
    if isinstance(configured, str):
        allowed.append(configured)
    elif isinstance(configured, list):
        allowed.extend(str(item) for item in configured if str(item))
    if isinstance(image_configured, str):
        allowed.append(image_configured)
    elif isinstance(image_configured, list):
        allowed.extend(str(item) for item in image_configured if str(item))
    return list(dict.fromkeys(allowed))


def _identity_exchanges(result: dict[str, Any]) -> list[dict[str, Any]]:
    audit = result.get("model_identity_audit") or {}
    return [
        item
        for item in audit.get("exchanges") or []
        if isinstance(item, dict)
    ]


def _aggregate_identity_status(exchanges: list[dict[str, Any]]) -> str:
    statuses = [str(item.get("status") or "unverifiable") for item in exchanges]
    if not statuses or all(status == "unverifiable" for status in statuses):
        return "unverifiable"
    if "mismatch" in statuses:
        return "mismatch"
    if "suspicious" in statuses:
        return "suspicious"
    if "unverifiable" in statuses:
        return "suspicious"
    return "match"


def _returned_model(
    response: dict[str, Any], transport: str
) -> tuple[str | None, str | None]:
    if transport == "gemini_generate_content":
        value = response.get("modelVersion")
        if value not in (None, ""):
            return str(value), "response.modelVersion"
    if transport == "gemini_interactions":
        value = response.get("model")
        if value not in (None, ""):
            return str(value).removeprefix("models/"), "response.model"
    for key in ("model", "model_name", "modelName"):
        value = response.get(key)
        if value not in (None, ""):
            return str(value), f"response.{key}"
    if transport in {"images-generations", "image_generation"}:
        data = response.get("data")
        if isinstance(data, list):
            models = {
                str(item.get("model"))
                for item in data
                if isinstance(item, dict) and item.get("model") not in (None, "")
            }
            if len(models) == 1:
                return models.pop(), "response.data[].model"
    return None, None


def _protocol_fingerprint(response: dict[str, Any], transport: str) -> dict[str, str]:
    if not response:
        return {"status": "missing", "reason": "response body has no protocol fingerprint"}
    if transport == "gemini_generate_content":
        matched = "candidates" in response or "usageMetadata" in response
        reason = "Gemini candidates/usageMetadata shape"
    elif transport == "gemini_interactions":
        matched = isinstance(response.get("steps"), list) and (
            "usage" in response or "status" in response or "id" in response
        )
        reason = "Gemini Interactions steps/usage shape"
    elif transport == "claude_messages":
        matched = isinstance(response.get("content"), list) or "stop_reason" in response
        reason = "Claude content-block/stop_reason shape"
    elif transport == "openai_responses":
        matched = "output" in response or str(response.get("object") or "").startswith("response")
        reason = "OpenAI Responses output/object shape"
    elif transport in {"images-generations", "image_generation"}:
        matched = isinstance(response.get("data"), list)
        reason = "image generation data-list shape"
    else:
        matched = isinstance(response.get("choices"), list)
        reason = "OpenAI-compatible choices shape"
    return {
        "status": "match" if matched else "conflict",
        "reason": reason if matched else f"response does not match expected {transport} protocol shape",
    }
