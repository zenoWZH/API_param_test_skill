from __future__ import annotations

import copy
import json
import os
import random
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

from locust import HttpUser, constant, constant_throughput, events, task
from locust.exception import StopUser

from lib.adaptive_load import (
    AdaptiveLengthController,
    AdaptiveRequestPlan,
    estimate_request_tokens,
    filter_context_unsafe_profiles,
    resolve_context_window,
)
from lib.config import (
    PROJECT_ROOT,
    get_active_provider_name,
    get_api_key,
    get_model_api_form,
    get_model_family,
    get_model_route_profile,
    get_model_transport,
    get_provider_interface,
    get_provider_config,
    get_selected_model,
    get_timeout_sec,
    load_config,
)
from lib.deepseek_params import (
    build_claude_tool_followup_request,
    apply_request_mode,
    build_request,
    build_tool_followup_request,
    extract_claude_tool_uses,
    extract_content,
    extract_finish_reason,
    extract_tool_calls,
    extract_usage,
    total_tokens_from_usage,
    load_pressure_capability,
    weighted_workload_profiles,
)
from lib.load_rate import ConstantThroughputPlan, MeasurementWindow
from lib.gemini_api_version import build_gemini_api_url, is_ai_studio_origin
from lib.metrics import (
    RequestRecord,
    RunRecorder,
    classify_failure,
    classify_pressure_failure,
    load_records,
    summarize_records,
    write_json,
)
from lib.job_spec import load_job_spec
from lib.model_profile_catalog import (
    binding_from_database_snapshot,
    capability_profile_from_database_snapshot,
    resolve_runtime_parameter_config,
)
from lib.param_specs import param_rows_for_family, param_spec_payload
from lib.reference_specs import pressure_test_runnable


def _optional_env_int(name: str) -> int | None:
    try:
        value = int(os.getenv(name, ""))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


CONFIG = load_config()
ACTIVE_PROVIDER = get_active_provider_name(CONFIG)
PROVIDER_CFG = get_provider_config(CONFIG, ACTIVE_PROVIDER)
SELECTED_MODEL = get_selected_model(CONFIG, ACTIVE_PROVIDER)
MODEL_FAMILY = get_model_family(CONFIG, SELECTED_MODEL, ACTIVE_PROVIDER)
MODEL_ROUTE_PROFILE = get_model_route_profile(
    CONFIG, SELECTED_MODEL, ACTIVE_PROVIDER
)
MODEL_API_FORM = get_model_api_form(
    CONFIG,
    SELECTED_MODEL,
    ACTIVE_PROVIDER,
    route_profile=MODEL_ROUTE_PROFILE,
)
MODEL_TRANSPORT = get_model_transport(
    CONFIG,
    SELECTED_MODEL,
    ACTIVE_PROVIDER,
    route_profile=MODEL_ROUTE_PROFILE,
    api_form=MODEL_API_FORM,
)
JOB_SPEC = load_job_spec(os.getenv("LOADTEST_JOB_SPEC"))
if JOB_SPEC:
    _runtime_identity = {
        "provider": ACTIVE_PROVIDER,
        "model": SELECTED_MODEL,
        "api_form": MODEL_API_FORM,
        "route_profile": MODEL_ROUTE_PROFILE,
    }
    _identity_mismatches = [
        field
        for field, expected in _runtime_identity.items()
        if JOB_SPEC.get(field) not in (None, "", expected)
    ]
    if _identity_mismatches:
        raise RuntimeError(
            "Job spec identity conflicts with Locust runtime selection: "
            + ", ".join(_identity_mismatches)
        )
_job_database = (
    JOB_SPEC.get("model_profile_database") if isinstance(JOB_SPEC, dict) else None
)
if isinstance(_job_database, dict):
    _snapshot_binding = binding_from_database_snapshot(_job_database)
    _target = _snapshot_binding.get("execution_target") or {}
    _target_expected = {
        "provider_id": ACTIVE_PROVIDER,
        "request_model_id": SELECTED_MODEL,
        "route_profile": MODEL_ROUTE_PROFILE,
        "api_form": MODEL_API_FORM,
    }
    _target_mismatches = [
        field
        for field, expected in _target_expected.items()
        if _target.get(field) not in (None, "", expected)
    ]
    if _target_mismatches:
        raise RuntimeError(
            "Job MPDB snapshot conflicts with Locust runtime selection: "
            + ", ".join(_target_mismatches)
        )
    MODEL_PROFILE_DATABASE = copy.deepcopy(_job_database)
else:
    try:
        _parameter_config = resolve_runtime_parameter_config(
            CONFIG,
            ACTIVE_PROVIDER,
            SELECTED_MODEL,
            MODEL_FAMILY,
            MODEL_ROUTE_PROFILE,
            MODEL_API_FORM,
            modality="text",
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            "App Locust pressure runner requires a registered pressure-enabled "
            "text capability; image/video or unregistered models are rejected."
        ) from exc
    MODEL_PROFILE_DATABASE = copy.deepcopy(
        _parameter_config["model_profile_database"]
    )
_expected_snapshot_digest = str(
    os.getenv("LOADTEST_EXPECTED_MPDB_SNAPSHOT_DIGEST") or ""
).strip()
_expected_reference_contract = str(
    os.getenv("LOADTEST_EXPECTED_REFERENCE_CONTRACT_ID") or ""
).strip()
if _expected_snapshot_digest and (
    MODEL_PROFILE_DATABASE.get("snapshot_digest") != _expected_snapshot_digest
):
    raise RuntimeError(
        "Locust MPDB snapshot digest conflicts with its prepared sweep plan."
    )
if _expected_reference_contract and (
    MODEL_PROFILE_DATABASE.get("reference_contract_id")
    != _expected_reference_contract
):
    raise RuntimeError(
        "Locust reference Contract conflicts with its prepared sweep plan."
    )
CONFIG["_model_profile_database"] = copy.deepcopy(MODEL_PROFILE_DATABASE)
_job_capability = (
    JOB_SPEC.get("model_capability_profile") if isinstance(JOB_SPEC, dict) else None
)
REFERENCE_CONTRACT_ID = str(
    (JOB_SPEC or {}).get("reference_contract_id")
    or MODEL_PROFILE_DATABASE.get("reference_contract_id")
    or ""
) or None
if _job_capability is not None:
    CONFIG["_model_capability_profile"] = copy.deepcopy(_job_capability)
try:
    MODEL_CAPABILITY = load_pressure_capability(
        CONFIG, ACTIVE_PROVIDER, MODEL_FAMILY, SELECTED_MODEL,
        MODEL_API_FORM, MODEL_ROUTE_PROFILE, REFERENCE_CONTRACT_ID,
    )
except (KeyError, RuntimeError, ValueError) as exc:
    raise RuntimeError(
        "App Locust pressure runner requires a registered pressure-enabled "
        "text capability; image/video or unregistered models are rejected."
    ) from exc
if not (
    MODEL_CAPABILITY.get("known_model") is True
    and MODEL_CAPABILITY.get("known_api_profile") is True
    and MODEL_CAPABILITY.get("route_profile_known") is True
    and pressure_test_runnable(MODEL_CAPABILITY)
):
    raise RuntimeError(
        "App Locust pressure runner requires a registered pressure-enabled "
        "text capability; image/video or unregistered models are rejected."
    )
MODEL_CAPABILITY["model_profile_database"] = copy.deepcopy(
    MODEL_PROFILE_DATABASE
)
CONFIG["_model_capability_profile"] = copy.deepcopy(MODEL_CAPABILITY)
WORKLOAD = os.getenv("LOADTEST_WORKLOAD", "throughput")
if WORKLOAD == "cache_suite":
    raise RuntimeError(
        "LOADTEST_WORKLOAD=cache_suite is a legacy weighted load workload and "
        "does not provide valid cache-hit evidence. Use scripts/run_cache.py "
        "for the MPDB-bound cache suite."
    )
REQUEST_MODE = os.getenv("LOADTEST_REQUEST_MODE", "unique")
if REQUEST_MODE not in {"unique", "fixed"}:
    raise RuntimeError("LOADTEST_REQUEST_MODE must be unique or fixed.")
