from __future__ import annotations

from pathlib import Path
from typing import Any

from .metrics import load_history, load_records, summarize_records, write_json


def check_smoke(
    profile_results: list[dict[str, Any]],
    config: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Any]:
    thresholds = config.get("thresholds", {}).get("smoke", {})
    minimum = float(thresholds.get("success_rate_min", 1.0))
    total = len(profile_results)
    passed = sum(1 for item in profile_results if item.get("pass"))
    success_rate = passed / total if total else 0.0
    failures = [
        {
            "name": item.get("name"),
            "classification": item.get("failure_classification"),
            "message": item.get("message"),
            "status_code": item.get("status_code"),
        }
        for item in profile_results
        if not item.get("pass")
    ]
    verdict = {
        "pass": success_rate >= minimum,
        "stage": "smoke",
        "success_rate": success_rate,
        "success_rate_min": minimum,
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "failures": failures,
    }
    write_json(Path(output_dir) / "verdict.json", verdict)
    return verdict


def check_cache(
    cache_result: dict[str, Any],
    config: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Any]:
    thresholds = config.get("thresholds", {}).get("cache", {})
    mode = str(thresholds.get("mode", "observe"))
    require_usage_fields = bool(thresholds.get("require_usage_fields", True))

    summary = cache_result.get("summary", {})
    hit_rate = summary.get("cached_input_token_ratio", summary.get("cache_hit_rate"))
    measurement_coverage = summary.get("cache_measurement_coverage")
    usage_fields_seen = int(summary.get("cache_usage_fields_seen") or 0)
    speedup_ratio = cache_result.get("latency_speedup_ratio")
    failures: list[dict[str, Any]] = []
    cache_usage_accuracy_pass = summary.get("cache_usage_accuracy_pass")
    if cache_usage_accuracy_pass is False:
        failures.append(
            {
                "metric": "cache_usage_accuracy",
                "actual": summary.get("cache_usage_accuracy_status"),
                "expected": "no impossible cached-token values and valid positive/negative controls",
                "details": summary.get("cache_usage_accuracy_failures") or [],
            }
        )

    if require_usage_fields and hit_rate is None:
        failures.append(
            {
                "metric": "official_cache_usage",
                "actual": None,
                "expected": "provider cached-token usage fields",
            }
        )

    explicit_checks = {
        "cached_input_token_ratio_min": (hit_rate, "min"),
        "measurement_coverage_min": (measurement_coverage, "min"),
    }
    control_metrics = summary.get("cache_control_metrics") or {}
    explicit_checks["positive_control_cached_ratio_min"] = (
        (control_metrics.get("positive_long_prefix") or {}).get("cached_input_token_ratio"),
        "min",
    )
    explicit_checks["negative_control_cached_ratio_max"] = (
        (control_metrics.get("negative_unique_prefix") or {}).get("cached_input_token_ratio"),
        "max",
    )
    gate_mode = mode in {"gate", "hard_fail"}
    if gate_mode:
        missing = [name for name in explicit_checks if name not in thresholds]
        if missing:
            failures.append(
                {
                    "metric": "cache_gate_thresholds",
                    "actual": missing,
                    "expected": "all customer/control thresholds explicitly configured",
                }
            )
    for name, (actual, direction) in explicit_checks.items():
        if name not in thresholds:
            continue
        expected = float(thresholds[name])
        if actual is None or (direction == "min" and float(actual) < expected) or (
            direction == "max" and float(actual) > expected
        ):
            failures.append(
                {
                    "metric": name.removesuffix("_min").removesuffix("_max"),
                    "actual": actual,
                    "expected": f"{'>=' if direction == 'min' else '<='}{expected}",
                }
            )

    if cache_result.get("aborted_reason"):
        failures.append(
            {
                "metric": "cache_run_completed",
                "actual": cache_result.get("aborted_reason"),
                "expected": "completed without budget/circuit-breaker abort",
            }
        )

    threshold_pass = not failures
    hard_fail = gate_mode
    accuracy_failed = cache_usage_accuracy_pass is False
    verdict = {
        "pass": False if accuracy_failed else threshold_pass if hard_fail else True,
        "threshold_pass": threshold_pass,
        "stage": "cache",
        "mode": mode,
        "summary": summary,
        "latency_speedup_ratio": speedup_ratio,
        "latency_evidence_only": True,
        "official_usage_required_for_hit_rate": True,
        "failures": failures,
    }
    write_json(Path(output_dir) / "verdict.json", verdict)
    return verdict


