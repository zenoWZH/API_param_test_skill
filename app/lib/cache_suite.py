from __future__ import annotations

import copy
import json
import random
import secrets
import statistics
import time
from pathlib import Path
from typing import Any

from .client import DeepSeekClient
from .config import (
    ensure_dir,
    get_active_provider_name,
    get_model_api_form,
    get_model_family,
    get_model_route_profile,
    get_model_transport,
    get_provider_config,
    get_selected_model,
    resolve_project_path,
)
from .deepseek_params import (
    _claude_native_tools,
    build_claude_tool_followup_request,
    build_native_tool_followup_request,
    build_request,
    build_tool_followup_request,
    cache_tokens_from_usage,
    extract_claude_tool_uses,
    extract_message,
    extract_native_function_calls,
    extract_tool_calls,
    prompt_tokens_from_usage,
)
from .metrics import (
    CACHE_CONTROL_NEGATIVE,
    CACHE_CONTROL_POSITIVE,
    CACHE_CONTROL_ROLE_COLD,
    CACHE_CONTROL_ROLE_UNIQUE,
    CACHE_CONTROL_ROLE_WARM,
    RequestRecord,
    apply_cache_token_audits,
    request_record_from_result,
    summarize_records,
    write_json,
)


def _send_cache_request(client: DeepSeekClient, transport: str, body: dict[str, Any], model: str):
    if transport == "claude_messages":
        return client.claude_messages(body)
    if transport == "gemini_generate_content":
        return client.gemini_generate_content(model, body)
    return client.chat_completion(body)


def run_cache_suite(
    config: dict[str, Any],
    client: DeepSeekClient,
    output_dir: str | Path,
    measured_requests: int | None = None,
) -> dict[str, Any]:
    cache_cfg = config.get("cache_test") or {}
    report_dir = ensure_dir(output_dir)
    provider = get_active_provider_name(config)
    provider_cfg = get_provider_config(config, provider)
    model = get_selected_model(config, provider)
    family = get_model_family(config, model, provider)
    warmup_requests = int(cache_cfg.get("warmup_requests", 2))
    measured_request_count = int(
        measured_requests
        if measured_requests is not None
        else cache_cfg.get("measured_requests", cache_cfg.get("repeat_count", 50))
    )
    if measured_request_count <= 0:
        raise ValueError("measured_requests must be positive")
    wait_after_warmup_sec = float(cache_cfg.get("wait_after_warmup_sec", 5))
    max_tokens = int(cache_cfg.get("max_tokens", 128))
    scenario = str(cache_cfg.get("scenario") or "progressive_customer_session")

    if scenario == "progressive_customer_session":
        return _run_progressive_customer_session_cache_suite(
            config,
            client,
            report_dir,
            provider,
            provider_cfg,
            model,
            family,
            cache_cfg,
        )

    if scenario == "kilocode_agent_session":
        return _run_kilocode_agent_session_cache_suite(
            config,
            client,
            report_dir,
            provider,
            provider_cfg,
            model,
            family,
            cache_cfg,
        )

    if scenario == "shared_prefix":
        return _run_shared_prefix_cache_suite(
            config,
            client,
            report_dir,
            provider,
            provider_cfg,
            model,
            family,
            warmup_requests,
            measured_request_count,
            wait_after_warmup_sec,
            max_tokens,
        )
    if scenario == "growing_conversation":
        return _run_growing_conversation_cache_suite(
            config,
            client,
            report_dir,
            provider,
            provider_cfg,
            model,
            family,
            warmup_requests,
            measured_request_count,
            wait_after_warmup_sec,
            max_tokens,
        )
    raise ValueError(
        "cache_test.scenario must be progressive_customer_session, "
        "kilocode_agent_session, growing_conversation, or shared_prefix"
    )


class _CacheAbort(RuntimeError):
    pass


def _required_control_counts(cache_cfg: dict[str, Any]) -> tuple[int, int]:
    controls = cache_cfg.get("controls") or {}
    mode = str(controls.get("mode") or "auto")
    if mode == "off":
        raise ValueError("cache controls cannot be disabled; positive and negative controls are required")
    if mode == "auto":
        positive = int(
            controls.get(
                "positive_long_prefix_pairs",
                controls.get("auto_positive_long_prefix_pairs", 3),
            )
        )
        negative = int(
            controls.get(
                "negative_unique_prefix_requests",
                controls.get("auto_negative_unique_prefix_requests", 3),
            )
        )
    elif mode == "custom":
        positive = int(controls.get("positive_long_prefix_pairs", 0))
        negative = int(controls.get("negative_unique_prefix_requests", 0))
    else:
        raise ValueError("cache controls mode must be auto or custom")
    if positive <= 0 or negative <= 0:
        raise ValueError("cache tests require at least one positive pair and one negative request")
    return positive, negative


class _CacheBudget:
    def __init__(self, max_run_seconds: int, failure_limit: int) -> None:
        self.deadline = time.monotonic() + max_run_seconds
        self.failure_limit = failure_limit
        self.consecutive_failures = 0

    def before_request(self) -> None:
        if time.monotonic() >= self.deadline:
            raise _CacheAbort("max_run_seconds_exceeded")
        if self.consecutive_failures >= self.failure_limit:
            raise _CacheAbort("consecutive_failure_limit_reached")

    def observe(self, success: bool) -> None:
        self.consecutive_failures = 0 if success else self.consecutive_failures + 1