if WORKLOAD == "throughput_rpm" and REQUEST_MODE != "fixed":
    raise RuntimeError(
        "throughput_rpm is the standard fixed-body pressure workload; "
        "LOADTEST_REQUEST_MODE must be fixed."
    )
PHASE = os.getenv("LOADTEST_PHASE", "measure")
REPORT_DIR = Path(os.getenv("LOADTEST_REPORT_DIR", str(PROJECT_ROOT / "reports" / "locust")))
METRICS = CONFIG.get("metrics") or {}
BUSINESS_GROUP = (
    "throughput_profiles"
    if WORKLOAD.startswith("throughput")
    else {"cache_suite": "cache_profiles"}.get(WORKLOAD)
)

RECORDER = RunRecorder(
    REPORT_DIR,
    history_interval_sec=int(METRICS.get("history_interval_sec", 60)),
    records_file=str(METRICS.get("records_file", "request_records.jsonl")),
    history_file=str(METRICS.get("history_file", "history.jsonl")),
    business_request_prefix=str(METRICS.get("business_request_prefix", "chat:")),
    business_group=BUSINESS_GROUP,
    cache_min_prompt_tokens=int(METRICS.get("cache_min_prompt_tokens", 4000)),
)
RAW_TASK_ENTRIES = weighted_workload_profiles(
    CONFIG,
    WORKLOAD,
    api_form=MODEL_API_FORM,
    route_profile=MODEL_ROUTE_PROFILE,
    reference_source=REFERENCE_CONTRACT_ID,
)
TASK_ENTRIES, CONTEXT_SKIPPED_PROFILES = filter_context_unsafe_profiles(
    CONFIG,
    PROVIDER_CFG,
    SELECTED_MODEL,
    RAW_TASK_ENTRIES,
)
TASK_CHOICES = [(group, profile) for group, profile, _weight in TASK_ENTRIES]
TASK_WEIGHTS = [weight for _group, _profile, weight in TASK_ENTRIES]
CONTEXT_WINDOW_TOKENS, CONTEXT_WINDOW_SOURCE = resolve_context_window(
    CONFIG,
    PROVIDER_CFG,
    SELECTED_MODEL,
)
for skipped_profile in CONTEXT_SKIPPED_PROFILES:
    print(
        "[loadtest] skipping context-unsafe profile "
        f"{skipped_profile['profile']}: estimated "
        f"{skipped_profile['estimated_tokens']} tokens > safe limit "
        f"{skipped_profile['safe_context_tokens']} "
        f"({skipped_profile['context_window_source']})",
        flush=True,
    )
TARGET_RPM = float(os.getenv("LOADTEST_TARGET_RPM", "0") or 0)
TARGET_TPM = float(os.getenv("LOADTEST_TARGET_TPM", "0") or 0)
TARGET_TOKENS_PER_REQUEST = float(
    os.getenv("LOADTEST_TARGET_TOKENS_PER_REQUEST", "0") or 0
)
if TARGET_TOKENS_PER_REQUEST <= 0 and TARGET_RPM > 0 and TARGET_TPM > 0:
    TARGET_TOKENS_PER_REQUEST = TARGET_TPM / TARGET_RPM
CONFIGURED_USERS = _optional_env_int("LOADTEST_USERS")
STAIRCASE_STEP = _optional_env_int("LOADTEST_STAIRCASE_STEP")
WARMUP_SEC = max(float(os.getenv("LOADTEST_WARMUP_SEC", "0") or 0), 0.0)
MEASURE_DURATION_SEC = max(
    float(os.getenv("LOADTEST_MEASURE_DURATION_SEC", "0") or 0), 0.0
)
RATE_PLAN = ConstantThroughputPlan.build(TARGET_RPM, CONFIGURED_USERS)
MEASUREMENT_WINDOW = MeasurementWindow.build(WARMUP_SEC, MEASURE_DURATION_SEC)
_RATE_USER_LOCK = threading.Lock()
_RATE_USER_INDEX = 0
_RATE_EPOCH_MONOTONIC: float | None = None
_RUN_STARTED_WALL: float | None = None
_MEASURE_STARTED_WALL: float | None = None
_MEASURE_ENDED_WALL: float | None = None
_STARTED_REQUESTS: dict[str, dict[str, float | None]] = {}


@events.test_start.add_listener
def start_measurement_clock(environment: Any, **kwargs: Any) -> None:
    global _RATE_EPOCH_MONOTONIC, _RATE_USER_INDEX
    global _RUN_STARTED_WALL, _MEASURE_STARTED_WALL, _MEASURE_ENDED_WALL
    _RATE_EPOCH_MONOTONIC = time.perf_counter()
    _RATE_USER_INDEX = 0
    _STARTED_REQUESTS.clear()
    _RUN_STARTED_WALL = time.time()
    if MEASUREMENT_WINDOW is not None:
        _MEASURE_STARTED_WALL = (
            _RUN_STARTED_WALL + MEASUREMENT_WINDOW.measure_start_elapsed_sec
        )
        _MEASURE_ENDED_WALL = (
            _RUN_STARTED_WALL + MEASUREMENT_WINDOW.measure_end_elapsed_sec
        )
    else:
        _MEASURE_STARTED_WALL = None
        _MEASURE_ENDED_WALL = None
    _write_load_window("running")


def _claim_rate_user_index() -> int:
    global _RATE_USER_INDEX
    with _RATE_USER_LOCK:
        index = _RATE_USER_INDEX
        _RATE_USER_INDEX += 1
    if RATE_PLAN is not None and index >= RATE_PLAN.users:
        raise RuntimeError(
            "Locust spawned more users than LOADTEST_USERS; aggregate RPM would be invalid."
        )
    return index


def _is_warmup_timestamp(timestamp: float) -> bool:
    if PHASE == "warmup":
        return True
    if _MEASURE_STARTED_WALL is None:
        return False
    return timestamp < _MEASURE_STARTED_WALL


def _measurement_elapsed() -> float | None:
    if _RATE_EPOCH_MONOTONIC is None:
        return None
    return time.perf_counter() - _RATE_EPOCH_MONOTONIC


def _admit_request() -> float | None:
    if MEASUREMENT_WINDOW is None or _RATE_EPOCH_MONOTONIC is None:
        return None
    elapsed = _measurement_elapsed()
    assert elapsed is not None
    if not MEASUREMENT_WINDOW.admits_elapsed(elapsed):
        raise StopUser()
    return elapsed


def _admit_before_send(request_id: str) -> float:
    """Apply the final monotonic gate with no blocking work before HTTP send."""
    try:
        elapsed = _admit_request()
    except StopUser:
        closed_elapsed = _measurement_elapsed()
        RECORDER.record_attempt_cancelled_before_send(
            request_id=request_id,
            timestamp=time.time(),
            elapsed_sec=closed_elapsed,
        )
        raise
    started_at = time.time()
    _STARTED_REQUESTS[request_id] = {
        "elapsed_sec": elapsed,
        "timestamp": started_at,
    }
    return started_at


def _context_metadata() -> dict[str, Any]:
    return {
        "request_mode": REQUEST_MODE,
        "warmup_sec": WARMUP_SEC or None,
        "measure_duration_sec": MEASURE_DURATION_SEC or None,
        "measure_started_at": _MEASURE_STARTED_WALL,
        "measure_ended_at": _MEASURE_ENDED_WALL,
        "backend": PROVIDER_CFG.get("backend"),
        "context_window_tokens": CONTEXT_WINDOW_TOKENS,
        "context_window_source": CONTEXT_WINDOW_SOURCE,
        "context_skipped_profiles": [
            item["profile"] for item in CONTEXT_SKIPPED_PROFILES
        ],
    }


def _claude_stop_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    details = payload.get("stop_details")
    if not isinstance(details, dict):
        return {}
    return {
        "stop_details_type": details.get("type"),
        "stop_details_category": details.get("category"),
        "stop_details_explanation": details.get("explanation"),
    }


def _classify_request_failure(
    group: str,
    status_code: int | None,
    finish_reason: str | None,
    error_type: str | None,
) -> str | None:
    if group != "throughput_profiles":
        return classify_failure(status_code, finish_reason, error_type)
    configured_reasons = METRICS.get("failure_finish_reasons")
    return classify_pressure_failure(
        status_code,
        finish_reason,
        error_type,
        configured_reasons if isinstance(configured_reasons, list) else None,
    )


