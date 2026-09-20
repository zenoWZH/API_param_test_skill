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


VALID_RUN_SUCCESS_MODES = frozenset({"all", "any"})
_RUN_CONTEXT_FIELDS = (
    "provider", "model", "reference_source", "api_form", "route_profile",
    "transport", "request_endpoint", "expectation",
)
_AGGREGATED_OUTCOME_FIELDS = (
    "status", "compatibility_status", "compatibility_pass", "pass",
    "overall_pass", "overall_status",
)


def normalize_run_success_mode(value: Any, *, default: str = "all") -> str:
    raw = str(value if value not in (None, "") else default).strip().casefold()
    if raw not in VALID_RUN_SUCCESS_MODES:
        raise ValueError(f"run_success_mode must be all or any, got {value!r}")
    return raw


def apply_any_run_success(
    results: list[dict[str, Any]],
    *,
    profile: str,
    mode: Any = "all",
) -> list[dict[str, Any]]:
    """Satisfy stochastic preserved-thinking mismatches with a passing repeat.

    Official K3 preserved-thinking is stochastic: the model sometimes refuses
    to reuse planted ``reasoning_content``. One successful run is enough to
    prove the history field was accepted and used.
    """
    if normalize_run_success_mode(mode) != "any":
        return results
    items = [item for item in results if item.get("profile") == profile]
    passing_contexts = {
        tuple(item.get(field) for field in _RUN_CONTEXT_FIELDS)
        for item in items
        if item.get("status") == "pass"
        and is_http_success(item.get("status_code"))
        and not item.get("satisfied_by_sibling_run")
        and item.get("token_validation_pass") is True
        and (item.get("model_identity_audit") or {}).get("status") != "mismatch"
    }
    for item in items:
        if (
            item.get("status") != "incompatible"
            or profile != "kimi_k3_preserved_thinking"
            or item.get("failure_classification") != "preserved_thinking_mismatch"
            or not is_http_success(item.get("status_code"))
            or tuple(item.get(field) for field in _RUN_CONTEXT_FIELDS) not in passing_contexts
        ):
            continue
        item["pre_aggregation_status"] = "incompatible"
        item["pre_aggregation_outcome"] = {
            field: item[field] for field in _AGGREGATED_OUTCOME_FIELDS if field in item
        }
        item["satisfied_by_sibling_run"] = True
        item["run_success_mode"] = "any"
        item["status"] = "pass"
        item["compatibility_status"] = "pass"
        item["compatibility_pass"] = True
        item["pass"] = True
        token_ok = item.get("token_validation_pass") is True
        item["overall_pass"] = token_ok
        item["overall_status"] = "pass" if token_ok else "token_validation_failed"
    return results