def _run_progressive_customer_session_cache_suite(
    config: dict[str, Any],
    client: DeepSeekClient,
    report_dir: Path,
    provider: str,
    provider_cfg: dict[str, Any],
    model: str,
    family: str,
    cache_cfg: dict[str, Any],
) -> dict[str, Any]:
    records: list[RequestRecord] = []
    events: list[dict[str, Any]] = []
    seed = int(cache_cfg.get("seed", 20260715))
    rng = random.Random(seed)
    control_run_nonce = secrets.token_hex(16)
    stable_system = str(
        cache_cfg.get("stable_system")
        or "你是企业客服助手。回答应准确、简洁；需要外部信息时调用提供的工具，不要编造工具结果。"
    )
    stable_system = f"cache-run-{control_run_nonce}: {stable_system}"
    sessions_count = int(cache_cfg.get("sessions", 10))
    rounds_per_session = int(cache_cfg.get("rounds_per_session", 4))
    configured_profiles = cache_cfg.get("content_profiles") or {}
    ranges = (
        cache_cfg.get("resolved_content_ranges")
        or cache_cfg.get("content_ranges")
        or configured_profiles.get(str(cache_cfg.get("content_profile") or "realistic"))
        or {}
    )
    user_chars = ranges.get("user_chars") or {"min": 200, "max": 2000}
    tool_result_chars = ranges.get("tool_result_chars") or {"min": 500, "max": 5000}
    tool_stage = cache_cfg.get("tool_stage") or {}
    tool_enabled = bool(tool_stage.get("enabled", True))
    tool_round = int(tool_stage.get("round", 3))
    structure_probe_enabled = bool(
        (cache_cfg.get("structure_probe") or {}).get("enabled", True)
    )
    positive_pairs, negative_requests = _required_control_counts(cache_cfg)
    max_tokens = int(cache_cfg.get("max_tokens", 128))
    total_steps = int(
        cache_cfg.get("estimated_request_count")
        or sessions_count * (rounds_per_session + (1 if tool_enabled else 0))
        + (1 if structure_probe_enabled else 0)
        + positive_pairs * 2
        + negative_requests
    )
    completed_steps = 0
    budget = _CacheBudget(
        int(cache_cfg.get("max_run_seconds", 1800)),
        int(cache_cfg.get("consecutive_failure_limit", 3)),
    )
    transport = _customer_transport(config, provider, model)
    request_path = _transport_path(transport)
    profile = _tool_profile(family, transport) if tool_enabled else "cache_long_context"
    group = "compatibility_profiles" if tool_enabled else "cache_profiles"
    built = build_request(config, group, profile, overrides={"max_tokens": max_tokens})
    aborted_reason: str | None = None
    session_states: list[dict[str, Any]] = []
    _write_cache_progress(report_dir, "starting", 0, total_steps)

    def send_and_record(
        *,
        body: dict[str, Any],
        stage: str,
        extra: dict[str, Any],
        task_name: str | None = None,
        record_profile: str | None = None,
    ) -> tuple[Any, RequestRecord]:
        nonlocal completed_steps
        budget.before_request()
        result = _send_cache_request(client, transport, body, model)
        record = request_record_from_result(
            result=result,
            task_name=task_name or f"cache:progressive:{stage}",
            group="cache_profiles",
            profile=record_profile or f"progressive_{stage}",
            path=request_path,
            phase=f"cache_{stage}",
            extra={
                "provider": provider,
                "provider_label": provider_cfg.get("label") or provider,
                "backend": provider_cfg.get("backend"),
                "transport": transport,
                "request_endpoint": request_path,
                "requested_model": model,
                "model_family": family,
                **_route_metadata(config, provider, model),
                "cache_scenario": "progressive_customer_session",
                "cache_stage": stage,
                "cache_case": stage,
                "seed": seed,
                **extra,
            },
        )
        records.append(record)
        events.append(_event_from_record(record))
        completed_steps += 1
        budget.observe(record.success)
        _write_records(report_dir / "request_records.jsonl", records)
        _write_cache_progress(report_dir, f"cache_{stage}", completed_steps, total_steps)
        return result, record

    def stop_session(
        state: dict[str, Any],
        record: RequestRecord,
        reason: str,
        detail: str | None = None,
    ) -> None:
        state["active"] = False
        state["stop_reason"] = reason
        record.extra["session_stop_reason"] = reason
        if detail:
            record.extra["session_stop_detail"] = detail
        _write_records(report_dir / "request_records.jsonl", records)

    try:
        # Seed every independent customer session first so the cache has the
        # same readiness interval before any session is extended.
        for session_index in range(1, sessions_count + 1):
            user = _progressive_user_text(
                rng,
                seed,
                session_index,
                1,
                "seed",
                user_chars,
            )
            body = _customer_body(
                built.body,
                transport,
                stable_system,
                user,
                max_tokens,
            )
            result, record = send_and_record(
                body=body,
                stage="seed",
                extra={"session_index": session_index, "round_index": 1},
            )
            state = {
                "session_index": session_index,
                "body": body,
                "last_result": result,
                "previous_prompt_tokens": prompt_tokens_from_usage(result.usage or {}),
                "active": bool(record.success),
                "completed": False,
                "stop_reason": None if record.success else "request_failed",
                "tool_flow_status": "not_evaluated" if tool_enabled else "disabled",
            }
            session_states.append(state)
            if not record.success:
                record.extra["session_stop_reason"] = "request_failed"
            elif _response_has_tool_call(transport, result.response_json or {}):
                stop_session(state, record, "unexpected_tool_call")

        wait_after_seed = float(cache_cfg.get("wait_after_seed_sec", 5))
        if any(state["active"] for state in session_states) and wait_after_seed > 0:
            _write_cache_progress(report_dir, "cache_seed_wait", completed_steps, total_steps)
            time.sleep(wait_after_seed)

        # Advance all sessions one round at a time to reduce ordering bias.
        for round_index in range(2, rounds_per_session + 1):
            for state in session_states:
                if not state["active"]:
                    continue
                session_index = int(state["session_index"])
                is_tool_round = tool_enabled and round_index == tool_round
                stage = (
                    "tool_initial"
                    if is_tool_round
                    else "final_growth" if round_index == rounds_per_session else "direct_growth"
                )
                user = _progressive_user_text(
                    rng,
                    seed,
                    session_index,
                    round_index,
                    stage,
                    user_chars,
                )
                try:
                    body = _append_response_and_user(
                        state["body"],
                        transport,
                        state["last_result"],
                        user,
                    )
                    _assert_strict_conversation_extension(state["body"], body, transport)
                except ValueError as exc:
                    state["active"] = False
                    state["stop_reason"] = "malformed_assistant_response"
                    state["stop_detail"] = str(exc)
                    continue

                previous_prompt_tokens = state["previous_prompt_tokens"]
                result, record = send_and_record(
                    body=body,
                    stage=stage,
                    extra={
                        "session_index": session_index,
                        "round_index": round_index,
                        "strict_prefix_extension": True,
                        "reusable_prefix_tokens": previous_prompt_tokens,
                    },
                )
                state["body"] = body
                state["last_result"] = result
                state["previous_prompt_tokens"] = prompt_tokens_from_usage(result.usage or {})
                if not record.success:
                    stop_session(state, record, "request_failed")
                    continue

                if is_tool_round:
                    state["tool_flow_status"] = "evaluating"
                    try:
                        followup = _tool_followup_body(
                            transport,
                            body,
                            result.response_json or {},
                        )
                        _assert_strict_conversation_extension(body, followup, transport)
                        tool_result = _progressive_tool_result_text(
                            rng,
                            seed,
                            session_index,
                            tool_result_chars,
                        )
                        _replace_tool_results(followup, transport, tool_result)
                    except ValueError as exc:
                        state["tool_flow_status"] = "unsupported"
                        record.extra["tool_flow_status"] = "unsupported"
                        stop_session(state, record, "tool_flow_unsupported", str(exc))
                        continue

                    followup_result, followup_record = send_and_record(
                        body=followup,
                        stage="tool_followup",
                        extra={
                            "session_index": session_index,
                            "round_index": round_index,
                            "strict_prefix_extension": True,
                            "reusable_prefix_tokens": state["previous_prompt_tokens"],
                            "tool_flow_status": "supported",
                        },
                    )
                    state["body"] = followup
                    state["last_result"] = followup_result
                    state["previous_prompt_tokens"] = prompt_tokens_from_usage(
                        followup_result.usage or {}
                    )
                    if not followup_record.success:
                        state["tool_flow_status"] = "request_failed"
                        stop_session(state, followup_record, "request_failed")
                        continue
                    state["tool_flow_status"] = "supported"
                    if _response_has_tool_call(
                        transport, followup_result.response_json or {}
                    ):
                        stop_session(state, followup_record, "unexpected_tool_call")
                        continue
                    if round_index == rounds_per_session:
                        state["completed"] = True
                        followup_record.extra["session_completed"] = True
                else:
                    if _response_has_tool_call(transport, result.response_json or {}):
                        stop_session(state, record, "unexpected_tool_call")
                        continue
                    if round_index == rounds_per_session:
                        state["completed"] = True
                        record.extra["session_completed"] = True

            _write_records(report_dir / "request_records.jsonl", records)

        if structure_probe_enabled:
            structure_body = _customer_body(
                built.body,
                transport,
                stable_system,
                "x",
                1,
            )
            send_and_record(
                body=structure_body,
                stage="structure_probe",
                task_name="cache:structure_probe",
                record_profile="progressive_structure_probe",
                extra={
                    "cache_structure_probe": "stable_system_and_tools",
                    "structure_probe_user_chars": 1,
                },
            )

        cold_bodies: list[dict[str, Any]] = []
        for pair in range(positive_pairs):
            control_request = build_request(
                config,
                "cache_profiles",
                "cache_long_context",
                overrides={"max_tokens": max_tokens},
            )
            body = _customer_body(
                control_request.body,
                transport,
                f"positive-control-{control_run_nonce}-{pair}: {stable_system}",
                _long_control_text(config, pair),
                max_tokens,
            )
            cold_bodies.append(copy.deepcopy(body))
            send_and_record(
                body=body,
                stage="control_cold",
                task_name="cache:control:positive:cold",
                record_profile="positive_long_prefix_control",
                extra={
                    "cache_control": CACHE_CONTROL_POSITIVE,
                    "control_role": CACHE_CONTROL_ROLE_COLD,
                    "control_pair": pair,
                    "control_run_nonce": control_run_nonce,
                },
            )
        if cold_bodies and wait_after_seed > 0:
            _write_cache_progress(report_dir, "cache_control_wait", completed_steps, total_steps)
            time.sleep(wait_after_seed)
        for pair, body in enumerate(cold_bodies):
            send_and_record(
                body=body,
                stage="control_warm",
                task_name="cache:control:positive:warm",
                record_profile="positive_long_prefix_control",
                extra={
                    "cache_control": CACHE_CONTROL_POSITIVE,
                    "control_role": CACHE_CONTROL_ROLE_WARM,
                    "control_pair": pair,
                    "control_run_nonce": control_run_nonce,
                },
            )

        for index in range(negative_requests):
            control_request = build_request(
                config,
                "cache_profiles",
                "cache_long_context",
                overrides={"max_tokens": max_tokens},
            )
            body = _customer_body(
                control_request.body,
                transport,
                f"negative-control-{control_run_nonce}-{index}-{secrets.token_hex(16)}",
                _variable_customer_text(
                    rng,
                    f"negative-user-{rng.getrandbits(128):032x}",
                    {"min": 4500, "max": 5500},
                ),
                max_tokens,
            )
            send_and_record(
                body=body,
                stage="control_negative",
                task_name="cache:control:negative",
                record_profile="negative_unique_prefix_control",
                extra={
                    "cache_control": CACHE_CONTROL_NEGATIVE,
                    "control_role": CACHE_CONTROL_ROLE_UNIQUE,
                    "control_pair": index,
                    "control_run_nonce": control_run_nonce,
                },
            )
    except _CacheAbort as exc:
        aborted_reason = str(exc)

    session_outcomes = [
        {
            "session_index": state["session_index"],
            "status": (
                "completed"
                if state["completed"]
                else "aborted" if aborted_reason and state["active"] else "stopped"
            ),
            "stop_reason": state.get("stop_reason") or (aborted_reason if state["active"] else None),
            "tool_flow_status": state["tool_flow_status"],
        }
        for state in session_states
    ]
    result = _finalize_cache_suite(config, report_dir, records, events, total_steps)
    result["schema_version"] = 10
    result["scenario"] = "progressive_customer_session"
    result["aborted_reason"] = aborted_reason
    result["latency_speedup_ratio"] = _positive_control_latency_speedup(records)
    result["effective_cache_plan"] = cache_cfg
    result["session_outcomes"] = session_outcomes
    result["actual_request_count"] = len(records)
    write_json(report_dir / "cache_results.json", result)
    if aborted_reason:
        _write_cache_progress(report_dir, "aborted", completed_steps, total_steps)
    return result