def _begin_request_attempt(
    name: str,
    group: str,
    profile: str,
    transport: str,
) -> tuple[str, float]:
    admitted_elapsed = _admit_request()
    request_id = uuid.uuid4().hex
    timestamp = time.time()
    path = {
        "claude_messages": "/v1/messages",
        "gemini_generate_content": f"/v1beta/models/{SELECTED_MODEL}:generateContent",
    }.get(transport, "/v1/chat/completions")
    RECORDER.record_attempt(
        request_id=request_id,
        timestamp=timestamp,
        task_name=name,
        group=group,
        profile=profile,
        method="POST",
        path=path,
        is_warmup=(
            MEASUREMENT_WINDOW.phase_for_elapsed(admitted_elapsed) == "warmup"
            if MEASUREMENT_WINDOW is not None and admitted_elapsed is not None
            else _is_warmup_timestamp(timestamp)
        ),
        admitted_elapsed_sec=admitted_elapsed,
        extra={
            "provider": ACTIVE_PROVIDER,
            "workload": WORKLOAD,
            "requested_model": SELECTED_MODEL,
            "transport": transport,
            "target_rpm": TARGET_RPM or None,
            "configured_users": CONFIGURED_USERS,
            **_context_metadata(),
        },
    )
    return request_id, timestamp


def _load_attempt_rows() -> list[dict[str, Any]]:
    if not RECORDER.attempts_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with RECORDER.attempts_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _write_load_window(status: str) -> None:
    if RATE_PLAN is None or MEASUREMENT_WINDOW is None:
        return
    all_attempt_rows = _load_attempt_rows()
    measured_admissions = [
        item
        for item in all_attempt_rows
        if item.get("event") in {None, "admitted"}
        if item.get("group") == "throughput_profiles"
        and not item.get("is_warmup")
        and (
            item.get("admitted_elapsed_sec") is None
            or float(item.get("admitted_elapsed_sec") or 0)
            >= MEASUREMENT_WINDOW.measure_start_elapsed_sec
        )
        and (
            item.get("admitted_elapsed_sec") is None
            or float(item.get("admitted_elapsed_sec") or 0)
            < MEASUREMENT_WINDOW.measure_end_elapsed_sec
        )
        and (
            item.get("admitted_elapsed_sec") is not None
            or _MEASURE_STARTED_WALL is None
            or float(item.get("timestamp") or 0) >= _MEASURE_STARTED_WALL
        )
        and (
            item.get("admitted_elapsed_sec") is not None
            or _MEASURE_ENDED_WALL is None
            or float(item.get("timestamp") or 0) < _MEASURE_ENDED_WALL
        )
    ]
    measured_admission_ids = {
        str(item.get("request_id"))
        for item in measured_admissions
        if item.get("request_id")
    }
    cancelled_ids = {
        str(item.get("request_id"))
        for item in all_attempt_rows
        if item.get("event") == "cancelled_before_send"
        and item.get("request_id")
        and str(item.get("request_id")) in measured_admission_ids
    }
    started_ids = {
        request_id
        for request_id, details in _STARTED_REQUESTS.items()
        if details.get("elapsed_sec") is not None
        and float(details["elapsed_sec"])
        >= MEASUREMENT_WINDOW.measure_start_elapsed_sec
        and float(details["elapsed_sec"])
        < MEASUREMENT_WINDOW.measure_end_elapsed_sec
    }
    # Legacy ledgers predate explicit lifecycle events. Preserve their old
    # admitted-minus-cancelled interpretation when reconstructing such a run.
    if measured_admission_ids and not any(item.get("event") for item in all_attempt_rows):
        started_ids = measured_admission_ids - cancelled_ids
    admitted_ids = measured_admission_ids | started_ids
    records = [
        item
        for item in load_records(RECORDER.records_path)
        if item.group == "throughput_profiles"
        and item.request_id
        and str(item.request_id) in started_ids
    ]
    completed_ids = {
        str(item.request_id)
        for item in records
        if item.request_id and str(item.request_id) in started_ids
    }
    planned = RATE_PLAN.planned_requests(MEASUREMENT_WINDOW.measure_sec)
    started = len(started_ids)
    unresolved = admitted_ids - started_ids - cancelled_ids
    write_json(
        REPORT_DIR / "load_window.json",
        {
            "schema_version": 1,
            "status": status,
            "scheduler": "locust.constant_throughput",
            "scheduler_semantics": "closed_loop_capped_throughput",
            "target_rpm": RATE_PLAN.target_rpm,
            "users": RATE_PLAN.users,
            "per_user_rps": RATE_PLAN.per_user_rps,
            "warmup_sec": MEASUREMENT_WINDOW.warmup_sec,
            "measure_sec": MEASUREMENT_WINDOW.measure_sec,
            "measure_started_at": _MEASURE_STARTED_WALL,
            "measure_ended_at": _MEASURE_ENDED_WALL,
            "planned_start_count": planned,
            "admitted_request_count": len(admitted_ids),
            "cancelled_before_send_count": len(cancelled_ids),
            "unresolved_admission_count": len(unresolved),
            "admission_conservation_ok": (
                len(admitted_ids)
                == len(started_ids) + len(cancelled_ids) + len(unresolved)
            ),
            "started_request_count": started,
            "completed_request_count": len(completed_ids),
            "successful_request_count": sum(
                1
                for item in records
                if item.request_id in completed_ids and item.success
            ),
            "unfinished_after_drain_count": len(started_ids - completed_ids),
            "completion_conservation_ok": (
                started
                == len(completed_ids) + len(started_ids - completed_ids)
            ),
            "unscheduled_count": max(planned - started, 0.0),
            "overrun_start_count": max(started - planned, 0.0),
        },
    )


def _transport_target(transport: str, model: str | None = None) -> str:
    interface = get_provider_interface(CONFIG, transport, ACTIVE_PROVIDER)
    path = str(interface.get("path") or "")
    if model is not None:
        path = path.format(model=quote(model, safe=""))
    return build_gemini_api_url(
        str(interface["base_url"]), path,
        api_version=interface.get("api_version") or interface.get("default_api_version"),
    )


def _transport_headers(transport: str, api_key: str) -> dict[str, str]:
    interface = get_provider_interface(CONFIG, transport, ACTIVE_PROVIDER)
    auth = str(interface.get("auth") or "bearer")
    if auth == "anthropic":
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    elif auth == "google_api_key":
        headers = {"x-goog-api-key": api_key, "content-type": "application/json"}
    else:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    version = str(interface.get("anthropic_version") or "").strip()
    if version:
        headers["anthropic-version"] = version
    # Optional JSON object for Cloudflare / gateway quirks (e.g. browser UA).
    raw_extra = str(os.getenv("LOADTEST_HTTP_EXTRA_HEADERS") or "").strip()
    if raw_extra:
        extra = json.loads(raw_extra)
        if not isinstance(extra, dict):
            raise ValueError("LOADTEST_HTTP_EXTRA_HEADERS must be a JSON object")
        headers.update({str(k): str(v) for k, v in extra.items()})
    return headers


def _apply_request_mode(body: dict[str, Any], transport: str) -> None:
    apply_request_mode(body, transport, REQUEST_MODE)


class TargetTokenRateLimiter:
    def __init__(self, tpm: float) -> None:
        self.interval_per_token_sec = 60.0 / tpm
        self._lock = threading.Lock()
        self._next_at = 0.0

    def reserve(self, body: dict[str, Any], estimated_tokens: int | None = None) -> int:
        estimated_tokens = estimated_tokens or estimate_request_tokens(body)
        with self._lock:
            now = time.perf_counter()
            scheduled_at = max(self._next_at, now)
            sleep_for = scheduled_at - now
            self._next_at = scheduled_at + estimated_tokens * self.interval_per_token_sec
        if sleep_for > 0:
            time.sleep(sleep_for)
        return estimated_tokens

    def reconcile(self, estimated_tokens: int, usage: dict[str, Any]) -> None:
        actual_tokens = total_tokens_from_usage(usage)
        if actual_tokens is None:
            return
        adjustment = (actual_tokens - estimated_tokens) * self.interval_per_token_sec
        with self._lock:
            self._next_at = max(time.perf_counter(), self._next_at + adjustment)


