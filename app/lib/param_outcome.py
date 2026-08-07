"""Shared parameter-probe outcome status mapping for text and image suites."""

from __future__ import annotations

from typing import Any

EXPECTED_REJECTION_STATUS_CODES = {400, 422}
HARD_FAIL_STATUS_CODES = {401, 403, 404, 429}

COMPATIBILITY_OK_STATUSES = frozenset({"pass", "expected_rejection"})
COMPATIBILITY_BAD_STATUSES = frozenset({"incompatible", "unexpected_acceptance", "fail"})

VALID_EXPECTATIONS = frozenset({"supported", "unsupported"})


def normalize_expectation(value: Any, *, default: str = "supported") -> str:
    raw = str(value or default).strip().casefold()
    if raw not in VALID_EXPECTATIONS:
        raise ValueError(f"expectation must be supported or unsupported, got {value!r}")
    return raw


def is_http_success(status_code: int | None) -> bool:
    return status_code is not None and 200 <= status_code <= 299


def is_param_rejection(status_code: int | None) -> bool:
    return status_code in EXPECTED_REJECTION_STATUS_CODES


def is_hard_fail(status_code: int | None) -> bool:
    if status_code is None:
        return False
    if status_code in HARD_FAIL_STATUS_CODES:
        return True
    if status_code >= 500:
        return True
    return False


def map_probe_outcome(
    expectation: str,
    *,
    status_code: int | None,
    validation_ok: bool = True,
) -> dict[str, Any]:
    """Map expectation + HTTP/validation result to a probe status.

    Judgment rules (fixed):
    - supported + 2xx + validation → pass
    - supported + 400/422 or validation fail → incompatible
    - unsupported + 400/422 → expected_rejection
    - unsupported + 2xx → unexpected_acceptance
    - any + 401/403/404/429/5xx → fail
    """
    expected = normalize_expectation(expectation)

    if is_hard_fail(status_code):
        return {
            "expectation": expected,
            "status": "fail",
            "pass": False,
            "compatibility_ok": False,
        }

    if expected == "unsupported":
        if is_param_rejection(status_code):
            return {
                "expectation": expected,
                "status": "expected_rejection",
                "pass": True,
                "compatibility_ok": True,
            }
        if is_http_success(status_code):
            return {
                "expectation": expected,
                "status": "unexpected_acceptance",
                "pass": False,
                "compatibility_ok": False,
            }
        # Other 4xx outside the rejection set (and missing status) are hard fails.
        return {
            "expectation": expected,
            "status": "fail",
            "pass": False,
            "compatibility_ok": False,
        }

    # supported
    if is_http_success(status_code) and validation_ok:
        return {
            "expectation": expected,
            "status": "pass",
            "pass": True,
            "compatibility_ok": True,
        }
    if is_param_rejection(status_code) or (is_http_success(status_code) and not validation_ok):
        return {
            "expectation": expected,
            "status": "incompatible",
            "pass": False,
            "compatibility_ok": False,
        }
    if status_code is not None and 400 <= status_code <= 499:
        return {
            "expectation": expected,
            "status": "incompatible",
            "pass": False,
            "compatibility_ok": False,
        }
    return {
        "expectation": expected,
        "status": "fail",
        "pass": False,
        "compatibility_ok": False,
    }


def compatibility_pass_from_statuses(statuses: list[str]) -> bool:
    return not any(status in COMPATIBILITY_BAD_STATUSES for status in statuses)