def check_staircase(
    step_summaries: list[dict[str, Any]],
    config: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Any]:
    thresholds = config.get("thresholds", {}).get("staircase", {})
    target_rpm = float(thresholds.get("target_business_rpm_min", 500))
    target_tpm = float(thresholds.get("target_total_tpm_min", 0))
    success_min = float(thresholds.get("success_rate_min", 0.99))
    p95_max = thresholds.get("p95_latency_max_ms")
    error_429_max = float(thresholds.get("error_429_max_ratio", 0.01))
    error_5xx_max = float(thresholds.get("error_5xx_max_ratio", 0.01))

    annotated: list[dict[str, Any]] = []
    for raw_step in step_summaries:
        step = dict(raw_step)
        quality_failures = staircase_step_quality_failures(step, thresholds)
        step["quality_pass"] = not quality_failures
        step["quality_failures"] = quality_failures
        step["target_rpm_pass"] = float(step.get("business_rpm") or 0) >= target_rpm
        step["target_tpm_pass"] = target_tpm <= 0 or float(step.get("total_tpm") or 0) >= target_tpm
        step["qualified"] = bool(
            step["quality_pass"]
            and step["target_rpm_pass"]
            and step["target_tpm_pass"]
        )
        annotated.append(step)

    peak_rpm = max((float(step.get("business_rpm") or 0.0) for step in annotated), default=0.0)
    peak_tpm = max((float(step.get("total_tpm") or 0.0) for step in annotated), default=0.0)
    qualified = [step for step in annotated if step["qualified"]]
    quality_passing = [step for step in annotated if step["quality_pass"]]
    first_failing = next((step for step in annotated if not step["quality_pass"]), None)
    failures: list[dict[str, Any]] = []
    if not qualified:
        if peak_rpm < target_rpm:
            failures.append(
                {"metric": "peak_business_rpm", "actual": peak_rpm, "expected": f">={target_rpm}"}
            )
        if target_tpm > 0 and peak_tpm < target_tpm:
            failures.append(
                {"metric": "peak_total_tpm", "actual": peak_tpm, "expected": f">={target_tpm}"}
            )
        failures.append(
            {
                "metric": "qualified_staircase_step",
                "actual": 0,
                "expected": "at least one step meeting quality and enabled RPM/TPM targets",
            }
        )
        if first_failing:
            failures.extend(first_failing.get("quality_failures") or [])

    verdict = {
        "pass": bool(qualified),
        "stage": "staircase",
        "peak_business_rpm": peak_rpm,
        "peak_total_tpm": peak_tpm,
        "target_business_rpm_min": target_rpm,
        "target_total_tpm_min": target_tpm,
        "highest_passing_step": max(qualified, key=lambda step: int(step.get("users") or 0), default=None),
        "first_failing_step": first_failing,
        "max_qualified_business_rpm": max((float(step.get("business_rpm") or 0) for step in qualified), default=0.0),
        "max_qualified_total_tpm": max((float(step.get("total_tpm") or 0) for step in qualified), default=0.0),
        "max_quality_passing_business_rpm": max((float(step.get("business_rpm") or 0) for step in quality_passing), default=0.0),
        "steps": annotated,
        "failures": failures,
    }
    write_json(Path(output_dir) / "verdict.json", verdict)
    return verdict