def _run_kilocode_agent_session_cache_suite(
    config: dict[str, Any],
    client: DeepSeekClient,
    report_dir: Path,
    provider: str,
    provider_cfg: dict[str, Any],
    model: str,
    family: str,
    cache_cfg: dict[str, Any],
) -> dict[str, Any]:
    records: list[RequestRecord] = []
    events: list[dict[str, Any]] = []
    seed = int(cache_cfg.get("seed", 20260715))
    rng = random.Random(seed)
    control_run_nonce = secrets.token_hex(16)
    max_tokens = int(cache_cfg.get("max_tokens", 128))
    steps = int(cache_cfg.get("steps", 20))
    trajectory_mode = str(cache_cfg.get("trajectory_mode") or "scripted")
    warmup_requests = int(cache_cfg.get("warmup_requests", 1))
    positive_pairs, negative_requests = _required_control_counts(cache_cfg)
    wait_after_seed = float(cache_cfg.get("wait_after_seed_sec", 5))
    total_steps = int(
        cache_cfg.get("estimated_request_count")
        or warmup_requests + steps + positive_pairs * 2 + negative_requests
    )
    completed_steps = 0
    budget = _CacheBudget(
        int(cache_cfg.get("max_run_seconds", 1800)),
        int(cache_cfg.get("consecutive_failure_limit", 3)),
    )
    transport = _customer_transport(config, provider, model)
    request_path = _transport_path(transport)
    system_prompt, tools, result_fixture_text = _load_kilocode_fixtures(cache_cfg)
    system_prompt = f"cache-run-{control_run_nonce}: {system_prompt}"
    if trajectory_mode == "random":
        trajectory = _random_trajectory(steps, rng)
    else:
        trajectory = _scripted_trajectory(steps)
    task_text = (
        "分析该代码库结构并修复 lint 错误。先了解项目布局，再定位 lint 失败点，"
        "逐项修复并验证。你可以使用提供的工具读取文件、搜索代码并执行命令。"
    )
    profile = _tool_profile(family, transport)
    built = build_request(
        config,
        "compatibility_profiles",
        profile,
        overrides={"max_tokens": max_tokens},
    )
    base_body = _kilocode_base_body(
        built.body,
        transport,
        system_prompt,
        tools,
        task_text,
        max_tokens,
        temperature=cache_cfg.get("temperature", 0),
    )
    aborted_reason: str | None = None
    step_records: list[dict[str, Any]] = []
    _write_cache_progress(report_dir, "starting", 0, total_steps)

    def send_and_record(
        *,
        body: dict[str, Any],
        stage: str,
        extra: dict[str, Any],
        task_name: str | None = None,
        record_profile: str | None = None,
        is_warmup: bool = False,
    ) -> tuple[Any, RequestRecord]:
        nonlocal completed_steps
        budget.before_request()
        result = _send_cache_request(client, transport, body, model)
        record = request_record_from_result(
            result=result,
            task_name=task_name or f"cache:kilocode:{stage}",
            group="cache_profiles",
            profile=record_profile or f"kilocode_{stage}",
            path=request_path,
            is_warmup=is_warmup,
            phase=f"cache_{stage}",
            extra={
                "provider": provider,
                "provider_label": provider_cfg.get("label") or provider,
                "backend": provider_cfg.get("backend"),
                "transport": transport,
                "request_endpoint": request_path,
                "requested_model": model,
                        "model_family": family,
                        **_route_metadata(config, provider, model),
                "cache_scenario": "kilocode_agent_session",
                "cache_stage": stage,
                "seed": seed,
                **extra,
            },
        )
        records.append(record)
        events.append(_event_from_record(record))
        completed_steps += 1
        budget.observe(record.success)
        _write_records(report_dir / "request_records.jsonl", records)
        _write_cache_progress(report_dir, f"cache_{stage}", completed_steps, total_steps)
        return result, record

    try:
        previous_prompt_tokens: int | None = None
        for _index in range(warmup_requests):
            result, record = send_and_record(
                body=copy.deepcopy(base_body),
                stage="warmup",
                is_warmup=True,
                extra={"trajectory_mode": trajectory_mode},
            )
            if record.success:
                previous_prompt_tokens = prompt_tokens_from_usage(result.usage or {})
        if warmup_requests and wait_after_seed > 0:
            _write_cache_progress(report_dir, "cache_seed_wait", completed_steps, total_steps)
            time.sleep(wait_after_seed)

        body = copy.deepcopy(base_body)
        for step_index, step in enumerate(trajectory, start=1):
            call = {
                "id": f"call_{seed}_{step_index}",
                "name": step["tool"],
                "arguments": step["arguments"],
            }
            result_text = _kilocode_tool_result_text(
                result_fixture_text,
                step["tool"],
                step_index,
                int(step["result_chars"]),
            )
            previous_body = body
            body = _append_scripted_tool_exchange(
                body,
                call,
                result_text,
                step["instruction"],
                transport,
            )
            _assert_strict_conversation_extension(previous_body, body, transport)
            result, record = send_and_record(
                body=body,
                stage="step",
                extra={
                    "step_index": step_index,
                    "trajectory_mode": trajectory_mode,
                    "tool": step["tool"],
                    "strict_prefix_extension": True,
                    "reusable_prefix_tokens": previous_prompt_tokens,
                },
            )
            prompt_tokens = prompt_tokens_from_usage(result.usage or {})
            hit, miss = cache_tokens_from_usage(result.usage or {})
            step_records.append(
                {
                    "step_index": step_index,
                    "tool": step["tool"],
                    "success": record.success,
                    "prompt_tokens": prompt_tokens,
                    "cache_hit_tokens": hit,
                    "cache_miss_tokens": miss,
                    "latency_ms": record.latency_ms,
                }
            )
            if record.success and prompt_tokens is not None:
                previous_prompt_tokens = prompt_tokens
            if not record.success:
                break

        cold_bodies: list[dict[str, Any]] = []
        for pair in range(positive_pairs):
            control_request = build_request(
                config,
                "cache_profiles",
                "cache_long_context",
                overrides={"max_tokens": max_tokens},
            )
            control_body = _customer_body(
                control_request.body,
                transport,
                f"positive-control-{control_run_nonce}-{pair}: {system_prompt}",
                _long_control_text(config, pair),
                max_tokens,
            )
            cold_bodies.append(copy.deepcopy(control_body))
            send_and_record(
                body=control_body,
                stage="control_cold",
                task_name="cache:control:positive:cold",
                record_profile="positive_long_prefix_control",
                extra={
                    "cache_control": CACHE_CONTROL_POSITIVE,
                    "control_role": CACHE_CONTROL_ROLE_COLD,
                    "control_pair": pair,
                    "control_run_nonce": control_run_nonce,
                },
            )
        if cold_bodies and wait_after_seed > 0:
            _write_cache_progress(report_dir, "cache_control_wait", completed_steps, total_steps)
            time.sleep(wait_after_seed)
        for pair, control_body in enumerate(cold_bodies):
            send_and_record(
                body=control_body,
                stage="control_warm",
                task_name="cache:control:positive:warm",
                record_profile="positive_long_prefix_control",
                extra={
                    "cache_control": CACHE_CONTROL_POSITIVE,
                    "control_role": CACHE_CONTROL_ROLE_WARM,
                    "control_pair": pair,
                    "control_run_nonce": control_run_nonce,
                },
            )

        for index in range(negative_requests):
            control_request = build_request(
                config,
                "cache_profiles",
                "cache_long_context",
                overrides={"max_tokens": max_tokens},
            )
            unique_system = f"negative-control-{control_run_nonce}-{index}-{secrets.token_hex(16)}"
            unique_user = _variable_customer_text(
                rng,
                f"negative-user-{rng.getrandbits(128):032x}",
                {"min": 4500, "max": 5500},
            )
            control_body = _customer_body(
                control_request.body,
                transport,
                unique_system,
                unique_user,
                max_tokens,
            )
            send_and_record(
                body=control_body,
                stage="control_negative",
                task_name="cache:control:negative",
                record_profile="negative_unique_prefix_control",
                extra={
                    "cache_control": CACHE_CONTROL_NEGATIVE,
                    "control_role": CACHE_CONTROL_ROLE_UNIQUE,
                    "control_pair": index,
                    "control_run_nonce": control_run_nonce,
                },
            )
    except _CacheAbort as exc:
        aborted_reason = str(exc)

    result = _finalize_cache_suite(config, report_dir, records, events, total_steps)
    result["schema_version"] = 11
    result["scenario"] = "kilocode_agent_session"
    result["aborted_reason"] = aborted_reason
    result["latency_speedup_ratio"] = _positive_control_latency_speedup(records)
    result["effective_cache_plan"] = cache_cfg
    result["actual_request_count"] = len(records)
    result["step_records"] = step_records
    write_json(report_dir / "cache_results.json", result)
    _write_cache_progress(
        report_dir,
        "aborted" if aborted_reason else "completed",
        completed_steps,
        total_steps,
    )
    return result