TARGET_TOKEN_RATE_LIMITER = TargetTokenRateLimiter(TARGET_TPM) if TARGET_TPM > 0 else None
ADAPTIVE_CONTROLLER = (
    AdaptiveLengthController(
        CONFIG,
        PROVIDER_CFG,
        SELECTED_MODEL,
        TARGET_TOKENS_PER_REQUEST,
    )
    if (
        TARGET_TOKENS_PER_REQUEST > 0
        and WORKLOAD.startswith("throughput")
        and WORKLOAD != "throughput_streaming"
    )
    else None
)

OFFICIAL_PARAM_ROWS = [
    {
        "parameter": "messages",
        "official": "required; system/user/assistant/tool messages",
        "local": "supported",
        "coverage": "all profiles; arbitrary messages pass-through",
    },
    {
        "parameter": "model",
        "official": "deepseek-v4-flash | deepseek-v4-pro",
        "local": "supported",
        "coverage": "models.default or per-profile model override",
    },
    {
        "parameter": "thinking",
        "official": "enabled | disabled; default enabled",
        "local": "supported",
        "coverage": "throughput disabled; compatibility enabled/disabled",
    },
    {
        "parameter": "reasoning_effort",
        "official": "high | max; low/medium->high, xhigh->max",
        "local": "supported",
        "coverage": "thinking_enabled / thinking_max",
    },
    {
        "parameter": "max_tokens",
        "official": "nullable integer",
        "local": "supported",
        "coverage": "default and profile override",
    },
    {
        "parameter": "response_format",
        "official": "text | json_object",
        "local": "supported",
        "coverage": "json_output profile",
    },
    {
        "parameter": "stop",
        "official": "string or up to 16 strings",
        "local": "supported",
        "coverage": "stop_sequences profile",
    },
    {
        "parameter": "stream",
        "official": "boolean SSE stream",
        "local": "supported",
        "coverage": "basic_stream / stream_with_usage",
    },
    {
        "parameter": "stream_options.include_usage",
        "official": "usage chunk before data: [DONE]",
        "local": "supported",
        "coverage": "stream_with_usage profile",
    },
    {
        "parameter": "temperature",
        "official": "0..2",
        "local": "supported",
        "coverage": "sampling_non_thinking profile",
    },
    {
        "parameter": "top_p",
        "official": "0..1",
        "local": "supported",
        "coverage": "sampling_non_thinking profile",
    },
    {
        "parameter": "tools",
        "official": "function tools, max 128; strict beta inside function",
        "local": "supported",
        "coverage": "tool_calls fixture; strict can pass through fixture JSON",
    },
    {
        "parameter": "tool_choice",
        "official": "none | auto | required | named function",
        "local": "supported",
        "coverage": "tool_calls uses required; thinking tools use default auto for Yibu compatibility",
    },
    {
        "parameter": "logprobs",
        "official": "boolean",
        "local": "supported/requested",
        "coverage": "logprobs profile; latest v4-pro smoke returned 200 but omitted logprobs",
    },
    {
        "parameter": "top_logprobs",
        "official": "0..20; requires logprobs=true",
        "local": "supported/requested",
        "coverage": "logprobs profile",
    },
    {
        "parameter": "user_id",
        "official": "a-zA-Z0-9-_ up to 512; KVCache/isolation",
        "local": "supported",
        "coverage": "cache and long_context profiles",
    },
    {
        "parameter": "frequency_penalty",
        "official": "deprecated; no effect",
        "local": "filtered by default",
        "coverage": "allow_deprecated=true can send for probing",
    },
    {
        "parameter": "presence_penalty",
        "official": "deprecated; no effect",
        "local": "filtered by default",
        "coverage": "allow_deprecated=true can send for probing",
    },
    {
        "parameter": "assistant.prefix",
        "official": "beta; requires beta base_url",
        "local": "not profiled",
        "coverage": "out of current scope",
    },
]
OFFICIAL_PARAM_ROWS = param_rows_for_family(MODEL_FAMILY)


