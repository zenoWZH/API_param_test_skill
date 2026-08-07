from __future__ import annotations

from lib.metrics import RequestRecord, apply_cache_token_audits, summarize_records


def _record(
    stage: str,
    usage: dict,
    *,
    control: str | None = None,
    role: str | None = None,
    pair: int = 0,
    reusable: int | None = None,
) -> RequestRecord:
    extra = {
        "cache_scenario": "growing_conversation",
        "cache_stage": stage,
        "transport": "chat_completions",
    }
    if control:
        extra.update(
            {
                "cache_control": control,
                "control_role": role,
                "control_pair": pair,
            }
        )
    if reusable is not None:
        extra["reusable_prefix_tokens"] = reusable
    return RequestRecord(
        timestamp=1.0,
        task_name=f"cache:{stage}",
        group="cache_profiles",
        profile=stage,
        method="POST",
        path="/v1/chat/completions",
        success=True,
        status_code=200,
        usage=usage,
        extra=extra,
    )


def _usage(prompt: int, hit: int, miss: int) -> dict:
    return {
        "prompt_tokens": prompt,
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
    }


def _valid_controls() -> list[RequestRecord]:
    return [
        _record(
            "positive_cold",
            _usage(100, 0, 100),
            control="positive_long_prefix",
            role="cold",
        ),
        _record(
            "positive_warm",
            _usage(100, 80, 20),
            control="positive_long_prefix",
            role="warm",
        ),
        _record(
            "negative",
            _usage(100, 0, 100),
            control="negative_unique_prefix",
            role="unique",
        ),
    ]


def _summary(records: list[RequestRecord]) -> dict:
    apply_cache_token_audits(
        records,
        {
            "positive_control_cached_ratio_min": 0.5,
            "negative_control_cached_ratio_max": 0.05,
        },
    )
    return summarize_records(
        records,
        business_prefix="cache:",
        business_group="cache_profiles",
        cache_min_prompt_tokens=0,
        duration_sec=60,
    )


def test_valid_positive_negative_controls_and_reusable_prefix_pass() -> None:
    records = [
        _record("growth", _usage(150, 90, 60), reusable=100),
        *_valid_controls(),
    ]

    summary = _summary(records)

    assert summary["cache_usage_accuracy_status"] == "pass"
    assert summary["cache_usage_accuracy_pass"] is True
    assert summary["cache_control_group_coverage"] == 1.0
    assert summary["cache_control_usage_coverage"] == 1.0
    assert all(record.cache_token_audit for record in records)


def test_cached_greater_than_input_and_reusable_prefix_are_not_clipped() -> None:
    over_input = _record("growth", _usage(100, 120, -20), reusable=80)
    over_reusable = _record("growth", _usage(150, 101, 49), reusable=100)
    summary = _summary([over_input, over_reusable, *_valid_controls()])

    assert summary["cache_usage_accuracy_status"] == "fail"
    assert summary["cache_usage_accuracy_excess_tokens"] == 41
    assert over_input.cache_token_audit["reported_cached_tokens"] == 120
    assert "cached tokens exceed input tokens" in over_input.cache_token_audit["errors"]
    assert "cached tokens exceed structurally reusable prefix tokens" in over_reusable.cache_token_audit["errors"]


def test_hit_miss_arithmetic_and_control_contradictions_fail() -> None:
    bad_math = _record("growth", _usage(100, 40, 50), reusable=80)
    bad_controls = _valid_controls()
    bad_controls[1].usage = _usage(100, 10, 90)
    bad_controls[2].usage = _usage(100, 20, 80)

    summary = _summary([bad_math, *bad_controls])

    assert summary["cache_usage_accuracy_status"] == "fail"
    reasons = "; ".join(summary["cache_usage_accuracy_failures"])
    assert "do not equal input" in reasons
    assert "positive warm" in reasons
    assert "unique/cold" in reasons


def test_positive_warm_must_increase_over_its_cold_pair() -> None:
    controls = _valid_controls()
    controls[0].usage = _usage(100, 60, 40)
    controls[1].usage = _usage(100, 60, 40)

    summary = _summary(controls)

    assert summary["cache_usage_accuracy_status"] == "fail"
    assert any(
        "did not increase" in reason
        for reason in summary["cache_usage_accuracy_failures"]
    )


def test_missing_official_fields_is_not_available_without_latency_inference() -> None:
    controls = _valid_controls()
    for record in controls:
        record.usage = {"total_tokens": 100}

    summary = _summary(controls)

    assert summary["cache_usage_accuracy_status"] == "not_available"
    assert summary["cache_usage_accuracy_pass"] is True
    assert summary["cache_usage_accuracy_coverage"] == 0.0


def test_positive_pair_without_cold_cached_telemetry_is_partial_not_pass() -> None:
    controls = _valid_controls()
    controls[0].usage = {
        "prompt_tokens": 100,
        "prompt_cache_miss_tokens": 100,
    }

    summary = _summary(controls)

    assert controls[1].cache_token_audit["status"] == "not_available"
    assert "positive cold cached token telemetry is missing" in controls[1].cache_token_audit[
        "unavailable_reasons"
    ]
    assert summary["cache_usage_accuracy_status"] == "partial"


def test_missing_control_group_is_confirmed_failure() -> None:
    summary = _summary([_record("growth", _usage(100, 50, 50), reusable=80)])

    assert summary["cache_usage_accuracy_status"] == "fail"
    assert summary["cache_controls_present"] is False
    assert summary["cache_control_group_coverage"] == 0.0