_KILOCODE_DEFAULT_FIXTURES = {
    "system_prompt_fixture": "fixtures/kilocode_system_prompt.txt",
    "tools_fixture": "fixtures/kilocode_tools.json",
    "result_fixture": "fixtures/long_context.txt",
}


def _load_kilocode_fixtures(
    cache_cfg: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], str]:
    system_path = str(
        cache_cfg.get("system_prompt_fixture")
        or _KILOCODE_DEFAULT_FIXTURES["system_prompt_fixture"]
    )
    tools_path = str(
        cache_cfg.get("tools_fixture") or _KILOCODE_DEFAULT_FIXTURES["tools_fixture"]
    )
    result_path = str(
        cache_cfg.get("result_fixture")
        or cache_cfg.get("fixture")
        or _KILOCODE_DEFAULT_FIXTURES["result_fixture"]
    )
    system_prompt = resolve_project_path(system_path).read_text(encoding="utf-8")
    tools = json.loads(resolve_project_path(tools_path).read_text(encoding="utf-8"))
    if not isinstance(tools, list) or not tools:
        raise ValueError("kilocode tools fixture must be a non-empty JSON array.")
    result_text = resolve_project_path(result_path).read_text(encoding="utf-8")
    return system_prompt, tools, result_text


_KILOCODE_SCRIPTED_SIZES: list[tuple[str, int]] = [
    ("glob", 500),
    ("grep", 2000),
    ("read", 20000),
    ("read", 18000),
    ("grep", 2000),
    ("read", 15000),
    ("read", 20000),
    ("bash", 3000),
    ("edit", 500),
    ("bash", 4000),
    ("read", 18000),
    ("grep", 2500),
    ("read", 20000),
    ("bash", 3000),
    ("read", 15000),
    ("grep", 3000),
    ("read", 20000),
    ("bash", 4000),
    ("read", 18000),
    ("grep", 2000),
]

_KILOCODE_RANDOM_WEIGHTS: list[tuple[str, int, tuple[int, int]]] = [
    ("read", 40, (15000, 20000)),
    ("grep", 20, (2000, 3000)),
    ("bash", 20, (2000, 4000)),
    ("glob", 10, (500, 500)),
    ("edit", 10, (500, 500)),
]


def _kilocode_tool_arguments(tool: str, step_index: int) -> dict[str, Any]:
    if tool == "glob":
        return {"pattern": "lib/**/*.py", "path": "."}
    if tool == "grep":
        return {
            "pattern": f"def _[a-z_]+\\({step_index}",
            "path": "lib",
            "include": "*.py",
        }
    if tool == "bash":
        return {
            "command": f"python -m pytest tests/test_cache_suite.py -q -k step{step_index}",
            "workdir": ".",
        }
    if tool == "edit":
        return {
            "filePath": "lib/cache_suite.py",
            "oldString": f"step_{step_index}_old",
            "newString": f"step_{step_index}_new",
        }
    return {
        "filePath": "fixtures/long_context.txt",
        "offset": 1 + step_index * 40,
        "limit": 200 + step_index * 10,
    }


def _kilocode_step_instruction(tool: str, step_index: int, steps: int) -> str:
    return (
        f"Step {step_index}/{steps}: 继续执行代码库分析任务。基于上面 {tool} 工具返回的结果，"
        "判断下一步动作：若已定位 lint 错误则修复并验证，否则继续读取相关源码。"
        "保持当前步输出简短，不超过 120 字，不要重复已有结论。"
    )


def _scripted_trajectory(steps: int) -> list[dict[str, Any]]:
    trajectory: list[dict[str, Any]] = []
    for step_index in range(1, steps + 1):
        tool, result_chars = _KILOCODE_SCRIPTED_SIZES[
            (step_index - 1) % len(_KILOCODE_SCRIPTED_SIZES)
        ]
        trajectory.append(
            {
                "tool": tool,
                "arguments": _kilocode_tool_arguments(tool, step_index),
                "result_chars": result_chars,
                "instruction": _kilocode_step_instruction(tool, step_index, steps),
            }
        )
    return trajectory


def _random_trajectory(steps: int, rng: random.Random) -> list[dict[str, Any]]:
    total_weight = sum(weight for _tool, weight, _size in _KILOCODE_RANDOM_WEIGHTS)
    trajectory: list[dict[str, Any]] = []
    for step_index in range(1, steps + 1):
        pick = rng.uniform(0, total_weight)
        cumulative = 0.0
        tool, size_range = "read", (15000, 20000)
        for candidate, weight, candidate_range in _KILOCODE_RANDOM_WEIGHTS:
            cumulative += weight
            if pick <= cumulative:
                tool, size_range = candidate, candidate_range
                break
        trajectory.append(
            {
                "tool": tool,
                "arguments": _kilocode_tool_arguments(tool, step_index),
                "result_chars": rng.randint(size_range[0], size_range[1]),
                "instruction": _kilocode_step_instruction(tool, step_index, steps),
            }
        )
    return trajectory


def _kilocode_tool_result_text(
    fixture_text: str,
    tool: str,
    step_index: int,
    size: int,
) -> str:
    header = f"<tool_result tool=\"{tool}\" step=\"{step_index}\">\n"
    footer = "\n</tool_result>"
    budget = max(size - len(header) - len(footer), 0)
    if not fixture_text:
        body = ""
    elif len(fixture_text) >= budget:
        span = len(fixture_text) - budget
        offset = (step_index * 977) % span if span > 0 else 0
        body = fixture_text[offset : offset + budget]
    else:
        body = (fixture_text * (budget // len(fixture_text) + 1))[:budget]
    return f"{header}{body}{footer}"


def _kilocode_gemini_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        converted: dict[str, Any] = {}
        for key, value in schema.items():
            if key == "type" and isinstance(value, str):
                converted[key] = value.upper()
            else:
                converted[key] = _kilocode_gemini_schema(value)
        return converted
    if isinstance(schema, list):
        return [_kilocode_gemini_schema(item) for item in schema]
    return schema


def _kilocode_gemini_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict):
            raise ValueError("kilocode tools fixture must use OpenAI function format.")
        declarations.append(
            {
                "name": str(function.get("name") or ""),
                "description": str(function.get("description") or ""),
                "parameters": _kilocode_gemini_schema(
                    copy.deepcopy(function.get("parameters") or {})
                ),
            }
        )
    return [{"functionDeclarations": declarations}]


def _kilocode_base_body(
    original: dict[str, Any],
    transport: str,
    system_prompt: str,
    tools: list[dict[str, Any]],
    task_text: str,
    max_tokens: int,
    temperature: float | int | None = 0,
) -> dict[str, Any]:
    body = copy.deepcopy(original)
    if transport == "gemini_generate_content":
        body["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        body["contents"] = [{"role": "user", "parts": [{"text": task_text}]}]
        body["tools"] = _kilocode_gemini_tools(tools)
        generation = body.setdefault("generationConfig", {})
        generation["maxOutputTokens"] = max_tokens
        if temperature is not None:
            generation["temperature"] = temperature
        return body
    if transport == "claude_messages":
        body["system"] = system_prompt
        body["messages"] = [{"role": "user", "content": task_text}]
        body["tools"] = _claude_native_tools(tools)
        body["max_tokens"] = max_tokens
        body["stream"] = False
        return body
    body["messages"] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_text},
    ]
    body["tools"] = copy.deepcopy(tools)
    body["max_tokens"] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    else:
        body.pop("temperature", None)
    body["stream"] = False
    body.pop("stream_options", None)
    return body


