from __future__ import annotations

import json
import statistics
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .credential_security import redact_secrets
from .deepseek_params import (
    cache_tokens_from_usage,
    prompt_tokens_from_usage,
)
from .token_audit import normalize_usage


CACHE_CONTROL_POSITIVE = "positive_long_prefix"
CACHE_CONTROL_NEGATIVE = "negative_unique_prefix"
CACHE_CONTROL_ROLE_COLD = "cold"
CACHE_CONTROL_ROLE_WARM = "warm"
CACHE_CONTROL_ROLE_UNIQUE = "unique"


@dataclass
class RequestRecord:
    timestamp: float
    task_name: str
    group: str
    profile: str
    method: str
    path: str
    success: bool
    status_code: int | None = None
    latency_ms: float | None = None
    ttft_ms: float | None = None
    response_length: int | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    error_type: str | None = None
    failure_classification: str | None = None
    cache_headers: dict[str, str] = field(default_factory=dict)
    cache_token_audit: dict[str, Any] = field(default_factory=dict)
    is_warmup: bool = False
    is_retry: bool = False
    phase: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(redact_secrets(asdict(self)), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RequestRecord":
        known = {field.name for field in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        payload = {key: value.get(key) for key in known if key in value}
        return cls(**payload)


class RunRecorder:
    def __init__(
        self,
        report_dir: str | Path,
        history_interval_sec: int = 60,
        records_file: str = "request_records.jsonl",
        history_file: str = "history.jsonl",
        business_request_prefix: str = "chat:",
        business_group: str | None = "throughput_profiles",
        cache_min_prompt_tokens: int = 4000,
    ) -> None:
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.records_path = self.report_dir / records_file
        self.history_path = self.report_dir / history_file
        self.history_interval_sec = int(history_interval_sec)
        self.business_request_prefix = business_request_prefix
        self.business_group = business_group
        self.cache_min_prompt_tokens = int(cache_min_prompt_tokens)
        self._lock = threading.Lock()
        self._window_start = time.time()
        self._window_records: list[RequestRecord] = []

    def record(self, record: RequestRecord) -> None:
        with self._lock:
            with self.records_path.open("a", encoding="utf-8") as fh:
                fh.write(record.to_json() + "\n")
            self._window_records.append(record)
            now = time.time()
            if now - self._window_start >= self.history_interval_sec:
                self._flush_history_locked(now)

    def flush(self) -> None:
        with self._lock:
            self._flush_history_locked(time.time(), force=True)

    def _flush_history_locked(self, now: float, force: bool = False) -> None:
        if not self._window_records:
            return
        window_sec = max(now - self._window_start, 1.0)
        summary = summarize_records(
            self._window_records,
            business_prefix=self.business_request_prefix,
            business_group=self.business_group,
            cache_min_prompt_tokens=self.cache_min_prompt_tokens,
            duration_sec=window_sec,
        )
        row = {
            "timestamp": now,
            "window_sec": window_sec,
            **summary,
        }
        with self.history_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(redact_secrets(row), ensure_ascii=False, sort_keys=True) + "\n")
        self._window_records = []
        self._window_start = now


def classify_failure(
    status_code: int | None = None,
    finish_reason: str | None = None,
    error_type: str | None = None,
) -> str | None:
    if status_code == 429:
        return "http_429"
    if status_code is not None and 500 <= status_code <= 599:
        return "http_5xx"
    if status_code is not None and 400 <= status_code <= 499:
        return "http_4xx"
    if error_type:
        return error_type
    if finish_reason == "insufficient_system_resource":
        return "finish_reason:insufficient_system_resource"
    if finish_reason == "content_filter":
        return "finish_reason:content_filter"
    return None


def summarize_records(
    records: Iterable[RequestRecord],
    business_prefix: str = "chat:",
    business_group: str | None = "throughput_profiles",
    cache_min_prompt_tokens: int = 4000,
    duration_sec: float | None = None,
) -> dict[str, Any]:
    record_list = list(records)
    measured = [
        item
        for item in record_list
        if item.task_name.startswith(business_prefix)
        and not item.is_warmup
        and not item.is_retry
        and (business_group is None or item.group == business_group)
    ]
    cache_audit_records = measured
    if business_group == "cache_profiles":
        measured = [
            item
            for item in measured
            if not (
                isinstance(item.extra, dict)
                and (
                    item.extra.get("cache_control")
                    or item.extra.get("cache_structure_probe")
                )
            )
        ]
    successful_business = [item for item in measured if item.success]

    if duration_sec is None:
        duration_sec = _duration_from_records(record_list)
    minutes = max(duration_sec / 60.0, 1 / 60.0)

    latencies = [item.latency_ms for item in measured if item.latency_ms is not None]
    successful_latencies = [
        item.latency_ms
        for item in successful_business
        if item.latency_ms is not None
    ]
    successful_ttfts = [
        item.ttft_ms
        for item in successful_business
        if item.ttft_ms is not None
    ]
    total_count = len(measured)
    success_count = len(successful_business)
    status_codes = [item.status_code for item in measured if item.status_code is not None]
    input_token_count = 0
    output_token_count = 0
    total_token_count = 0
    token_usage_records = 0
    total_tokens_per_request: list[int] = []
    for item in measured:
        usage = item.usage or {}
        accounting = normalize_usage(usage, _record_transport(item))
        item_prompt_tokens = accounting.get("input_tokens")
        item_completion_tokens = accounting.get("output_tokens")
        item_total_tokens = accounting.get("total_tokens")
        if item_prompt_tokens is not None or item_completion_tokens is not None or item_total_tokens is not None:
            token_usage_records += 1
        if item_total_tokens is not None:
            total_tokens_per_request.append(item_total_tokens)
        input_token_count += item_prompt_tokens or 0
        output_token_count += item_completion_tokens or 0
        total_token_count += item_total_tokens or 0

    finish_counter = Counter(item.finish_reason for item in measured if item.finish_reason)
    failure_counter = Counter(
        item.failure_classification
        or classify_failure(item.status_code, item.finish_reason, item.error_type)
        for item in measured
        if not item.success
        or item.failure_classification
        or classify_failure(item.status_code, item.finish_reason, item.error_type)
    )
    failure_counter.pop(None, None)

    cache_hit_tokens = 0
    cache_miss_tokens = 0
    cache_fields_seen = 0
    cache_eligible_records = 0
    cache_excluded_below_min_records = 0
    cache_eligible_prompt_tokens = 0
    cache_excluded_below_min_prompt_tokens = 0
    cache_shared_prefix_record_count = 0
    cache_shared_prefix_tokens = 0
    for item in measured:
        prompt_tokens = prompt_tokens_from_usage(item.usage or {})
        if prompt_tokens is None or prompt_tokens < cache_min_prompt_tokens:
            cache_excluded_below_min_records += 1
            cache_excluded_below_min_prompt_tokens += prompt_tokens or 0
            continue
        cache_eligible_records += 1
        cache_eligible_prompt_tokens += prompt_tokens
        hit, miss = cache_tokens_from_usage(item.usage or {})
        if (
            isinstance(item.extra, dict)
            and item.extra.get("cache_scope") == "shared_prefix"
        ):
            try:
                prefix_tokens = int(item.extra.get("cacheable_prefix_tokens"))
            except (TypeError, ValueError):
                continue
            if prefix_tokens <= 0:
                continue
            prefix_tokens = min(prefix_tokens, prompt_tokens)
            if hit is not None or miss is not None:
                prefix_hit_tokens = min(hit or 0, prefix_tokens)
                cache_fields_seen += 1
                cache_shared_prefix_record_count += 1
                cache_shared_prefix_tokens += prefix_tokens
                cache_hit_tokens += prefix_hit_tokens
                cache_miss_tokens += prefix_tokens - prefix_hit_tokens
            continue
        if hit is not None or miss is not None:
            cache_fields_seen += 1
            cache_hit_tokens += hit or 0
            cache_miss_tokens += miss or 0

    cache_total = cache_hit_tokens + cache_miss_tokens
    target_tokens_per_request = _dominant_record_extra(measured, "target_tokens_per_request")
    try:
        target_tokens_per_request = (
            float(target_tokens_per_request) if target_tokens_per_request is not None else None
        )
    except (TypeError, ValueError):
        target_tokens_per_request = None
    avg_tokens_per_request = (
        sum(total_tokens_per_request) / len(total_tokens_per_request)
        if total_tokens_per_request
        else None
    )
    token_deviation_ratio = (
        (avg_tokens_per_request - target_tokens_per_request) / target_tokens_per_request
        if avg_tokens_per_request is not None
        and target_tokens_per_request is not None
        and target_tokens_per_request > 0
        else None
    )
    if target_tokens_per_request is None:
        adaptive_controller_status = "disabled"
    elif token_usage_records < 20:
        adaptive_controller_status = "learning"
    elif token_usage_records / total_count < 0.90:
        adaptive_controller_status = "usage_degraded"
    elif token_deviation_ratio is not None and abs(token_deviation_ratio) <= 0.10:
        adaptive_controller_status = "on_target"
    else:
        adaptive_controller_status = "off_target"
    adaptive_warnings = sorted(
        {
            str(warning)
            for item in measured
            for warning in (
                item.extra.get("adaptive_warnings", [])
                if isinstance(item.extra, dict)
                and isinstance(item.extra.get("adaptive_warnings"), list)
                else []
            )
        }
    )
    summary = {
        "record_count": len(record_list),
        "business_record_count": total_count,
        "business_success_count": success_count,
        "business_failure_count": max(total_count - success_count, 0),
        "business_rpm": success_count / minutes,
        "attempted_business_rpm": total_count / minutes,
        "total_rpm": len(record_list) / minutes,
        "input_tpm": input_token_count / minutes,
        "output_tpm": output_token_count / minutes,
        "total_tpm": total_token_count / minutes,
        "token_usage_record_count": token_usage_records,
        "token_usage_coverage": token_usage_records / total_count if total_count else 0.0,
        "target_tokens_per_request": target_tokens_per_request,
        "avg_tokens_per_request": avg_tokens_per_request,
        "p50_tokens_per_request": percentile(total_tokens_per_request, 50),
        "p95_tokens_per_request": percentile(total_tokens_per_request, 95),
        "tokens_per_request_deviation_ratio": token_deviation_ratio,
        "adaptive_controller_status": adaptive_controller_status,
        "adaptive_band_counts": dict(
            Counter(
                item.extra.get("adaptive_band")
                for item in measured
                if isinstance(item.extra, dict) and item.extra.get("adaptive_band")
            )
        ),
        "adaptive_context_clamped_count": sum(
            1
            for item in measured
            if isinstance(item.extra, dict) and item.extra.get("context_clamped")
        ),
        "adaptive_context_window_tokens": _dominant_record_extra(
            measured, "context_window_tokens"
        ),
        "adaptive_context_window_source": _dominant_record_extra(
            measured, "context_window_source"
        ),
        "adaptive_warnings": adaptive_warnings,
        "success_rate": success_count / total_count if total_count else 0.0,
        "p95_latency_ms": percentile(latencies, 95),
        "e2e_latency_sample_count": len(successful_latencies),
        "e2e_latency_p50_ms": percentile(successful_latencies, 50),
        "e2e_latency_p90_ms": percentile(successful_latencies, 90),
        "e2e_latency_p95_ms": percentile(successful_latencies, 95),
        "e2e_latency_p99_ms": percentile(successful_latencies, 99),
        "ttft_sample_count": len(successful_ttfts),
        "ttft_coverage": (
            len(successful_ttfts) / success_count if success_count else 0.0
        ),
        "ttft_p50_ms": percentile(successful_ttfts, 50),
        "ttft_p90_ms": percentile(successful_ttfts, 90),
        "ttft_p95_ms": percentile(successful_ttfts, 95),
        "ttft_p99_ms": percentile(successful_ttfts, 99),
        "error_429_ratio": _ratio(status_codes.count(429), total_count),
        "error_5xx_ratio": _ratio(
            sum(1 for code in status_codes if code is not None and 500 <= code <= 599),
            total_count,
        ),
        "finish_reason_counts": dict(finish_counter),
        "failure_classification_counts": dict(failure_counter),
        "cache_hit_tokens": cache_hit_tokens,
        "cache_miss_tokens": cache_miss_tokens,
        "cache_hit_rate": cache_hit_tokens / cache_total if cache_total else None,
        "cache_usage_fields_seen": cache_fields_seen,
        "cache_min_prompt_tokens": cache_min_prompt_tokens,
        "cache_eligible_record_count": cache_eligible_records,
        "cache_eligible_prompt_tokens": cache_eligible_prompt_tokens,
        "cache_shared_prefix_record_count": cache_shared_prefix_record_count,
        "cache_shared_prefix_tokens": cache_shared_prefix_tokens,
        "cache_excluded_below_min_record_count": cache_excluded_below_min_records,
        "cache_excluded_below_min_prompt_tokens": cache_excluded_below_min_prompt_tokens,
    }
    customer_cache = _customer_cache_metrics(cache_audit_records)
    if customer_cache:
        summary.update(customer_cache)
    cache_accuracy = _cache_usage_accuracy_summary(cache_audit_records)
    if cache_accuracy:
        summary.update(cache_accuracy)
    return summary


def apply_cache_token_audits(
    records: list[RequestRecord],
    thresholds: dict[str, Any] | None = None,
) -> None:
    thresholds = thresholds or {}
    positive_min = float(thresholds.get("positive_control_cached_ratio_min", 0.50))
    negative_max = float(thresholds.get("negative_control_cached_ratio_max", 0.05))
    positive_cold_prompts: dict[tuple[str, Any], int] = {}
    positive_cold_hits: dict[tuple[str, Any], int] = {}
    structure_probe_tokens: dict[str, int] = {}

    for record in records:
        if not isinstance(record.extra, dict):
            continue
        scenario = str(record.extra.get("cache_scenario") or "")
        prompt = prompt_tokens_from_usage(record.usage or {})
        if record.extra.get("cache_structure_probe") and prompt is not None:
            structure_probe_tokens[scenario] = prompt
        if (
            record.extra.get("cache_control") == CACHE_CONTROL_POSITIVE
            and record.extra.get("control_role") == CACHE_CONTROL_ROLE_COLD
            and prompt is not None
        ):
            control_key = (scenario, record.extra.get("control_pair"))
            positive_cold_prompts[control_key] = prompt
            cold_hit, _cold_miss = cache_tokens_from_usage(record.usage or {})
            if cold_hit is not None:
                positive_cold_hits[control_key] = cold_hit

    for record in records:
        if not isinstance(record.extra, dict):
            continue
        scenario = str(record.extra.get("cache_scenario") or "")
        control = record.extra.get("cache_control")
        role = record.extra.get("control_role")
        expected_reusable = _record_expected_reusable_tokens(
            record,
            structure_probe_tokens.get(scenario),
            positive_cold_prompts,
        )
        prompt = prompt_tokens_from_usage(record.usage or {})
        hit, miss = cache_tokens_from_usage(record.usage or {})
        errors: list[str] = []
        if prompt is not None and prompt < 0:
            errors.append("input tokens must be non-negative")
        if hit is not None and hit < 0:
            errors.append("cached tokens must be non-negative")
        if miss is not None and miss < 0:
            errors.append("cache miss tokens must be non-negative")
        if prompt is not None and hit is not None and hit > prompt:
            errors.append("cached tokens exceed input tokens")
        if prompt is not None and hit is not None and miss is not None and hit + miss != prompt:
            errors.append("cached plus uncached tokens do not equal input tokens")

        ratio = hit / prompt if hit is not None and prompt and prompt > 0 else None
        if (
            expected_reusable is not None
            and hit is not None
            and control not in {CACHE_CONTROL_NEGATIVE, CACHE_CONTROL_POSITIVE}
            and hit > expected_reusable
        ):
            errors.append("cached tokens exceed structurally reusable prefix tokens")
        if control == CACHE_CONTROL_POSITIVE and role == CACHE_CONTROL_ROLE_WARM:
            control_key = (scenario, record.extra.get("control_pair"))
            cold_hit = positive_cold_hits.get(control_key)
            reusable_ratio = (
                hit / expected_reusable
                if hit is not None and expected_reusable and expected_reusable > 0
                else None
            )
            if reusable_ratio is not None and reusable_ratio < positive_min:
                errors.append("positive warm control cached ratio is below the configured minimum")
            if hit is not None and cold_hit is not None and hit <= cold_hit:
                errors.append("positive warm control cached tokens did not increase over cold")
            if hit is not None and expected_reusable is not None and hit > expected_reusable:
                errors.append("positive warm cached tokens exceed cold-request input tokens")
        elif control == CACHE_CONTROL_NEGATIVE or (
            control == CACHE_CONTROL_POSITIVE and role == CACHE_CONTROL_ROLE_COLD
        ):
            if ratio is not None and ratio > negative_max:
                errors.append("unique/cold control cached ratio exceeds the configured maximum")

        unavailable_reasons: list[str] = []
        if prompt is None:
            unavailable_reasons.append("input token telemetry is missing")
        if hit is None:
            unavailable_reasons.append("cached token telemetry is missing")
        if control == CACHE_CONTROL_POSITIVE and role == CACHE_CONTROL_ROLE_WARM:
            control_key = (scenario, record.extra.get("control_pair"))
            if expected_reusable is None:
                unavailable_reasons.append("positive cold input token telemetry is missing")
            if control_key not in positive_cold_hits:
                unavailable_reasons.append("positive cold cached token telemetry is missing")
        elif control not in {CACHE_CONTROL_POSITIVE, CACHE_CONTROL_NEGATIVE}:
            if expected_reusable is None:
                unavailable_reasons.append("structurally reusable token ceiling is unavailable")
        status = (
            "fail"
            if errors
            else "not_available"
            if unavailable_reasons
            else "pass"
        )
        record.cache_token_audit = {
            "schema_version": 1,
            "status": status,
            "prompt_tokens": prompt,
            "reported_cached_tokens": hit,
            "reported_uncached_tokens": miss,
            "expected_reusable_tokens": expected_reusable,
            "cached_input_token_ratio": ratio,
            "excess_cached_tokens": (
                max(int(hit) - int(expected_reusable), 0)
                if hit is not None and expected_reusable is not None
                else None
            ),
            "control": control,
            "control_role": role,
            "errors": errors,
            "unavailable_reasons": unavailable_reasons,
        }


def _record_expected_reusable_tokens(
    record: RequestRecord,
    structure_probe_tokens: int | None,
    positive_cold_prompts: dict[tuple[str, Any], int],
) -> int | None:
    extra = record.extra
    scenario = str(extra.get("cache_scenario") or "")
    control = extra.get("cache_control")
    role = extra.get("control_role")
    if control == CACHE_CONTROL_POSITIVE and role == CACHE_CONTROL_ROLE_WARM:
        return positive_cold_prompts.get((scenario, extra.get("control_pair")))
    if control in {CACHE_CONTROL_POSITIVE, CACHE_CONTROL_NEGATIVE}:
        return 0
    if scenario == "progressive_customer_session" and extra.get("cache_stage") == "seed":
        return structure_probe_tokens
    for key in ("reusable_prefix_tokens", "cacheable_prefix_tokens"):
        try:
            value = int(extra.get(key))
        except (TypeError, ValueError):
            continue
        return max(value, 0)
    if scenario == "kilocode_agent_session" and extra.get("cache_stage") == "step":
        return 0
    if scenario == "growing_conversation" and extra.get("conversation_turn") == 1:
        return 0
    return None


def _cache_usage_accuracy_summary(records: list[RequestRecord]) -> dict[str, Any]:
    audits = [
        item.cache_token_audit
        for item in records
        if isinstance(item.cache_token_audit, dict) and item.cache_token_audit
    ]
    if not audits:
        return {}
    failures = [item for item in audits if item.get("status") == "fail"]
    available = [item for item in audits if item.get("status") in {"pass", "fail"}]
    unavailable = [item for item in audits if item.get("status") == "not_available"]
    positive_warm = [
        item
        for item in audits
        if item.get("control") == CACHE_CONTROL_POSITIVE
        and item.get("control_role") == CACHE_CONTROL_ROLE_WARM
    ]
    negative = [
        item for item in audits if item.get("control") == CACHE_CONTROL_NEGATIVE
    ]
    control_audits = [
        item
        for item in audits
        if item.get("control") in {CACHE_CONTROL_POSITIVE, CACHE_CONTROL_NEGATIVE}
    ]
    available_control_audits = [
        item for item in control_audits if item.get("status") in {"pass", "fail"}
    ]
    controls_present = bool(positive_warm and negative)
    if failures or not controls_present:
        status = "fail"
    elif not available:
        status = "not_available"
    elif unavailable:
        status = "partial"
    else:
        status = "pass"
    return {
        "cache_usage_accuracy_status": status,
        "cache_usage_accuracy_pass": status != "fail",
        "cache_usage_accuracy_record_count": len(audits),
        "cache_usage_accuracy_available_count": len(available),
        "cache_usage_accuracy_coverage": len(available) / len(audits) if audits else 0.0,
        "cache_usage_accuracy_failure_count": len(failures) + (0 if controls_present else 1),
        "cache_usage_accuracy_unavailable_count": len(unavailable),
        "cache_usage_accuracy_excess_tokens": sum(
            int(item.get("excess_cached_tokens") or 0) for item in failures
        ),
        "cache_controls_present": controls_present,
        "cache_control_group_coverage": 1.0 if controls_present else 0.0,
        "cache_control_usage_coverage": (
            len(available_control_audits) / len(control_audits)
            if control_audits
            else 0.0
        ),
        "cache_control_positive_warm_count": len(positive_warm),
        "cache_control_negative_count": len(negative),
        "cache_usage_accuracy_failures": [
            error
            for item in failures
            for error in item.get("errors") or []
        ]
        + ([] if controls_present else ["positive and negative cache controls are required"]),
    }


def _cache_control_metrics(
    records: list[RequestRecord], scenario: str
) -> dict[str, Any]:
    controls = [
        item
        for item in records
        if isinstance(item.extra, dict)
        and item.extra.get("cache_scenario") == scenario
        and item.extra.get("cache_control")
    ]
    control_metrics: dict[str, Any] = {}
    for control_name in (CACHE_CONTROL_POSITIVE, CACHE_CONTROL_NEGATIVE):
        selected = [
            item
            for item in controls
            if item.extra.get("cache_control") == control_name and item.success
        ]
        measured_control = (
            [
                item
                for item in selected
                if item.extra.get("control_role") == CACHE_CONTROL_ROLE_WARM
            ]
            if control_name == CACHE_CONTROL_POSITIVE
            else selected
        )
        control_usage: list[tuple[int, int]] = []
        for item in measured_control:
            prompt = prompt_tokens_from_usage(item.usage or {})
            hit, miss = cache_tokens_from_usage(item.usage or {})
            if prompt is not None and (hit is not None or miss is not None):
                control_usage.append((prompt, hit or 0))
        denominator = sum(prompt for prompt, _hit in control_usage)
        numerator = sum(min(hit, prompt) for prompt, hit in control_usage)
        control_metrics[control_name] = {
            "request_count": len(selected),
            "usage_record_count": len(control_usage),
            "cached_input_token_ratio": numerator / denominator if denominator else None,
        }
        if control_name == CACHE_CONTROL_POSITIVE:
            control_metrics[control_name]["pair_count"] = len(
                {item.extra.get("control_pair") for item in selected}
            )
    return control_metrics


def _customer_cache_metrics(records: list[RequestRecord]) -> dict[str, Any]:
    scenarios = {
        item.extra.get("cache_scenario")
        for item in records
        if isinstance(item.extra, dict)
    }
    if "progressive_customer_session" in scenarios:
        return _progressive_customer_cache_metrics(records)
    if "kilocode_agent_session" in scenarios:
        return _kilocode_agent_cache_metrics(records)
    for scenario in ("growing_conversation", "shared_prefix"):
        if scenario in scenarios:
            return {"cache_control_metrics": _cache_control_metrics(records, scenario)}
    return {}


def _kilocode_agent_cache_metrics(records: list[RequestRecord]) -> dict[str, Any]:
    kilocode_records = [
        item
        for item in records
        if isinstance(item.extra, dict)
        and item.extra.get("cache_scenario") == "kilocode_agent_session"
        and item.extra.get("cache_stage") == "step"
        and not item.extra.get("cache_control")
        and not item.is_warmup
    ]
    if not kilocode_records:
        return {}

    successful = [item for item in kilocode_records if item.success]
    usage_records: list[tuple[RequestRecord, int, int]] = []
    for item in successful:
        prompt_tokens = prompt_tokens_from_usage(item.usage or {})
        hit, miss = cache_tokens_from_usage(item.usage or {})
        if prompt_tokens is None or (hit is None and miss is None):
            continue
        usage_records.append((item, prompt_tokens, hit or 0))

    total_input = sum(prompt for _item, prompt, _hit in usage_records)
    total_hit = sum(min(hit, prompt) for _item, prompt, hit in usage_records)
    hit_requests = sum(1 for _item, _prompt, hit in usage_records if hit > 0)

    step_metrics: dict[str, dict[str, Any]] = {}
    for item, prompt, hit in usage_records:
        try:
            step_index = int(item.extra.get("step_index"))
        except (TypeError, ValueError):
            continue
        entry = step_metrics.setdefault(
            f"step_{step_index}",
            {
                "request_count": 0,
                "prompt_tokens": 0,
                "cache_hit_tokens": 0,
                "latency_ms": [],
            },
        )
        entry["request_count"] += 1
        entry["prompt_tokens"] += prompt
        entry["cache_hit_tokens"] += min(hit, prompt)
        if item.latency_ms is not None:
            entry["latency_ms"].append(float(item.latency_ms))
    kilocode_step_metrics: dict[str, dict[str, Any]] = {}
    for step_name in sorted(
        step_metrics, key=lambda name: int(name.removeprefix("step_"))
    ):
        entry = step_metrics[step_name]
        latencies = entry.pop("latency_ms")
        prompt = entry["prompt_tokens"]
        hit = entry["cache_hit_tokens"]
        kilocode_step_metrics[step_name] = {
            **entry,
            "cached_input_token_ratio": hit / prompt if prompt else None,
            "latency_ms": (
                sum(latencies) / len(latencies) if latencies else None
            ),
        }

    control_metrics = _cache_control_metrics(records, "kilocode_agent_session")

    cached_ratio = total_hit / total_input if total_input else None
    return {
        "cache_hit_rate": cached_ratio,
        "cache_hit_rate_semantics": "cached_input_tokens/input_tokens",
        "cached_input_token_ratio": cached_ratio,
        "cached_input_tokens": total_hit,
        "customer_input_tokens": total_input,
        "cache_hit_request_ratio": hit_requests / len(usage_records) if usage_records else None,
        "cache_measurement_coverage": len(usage_records) / len(successful) if successful else 0.0,
        "cache_usage_fields_seen": len(usage_records),
        "cache_eligible_record_count": len(usage_records),
        "kilocode_step_count": len(kilocode_records),
        "kilocode_step_success_count": len(successful),
        "kilocode_step_metrics": kilocode_step_metrics,
        "cache_control_metrics": control_metrics,
    }


def _progressive_customer_cache_metrics(records: list[RequestRecord]) -> dict[str, Any]:
    stages = (
        "seed",
        "direct_growth",
        "tool_initial",
        "tool_followup",
        "final_growth",
    )
    customer_records = [
        item
        for item in records
        if isinstance(item.extra, dict)
        and item.extra.get("cache_scenario") == "progressive_customer_session"
        and item.extra.get("cache_stage") in stages
        and not item.extra.get("cache_control")
        and not item.is_warmup
    ]
    if not customer_records:
        return {}

    successful = [item for item in customer_records if item.success]
    usage_records: list[tuple[RequestRecord, int, int]] = []
    for item in successful:
        prompt_tokens = prompt_tokens_from_usage(item.usage or {})
        hit, miss = cache_tokens_from_usage(item.usage or {})
        if prompt_tokens is None or (hit is None and miss is None):
            continue
        usage_records.append((item, prompt_tokens, hit or 0))

    total_input = sum(prompt for _item, prompt, _hit in usage_records)
    total_hit = sum(min(hit, prompt) for _item, prompt, hit in usage_records)
    hit_requests = sum(1 for _item, _prompt, hit in usage_records if hit > 0)

    structure_probes = [
        item
        for item in records
        if isinstance(item.extra, dict)
        and item.extra.get("cache_scenario") == "progressive_customer_session"
        and item.extra.get("cache_structure_probe")
        and item.success
    ]
    structure_probe_tokens = next(
        (
            prompt
            for item in structure_probes
            if (prompt := prompt_tokens_from_usage(item.usage or {})) is not None
        ),
        None,
    )

    def structural_prefix_tokens(item: RequestRecord, prompt: int) -> int | None:
        if item.extra.get("cache_stage") == "seed":
            if structure_probe_tokens is None:
                return None
            return min(max(int(structure_probe_tokens), 0), prompt)
        if not item.extra.get("strict_prefix_extension"):
            return None
        try:
            reusable = int(item.extra.get("reusable_prefix_tokens"))
        except (TypeError, ValueError):
            return None
        return min(max(reusable, 0), prompt)

    structural_usage = [
        (item, prompt, hit, prefix)
        for item, prompt, hit in usage_records
        if (prefix := structural_prefix_tokens(item, prompt)) is not None
    ]
    structural_coverage = (
        len(structural_usage) / len(usage_records) if usage_records else 0.0
    )
    structural_prefix_total = sum(
        prefix for _item, _prompt, _hit, prefix in structural_usage
    )
    structural_ceiling = (
        structural_prefix_total / total_input
        if total_input and structural_coverage == 1.0
        else None
    )

    prefix_total = 0
    prefix_hit = 0
    tool_prefix_total = 0
    tool_prefix_hit = 0
    for item, prompt, hit in usage_records:
        if not item.extra.get("strict_prefix_extension"):
            continue
        try:
            reusable = int(item.extra.get("reusable_prefix_tokens"))
        except (TypeError, ValueError):
            continue
        reusable = min(max(reusable, 0), prompt)
        prefix_total += reusable
        prefix_hit += min(hit, reusable)
        if item.extra.get("cache_stage") == "tool_followup":
            tool_prefix_total += reusable
            tool_prefix_hit += min(hit, reusable)

    by_stage: dict[str, dict[str, Any]] = {}
    for stage in stages:
        stage_all = [item for item in customer_records if item.extra.get("cache_stage") == stage]
        stage_usage = [item for item in usage_records if item[0].extra.get("cache_stage") == stage]
        stage_input = sum(prompt for _item, prompt, _hit in stage_usage)
        stage_hit = sum(min(hit, prompt) for _item, prompt, hit in stage_usage)
        stage_structural = [
            (item, prompt, hit, prefix)
            for item, prompt, hit in stage_usage
            if (prefix := structural_prefix_tokens(item, prompt)) is not None
        ]
        stage_structural_prefix = sum(
            prefix for _item, _prompt, _hit, prefix in stage_structural
        )
        stage_structural_ceiling = (
            stage_structural_prefix / stage_input
            if stage_input and len(stage_structural) == len(stage_usage)
            else None
        )
        stage_actual_rate = stage_hit / stage_input if stage_input else None
        stage_cache_efficiency = (
            stage_actual_rate / stage_structural_ceiling
            if stage_actual_rate is not None
            and stage_structural_ceiling is not None
            and stage_structural_ceiling > 0
            else None
        )
        by_stage[stage] = {
            "request_count": len(stage_all),
            "success_count": sum(1 for item in stage_all if item.success),
            "usage_record_count": len(stage_usage),
            "measurement_coverage": (
                len(stage_usage) / sum(1 for item in stage_all if item.success)
                if any(item.success for item in stage_all)
                else 0.0
            ),
            "input_tokens": stage_input,
            "structural_cacheable_prefix_tokens": stage_structural_prefix,
            "structural_hit_rate_ceiling": stage_structural_ceiling,
            "cached_input_tokens": stage_hit,
            "cached_input_token_ratio": stage_actual_rate,
            "actual_cache_hit_rate": stage_actual_rate,
            "cache_efficiency": stage_cache_efficiency,
            "cache_hit_request_ratio": (
                sum(1 for _item, _prompt, hit in stage_usage if hit > 0) / len(stage_usage)
                if stage_usage
                else None
            ),
        }

    session_ids = {
        int(item.extra["session_index"])
        for item in customer_records
        if item.extra.get("session_index") is not None
    }
    completed_ids = {
        int(item.extra["session_index"])
        for item in customer_records
        if item.extra.get("session_completed") and item.extra.get("session_index") is not None
    }
    unsupported_ids = {
        int(item.extra["session_index"])
        for item in customer_records
        if item.extra.get("session_stop_reason") == "tool_flow_unsupported"
        and item.extra.get("session_index") is not None
    }
    supported_ids = {
        int(item.extra["session_index"])
        for item in customer_records
        if item.extra.get("cache_stage") == "tool_followup"
        and item.extra.get("tool_flow_status") == "supported"
        and item.success
        and item.extra.get("session_index") is not None
    }
    tool_evaluated_ids = unsupported_ids | supported_ids

    control_metrics = _cache_control_metrics(records, "progressive_customer_session")

    cached_ratio = total_hit / total_input if total_input else None
    cache_efficiency = (
        cached_ratio / structural_ceiling
        if cached_ratio is not None
        and structural_ceiling is not None
        and structural_ceiling > 0
        else None
    )
    if cache_efficiency is None:
        cache_efficiency_status = "unavailable"
    elif cache_efficiency > 1.000001:
        cache_efficiency_status = "exceeds_structure"
    else:
        cache_efficiency_status = "measured"
    return {
        "cache_hit_rate": cached_ratio,
        "cache_hit_rate_semantics": "cached_input_tokens/input_tokens",
        "cached_input_token_ratio": cached_ratio,
        "actual_cache_hit_rate": cached_ratio,
        "actual_cache_hit_rate_semantics": "cached_input_tokens/input_tokens",
        "cached_input_tokens": total_hit,
        "customer_input_tokens": total_input,
        "structural_hit_rate_ceiling": structural_ceiling,
        "structural_hit_rate_ceiling_semantics": (
            "structurally_cacheable_prefix_tokens/input_tokens"
        ),
        "structural_cacheable_prefix_tokens": structural_prefix_total,
        "structure_probe_record_count": len(structure_probes),
        "structure_probe_input_tokens": structure_probe_tokens,
        "structure_ceiling_measurement_coverage": structural_coverage,
        "cache_efficiency": cache_efficiency,
        "cache_efficiency_semantics": (
            "actual_cache_hit_rate/structural_hit_rate_ceiling"
        ),
        "cache_efficiency_status": cache_efficiency_status,
        "cache_hit_request_ratio": hit_requests / len(usage_records) if usage_records else None,
        "cache_measurement_coverage": len(usage_records) / len(successful) if successful else 0.0,
        "cache_usage_fields_seen": len(usage_records),
        "cache_eligible_record_count": len(usage_records),
        "cache_customer_request_count": len(customer_records),
        "cache_customer_success_count": len(successful),
        "progressive_prefix_reuse_rate": prefix_hit / prefix_total if prefix_total else None,
        "progressive_prefix_reusable_tokens": prefix_total,
        "progressive_prefix_reused_tokens": prefix_hit,
        "tool_followup_reuse_rate": (
            tool_prefix_hit / tool_prefix_total if tool_prefix_total else None
        ),
        "tool_followup_reusable_prefix_tokens": tool_prefix_total,
        "tool_followup_reused_tokens": tool_prefix_hit,
        "session_count": len(session_ids),
        "session_completed_count": len(completed_ids),
        "session_completion_ratio": len(completed_ids) / len(session_ids) if session_ids else None,
        "tool_flow_evaluated_session_count": len(tool_evaluated_ids),
        "tool_flow_supported_session_count": len(supported_ids),
        "tool_flow_unsupported_session_count": len(unsupported_ids),
        "tool_flow_supported_session_ratio": (
            len(supported_ids) / len(tool_evaluated_ids) if tool_evaluated_ids else None
        ),
        "cache_stage_metrics": by_stage,
        "cache_case_metrics": by_stage,
        "cache_control_metrics": control_metrics,
    }


def build_time_series(
    records: Iterable[RequestRecord],
    *,
    business_prefix: str = "chat:",
    business_group: str | None = "throughput_profiles",
    cache_min_prompt_tokens: int = 4000,
    bucket_sec: int = 10,
    now: float | None = None,
    max_points: int = 240,
) -> list[dict[str, Any]]:
    interval = max(int(bucket_sec), 1)
    measured = sorted(
        (
            item
            for item in records
            if item.task_name.startswith(business_prefix)
            and not item.is_warmup
            and not item.is_retry
            and (business_group is None or item.group == business_group)
        ),
        key=lambda item: item.timestamp,
    )
    if not measured:
        return []

    first_timestamp = measured[0].timestamp
    buckets: dict[int, list[RequestRecord]] = {}
    for item in measured:
        bucket_index = max(int((item.timestamp - first_timestamp) // interval), 0)
        buckets.setdefault(bucket_index, []).append(item)

    current_time = max(float(now if now is not None else time.time()), measured[-1].timestamp)
    last_bucket = max(buckets)
    points: list[dict[str, Any]] = []
    for bucket_index, items in sorted(buckets.items()):
        bucket_start = first_timestamp + bucket_index * interval
        window_sec = (
            max(min(current_time - bucket_start, interval), 1.0)
            if bucket_index == last_bucket
            else float(interval)
        )
        summary = summarize_records(
            items,
            business_prefix=business_prefix,
            business_group=business_group,
            cache_min_prompt_tokens=cache_min_prompt_tokens,
            duration_sec=window_sec,
        )
        users = _dominant_record_extra(items, "configured_users")
        staircase_step = _dominant_record_extra(items, "staircase_step")
        points.append(
            {
                "timestamp": bucket_start + window_sec,
                "window_sec": window_sec,
                "business_rpm": summary["business_rpm"],
                "attempted_business_rpm": summary["attempted_business_rpm"],
                "input_tpm": summary["input_tpm"] if summary["token_usage_record_count"] else None,
                "output_tpm": summary["output_tpm"] if summary["token_usage_record_count"] else None,
                "total_tpm": summary["total_tpm"] if summary["token_usage_record_count"] else None,
                "token_usage_coverage": summary["token_usage_coverage"],
                "target_tokens_per_request": summary["target_tokens_per_request"],
                "avg_tokens_per_request": summary["avg_tokens_per_request"],
                "p50_tokens_per_request": summary["p50_tokens_per_request"],
                "p95_tokens_per_request": summary["p95_tokens_per_request"],
                "tokens_per_request_deviation_ratio": summary[
                    "tokens_per_request_deviation_ratio"
                ],
                "adaptive_controller_status": summary["adaptive_controller_status"],
                "success_rate": summary["success_rate"],
                "p95_latency_ms": summary["p95_latency_ms"],
                "e2e_latency_p95_ms": summary["e2e_latency_p95_ms"],
                "ttft_p95_ms": summary["ttft_p95_ms"],
                "ttft_coverage": summary["ttft_coverage"],
                "business_record_count": summary["business_record_count"],
                "business_success_count": summary["business_success_count"],
                "configured_users": users,
                "staircase_step": staircase_step,
            }
        )
    return points[-max(max_points, 1) :]


def percentile(values: Iterable[float | int | None], p: int) -> float | None:
    clean = sorted(float(value) for value in values if value is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    rank = (len(clean) - 1) * (p / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(clean) - 1)
    fraction = rank - lower
    return clean[lower] + (clean[upper] - clean[lower]) * fraction


def load_records(path: str | Path) -> list[RequestRecord]:
    target = Path(path)
    if not target.exists():
        return []
    records: list[RequestRecord] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(RequestRecord.from_dict(json.loads(line)))
    return records


def load_history(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    return [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: str | Path, payload: dict[str, Any] | list[Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_payload = redact_secrets(payload)
    target.write_text(json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def request_record_from_result(
    *,
    result: Any,
    task_name: str,
    group: str,
    profile: str,
    method: str = "POST",
    path: str = "/v1/chat/completions",
    is_warmup: bool = False,
    phase: str | None = None,
    extra: dict[str, Any] | None = None,
) -> RequestRecord:
    return RequestRecord(
        timestamp=getattr(result, "timestamp", time.time()),
        task_name=task_name,
        group=group,
        profile=profile,
        method=method,
        path=path,
        success=bool(getattr(result, "success", False)),
        status_code=getattr(result, "status_code", None),
        latency_ms=getattr(result, "latency_ms", None),
        ttft_ms=getattr(result, "ttft_ms", None),
        response_length=getattr(result, "response_length", None),
        finish_reason=getattr(result, "finish_reason", None),
        usage=getattr(result, "usage", {}) or {},
        error_type=getattr(result, "error_type", None),
        failure_classification=getattr(result, "failure_classification", None),
        cache_headers=getattr(result, "cache_headers", {}) or {},
        is_warmup=is_warmup,
        phase=phase,
        extra=extra or {},
    )


def _record_transport(record: RequestRecord) -> str:
    if isinstance(record.extra, dict) and record.extra.get("transport"):
        return str(record.extra["transport"])
    if str(record.path).rstrip("/").endswith("/messages"):
        return "claude_messages"
    if str(record.path).rstrip("/").endswith("/chat/completions"):
        return "chat_completions"
    return "generic"


def _duration_from_records(records: list[RequestRecord]) -> float:
    if len(records) < 2:
        return 1.0
    timestamps = [item.timestamp for item in records]
    return max(max(timestamps) - min(timestamps), 1.0)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _dominant_record_extra(records: list[RequestRecord], key: str) -> Any:
    values = [
        item.extra.get(key)
        for item in records
        if isinstance(item.extra, dict) and item.extra.get(key) is not None
    ]
    if not values:
        return None
    return Counter(values).most_common(1)[0][0]