def staircase_step_quality_failures(
    step: dict[str, Any], thresholds: dict[str, Any]
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    step_id = step.get("step")
    if int(step.get("business_record_count") or 0) <= 0:
        return [
            {
                "step": step_id,
                "metric": "business_record_count",
                "actual": 0,
                "expected": ">0",
            }
        ]
    _append_min_failure(
        failures,
        step_id,
        "success_rate",
        step.get("success_rate"),
        float(thresholds.get("success_rate_min", 0.99)),
    )
    if thresholds.get("p95_latency_max_ms") is not None:
        _append_max_failure(
            failures,
            step_id,
            "p95_latency_ms",
            step.get("p95_latency_ms"),
            float(thresholds["p95_latency_max_ms"]),
        )
    _append_max_failure(
        failures,
        step_id,
        "error_429_ratio",
        step.get("error_429_ratio"),
        float(thresholds.get("error_429_max_ratio", 0.01)),
    )
    _append_max_failure(
        failures,
        step_id,
        "error_5xx_ratio",
        step.get("error_5xx_ratio"),
        float(thresholds.get("error_5xx_max_ratio", 0.01)),
    )
    return failures


def check_soak(
    summary: dict[str, Any],
    history_rows: list[dict[str, Any]],
    config: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Any]:
    thresholds = config.get("thresholds", {}).get("soak_1h", {})
    success_min = float(thresholds.get("success_rate_min", 0.98))
    p95_max = thresholds.get("p95_latency_max_ms")
    error_429_max = float(thresholds.get("error_429_max_ratio", 0.02))
    error_5xx_max = float(thresholds.get("error_5xx_max_ratio", 0.01))
    drift_max = float(thresholds.get("rpm_drift_max_ratio", 0.20))

    failures: list[dict[str, Any]] = []
    if int(summary.get("business_record_count") or 0) <= 0:
        failures.append({"metric": "business_record_count", "actual": 0, "expected": ">0"})
    _append_min_failure(failures, "soak_1h", "success_rate", summary.get("success_rate"), success_min)
    if p95_max is not None:
        _append_max_failure(failures, "soak_1h", "p95_latency_ms", summary.get("p95_latency_ms"), float(p95_max))
    _append_max_failure(failures, "soak_1h", "error_429_ratio", summary.get("error_429_ratio"), error_429_max)
    _append_max_failure(failures, "soak_1h", "error_5xx_ratio", summary.get("error_5xx_ratio"), error_5xx_max)

    drift = compute_rpm_drift(history_rows)
    if drift is None:
        failures.append({"metric": "rpm_drift", "actual": None, "expected": "history with non-zero first bucket"})
    elif drift > drift_max:
        failures.append({"metric": "rpm_drift", "actual": drift, "expected": f"<={drift_max}"})

    verdict = {
        "pass": not failures,
        "stage": "soak_1h",
        "summary": summary,
        "rpm_drift": drift,
        "rpm_drift_max_ratio": drift_max,
        "history_buckets": len(history_rows),
        "failures": failures,
    }
    write_json(Path(output_dir) / "verdict.json", verdict)
    return verdict


def summarize_records_file(
    records_path: str | Path,
    config: dict[str, Any],
    duration_sec: float | None = None,
    business_group: str | None = "throughput_profiles",
) -> dict[str, Any]:
    metrics_cfg = config.get("metrics") or {}
    return summarize_records(
        load_records(records_path),
        business_prefix=str(metrics_cfg.get("business_request_prefix", "chat:")),
        business_group=business_group,
        cache_min_prompt_tokens=int(metrics_cfg.get("cache_min_prompt_tokens", 4000)),
        duration_sec=duration_sec,
    )


def load_history_file(history_path: str | Path) -> list[dict[str, Any]]:
    return load_history(history_path)


def compute_rpm_drift(history_rows: list[dict[str, Any]]) -> float | None:
    rows = [row for row in history_rows if not row.get("is_warmup")]
    if not rows:
        return None
    first = rows[:10]
    last = rows[-10:]
    first_avg = _average(row.get("business_rpm") for row in first)
    last_avg = _average(row.get("business_rpm") for row in last)
    if first_avg is None or first_avg <= 0 or last_avg is None:
        return None
    return abs(last_avg - first_avg) / first_avg


def _append_min_failure(
    failures: list[dict[str, Any]],
    step: Any,
    metric: str,
    actual: Any,
    minimum: float,
) -> None:
    if actual is None or float(actual) < minimum:
        failures.append({"step": step, "metric": metric, "actual": actual, "expected": f">={minimum}"})


def _append_max_failure(
    failures: list[dict[str, Any]],
    step: Any,
    metric: str,
    actual: Any,
    maximum: float,
) -> None:
    if actual is None or float(actual) > maximum:
        failures.append({"step": step, "metric": metric, "actual": actual, "expected": f"<={maximum}"})


def _average(values: Any) -> float | None:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)