def _append_scripted_tool_exchange(
    body: dict[str, Any],
    call: dict[str, Any],
    result_text: str,
    instruction: str,
    transport: str,
) -> dict[str, Any]:
    # The conversation is append-only and existing entries are never mutated,
    # so the prefix is shared between steps: the outer body is a shallow copy
    # and only the newly appended entries are constructed fresh. This keeps
    # per-step copying and prefix comparison O(appended entries) instead of
    # O(accumulated conversation size).
    updated = dict(body)
    if transport == "gemini_generate_content":
        contents = list(updated.get("contents") or [])
        contents.append(
            {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {
                            "name": call["name"],
                            "args": copy.deepcopy(call["arguments"]),
                        }
                    }
                ],
            }
        )
        contents.append(
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": call["name"],
                            "response": {"content": result_text},
                        }
                    },
                    {"text": instruction},
                ],
            }
        )
        updated["contents"] = contents
        return updated
    if transport == "claude_messages":
        messages = list(updated.get("messages") or [])
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": copy.deepcopy(call["arguments"]),
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "content": result_text,
                    },
                    {"type": "text", "text": instruction},
                ],
            }
        )
        updated["messages"] = messages
        return updated
    messages = list(updated.get("messages") or [])
    messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(
                            call["arguments"], ensure_ascii=False
                        ),
                    },
                }
            ],
        }
    )
    messages.append(
        {"role": "tool", "tool_call_id": call["id"], "content": result_text}
    )
    messages.append({"role": "user", "content": instruction})
    updated["messages"] = messages
    updated["stream"] = False
    updated.pop("stream_options", None)
    return updated


def _progressive_user_text(
    rng: random.Random,
    seed: int,
    session_index: int,
    round_index: int,
    stage: str,
    length_range: dict[str, Any],
) -> str:
    prompts = {
        "seed": "请根据客户提供的订单情况，概括问题并列出下一步处理动作。订单信息：",
        "direct_growth": "客户补充了新的交付和库存信息，请结合前文更新处理建议。补充内容：",
        "tool_initial": "请调用 get_weather 查询杭州天气，并结合订单配送计划给出简短建议。补充内容：",
        "final_growth": "客户确认了部分信息，请结合完整对话给出最终处理摘要和风险提示。确认内容：",
    }
    marker = f"session={seed}-{session_index};round={round_index};stage={stage}|"
    return _variable_customer_text(
        rng,
        marker + prompts.get(stage, prompts["direct_growth"]),
        length_range,
    )


def _progressive_tool_result_text(
    rng: random.Random,
    seed: int,
    session_index: int,
    length_range: dict[str, Any],
) -> str:
    prefix = json.dumps(
        {
            "request_id": f"{seed}-tool-result-{session_index}-{rng.getrandbits(64):016x}",
            "city": "杭州",
            "temperature_c": 18 + session_index % 12,
            "condition": ["晴", "多云", "小雨"][session_index % 3],
            "delivery_risk": ["低", "中", "高"][session_index % 3],
        },
        ensure_ascii=False,
    )
    return _variable_customer_text(rng, prefix, length_range)


def _append_response_and_user(
    previous_body: dict[str, Any],
    transport: str,
    result: Any,
    user_text: str,
) -> dict[str, Any]:
    response_json = getattr(result, "response_json", None) or {}
    body = copy.deepcopy(previous_body)
    if transport == "gemini_generate_content":
        candidates = response_json.get("candidates") or []
        content = copy.deepcopy(candidates[0].get("content") or {}) if candidates else {}
        if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
            fallback = str(getattr(result, "text", "") or "").strip()
            if not fallback:
                raise ValueError("Gemini response contains no reusable model content.")
            content = {"role": "model", "parts": [{"text": fallback}]}
        content["role"] = "model"
        body["contents"] = list(copy.deepcopy(previous_body.get("contents") or [])) + [
            content,
            {"role": "user", "parts": [{"text": user_text}]},
        ]
        return body

    if transport == "claude_messages":
        content = copy.deepcopy(response_json.get("content") or [])
        if not isinstance(content, list) or not content:
            fallback = str(getattr(result, "text", "") or "").strip()
            if not fallback:
                raise ValueError("Claude response contains no reusable assistant content.")
            content = [{"type": "text", "text": fallback}]
        body["messages"] = list(copy.deepcopy(previous_body.get("messages") or [])) + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": user_text},
        ]
        return body

    message = copy.deepcopy(extract_message(response_json))
    if not message:
        fallback = str(getattr(result, "text", "") or "").strip()
        if not fallback:
            raise ValueError("Chat response contains no reusable assistant message.")
        message = {"content": fallback}
    message["role"] = "assistant"
    body["messages"] = list(copy.deepcopy(previous_body.get("messages") or [])) + [
        message,
        {"role": "user", "content": user_text},
    ]
    body["stream"] = False
    body.pop("stream_options", None)
    return body


def _response_has_tool_call(transport: str, response_json: dict[str, Any]) -> bool:
    if transport == "gemini_generate_content":
        return bool(extract_native_function_calls(response_json))
    if transport == "claude_messages":
        return bool(extract_claude_tool_uses(response_json))
    return bool(extract_tool_calls(response_json))


def _assert_strict_conversation_extension(
    previous_body: dict[str, Any],
    next_body: dict[str, Any],
    transport: str,
) -> None:
    conversation_key = "contents" if transport == "gemini_generate_content" else "messages"
    previous = previous_body.get(conversation_key)
    current = next_body.get(conversation_key)
    if not isinstance(previous, list) or not isinstance(current, list):
        raise ValueError("Progressive request has no conversation list.")
    if len(current) <= len(previous) or current[: len(previous)] != previous:
        raise ValueError("Progressive request is not a strict conversation-prefix extension.")
    for key in ("system", "systemInstruction", "tools", "tool_choice", "toolConfig"):
        if key in previous_body or key in next_body:
            if previous_body.get(key) != next_body.get(key):
                raise ValueError(f"Progressive request changed stable field {key}.")


def _customer_transport(config: dict[str, Any], provider: str, model: str) -> str:
    route_profile = get_model_route_profile(config, model, provider)
    api_form = get_model_api_form(
        config, model, provider, route_profile=route_profile
    )
    return get_model_transport(
        config,
        model,
        provider,
        route_profile=route_profile,
        api_form=api_form,
    )


def _transport_path(transport: str) -> str:
    return {
        "chat_completions": "/v1/chat/completions",
        "claude_messages": "/v1/messages",
        "gemini_generate_content": "/models/{model}:generateContent",
    }.get(transport, "/v1/chat/completions")


def _tool_profile(family: str, transport: str) -> str:
    if transport == "claude_messages":
        return "claude_native_tool_choice_auto"
    if transport == "gemini_generate_content":
        return "gemini_native_tool_config"
    return {
        "qwen": "qwen_tool_choice_auto",
        "gemini": "gemini_tool_choice_auto",
        "claude": "claude_tool_choice_auto",
        "claude_fable": "claude_tool_choice_auto",
        "glm": "glm_tool_choice_auto",
    }.get(family, "tool_calls")


def _customer_body(
    original: dict[str, Any],
    transport: str,
    system: str,
    user: str,
    max_tokens: int,
) -> dict[str, Any]:
    body = copy.deepcopy(original)
    if transport == "gemini_generate_content":
        body["systemInstruction"] = {"parts": [{"text": system}]}
        body["contents"] = [{"role": "user", "parts": [{"text": user}]}]
        generation = body.setdefault("generationConfig", {})
        generation["maxOutputTokens"] = max_tokens
        return body
    if transport == "claude_messages":
        body["system"] = system
        body["messages"] = [{"role": "user", "content": user}]
        body["max_tokens"] = max_tokens
        body["stream"] = False
        return body
    body["messages"] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    body["max_tokens"] = max_tokens
    body["stream"] = False
    body.pop("stream_options", None)
    return body


