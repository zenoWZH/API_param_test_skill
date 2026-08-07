from __future__ import annotations

from types import SimpleNamespace

from lib.model_identity import (
    audit_model_identity,
    combine_model_identity_audits,
    summarize_model_identity_audits,
)


def _result(response: dict, headers: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(response_json=response, headers=headers or {})


def _audit(
    response: dict,
    *,
    requested: str = "requested-model",
    transport: str = "chat_completions",
    aliases: dict | None = None,
) -> dict:
    return audit_model_identity(
        requested_model=requested,
        result=_result(response),
        transport=transport,
        provider_cfg={
            "backend": "test_backend",
            "models": {"identity_aliases": aliases or {}},
        },
        exchange="initial",
        request_endpoint="/v1/chat/completions",
    )


def test_exact_model_and_explicit_alias_match() -> None:
    exact = _audit({"model": "requested-model", "choices": []})
    alias = _audit(
        {"model": "requested-model-2026-07-01", "choices": []},
        aliases={"requested-model": ["requested-model-2026-07-01"]},
    )

    assert exact["status"] == "match"
    assert alias["status"] == "match"
    assert alias["allowed_identities"] == [
        "requested-model",
        "requested-model-2026-07-01",
    ]


def test_explicit_returned_model_mismatch_is_high_confidence() -> None:
    audit = _audit({"model": "substituted-model", "choices": []})

    assert audit["status"] == "mismatch"
    assert audit["confidence"] == "high"
    assert "not allowed" in audit["conflicts"][0]


def test_gemini_model_version_is_authoritative_identity_signal() -> None:
    audit = _audit(
        {
            "modelVersion": "gemini-2.5-flash-001",
            "candidates": [],
            "usageMetadata": {},
        },
        requested="gemini-2.5-flash",
        transport="gemini_generate_content",
        aliases={"gemini-2.5-flash": "gemini-2.5-flash-001"},
    )

    assert audit["returned_model_source"] == "response.modelVersion"
    assert audit["status"] == "match"


def test_missing_identity_is_unverifiable_and_protocol_conflict_is_suspicious() -> None:
    unverifiable = _audit({"choices": []})
    suspicious = _audit({"unexpected": True})

    assert unverifiable["status"] == "unverifiable"
    assert suspicious["status"] == "suspicious"


def test_conflicting_auxiliary_model_header_is_suspicious_not_confirmed_mismatch() -> None:
    audit = audit_model_identity(
        requested_model="requested-model",
        result=_result(
            {"choices": []},
            {"x-upstream-model": "other-model"},
        ),
        transport="chat_completions",
        provider_cfg={"backend": "test_backend", "models": {}},
        exchange="initial",
        request_endpoint="/v1/chat/completions",
    )

    assert audit["status"] == "suspicious"
    assert any("auxiliary header" in conflict for conflict in audit["conflicts"])


def test_cross_request_model_drift_and_explicit_mismatch_block() -> None:
    aliases = {
        "requested-model": ["requested-model-a", "requested-model-b"]
    }
    first = _audit(
        {"model": "requested-model-a", "choices": []}, aliases=aliases
    )
    second = _audit(
        {"model": "requested-model-b", "choices": []}, aliases=aliases
    )
    drift = summarize_model_identity_audits(
        [
            {"model_identity_audit": combine_model_identity_audits([first])},
            {"model_identity_audit": combine_model_identity_audits([second])},
        ]
    )
    mismatch = summarize_model_identity_audits(
        [
            {
                "model_identity_audit": combine_model_identity_audits(
                    [_audit({"model": "other", "choices": []})]
                )
            }
        ]
    )

    assert drift["status"] == "mismatch"
    assert drift["pass"] is False
    assert mismatch["status"] == "mismatch"
    assert mismatch["pass"] is False


def test_historical_result_without_identity_field_remains_unverifiable() -> None:
    summary = summarize_model_identity_audits([{"status": "pass"}])

    assert summary["status"] == "unverifiable"
    assert summary["pass"] is True


def test_multiple_explicitly_requested_image_models_are_not_cross_request_drift() -> None:
    first = _audit(
        {"model": "image-1k", "choices": []}, requested="image-1k"
    )
    second = _audit(
        {"model": "image-2k", "choices": []}, requested="image-2k"
    )

    summary = summarize_model_identity_audits(
        [
            {"model_identity_audit": combine_model_identity_audits([first])},
            {"model_identity_audit": combine_model_identity_audits([second])},
        ]
    )

    assert summary["status"] == "match"
    assert summary["pass"] is True


def test_image_response_model_is_used_only_when_the_api_returns_it() -> None:
    audit = _audit(
        {"data": [{"model": "image-model", "b64_json": "..."}]},
        requested="image-model",
        transport="images-generations",
    )

    assert audit["returned_model_source"] == "response.data[].model"
    assert audit["status"] == "match"