class DeepSeekLoadUser(HttpUser):
    host = PROVIDER_CFG.get("base_url", "https://yibuapi.com/v1")
    wait_time = (
        constant_throughput(RATE_PLAN.per_user_rps)
        if RATE_PLAN is not None
        else constant(0)
    )

    def on_start(self) -> None:
        self.timeout_sec = get_timeout_sec(CONFIG)
        api_key = get_api_key(CONFIG, ACTIVE_PROVIDER)
        self.api_key = api_key
        if RATE_PLAN is not None:
            user_index = _claim_rate_user_index()
            epoch = _RATE_EPOCH_MONOTONIC or time.perf_counter()
            scheduled_at = epoch + RATE_PLAN.initial_offset_sec(user_index)
            delay = scheduled_at - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            # Locust's native constant_throughput uses these values to pace
            # subsequent task starts. Reset them after the initial staggering.
            self._cp_last_run = time.time()
            self._cp_last_wait_time = 0

    @task
    def run_weighted_profile(self) -> None:
        _admit_request()
        adaptive_plan: AdaptiveRequestPlan | None = None
        if ADAPTIVE_CONTROLLER is not None:
            group, profile = "throughput_profiles", "adaptive_context"
        else:
            group, profile = random.choices(TASK_CHOICES, weights=TASK_WEIGHTS, k=1)[0]
        if group == "control" and profile == "list_models":
            self._list_models()
            return

        try:
            built = build_request(
                CONFIG,
                group,
                profile,
                api_form_override=MODEL_API_FORM,
                route_profile_override=MODEL_ROUTE_PROFILE,
                reference_source=REFERENCE_CONTRACT_ID,
                pressure_policy_override=MODEL_CAPABILITY,
            )
            if ADAPTIVE_CONTROLLER is not None:
                adaptive_plan = ADAPTIVE_CONTROLLER.apply_to_body(built.body)
            _apply_request_mode(
                built.body,
                str(built.metadata.get("transport") or MODEL_TRANSPORT),
            )
        except Exception as exc:
            self._record_build_failure(group, profile, exc)
            return

        request_profile = (
            f"{profile}:{adaptive_plan.band}" if adaptive_plan is not None else profile
        )
        name = _request_name(group, request_profile)
        transport = str(built.metadata.get("transport") or "chat_completions")
        response_payload = self._post_chat(
            name,
            group,
            request_profile,
            built.body,
            transport=transport,
            adaptive_plan=adaptive_plan,
        )
        if not built.metadata.get("multi_turn") or not response_payload:
            return
        if transport == "claude_messages" and extract_claude_tool_uses(response_payload):
            try:
                followup = build_claude_tool_followup_request(
                    built.body,
                    response_payload,
                )
                self._post_chat(
                    f"{name}:followup",
                    group,
                    profile,
                    followup,
                    validate=False,
                    transport=transport,
                )
            except StopUser:
                raise
            except Exception as exc:
                self._record_build_failure(group, profile, exc, task_name=f"{name}:followup")
        elif extract_tool_calls(response_payload):
            try:
                followup = build_tool_followup_request(
                    built.body,
                    response_payload,
                    pass_reasoning_content=bool(built.metadata.get("pass_reasoning_content")),
                )
                self._post_chat(
                    f"{name}:followup",
                    group,
                    profile,
                    followup,
                    validate=False,
                    transport=transport,
                )
            except StopUser:
                raise
            except Exception as exc:
                self._record_build_failure(group, profile, exc, task_name=f"{name}:followup")

    def _list_models(self) -> None:
        name = "control:list_models" if PHASE != "warmup" else "warmup:list_models"
        target = build_gemini_api_url(
            str(get_provider_interface(CONFIG, "chat_completions", ACTIVE_PROVIDER)["base_url"]),
            "/models",
        )
        headers = _transport_headers("chat_completions", self.api_key)
        _admit_request()
        started = time.perf_counter()
        timestamp = time.time()
        with self.client.get(
            target,
            headers=headers,
            allow_redirects=not is_ai_studio_origin(target),
            timeout=self.timeout_sec,
            name=name,
            catch_response=True,
        ) as response:
            latency_ms = (time.perf_counter() - started) * 1000
            success = 200 <= response.status_code <= 299
            if success:
                response.success()
            else:
                response.failure(f"HTTP {response.status_code}")
            record = RequestRecord(
                timestamp=timestamp,
                task_name=name,
                group="control",
                profile="list_models",
                method="GET",
                path="/v1/models",
                success=success,
                status_code=response.status_code,
                latency_ms=latency_ms,
                response_length=len(response.content or b""),
                error_type=None if success else "http_error",
                failure_classification=classify_failure(response.status_code),
                is_warmup=_is_warmup_timestamp(timestamp),
                extra={
                    "provider": ACTIVE_PROVIDER,
                    "provider_label": PROVIDER_CFG.get("label") or ACTIVE_PROVIDER,
                    "workload": WORKLOAD,
                    "requested_model": SELECTED_MODEL,
                    "model_family": MODEL_FAMILY,
                    "api_form": MODEL_API_FORM,
                    "route_profile": MODEL_ROUTE_PROFILE,
                    "configured_users": CONFIGURED_USERS,
                    "staircase_step": STAIRCASE_STEP,
                    **_context_metadata(),
                },
            )
            RECORDER.record(record)

    def _post_chat(
        self,
        name: str,
        group: str,
        profile: str,
        body: dict[str, Any],
        validate: bool = True,
        transport: str = "chat_completions",
        adaptive_plan: AdaptiveRequestPlan | None = None,
    ) -> dict[str, Any] | None:
        estimated_tokens: int | None = None
        if TARGET_TOKEN_RATE_LIMITER is not None and PHASE != "warmup":
            estimated_tokens = TARGET_TOKEN_RATE_LIMITER.reserve(
                body,
                adaptive_plan.estimated_total_tokens if adaptive_plan else None,
            )
        request_id, timestamp = _begin_request_attempt(
            name, group, profile, transport
        )
        if transport == "claude_messages":
            if body.get("stream"):
                payload = self._post_claude_messages_stream(
                    name,
                    group,
                    profile,
                    body,
                    request_id,
                    timestamp,
                    validate=validate,
                    adaptive_plan=adaptive_plan,
                )
            else:
                payload = self._post_claude_messages_json(
                    name,
                    group,
                    profile,
                    body,
                    request_id,
                    timestamp,
                    validate=validate,
                    adaptive_plan=adaptive_plan,
                )
        elif transport == "gemini_generate_content":
            payload = self._post_gemini_native_json(
                name,
                group,
                profile,
                body,
                request_id,
                timestamp,
                validate=validate,
                adaptive_plan=adaptive_plan,
            )
        elif body.get("stream"):
            payload = self._post_chat_stream(
                name,
                group,
                profile,
                body,
                request_id,
                timestamp,
                validate=validate,
                adaptive_plan=adaptive_plan,
            )
        else:
            payload = self._post_chat_json(
                name,
                group,
                profile,
                body,
                request_id,
                timestamp,
                validate=validate,
                adaptive_plan=adaptive_plan,
            )
        if TARGET_TOKEN_RATE_LIMITER is not None and estimated_tokens is not None and payload is not None:
            TARGET_TOKEN_RATE_LIMITER.reconcile(estimated_tokens, extract_usage(payload))
        if ADAPTIVE_CONTROLLER is not None and adaptive_plan is not None and payload is not None:
            ADAPTIVE_CONTROLLER.feedback(adaptive_plan, extract_usage(payload), transport)
        return payload

    def _post_chat_json(
        self,
        name: str,
        group: str,
        profile: str,
        body: dict[str, Any],
        request_id: str,
        timestamp: float,
        validate: bool = True,
        adaptive_plan: AdaptiveRequestPlan | None = None,
    ) -> dict[str, Any] | None:
        target = _transport_target("chat_completions")
        headers = _transport_headers("chat_completions", self.api_key)
        started = time.perf_counter()
        payload: dict[str, Any] = {}
        error_type: str | None = None
        timestamp = _admit_before_send(request_id)
        with self.client.post(
            target,
            json=body,
            headers=headers,
            allow_redirects=not is_ai_studio_origin(target),
            timeout=self.timeout_sec,
            name=name,
            catch_response=True,
        ) as response:
            latency_ms = (time.perf_counter() - started) * 1000
            try:
                parsed = response.json()
                payload = parsed if isinstance(parsed, dict) else {"data": parsed}
            except ValueError:
                error_type = "json_parse"

            finish_reason = extract_finish_reason(payload)
            usage = extract_usage(payload)
            validation_error = _validate_profile(profile, payload, usage) if validate else None
            failure = _classify_request_failure(
                group,
                response.status_code,
                finish_reason,
                error_type or validation_error,
            )
            success = 200 <= response.status_code <= 299 and failure is None
            if success:
                response.success()
            else:
                response.failure(failure or f"HTTP {response.status_code}")

            RECORDER.record(
                RequestRecord(
                    timestamp=timestamp,
                    task_name=name,
                    group=group,
                    profile=profile,
                    method="POST",
                    path="/v1/chat/completions",
                    success=success,
                    request_id=request_id,
                    completed_at=time.time(),
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    response_length=len(response.content or b""),
                    finish_reason=finish_reason,
                    usage=usage,
                    error_type=error_type,
                    failure_classification=failure,
                    cache_headers=_cache_headers(response.headers),
                    is_warmup=_is_warmup_timestamp(timestamp),
                    extra={
                        "provider": ACTIVE_PROVIDER,
                        "provider_label": PROVIDER_CFG.get("label") or ACTIVE_PROVIDER,
                        "workload": WORKLOAD,
                        "transport": "chat_completions",
                        "request_endpoint": "/chat/completions",
                        "model_family": MODEL_FAMILY,
                        "api_form": MODEL_API_FORM,
                        "route_profile": MODEL_ROUTE_PROFILE,
                        "requested_model": body.get("model"),
                        "response_model": payload.get("model"),
                        "response_id": payload.get("id"),
                        "target_rpm": TARGET_RPM or None,
                        "target_tpm": TARGET_TPM or None,
                        "target_tokens_per_request": TARGET_TOKENS_PER_REQUEST or None,
                        "configured_users": CONFIGURED_USERS,
                        "staircase_step": STAIRCASE_STEP,
                        **_context_metadata(),
                        **(adaptive_plan.record_extra() if adaptive_plan else {}),
                    },
                )
            )
            return payload

    def _post_chat_stream(
        self,
        name: str,
        group: str,
        profile: str,
        body: dict[str, Any],
        request_id: str,
        timestamp: float,
        validate: bool = True,
        adaptive_plan: AdaptiveRequestPlan | None = None,
    ) -> dict[str, Any] | None:
        target = _transport_target("chat_completions")
        headers = _transport_headers("chat_completions", self.api_key)
        started = time.perf_counter()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        raw_lines: list[str] = []
        usage: dict[str, Any] = {}
        finish_reason: str | None = None
        pending_json = ""
        ttft_ms: float | None = None
        error_type: str | None = None

        timestamp = _admit_before_send(request_id)
        with self.client.post(
            target,
            json=body,
            headers=headers,
            allow_redirects=not is_ai_studio_origin(target),
            timeout=self.timeout_sec,
            stream=True,
            name=name,
            catch_response=True,
        ) as response:
            response.encoding = "utf-8"
            for raw_line in response.iter_lines(decode_unicode=True):
                line = _sse_payload_line(raw_line)
                if line is None:
                    continue
                raw_lines.append(line)
                if line == "[DONE]":
                    break
                if pending_json:
                    pending_json += line
                elif line.lstrip().startswith("{"):
                    pending_json = line
                else:
                    continue
                try:
                    chunk = json.loads(pending_json)
                    pending_json = ""
                except json.JSONDecodeError:
                    if len(pending_json) > 65536:
                        error_type = "stream_json_parse"
                        pending_json = ""
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    if ttft_ms is None:
                        ttft_ms = (time.perf_counter() - started) * 1000
                    content_parts.append(delta["content"])
                if delta.get("reasoning_content"):
                    if ttft_ms is None:
                        ttft_ms = (time.perf_counter() - started) * 1000
                    reasoning_parts.append(delta["reasoning_content"])
                if delta.get("tool_calls"):
                    if ttft_ms is None:
                        ttft_ms = (time.perf_counter() - started) * 1000
                    tool_calls.extend(delta["tool_calls"])

            latency_ms = (time.perf_counter() - started) * 1000
            if pending_json and finish_reason is None:
                error_type = error_type or "stream_json_parse"
            payload = {
                "choices": [
                    {
                        "message": {
                            "content": "".join(content_parts),
                            "reasoning_content": "".join(reasoning_parts),
                            "tool_calls": tool_calls,
                        },
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": usage,
            }
            validation_error = _validate_profile(profile, payload, usage) if validate else None
            failure = _classify_request_failure(
                group,
                response.status_code,
                finish_reason,
                error_type or validation_error,
            )
            success = 200 <= response.status_code <= 299 and failure is None
            if success:
                response.success()
            else:
                response.failure(failure or f"HTTP {response.status_code}")

            RECORDER.record(
                RequestRecord(
                    timestamp=timestamp,
                    task_name=name,
                    group=group,
                    profile=profile,
                    method="POST",
                    path="/v1/chat/completions",
                    success=success,
                    request_id=request_id,
                    completed_at=time.time(),
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    ttft_ms=ttft_ms,
                    response_length=len("\n".join(raw_lines).encode("utf-8")),
                    finish_reason=finish_reason,
                    usage=usage,
                    error_type=error_type,
                    failure_classification=failure,
                    cache_headers=_cache_headers(response.headers),
                    is_warmup=_is_warmup_timestamp(timestamp),
                    extra={
                        "provider": ACTIVE_PROVIDER,
                        "provider_label": PROVIDER_CFG.get("label") or ACTIVE_PROVIDER,
                        "workload": WORKLOAD,
                        "transport": "chat_completions",
                        "request_endpoint": "/chat/completions",
                        "model_family": MODEL_FAMILY,
                        "api_form": MODEL_API_FORM,
                        "route_profile": MODEL_ROUTE_PROFILE,
                        "requested_model": body.get("model"),
                        "response_model": payload.get("model"),
                        "target_rpm": TARGET_RPM or None,
                        "target_tpm": TARGET_TPM or None,
                        "target_tokens_per_request": TARGET_TOKENS_PER_REQUEST or None,
                        "configured_users": CONFIGURED_USERS,
                        "staircase_step": STAIRCASE_STEP,
                        **_context_metadata(),
                        **(adaptive_plan.record_extra() if adaptive_plan else {}),
                    },
                )
            )
            return payload

    def _post_gemini_native_json(
        self,
        name: str,
        group: str,
        profile: str,
        body: dict[str, Any],
        request_id: str,
        timestamp: float,
        validate: bool = True,
        adaptive_plan: AdaptiveRequestPlan | None = None,
    ) -> dict[str, Any] | None:
        target = _transport_target("gemini_generate_content", SELECTED_MODEL)
        headers = _transport_headers("gemini_generate_content", self.api_key)
        started = time.perf_counter()
        payload: dict[str, Any] = {}
        error_type: str | None = None
        timestamp = _admit_before_send(request_id)
        with self.client.post(
            target,
            json=body,
            headers=headers,
            allow_redirects=not is_ai_studio_origin(target),
            timeout=self.timeout_sec,
            name=name,
            catch_response=True,
        ) as response:
            latency_ms = (time.perf_counter() - started) * 1000
            try:
                parsed = response.json()
                payload = parsed if isinstance(parsed, dict) else {"data": parsed}
            except ValueError:
                error_type = "json_parse"
            candidates = payload.get("candidates") or []
            candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
            finish_reason = candidate.get("finishReason")
            usage = payload.get("usageMetadata") or {}
            validation_error = _validate_profile(profile, payload, usage) if validate else None
            failure = _classify_request_failure(
                group,
                response.status_code,
                str(finish_reason) if finish_reason else None,
                error_type or validation_error,
            )
            success = 200 <= response.status_code <= 299 and failure is None
            if success:
                response.success()
            else:
                response.failure(failure or f"HTTP {response.status_code}")
            RECORDER.record(
                RequestRecord(
                    timestamp=timestamp,
                    task_name=name,
                    group=group,
                    profile=profile,
                    method="POST",
                    path="/models/{model}:generateContent",
                    success=success,
                    request_id=request_id,
                    completed_at=time.time(),
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    response_length=len(response.content or b""),
                    finish_reason=str(finish_reason) if finish_reason else None,
                    usage=usage if isinstance(usage, dict) else {},
                    error_type=error_type,
                    failure_classification=failure,
                    cache_headers=_cache_headers(response.headers),
                    is_warmup=_is_warmup_timestamp(timestamp),
                    extra={
                        "provider": ACTIVE_PROVIDER,
                        "provider_label": PROVIDER_CFG.get("label") or ACTIVE_PROVIDER,
                        "backend": PROVIDER_CFG.get("backend"),
                        "workload": WORKLOAD,
                        "transport": "gemini_generate_content",
                        "request_endpoint": "/models/{model}:generateContent",
                        "model_family": MODEL_FAMILY,
                        "api_form": MODEL_API_FORM,
                        "route_profile": MODEL_ROUTE_PROFILE,
                        "requested_model": SELECTED_MODEL,
                        "response_model": candidate.get("modelVersion"),
                        "target_rpm": TARGET_RPM or None,
                        "target_tpm": TARGET_TPM or None,
                        "target_tokens_per_request": TARGET_TOKENS_PER_REQUEST or None,
                        "configured_users": CONFIGURED_USERS,
                        "staircase_step": STAIRCASE_STEP,
                        **_context_metadata(),
                        **(adaptive_plan.record_extra() if adaptive_plan else {}),
                    },
                )
            )
            return payload

    def _post_claude_messages_json(
        self,
        name: str,
        group: str,
        profile: str,
        body: dict[str, Any],
        request_id: str,
        timestamp: float,
        validate: bool = True,
        adaptive_plan: AdaptiveRequestPlan | None = None,
    ) -> dict[str, Any] | None:
        target = _transport_target("claude_messages")
        headers = _transport_headers("claude_messages", self.api_key)
        started = time.perf_counter()
        payload: dict[str, Any] = {}
        error_type: str | None = None
        timestamp = _admit_before_send(request_id)
        with self.client.post(
            target,
            json=body,
            headers=headers,
            allow_redirects=not is_ai_studio_origin(target),
            timeout=self.timeout_sec,
            name=name,
            catch_response=True,
        ) as response:
            latency_ms = (time.perf_counter() - started) * 1000
            try:
                parsed = response.json()
                payload = parsed if isinstance(parsed, dict) else {"data": parsed}
            except ValueError:
                error_type = "json_parse"

            finish_reason = extract_finish_reason(payload)
            usage = extract_usage(payload)
            validation_error = _validate_profile(profile, payload, usage) if validate else None
            failure = _classify_request_failure(
                group,
                response.status_code,
                finish_reason,
                error_type or validation_error,
            )
            success = 200 <= response.status_code <= 299 and failure is None
            if success:
                response.success()
            else:
                response.failure(failure or f"HTTP {response.status_code}")

            RECORDER.record(
                RequestRecord(
                    timestamp=timestamp,
                    task_name=name,
                    group=group,
                    profile=profile,
                    method="POST",
                    path="/v1/messages",
                    success=success,
                    request_id=request_id,
                    completed_at=time.time(),
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    response_length=len(response.content or b""),
                    finish_reason=finish_reason,
                    usage=usage,
                    error_type=error_type,
                    failure_classification=failure,
                    cache_headers=_cache_headers(response.headers),
                    is_warmup=_is_warmup_timestamp(timestamp),
                    extra={
                        "provider": ACTIVE_PROVIDER,
                        "provider_label": PROVIDER_CFG.get("label") or ACTIVE_PROVIDER,
                        "workload": WORKLOAD,
                        "transport": "claude_messages",
                        "request_endpoint": "/messages",
                        "model_family": MODEL_FAMILY,
                        "api_form": MODEL_API_FORM,
                        "route_profile": MODEL_ROUTE_PROFILE,
                        "requested_model": body.get("model"),
                        "response_model": payload.get("model"),
                        "response_id": payload.get("id"),
                        "target_rpm": TARGET_RPM or None,
                        "target_tpm": TARGET_TPM or None,
                        "target_tokens_per_request": TARGET_TOKENS_PER_REQUEST or None,
                        "configured_users": CONFIGURED_USERS,
                        "staircase_step": STAIRCASE_STEP,
                        **_context_metadata(),
                        **_claude_stop_metadata(payload),
                        **(adaptive_plan.record_extra() if adaptive_plan else {}),
                    },
                )
            )
            return payload

    def _post_claude_messages_stream(
        self,
        name: str,
        group: str,
        profile: str,
        body: dict[str, Any],
        request_id: str,
        timestamp: float,
        validate: bool = True,
        adaptive_plan: AdaptiveRequestPlan | None = None,
    ) -> dict[str, Any] | None:
        target = _transport_target("claude_messages")
        headers = _transport_headers("claude_messages", self.api_key)
        started = time.perf_counter()
        content_parts: list[str] = []
        content_blocks: list[dict[str, Any]] = []
        raw_lines: list[str] = []
        usage: dict[str, Any] = {}
        finish_reason: str | None = None
        stop_details: dict[str, Any] | None = None
        response_id: str | None = None
        response_model: str | None = None
        pending_json = ""
        ttft_ms: float | None = None
        error_type: str | None = None

        timestamp = _admit_before_send(request_id)
        with self.client.post(
            target,
            json=body,
            headers=headers,
            allow_redirects=not is_ai_studio_origin(target),
            timeout=self.timeout_sec,
            stream=True,
            name=name,
            catch_response=True,
        ) as response:
            response.encoding = "utf-8"
            for raw_line in response.iter_lines(decode_unicode=True):
                line = _sse_payload_line(raw_line)
                if line is None:
                    continue
                raw_lines.append(line)
                if pending_json:
                    pending_json += line
                elif line.lstrip().startswith("{"):
                    pending_json = line
                else:
                    continue
                try:
                    event = json.loads(pending_json)
                    pending_json = ""
                except json.JSONDecodeError:
                    if len(pending_json) > 65536:
                        error_type = "stream_json_parse"
                        pending_json = ""
                    continue

                event_type = event.get("type")
                if event_type == "message_start":
                    message = event.get("message") or {}
                    response_id = message.get("id") or response_id
                    response_model = message.get("model") or response_model
                    if isinstance(message.get("usage"), dict):
                        usage.update(message["usage"])
                elif event_type == "content_block_start":
                    block = event.get("content_block")
                    if isinstance(block, dict):
                        content_blocks.append(block)
                        if block.get("type") in {"text", "tool_use"} and ttft_ms is None:
                            ttft_ms = (time.perf_counter() - started) * 1000
                elif event_type == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        if ttft_ms is None:
                            ttft_ms = (time.perf_counter() - started) * 1000
                        text = str(delta["text"])
                        content_parts.append(text)
                        if content_blocks and content_blocks[-1].get("type") == "text":
                            content_blocks[-1]["text"] = str(content_blocks[-1].get("text") or "") + text
                    elif delta.get("type") == "input_json_delta" and content_blocks:
                        content_blocks[-1]["partial_json"] = (
                            str(content_blocks[-1].get("partial_json") or "")
                            + str(delta.get("partial_json") or "")
                        )
                elif event_type == "message_delta":
                    delta = event.get("delta") or {}
                    finish_reason = delta.get("stop_reason") or finish_reason
                    if isinstance(delta.get("stop_details"), dict):
                        stop_details = delta["stop_details"]
                    if isinstance(event.get("usage"), dict):
                        usage.update(event["usage"])
                elif event_type == "message_stop":
                    break

            latency_ms = (time.perf_counter() - started) * 1000
            if pending_json and finish_reason is None:
                error_type = error_type or "stream_json_parse"
            payload = {
                "id": response_id,
                "content": content_blocks,
                "model": response_model,
                "stop_reason": finish_reason,
                "stop_details": stop_details,
                "usage": usage,
            }
            validation_error = _validate_profile(profile, payload, usage) if validate else None
            failure = _classify_request_failure(
                group,
                response.status_code,
                finish_reason,
                error_type or validation_error,
            )
            success = 200 <= response.status_code <= 299 and failure is None
            if success:
                response.success()
            else:
                response.failure(failure or f"HTTP {response.status_code}")

            RECORDER.record(
                RequestRecord(
                    timestamp=timestamp,
                    task_name=name,
                    group=group,
                    profile=profile,
                    method="POST",
                    path="/v1/messages",
                    success=success,
                    request_id=request_id,
                    completed_at=time.time(),
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    ttft_ms=ttft_ms,
                    response_length=len("\n".join(raw_lines).encode("utf-8")),
                    finish_reason=finish_reason,
                    usage=usage,
                    error_type=error_type,
                    failure_classification=failure,
                    cache_headers=_cache_headers(response.headers),
                    is_warmup=_is_warmup_timestamp(timestamp),
                    extra={
                        "provider": ACTIVE_PROVIDER,
                        "provider_label": PROVIDER_CFG.get("label") or ACTIVE_PROVIDER,
                        "workload": WORKLOAD,
                        "transport": "claude_messages",
                        "request_endpoint": "/messages",
                        "model_family": MODEL_FAMILY,
                        "api_form": MODEL_API_FORM,
                        "route_profile": MODEL_ROUTE_PROFILE,
                        "requested_model": body.get("model"),
                        "response_model": payload.get("model"),
                        "response_id": payload.get("id"),
                        "target_rpm": TARGET_RPM or None,
                        "target_tpm": TARGET_TPM or None,
                        "target_tokens_per_request": TARGET_TOKENS_PER_REQUEST or None,
                        "configured_users": CONFIGURED_USERS,
                        "staircase_step": STAIRCASE_STEP,
                        **_context_metadata(),
                        **_claude_stop_metadata(payload),
                        **(adaptive_plan.record_extra() if adaptive_plan else {}),
                    },
                )
            )
            return payload

    def _record_build_failure(
        self,
        group: str,
        profile: str,
        exc: Exception,
        task_name: str | None = None,
    ) -> None:
        name = task_name or _request_name(group, profile)
        timestamp = time.time()
        self.environment.events.request.fire(
            request_type="BUILD",
            name=name,
            response_time=0,
            response_length=0,
            exception=exc,
        )
        RECORDER.record(
            RequestRecord(
                timestamp=timestamp,
                task_name=name,
                group=group,
                profile=profile,
                method="BUILD",
                path="config",
                success=False,
                error_type=exc.__class__.__name__,
                failure_classification=exc.__class__.__name__,
                is_warmup=_is_warmup_timestamp(timestamp),
                extra={
                    "provider": ACTIVE_PROVIDER,
                    "provider_label": PROVIDER_CFG.get("label") or ACTIVE_PROVIDER,
                    "workload": WORKLOAD,
                    "requested_model": SELECTED_MODEL,
                    "model_family": MODEL_FAMILY,
                    "api_form": MODEL_API_FORM,
                    "route_profile": MODEL_ROUTE_PROFILE,
                    "configured_users": CONFIGURED_USERS,
                    "staircase_step": STAIRCASE_STEP,
                    **_context_metadata(),
                },
            )
        )


@events.quitting.add_listener
def flush_metrics(environment: Any, **kwargs: Any) -> None:
    RECORDER.flush()
    _write_load_window("completed")


@events.init.add_listener
def add_yibu_routes(environment: Any, **kwargs: Any) -> None:
    if not getattr(environment, "web_ui", None):
        return

    from flask import Response, jsonify, request

    @environment.web_ui.app.route("/yibu/summary")
    def yibu_summary() -> Any:
        return jsonify(_live_summary())

    @environment.web_ui.app.route("/yibu/params")
    def yibu_params() -> Any:
        payload = param_spec_payload(MODEL_FAMILY)
        payload.update(
            {
                "provider": ACTIVE_PROVIDER,
                "provider_label": PROVIDER_CFG.get("label") or ACTIVE_PROVIDER,
                "requested_model": SELECTED_MODEL,
                "model_family": MODEL_FAMILY,
                "api_form": MODEL_API_FORM,
                "route_profile": MODEL_ROUTE_PROFILE,
                "profile_weights": CONFIG.get("profile_weights", {}).get("throughput", {}),
                "throughput_profiles": CONFIG.get("throughput_profiles", {}),
            }
        )
        return jsonify(
            payload
        )

    @environment.web_ui.app.route("/yibu")
    def yibu_metrics_page() -> Response:
        html = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Yibu Loadtest Metrics</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #1f2937; }
    h1 { margin: 0 0 16px; font-size: 24px; }
    h2 { margin-top: 28px; font-size: 18px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; max-width: 1000px; }
    .card { border: 1px solid #d1d5db; border-radius: 8px; padding: 14px; }
    .label { color: #6b7280; font-size: 12px; text-transform: uppercase; }
    .value { font-size: 24px; margin-top: 6px; font-weight: 650; }
    table { border-collapse: collapse; width: 100%; max-width: 1200px; font-size: 14px; }
    th, td { border: 1px solid #d1d5db; padding: 8px 10px; text-align: left; vertical-align: top; }
    th { background: #f9fafb; }
    pre { background: #f3f4f6; padding: 14px; border-radius: 8px; overflow: auto; max-width: 1000px; }
    a { color: #2563eb; }
  </style>
</head>
<body>
  <h1>Yibu Loadtest Metrics</h1>
  <div class="grid" id="grid"></div>
  <h2>Supported Parameters vs Official DeepSeek</h2>
  <p>Official source: <a href="https://api-docs.deepseek.com/api/create-chat-completion" target="_blank">DeepSeek Create Chat Completion</a></p>
  <table>
    <thead><tr><th>Parameter</th><th>Official</th><th>Local support</th><th>Coverage / note</th></tr></thead>
    <tbody id="params"></tbody>
  </table>
  <h2>Throughput Profiles</h2>
  <pre id="profiles">loading...</pre>
  <h2>Raw Summary</h2>
  <pre id="raw">loading...</pre>
  <script>
    const fmtPct = v => v === null || v === undefined ? "n/a" : (v * 100).toFixed(2) + "%";
    const fmtNum = v => v === null || v === undefined ? "n/a" : Number(v).toFixed(2);
    const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    async function refreshMetrics() {
      const [s, p] = await Promise.all([
        fetch("/yibu/summary", { cache: "no-store" }).then(r => r.json()),
        fetch("/yibu/params", { cache: "no-store" }).then(r => r.json())
      ]);
      const cards = [
        ["Provider", s.provider_label || s.provider || "n/a"],
        ["Requested model", s.requested_model || "n/a"],
        ["Model family", s.model_family || "n/a"],
        ["Business RPM", fmtNum(s.business_rpm)],
        ["Success rate", fmtPct(s.success_rate)],
        ["Cache hit rate", fmtPct(s.cache_hit_rate)],
        ["Cache hit tokens", s.cache_hit_tokens ?? 0],
        ["Cache miss tokens", s.cache_miss_tokens ?? 0],
        ["Cache eligible req", s.cache_eligible_record_count ?? 0],
        ["Cache min prompt tokens", s.cache_min_prompt_tokens ?? 0],
        ["P95 latency ms", fmtNum(s.p95_latency_ms)]
      ];
      document.getElementById("grid").innerHTML = cards.map(([k, v]) =>
        `<div class="card"><div class="label">${k}</div><div class="value">${v}</div></div>`
      ).join("");
      document.getElementById("params").innerHTML = p.comparison.map(row =>
        `<tr><td>${esc(row.parameter)}</td><td>${esc(row.official)}</td><td>${esc(row.local)}</td><td>${esc(row.coverage)}</td></tr>`
      ).join("");
      document.getElementById("profiles").textContent = JSON.stringify({
        weights: p.profile_weights,
        throughput_profiles: p.throughput_profiles
      }, null, 2);
      document.getElementById("raw").textContent = JSON.stringify(s, null, 2);
    }
    refreshMetrics();
    setInterval(refreshMetrics, 5000);
  </script>
</body>
</html>
"""
        return Response(html, mimetype="text/html")

    @environment.web_ui.app.after_request
    def add_yibu_link(response: Any) -> Any:
        if request.path != "/" or response.status_code != 200:
            return response
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            return response
        html = response.get_data(as_text=True)
        banner = """
<a href="/yibu" target="_blank" style="position:fixed;right:18px;bottom:18px;z-index:99999;background:#111827;color:#fff;padding:10px 14px;border-radius:8px;text-decoration:none;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;box-shadow:0 8px 24px rgba(0,0,0,.18)">Yibu metrics / cache / params</a>
"""
        if "Yibu metrics / cache / params" not in html:
            html = html.replace("</body>", banner + "\n</body>")
            response.set_data(html)
        return response


def _request_name(group: str, profile: str) -> str:
    if PHASE == "warmup":
        return f"warmup:{profile}"
    return f"chat:{group}:{profile}"


def _validate_profile(profile: str, payload: dict[str, Any], usage: dict[str, Any]) -> str | None:
    if profile == "json_output":
        try:
            parsed = json.loads(extract_content(payload))
        except json.JSONDecodeError:
            return "json_parse"
        if not isinstance(parsed, dict):
            return "json_not_object"
    if profile in ("tool_calls", "tool_calls_thinking") and not extract_tool_calls(payload):
        return "tool_calls_missing"
    if profile == "stream_with_usage" and not usage:
        return "stream_usage_missing"
    if profile == "logprobs":
        choices = payload.get("choices") or []
        if not choices or choices[0].get("logprobs") is None:
            return "logprobs_missing"
    return None


def _cache_headers(headers: Any) -> dict[str, str]:
    lower = {str(key).lower(): str(value) for key, value in headers.items()}
    names = ("x-cache", "cf-cache-status", "x-oneapi-cache")
    return {name: lower[name] for name in names if name in lower}


def _live_summary() -> dict[str, Any]:
    records = load_records(RECORDER.records_path)
    summary = summarize_records(
        records,
        business_prefix=str(METRICS.get("business_request_prefix", "chat:")),
        business_group=BUSINESS_GROUP,
        cache_min_prompt_tokens=int(METRICS.get("cache_min_prompt_tokens", 4000)),
    )
    measured = [
        item
        for item in records
        if item.task_name.startswith(str(METRICS.get("business_request_prefix", "chat:")))
        and not item.is_warmup
        and not item.is_retry
        and (BUSINESS_GROUP is None or item.group == BUSINESS_GROUP)
    ]
    profile_counts = Counter(item.profile for item in measured)
    response_models = Counter(
        item.extra.get("response_model")
        for item in measured
        if isinstance(item.extra, dict) and item.extra.get("response_model")
    )
    summary.update(
        {
            "workload": WORKLOAD,
            "phase": PHASE,
            "report_dir": str(REPORT_DIR),
            "provider": ACTIVE_PROVIDER,
            "provider_label": PROVIDER_CFG.get("label") or ACTIVE_PROVIDER,
            "requested_model": SELECTED_MODEL,
            "model_family": MODEL_FAMILY,
            "api_form": MODEL_API_FORM,
            "route_profile": MODEL_ROUTE_PROFILE,
            "model_profile_database": copy.deepcopy(MODEL_PROFILE_DATABASE),
            "profile_counts": dict(profile_counts),
            "response_model_counts": dict(response_models),
        }
    )
    return summary


def _sse_payload_line(raw_line: Any) -> str | None:
    if raw_line is None:
        return None
    if isinstance(raw_line, bytes):
        line = raw_line.decode("utf-8", errors="replace")
    else:
        line = str(raw_line)
        try:
            line = line.encode("latin1").decode("utf-8")
        except UnicodeError:
            pass
    line = line.strip().lstrip("\ufeff")
    if not line or line.startswith(":"):
        return None
    if line.startswith("data:"):
        line = line[len("data:") :].strip()
    elif line.startswith(("event:", "id:", "retry:")):
        return None
    return line or None