def _variable_customer_text(
    rng: random.Random,
    prefix: str,
    length_range: dict[str, Any],
) -> str:
    minimum = int(length_range.get("min") or 1)
    maximum = int(length_range.get("max") or minimum)
    target = rng.randint(minimum, maximum)
    unique = f"{rng.getrandbits(128):032x}|"
    start = f"{unique}{prefix}"
    corpus = "客户订单状态渠道告警工具响应库存地址时间优先级备注数据变化。"
    if len(start) >= target:
        return start[:target]
    repeat = (corpus * ((target - len(start)) // len(corpus) + 1))[: target - len(start)]
    return start + repeat


def _tool_followup_body(
    transport: str,
    initial_body: dict[str, Any],
    response_json: dict[str, Any],
) -> dict[str, Any]:
    if transport == "claude_messages":
        followup = build_claude_tool_followup_request(initial_body, response_json)
    elif transport == "gemini_generate_content":
        followup = build_native_tool_followup_request(initial_body, response_json)
    else:
        followup = build_tool_followup_request(initial_body, response_json)
    for key in ("tools", "tool_choice", "toolConfig"):
        if key in initial_body:
            followup[key] = copy.deepcopy(initial_body[key])
    return followup


def _replace_tool_results(
    body: dict[str, Any], transport: str, result_text: str
) -> None:
    if transport == "gemini_generate_content":
        contents = body.get("contents") or []
        parts = contents[-1].get("parts") if contents and isinstance(contents[-1], dict) else []
        changed = False
        for part in parts or []:
            response = part.get("functionResponse") if isinstance(part, dict) else None
            if isinstance(response, dict):
                response["response"] = {"content": result_text}
                changed = True
        if not changed:
            raise ValueError("Gemini follow-up contains no functionResponse.")
        return
    messages = body.get("messages") or []
    if transport == "claude_messages":
        content = messages[-1].get("content") if messages and isinstance(messages[-1], dict) else []
        changed = False
        for block in content or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                block["content"] = result_text
                changed = True
        if not changed:
            raise ValueError("Claude follow-up contains no tool_result.")
        return
    changed = False
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "tool":
            message["content"] = result_text
            changed = True
    if not changed:
        raise ValueError("Chat follow-up contains no tool message.")


def _long_control_text(config: dict[str, Any], pair: int) -> str:
    fixture = str((config.get("cache_test") or {}).get("fixture") or "fixtures/long_context.txt")
    text = resolve_project_path(fixture).read_text(encoding="utf-8")
    return f"positive-pair-{pair}|{text}"


def _positive_control_latency_speedup(records: list[RequestRecord]) -> float | None:
    pairs: dict[Any, dict[str, float]] = {}
    for record in records:
        if not record.success or record.latency_ms is None:
            continue
        if not isinstance(record.extra, dict) or record.extra.get("cache_control") != CACHE_CONTROL_POSITIVE:
            continue
        role = str(record.extra.get("control_role") or "")
        if role not in {CACHE_CONTROL_ROLE_COLD, CACHE_CONTROL_ROLE_WARM}:
            continue
        pairs.setdefault(record.extra.get("control_pair"), {})[role] = float(record.latency_ms)
    cold = [
        item[CACHE_CONTROL_ROLE_COLD]
        for item in pairs.values()
        if CACHE_CONTROL_ROLE_COLD in item and CACHE_CONTROL_ROLE_WARM in item
    ]
    warm = [
        item[CACHE_CONTROL_ROLE_WARM]
        for item in pairs.values()
        if CACHE_CONTROL_ROLE_COLD in item and CACHE_CONTROL_ROLE_WARM in item
    ]
    if not cold or not warm:
        return None
    cold_median = statistics.median(cold)
    warm_median = statistics.median(warm)
    if cold_median <= 0:
        return None
    return max((cold_median - warm_median) / cold_median, 0.0)


def _append_legacy_cache_controls(
    *,
    config: dict[str, Any],
    client: DeepSeekClient,
    report_dir: Path,
    records: list[RequestRecord],
    events: list[dict[str, Any]],
    provider: str,
    provider_cfg: dict[str, Any],
    model: str,
    family: str,
    cache_cfg: dict[str, Any],
    scenario: str,
    max_tokens: int,
    completed_steps: int,
    total_steps: int,
    run_nonce: str,
) -> int:
    positive_pairs, negative_requests = _required_control_counts(cache_cfg)
    rng = random.Random(int(cache_cfg.get("seed", 20260715)))
    transport = _customer_transport(config, provider, model)
    path = _transport_path(transport)

    def send(body: dict[str, Any], stage: str, extra: dict[str, Any]) -> None:
        nonlocal completed_steps
        result = _send_cache_request(client, transport, body, model)
        record = request_record_from_result(
            result=result,
            task_name=f"cache:control:{stage}",
            group="cache_profiles",
            profile=(
                "positive_long_prefix_control"
                if extra.get("cache_control") == CACHE_CONTROL_POSITIVE
                else "negative_unique_prefix_control"
            ),
            path=path,
            phase=f"cache_control_{stage}",
            extra={
                "provider": provider,
                "provider_label": provider_cfg.get("label") or provider,
                "backend": provider_cfg.get("backend"),
                "transport": transport,
                "request_endpoint": path,
                "requested_model": model,
                "model_family": family,
                **_route_metadata(config, provider, model),
                "cache_scenario": scenario,
                "cache_stage": f"control_{stage}",
                "control_run_nonce": run_nonce,
                **extra,
            },
        )
        records.append(record)
        events.append(_event_from_record(record))
        completed_steps += 1
        _write_records(report_dir / "request_records.jsonl", records)
        _write_cache_progress(report_dir, f"cache_control_{stage}", completed_steps, total_steps)

    cold_bodies: list[dict[str, Any]] = []
    for pair in range(positive_pairs):
        built = build_request(
            config,
            "cache_profiles",
            "cache_long_context",
            overrides={"max_tokens": max_tokens},
        )
        body = _customer_body(
            built.body,
            transport,
            f"positive-control-{run_nonce}-{pair}",
            _long_control_text(config, pair),
            max_tokens,
        )
        cold_bodies.append(copy.deepcopy(body))
        send(
            body,
            "positive:cold",
            {
                "cache_control": CACHE_CONTROL_POSITIVE,
                "control_role": CACHE_CONTROL_ROLE_COLD,
                "control_pair": pair,
            },
        )
    wait_seconds = float(cache_cfg.get("wait_after_warmup_sec", 5))
    if cold_bodies and wait_seconds > 0:
        _write_cache_progress(report_dir, "cache_control_wait", completed_steps, total_steps)
        time.sleep(wait_seconds)
    for pair, body in enumerate(cold_bodies):
        send(
            body,
            "positive:warm",
            {
                "cache_control": CACHE_CONTROL_POSITIVE,
                "control_role": CACHE_CONTROL_ROLE_WARM,
                "control_pair": pair,
            },
        )
    for index in range(negative_requests):
        built = build_request(
            config,
            "cache_profiles",
            "cache_long_context",
            overrides={"max_tokens": max_tokens},
        )
        body = _customer_body(
            built.body,
            transport,
            f"negative-control-{run_nonce}-{index}-{secrets.token_hex(16)}",
            _variable_customer_text(
                rng,
                f"negative-user-{secrets.token_hex(16)}",
                {"min": 4500, "max": 5500},
            ),
            max_tokens,
        )
        send(
            body,
            "negative",
            {
                "cache_control": CACHE_CONTROL_NEGATIVE,
                "control_role": CACHE_CONTROL_ROLE_UNIQUE,
                "control_pair": index,
            },
        )
    return completed_steps


def _run_shared_prefix_cache_suite(
    config: dict[str, Any],
    client: DeepSeekClient,
    report_dir: Path,
    provider: str,
    provider_cfg: dict[str, Any],
    model: str,
    family: str,
    warmup_requests: int,
    measured_request_count: int,
    wait_after_warmup_sec: float,
    max_tokens: int,
) -> dict[str, Any]:

    records: list[RequestRecord] = []
    events: list[dict[str, Any]] = []
    cache_cfg = config.get("cache_test") or {}
    positive_pairs, negative_requests = _required_control_counts(cache_cfg)
    total_steps = (
        warmup_requests
        + measured_request_count
        + positive_pairs * 2
        + negative_requests
        + (1 if wait_after_warmup_sec > 0 else 0)
    )
    completed_steps = 0
    run_nonce = secrets.token_hex(16)
    _write_cache_progress(report_dir, "starting", completed_steps, total_steps)

    warmup_request = build_request(
        config,
        "cache_profiles",
        "cache_long_context",
        overrides={"max_tokens": max_tokens},
    )
    warmup_request.body = _prepend_cache_run_nonce(
        warmup_request.body,
        str(warmup_request.metadata.get("transport") or "chat_completions"),
        run_nonce,
    )
    cacheable_prefix_tokens: int | None = None
    generated_suffixes: set[str] = set()
    for index in range(warmup_requests):
        reusable_before_request = cacheable_prefix_tokens
        suffix = "" if index == 0 else _unique_random_digits(generated_suffixes)
        body = _with_random_suffix(warmup_request.body, suffix)
        result = _send_cache_request(
            client,
            str(warmup_request.metadata.get("transport") or "chat_completions"),
            body,
            model,
        )
        record = request_record_from_result(
            result=result,
            task_name="cache:warmup:cache_long_context",
            group="cache_profiles",
            profile="cache_long_context",
            path=_transport_path(str(warmup_request.metadata.get("transport") or "chat_completions")),
            is_warmup=True,
            phase="cache_warmup",
            extra=_record_extra(
                config,
                provider,
                provider_cfg,
                model,
                family,
                index + 1,
                cacheable_prefix_tokens=(
                    reusable_before_request
                    if reusable_before_request is not None
                    else 0
                ),
                random_suffix_digits=len(suffix),
                cache_scenario="shared_prefix",
                transport=str(warmup_request.metadata.get("transport") or "chat_completions"),
                control_run_nonce=run_nonce,
            ),
        )
        records.append(record)
        events.append(_event_from_record(record))
        if cacheable_prefix_tokens is None:
            cacheable_prefix_tokens = prompt_tokens_from_usage(result.usage or {})
        completed_steps += 1
        _write_records(report_dir / "request_records.jsonl", records)
        _write_cache_progress(report_dir, "cache_warmup", completed_steps, total_steps)

    if wait_after_warmup_sec > 0:
        _write_cache_progress(report_dir, "cache_wait", completed_steps, total_steps)
        time.sleep(wait_after_warmup_sec)
        completed_steps += 1
        _write_cache_progress(report_dir, "cache_wait_done", completed_steps, total_steps)

    repeat_request = build_request(
        config,
        "cache_profiles",
        "cache_long_context_repeat",
        overrides={"max_tokens": max_tokens},
    )
    repeat_request.body = _prepend_cache_run_nonce(
        repeat_request.body,
        str(repeat_request.metadata.get("transport") or "chat_completions"),
        run_nonce,
    )
    for index in range(measured_request_count):
        reusable_before_request = cacheable_prefix_tokens
        phase = "cache_cold" if index == 0 else "cache_repeat"
        suffix = _unique_random_digits(generated_suffixes)
        body = _with_random_suffix(repeat_request.body, suffix)
        result = _send_cache_request(
            client,
            str(repeat_request.metadata.get("transport") or "chat_completions"),
            body,
            model,
        )
        record = request_record_from_result(
            result=result,
            task_name=f"cache:{phase}:cache_long_context_repeat",
            group="cache_profiles",
            profile="cache_long_context_repeat",
            path=_transport_path(str(repeat_request.metadata.get("transport") or "chat_completions")),
            phase=phase,
            extra=_record_extra(
                config,
                provider,
                provider_cfg,
                model,
                family,
                index + 1,
                cacheable_prefix_tokens=(
                    reusable_before_request
                    if reusable_before_request is not None
                    else 0
                ),
                random_suffix_digits=len(suffix),
                cache_scenario="shared_prefix",
                transport=str(repeat_request.metadata.get("transport") or "chat_completions"),
                control_run_nonce=run_nonce,
            ),
        )
        records.append(record)
        events.append(_event_from_record(record))
        if cacheable_prefix_tokens is None:
            cacheable_prefix_tokens = prompt_tokens_from_usage(result.usage or {})
        completed_steps += 1
        _write_records(report_dir / "request_records.jsonl", records)
        _write_cache_progress(report_dir, phase, completed_steps, total_steps)

    completed_steps = _append_legacy_cache_controls(
        config=config,
        client=client,
        report_dir=report_dir,
        records=records,
        events=events,
        provider=provider,
        provider_cfg=provider_cfg,
        model=model,
        family=family,
        cache_cfg=cache_cfg,
        scenario="shared_prefix",
        max_tokens=max_tokens,
        completed_steps=completed_steps,
        total_steps=total_steps,
        run_nonce=run_nonce,
    )
    return _finalize_cache_suite(config, report_dir, records, events, total_steps)


def _run_growing_conversation_cache_suite(
    config: dict[str, Any],
    client: DeepSeekClient,
    report_dir: Path,
    provider: str,
    provider_cfg: dict[str, Any],
    model: str,
    family: str,
    warmup_requests: int,
    measured_request_count: int,
    wait_after_warmup_sec: float,
    max_tokens: int,
) -> dict[str, Any]:
    records: list[RequestRecord] = []
    events: list[dict[str, Any]] = []
    cache_cfg = config.get("cache_test") or {}
    positive_pairs, negative_requests = _required_control_counts(cache_cfg)
    total_steps = (
        warmup_requests
        + measured_request_count
        + positive_pairs * 2
        + negative_requests
        + (1 if wait_after_warmup_sec > 0 else 0)
    )
    completed_steps = 0
    run_nonce = secrets.token_hex(16)
    _write_cache_progress(report_dir, "starting", completed_steps, total_steps)

    request_template = build_request(
        config,
        "cache_profiles",
        "cache_long_context",
        overrides={"max_tokens": max_tokens},
    )
    conversation_body = copy.deepcopy(request_template.body)
    conversation_body = _prepend_cache_run_nonce(
        conversation_body,
        str(request_template.metadata.get("transport") or "chat_completions"),
        run_nonce,
    )
    _validate_growing_conversation_body(conversation_body)
    assistant_history_max_chars = int(
        (config.get("cache_test") or {}).get("assistant_history_max_chars", 1000)
    )
    previous_prompt_tokens: int | None = None
    request_index = 0

    for index in range(warmup_requests):
        if request_index > 0:
            _append_growing_user_turn(conversation_body, request_index)
        body = copy.deepcopy(conversation_body)
        result = _send_cache_request(
            client,
            str(request_template.metadata.get("transport") or "chat_completions"),
            body,
            model,
        )
        record = request_record_from_result(
            result=result,
            task_name="cache:warmup:cache_growing_conversation",
            group="cache_profiles",
            profile="cache_growing_conversation",
            path=_transport_path(str(request_template.metadata.get("transport") or "chat_completions")),
            is_warmup=True,
            phase="cache_warmup",
            extra=_record_extra(
                config,
                provider,
                provider_cfg,
                model,
                family,
                index + 1,
                cacheable_prefix_tokens=previous_prompt_tokens,
                cache_scenario="growing_conversation",
                conversation_turn=request_index + 1,
                transport=str(request_template.metadata.get("transport") or "chat_completions"),
                control_run_nonce=run_nonce,
            ),
        )
        records.append(record)
        events.append(_event_from_record(record))
        prompt_tokens = prompt_tokens_from_usage(result.usage or {})
        if prompt_tokens is not None:
            previous_prompt_tokens = prompt_tokens
        _append_assistant_turn(
            conversation_body,
            result,
            request_index,
            assistant_history_max_chars,
        )
        request_index += 1
        completed_steps += 1
        _write_records(report_dir / "request_records.jsonl", records)
        _write_cache_progress(report_dir, "cache_warmup", completed_steps, total_steps)

    if wait_after_warmup_sec > 0:
        _write_cache_progress(report_dir, "cache_wait", completed_steps, total_steps)
        time.sleep(wait_after_warmup_sec)
        completed_steps += 1
        _write_cache_progress(report_dir, "cache_wait_done", completed_steps, total_steps)

    for index in range(measured_request_count):
        if request_index > 0:
            _append_growing_user_turn(conversation_body, request_index)
        phase = "cache_cold" if index == 0 else "cache_repeat"
        body = copy.deepcopy(conversation_body)
        result = _send_cache_request(
            client,
            str(request_template.metadata.get("transport") or "chat_completions"),
            body,
            model,
        )
        record = request_record_from_result(
            result=result,
            task_name=f"cache:{phase}:cache_growing_conversation",
            group="cache_profiles",
            profile="cache_growing_conversation",
            path=_transport_path(str(request_template.metadata.get("transport") or "chat_completions")),
            phase=phase,
            extra=_record_extra(
                config,
                provider,
                provider_cfg,
                model,
                family,
                index + 1,
                cacheable_prefix_tokens=previous_prompt_tokens,
                cache_scenario="growing_conversation",
                conversation_turn=request_index + 1,
                transport=str(request_template.metadata.get("transport") or "chat_completions"),
                control_run_nonce=run_nonce,
            ),
        )
        records.append(record)
        events.append(_event_from_record(record))
        prompt_tokens = prompt_tokens_from_usage(result.usage or {})
        if prompt_tokens is not None:
            previous_prompt_tokens = prompt_tokens
        _append_assistant_turn(
            conversation_body,
            result,
            request_index,
            assistant_history_max_chars,
        )
        request_index += 1
        completed_steps += 1
        _write_records(report_dir / "request_records.jsonl", records)
        _write_cache_progress(report_dir, phase, completed_steps, total_steps)

    completed_steps = _append_legacy_cache_controls(
        config=config,
        client=client,
        report_dir=report_dir,
        records=records,
        events=events,
        provider=provider,
        provider_cfg=provider_cfg,
        model=model,
        family=family,
        cache_cfg=cache_cfg,
        scenario="growing_conversation",
        max_tokens=max_tokens,
        completed_steps=completed_steps,
        total_steps=total_steps,
        run_nonce=run_nonce,
    )
    return _finalize_cache_suite(config, report_dir, records, events, total_steps)


def _finalize_cache_suite(
    config: dict[str, Any],
    report_dir: Path,
    records: list[RequestRecord],
    events: list[dict[str, Any]],
    total_steps: int,
) -> dict[str, Any]:
    measured = [record for record in records if not record.is_warmup]
    apply_cache_token_audits(
        records,
        (config.get("thresholds") or {}).get("cache") or {},
    )
    summary = summarize_records(
        measured,
        business_prefix="cache:",
        business_group="cache_profiles",
        cache_min_prompt_tokens=int(config.get("metrics", {}).get("cache_min_prompt_tokens", 4000)),
    )
    result = {
        "summary": summary,
        "latency_speedup_ratio": _latency_speedup_ratio(measured),
        "events": events,
    }
    first_extra = (measured[0].extra if measured else records[0].extra) if records else {}
    for key in (
        "provider",
        "provider_label",
        "requested_model",
        "model_family",
        "api_form",
        "route_profile",
        "transport",
    ):
        if first_extra.get(key) is not None:
            result[key if key != "requested_model" else "model"] = first_extra[key]
    _write_records(report_dir / "request_records.jsonl", records)
    write_json(report_dir / "cache_results.json", result)
    _write_cache_progress(report_dir, "completed", total_steps, total_steps)
    return result


def _write_records(path: Path, records: list[RequestRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(record.to_json() + "\n")


def _write_cache_progress(report_dir: Path, phase: str, completed: int, total: int) -> None:
    percent = int(completed * 100 / total) if total else 0
    write_json(
        report_dir / "cache_progress.json",
        {
            "phase": phase,
            "completed": completed,
            "total": total,
            "percent": percent,
            "label": f"{completed}/{total} steps",
        },
    )


def _record_extra(
    config: dict[str, Any],
    provider: str,
    provider_cfg: dict[str, Any],
    model: str,
    family: str,
    index: int,
    cacheable_prefix_tokens: int | None = None,
    random_suffix_digits: int = 0,
    cache_scenario: str = "shared_prefix",
    conversation_turn: int | None = None,
    transport: str = "chat_completions",
    control_run_nonce: str | None = None,
) -> dict[str, Any]:
    extra = {
        "cache_scope": "shared_prefix",
        "cache_scenario": cache_scenario,
        "cacheable_prefix_tokens": cacheable_prefix_tokens,
        "index": index,
        "provider": provider,
        "provider_label": provider_cfg.get("label") or provider,
        "random_suffix_digits": random_suffix_digits,
        "requested_model": model,
        "model_family": family,
        **_route_metadata(config, provider, model),
        "transport": transport,
        "request_endpoint": _transport_path(transport),
        "backend": provider_cfg.get("backend"),
    }
    if conversation_turn is not None:
        extra["conversation_turn"] = conversation_turn
    if control_run_nonce:
        extra["control_run_nonce"] = control_run_nonce
    return extra


def _route_metadata(
    config: dict[str, Any],
    provider: str,
    model: str,
) -> dict[str, str]:
    route_profile = get_model_route_profile(config, model, provider)
    api_form = get_model_api_form(
        config, model, provider, route_profile=route_profile
    )
    return {
        "api_form": api_form,
        "route_profile": route_profile,
    }


def _prepend_cache_run_nonce(
    body: dict[str, Any], transport: str, run_nonce: str
) -> dict[str, Any]:
    """Put an unpredictable marker at the first cacheable semantic token."""

    result = copy.deepcopy(body)
    marker = f"cache-run-{run_nonce}: "
    if transport == "openai_responses":
        result["instructions"] = marker + str(result.get("instructions") or "")
        return result
    if transport == "gemini_generate_content":
        instruction = result.get("systemInstruction")
        if isinstance(instruction, dict):
            parts = instruction.get("parts")
            if isinstance(parts, list) and parts and isinstance(parts[0], dict):
                parts[0]["text"] = marker + str(parts[0].get("text") or "")
                return result
        result["systemInstruction"] = {"parts": [{"text": marker.rstrip()}]}
        return result
    if transport == "claude_messages":
        system = result.get("system")
        if isinstance(system, str):
            result["system"] = marker + system
        elif isinstance(system, list):
            result["system"] = [{"type": "text", "text": marker.rstrip()}, *system]
        else:
            result["system"] = marker.rstrip()
        return result
    messages = result.get("messages")
    if not isinstance(messages, list):
        raise ValueError("cache request must contain messages for run nonce")
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        messages[0]["content"] = marker + str(messages[0].get("content") or "")
    else:
        messages.insert(0, {"role": "system", "content": marker.rstrip()})
    return result


def _with_random_suffix(body: dict[str, Any], digits: str) -> dict[str, Any]:
    result = copy.deepcopy(body)
    messages = result.get("messages") or []
    if not messages or not isinstance(messages[-1], dict):
        raise ValueError("cache request must contain a final message")
    content = messages[-1].get("content")
    if not isinstance(content, str):
        raise ValueError("cache request final message content must be text")
    marker = "\n\n请求唯一随机串（仅用于区分请求，不参与共享前缀命中率统计）："
    messages[-1]["content"] = f"{content}{marker}{digits}"
    return result


def _unique_random_digits(
    generated: set[str],
    length: int = 200,
) -> str:
    while True:
        value = "".join(secrets.choice("0123456789") for _ in range(length))
        if value not in generated:
            generated.add(value)
            return value


def _validate_growing_conversation_body(body: dict[str, Any]) -> None:
    messages = body.get("messages")
    # Claude Messages keeps system top-level, so a single user message is valid.
    if not isinstance(messages, list) or len(messages) < 1:
        raise ValueError("growing cache conversation requires a chat messages list")
    if not isinstance(messages[-1], dict) or messages[-1].get("role") != "user":
        raise ValueError("growing cache conversation must start from a final user message")


def _append_growing_user_turn(body: dict[str, Any], turn_index: int) -> None:
    questions = [
        "请基于前文补充两个评估 cache 命中率时最容易误判的因素。",
        "请说明上一轮结论里哪些指标最适合排查长上下文延迟。",
        "请把当前对话中关于 cache 分母的规则压缩成三条检查项。",
        "请指出如果命中率为零，下一步应优先核查哪些请求字段。",
    ]
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise ValueError("growing cache conversation requires messages")
    question = questions[(turn_index - 1) % len(questions)]
    messages.append(
        {
            "role": "user",
            "content": (
                f"对话缓存测试第 {turn_index + 1} 轮：{question}"
                "回答不超过 80 个汉字。"
            ),
        }
    )


def _append_assistant_turn(
    body: dict[str, Any],
    result: Any,
    turn_index: int,
    max_chars: int,
) -> None:
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise ValueError("growing cache conversation requires messages")
    text = str(getattr(result, "text", "") or "").strip()
    if not text:
        text = f"缓存测试第 {turn_index + 1} 轮占位回答。"
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars]
    messages.append({"role": "assistant", "content": text})


def _event_from_record(record: RequestRecord) -> dict[str, Any]:
    return {
        "phase": record.phase,
        "profile": record.profile,
        "success": record.success,
        "status_code": record.status_code,
        "latency_ms": record.latency_ms,
        "ttft_ms": record.ttft_ms,
        "finish_reason": record.finish_reason,
        "usage": record.usage,
        "cache_headers": record.cache_headers,
        "failure_classification": record.failure_classification,
        "extra": record.extra,
    }


def _latency_speedup_ratio(records: list[RequestRecord]) -> float | None:
    clean = [record for record in records if record.latency_ms is not None and record.success]
    if len(clean) < 2:
        return None
    cold = clean[0].latency_ms
    repeats = [record.latency_ms for record in clean[1:] if record.latency_ms is not None]
    if not cold or not repeats:
        return None
    repeat_avg = sum(repeats) / len(repeats)
    return max((cold - repeat_avg) / cold, 0.0)
