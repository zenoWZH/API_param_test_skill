from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .config import (
    PROJECT_ROOT,
    api_form_for_transport,
    deep_merge,
    get_image_auth_mode,
    get_image_endpoint,
    get_image_model_config,
    parse_duration_seconds,
    resolve_project_path,
    resolve_threshold_config,
)
from .image_validation import (
    banana_variant_cases,
    gpt_image_2_cases,
    grok_imagine_cases,
)
from .reference_specs import capability_profile_snapshot, load_model_capability_profile


JOB_SPEC_VERSION = 3
SUPPORTED_REQUEST_MODES = {"unique", "fixed"}
SUPPORTED_JOB_TYPES = {
    "param_test",
    "image_param_test",
    "quick_load",
    "cache_suite",
    "staircase",
    "soak",
    "trace_test",
}
MAX_CACHE_REQUESTS = 1000
LARGE_CACHE_REQUESTS = 100
CACHE_CONTENT_PROFILES = {
    "small": {
        "user_chars": {"min": 100, "max": 400},
        "tool_result_chars": {"min": 300, "max": 1000},
    },
    "realistic": {
        "user_chars": {"min": 200, "max": 2000},
        "tool_result_chars": {"min": 500, "max": 5000},
    },
    "large": {
        "user_chars": {"min": 1000, "max": 4000},
        "tool_result_chars": {"min": 3000, "max": 10000},
    },
}


def resolve_request_mode(payload: dict[str, Any], job_type: str) -> str:
    default = "unique" if job_type in {"quick_load", "staircase", "soak"} else "fixed"
    mode = str(payload.get("request_mode") or default)
    if mode not in SUPPORTED_REQUEST_MODES:
        raise ValueError("request_mode must be 'unique' or 'fixed'.")
    return mode


def validate_workload(config: dict[str, Any], job_type: str, workload: str) -> None:
    if job_type == "image_param_test":
        return
    configured = set((config.get("profile_weights") or {}).keys())
    if workload not in configured and workload != "cache_suite":
        raise ValueError(f"Unsupported workload: {workload!r}.")
    if job_type in {"staircase", "soak"} and (
        workload == "mixed_compat" or not workload.startswith("throughput")
    ):
        raise ValueError(f"{job_type} requires a deterministic throughput workload.")
    if job_type == "cache_suite" and workload not in {"cache_suite", "throughput"}:
        raise ValueError("cache_suite jobs do not accept load-test workloads.")


def resolve_staircase_plan(
    config: dict[str, Any],
    payload: dict[str, Any],
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    if any(key in payload for key in ("users", "spawn_rate", "duration")):
        raise ValueError(
            "staircase jobs require staircase_plan; top-level users/spawn_rate/duration are not accepted."
        )
    staircase_cfg = copy.deepcopy(config.get("staircase") or {})
    configured_steps = staircase_cfg.get("steps") or []
    defaults = {
        "steps": [
            int(step.get("users")) if isinstance(step, dict) else int(step)
            for step in configured_steps
        ],
        "step_duration": str(staircase_cfg.get("step_duration") or "5m"),
        "spawn_rate": int(staircase_cfg.get("spawn_rate") or 5),
        "warmup": {
            "enabled": bool((config.get("warmup") or {}).get("enabled", False)),
            "users": int((config.get("warmup") or {}).get("users") or 10),
            "duration": str((config.get("warmup") or {}).get("duration") or "1m"),
            "workload": str((config.get("warmup") or {}).get("workload") or "throughput_rpm"),
            "per_step": False,
        },
        "auto_extend": copy.deepcopy(staircase_cfg.get("auto_extend") or {}),
        "thresholds": resolve_threshold_config(
            config, "staircase", provider, model
        ),
    }
    plan = deep_merge(defaults, copy.deepcopy(payload.get("staircase_plan") or {}))
    steps = plan.get("steps") or []
    if not isinstance(steps, list) or not steps:
        raise ValueError("staircase_plan.steps must contain at least one user count.")
    plan["steps"] = [int(step.get("users")) if isinstance(step, dict) else int(step) for step in steps]
    if any(step <= 0 for step in plan["steps"]):
        raise ValueError("staircase_plan.steps must contain positive user counts.")
    plan["spawn_rate"] = int(plan.get("spawn_rate") or 0)
    if plan["spawn_rate"] <= 0:
        raise ValueError("staircase_plan.spawn_rate must be positive.")
    parse_duration_seconds(str(plan.get("step_duration") or ""))
    warmup = plan.get("warmup") or {}
    if bool(warmup.get("enabled")):
        if int(warmup.get("users") or 0) <= 0:
            raise ValueError("staircase_plan.warmup.users must be positive.")
        parse_duration_seconds(str(warmup.get("duration") or ""))
    auto = plan.get("auto_extend") or {}
    if bool(auto.get("enabled")):
        if int(auto.get("increment_users") or 0) <= 0:
            raise ValueError("staircase_plan.auto_extend.increment_users must be positive.")
        if int(auto.get("max_users") or 0) < max(plan["steps"]):
            raise ValueError("staircase_plan.auto_extend.max_users must cover configured steps.")
    return plan


def resolve_soak_plan(
    config: dict[str, Any],
    payload: dict[str, Any],
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    defaults = copy.deepcopy(config.get("soak") or {})
    defaults["thresholds"] = resolve_threshold_config(
        config, "soak_1h", provider, model
    )
    plan = deep_merge(defaults, copy.deepcopy(payload.get("soak_plan") or {}))
    plan["users"] = int(plan.get("users") or 0)
    plan["spawn_rate"] = int(plan.get("spawn_rate") or 0)
    plan["duration"] = str(plan.get("duration") or "1h")
    if plan["users"] <= 0 or plan["spawn_rate"] <= 0:
        raise ValueError("soak_plan users and spawn_rate must be positive.")
    parse_duration_seconds(plan["duration"])
    return plan


def resolve_cache_plan(
    config: dict[str, Any],
    payload: dict[str, Any],
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    defaults = copy.deepcopy(config.get("cache_test") or {})
    defaults["thresholds"] = resolve_threshold_config(
        config, "cache", provider, model
    )
    requested = copy.deepcopy(payload.get("cache_plan") or {})
    plan = deep_merge(defaults, requested)
    # v8 UI payloads always carried cases for the removed customer_tool_flow
    # scenario. Reject them with a clear error instead of silently remapping.
    if "scenario" not in requested and "cases" in requested:
        raise ValueError(
            "cache_plan.scenario is required; legacy v8 'cases' payloads "
            "(customer_tool_flow) are no longer supported."
        )
    scenario = str(plan.get("scenario") or "progressive_customer_session")
    supported = {
        "progressive_customer_session",
        "kilocode_agent_session",
        "growing_conversation",
        "shared_prefix",
    }
    if scenario not in supported:
        raise ValueError(
            "cache_plan.scenario must be progressive_customer_session, "
            "kilocode_agent_session, growing_conversation, or shared_prefix."
        )
    diagnostic_defaults = defaults.get("diagnostic_defaults") or {}
    if scenario != "progressive_customer_session" and isinstance(
        diagnostic_defaults.get(scenario), dict
    ):
        plan = deep_merge(
            deep_merge(defaults, copy.deepcopy(diagnostic_defaults[scenario])),
            requested,
        )
    plan["scenario"] = scenario
    plan.pop("diagnostic_defaults", None)
    if scenario == "progressive_customer_session":
        sessions = _positive_int(plan.get("sessions", 10), "sessions")
        rounds = _positive_int(plan.get("rounds_per_session", 4), "rounds_per_session")
        if rounds < 2:
            raise ValueError("cache_plan.rounds_per_session must be at least 2.")

        content_profile = str(plan.get("content_profile") or "realistic")
        configured_profiles = deep_merge(
            CACHE_CONTENT_PROFILES,
            copy.deepcopy(plan.get("content_profiles") or {}),
        )
        if content_profile == "custom":
            content_ranges = copy.deepcopy(plan.get("content_ranges") or {})
        elif content_profile in configured_profiles:
            content_ranges = copy.deepcopy(configured_profiles[content_profile])
        else:
            raise ValueError(
                "cache_plan.content_profile must be small, realistic, large, or custom."
            )
        _validate_range(content_ranges.get("user_chars"), "content_ranges.user_chars")
        _validate_range(
            content_ranges.get("tool_result_chars"),
            "content_ranges.tool_result_chars",
        )

        tool_stage = copy.deepcopy(plan.get("tool_stage") or {})
        enabled = tool_stage.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("cache_plan.tool_stage.enabled must be a boolean.")
        tool_round = int(tool_stage.get("round", 3))
        if enabled and not 2 <= tool_round <= rounds:
            raise ValueError(
                "cache_plan.tool_stage.round must be between 2 and rounds_per_session."
            )

        controls = copy.deepcopy(plan.get("controls") or {})
        control_mode = str(controls.get("mode") or "auto")
        if control_mode == "auto":
            positive_pairs = _positive_int(
                controls.get("auto_positive_long_prefix_pairs", 3),
                "automatic positive control pairs",
            )
            negative_requests = _positive_int(
                controls.get("auto_negative_unique_prefix_requests", 3),
                "automatic negative control requests",
            )
        elif control_mode == "custom":
            positive_pairs = _positive_int(
                controls.get("positive_long_prefix_pairs"),
                "positive control pairs",
            )
            negative_requests = _positive_int(
                controls.get("negative_unique_prefix_requests"),
                "negative control requests",
            )
        elif control_mode == "off":
            raise ValueError("Cache tests require positive and negative controls.")
        else:
            raise ValueError("cache_plan.controls.mode must be auto, custom, or off.")

        structure_probe = copy.deepcopy(plan.get("structure_probe") or {})
        structure_probe_enabled = structure_probe.get("enabled", True)
        if structure_probe_enabled is not True:
            raise ValueError(
                "cache_plan.structure_probe.enabled must remain true for progressive metrics."
            )

        customer_requests = sessions * (rounds + (1 if enabled else 0))
        structure_probe_requests = 1
        control_requests = positive_pairs * 2 + negative_requests
        request_count = customer_requests + structure_probe_requests + control_requests
        plan["sessions"] = sessions
        plan["rounds_per_session"] = rounds
        plan["content_profile"] = content_profile
        plan["resolved_content_ranges"] = content_ranges
        plan.pop("content_profiles", None)
        plan["tool_stage"] = {"enabled": enabled, "round": tool_round}
        plan["controls"] = {
            "mode": control_mode,
            "positive_long_prefix_pairs": positive_pairs,
            "negative_unique_prefix_requests": negative_requests,
        }
        plan["structure_probe"] = {"enabled": True}
        plan["estimated_customer_request_count"] = customer_requests
        plan["estimated_structure_probe_request_count"] = structure_probe_requests
        plan["estimated_control_request_count"] = control_requests
    elif scenario == "kilocode_agent_session":
        steps = _positive_int(plan.get("steps", 20), "steps")
        if steps < 2:
            raise ValueError("cache_plan.steps must be at least 2.")
        trajectory_mode = str(plan.get("trajectory_mode") or "scripted")
        if trajectory_mode not in {"scripted", "random"}:
            raise ValueError("cache_plan.trajectory_mode must be scripted or random.")
        fixture_defaults = {
            "system_prompt_fixture": "fixtures/kilocode_system_prompt.txt",
            "tools_fixture": "fixtures/kilocode_tools.json",
            "result_fixture": str(plan.get("fixture") or "fixtures/long_context.txt"),
        }
        project_root = PROJECT_ROOT.resolve()
        for key, default in fixture_defaults.items():
            path = str(plan.get(key) or default)
            resolved = resolve_project_path(path).resolve()
            if not resolved.is_relative_to(project_root):
                raise ValueError(f"cache_plan.{key} must stay inside the project root: {path}")
            if not resolved.is_file():
                raise ValueError(f"cache_plan.{key} does not exist: {path}")
            plan[key] = path
        controls = plan.get("controls") or {}
        positive_pairs = _positive_int(
            controls.get("positive_long_prefix_pairs", 3), "positive control pairs"
        )
        negative_requests = _positive_int(
            controls.get("negative_unique_prefix_requests", 3), "negative control requests"
        )
        warmup_requests = _non_negative_int(plan.get("warmup_requests", 1), "warmup requests")
        request_count = warmup_requests + steps + positive_pairs * 2 + negative_requests
        plan["steps"] = steps
        plan["trajectory_mode"] = trajectory_mode
        plan["warmup_requests"] = warmup_requests
        plan["controls"] = {
            "positive_long_prefix_pairs": positive_pairs,
            "negative_unique_prefix_requests": negative_requests,
        }
        for key in ("cases", "warmup_sessions", "stable_system"):
            plan.pop(key, None)
    else:
        measured = _positive_int(
            plan.get("measured_requests", plan.get("repeat_count", 50)),
            "measured requests",
        )
        warmup_requests = _non_negative_int(
            plan.get("warmup_requests", 2), "warmup requests"
        )
        controls = copy.deepcopy(plan.get("controls") or {})
        control_mode = str(controls.get("mode") or "auto")
        if control_mode == "off":
            raise ValueError("Cache tests require positive and negative controls.")
        if control_mode == "auto":
            positive_pairs = _positive_int(
                controls.get(
                    "positive_long_prefix_pairs",
                    controls.get("auto_positive_long_prefix_pairs", 3),
                ),
                "positive control pairs",
            )
            negative_requests = _positive_int(
                controls.get(
                    "negative_unique_prefix_requests",
                    controls.get("auto_negative_unique_prefix_requests", 3),
                ),
                "negative control requests",
            )
        elif control_mode == "custom":
            positive_pairs = _positive_int(
                controls.get("positive_long_prefix_pairs"),
                "positive control pairs",
            )
            negative_requests = _positive_int(
                controls.get("negative_unique_prefix_requests"),
                "negative control requests",
            )
        else:
            raise ValueError("cache_plan.controls.mode must be auto or custom.")
        plan["controls"] = {
            "mode": control_mode,
            "positive_long_prefix_pairs": positive_pairs,
            "negative_unique_prefix_requests": negative_requests,
        }
        plan["warmup_requests"] = warmup_requests
        request_count = (
            measured + warmup_requests + positive_pairs * 2 + negative_requests
        )
    if scenario != "progressive_customer_session":
        for key in (
            "sessions",
            "rounds_per_session",
            "content_profile",
            "content_profiles",
            "content_ranges",
            "resolved_content_ranges",
            "tool_stage",
            "structure_probe",
            "estimated_customer_request_count",
            "estimated_structure_probe_request_count",
            "estimated_control_request_count",
        ):
            plan.pop(key, None)
    if scenario in {"growing_conversation", "shared_prefix"}:
        for key in (
            "stable_system",
            "wait_after_seed_sec",
            "seed",
            "cases",
            "warmup_sessions",
        ):
            plan.pop(key, None)
    if request_count > MAX_CACHE_REQUESTS:
        raise ValueError(f"cache plan exceeds the hard limit of {MAX_CACHE_REQUESTS} requests.")
    if request_count > LARGE_CACHE_REQUESTS and not bool(payload.get("confirm_large_run")):
        raise ValueError(
            f"cache plan schedules {request_count} requests; set confirm_large_run=true above {LARGE_CACHE_REQUESTS}."
        )
    plan["estimated_request_count"] = request_count
    if scenario in {"progressive_customer_session", "kilocode_agent_session"}:
        plan["max_run_seconds"] = _positive_int(
            plan.get("max_run_seconds", 1800), "max_run_seconds"
        )
        plan["consecutive_failure_limit"] = _positive_int(
            plan.get("consecutive_failure_limit", 3), "consecutive_failure_limit"
        )
    else:
        plan.pop("max_run_seconds", None)
        plan.pop("consecutive_failure_limit", None)
    if str(plan.get("evidence_mode") or "official_usage") != "official_usage":
        raise ValueError("cache_plan.evidence_mode currently supports only official_usage.")
    plan["evidence_mode"] = "official_usage"
    return plan


def resolve_image_plan(
    config: dict[str, Any],
    payload: dict[str, Any],
    provider: str,
    model: str,
    timeout_sec: int,
) -> dict[str, Any]:
    raw_requested = payload.get("image_plan")
    requested = {} if raw_requested is None else copy.deepcopy(raw_requested)
    if not isinstance(requested, dict):
        raise ValueError("image_plan must be an object.")
    base_model_cfg = get_image_model_config(config, provider, model)
    family = str(base_model_cfg.get("family") or "")
    route_profile = str(
        requested.get("route_profile") or base_model_cfg.get("route_profile") or ""
    ).strip()
    requested_transport = str(requested.get("transport") or "").strip()
    requested_api_form = str(requested.get("api_form") or "").strip()
    legacy_transport_form = (
        api_form_for_transport(requested_transport, modality="image")
        if requested_transport
        else ""
    )
    if (
        requested_api_form
        and legacy_transport_form
        and requested_api_form != legacy_transport_form
    ):
        raise ValueError(
            f"image_plan.api_form {requested_api_form!r} conflicts with legacy "
            f"transport {requested_transport!r} ({legacy_transport_form!r})."
        )
    requested_api_form = requested_api_form or legacy_transport_form
    model_cfg = get_image_model_config(
        config,
        provider,
        model,
        route_profile=route_profile or None,
        api_form=requested_api_form or None,
    )
    api_form = str(model_cfg.get("api_form") or "")
    transport = str(model_cfg.get("transport") or "")
    capability = load_model_capability_profile(
        "image",
        family,
        model,
        route_profile=str(model_cfg.get("route_profile") or ""),
        api_form=api_form,
    )
    if (
        capability.get("known_model") is not True
        or capability.get("known_api_profile") is not True
        or capability.get("route_profile_known") is not True
    ):
        raise ValueError(
            f"Missing registered image model/API/route profile for "
            f"{family}/{api_form}/{model}/{capability.get('route_profile')}."
        )
    suite_id = str(capability.get("suite") or "")
    if suite_id not in {"banana", "gpt_image_2", "grok_imagine"}:
        raise ValueError(
            f"Image capability family {family!r} has invalid suite {suite_id!r}."
        )
    if (
        transport in {"chat-completions", "gemini-interactions"}
        and family != "banana"
    ):
        raise ValueError(
            f"{transport} image transport supports only Banana models."
        )

    suite = str(requested.get("suite") or "smoke")
    if suite not in {"smoke", "resolution", "full"}:
        raise ValueError("image_plan.suite must be smoke, resolution, or full.")
    include_2k = _strict_bool(requested, "include_2k", False)
    include_4k = _strict_bool(requested, "include_4k", False)
    if suite_id == "grok_imagine":
        if include_4k:
            raise ValueError(
                "Grok Imagine supports 1K/2K tiers; use image_plan.include_2k instead of include_4k."
            )
        if suite == "full" and not include_2k:
            raise ValueError(
                "Grok image_plan.full requires include_2k=true as billing acknowledgement."
            )
    else:
        if include_2k:
            raise ValueError("image_plan.include_2k currently supports only Grok Imagine models.")
        if suite == "full" and not include_4k:
            raise ValueError(
                "image_plan.full requires include_4k=true as billing acknowledgement."
            )
    no_negative = _strict_bool(requested, "no_negative", False)
    no_cross_control = _strict_bool(requested, "no_cross_control", False)
    visual_forensics = _strict_bool(requested, "visual_forensics", True)
    if suite_id == "banana":
        if "{resolution}" not in model and "{resolution_lower}" not in model:
            if not no_cross_control:
                raise ValueError(
                    "A fixed Banana model requires image_plan.no_cross_control=true."
                )
    else:
        no_cross_control = False

    quality = str(requested.get("quality") or "low")
    output_format = str(
        requested.get("output_format")
        or ("jpeg" if transport == "gemini-interactions" else "png")
    )
    if quality not in {"low", "medium", "high", "auto"}:
        raise ValueError("image_plan.quality must be low, medium, high, or auto.")
    if output_format not in {"png", "jpeg", "webp"}:
        raise ValueError("image_plan.output_format must be png, jpeg, or webp.")
    if transport == "gemini-interactions" and output_format != "jpeg":
        raise ValueError(
            "Gemini Interactions image output currently supports only jpeg."
        )
    if (
        transport in {"chat-completions", "gemini-interactions"}
        or family == "grok-imagine"
    ):
        quality_value: str | None = None
    else:
        quality_value = quality
    if (
        transport in {"chat-completions", "gemini-interactions"}
        or family == "grok-imagine"
    ):
        output_format_value: str | None = None
    else:
        output_format_value = output_format

    try:
        if suite_id == "banana":
            matrix = banana_variant_cases(
                suite,
                model_template=model,
                include_4k=include_4k,
                include_cross_control=not no_cross_control,
                include_negative=not no_negative,
                transport=transport,
            )
        elif suite_id == "gpt_image_2":
            matrix = gpt_image_2_cases(
                suite,
                include_4k=include_4k,
                include_negative=not no_negative,
            )
        elif suite_id == "grok_imagine":
            matrix = grok_imagine_cases(
                suite,
                include_2k=include_2k,
                include_negative=not no_negative,
            )
        else:  # pragma: no cover - guarded above
            raise ValueError(f"Unsupported image capability suite: {suite_id!r}.")
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    available = [case.name for case in matrix]
    selected_raw = requested.get("cases")
    if selected_raw in (None, []):
        selected = available
    else:
        if not isinstance(selected_raw, list) or not all(
            isinstance(item, str) and item for item in selected_raw
        ):
            raise ValueError("image_plan.cases must be a list of case names.")
        selected = [str(item) for item in selected_raw]
        if len(set(selected)) != len(selected):
            raise ValueError("image_plan.cases must not contain duplicates.")
        missing = [item for item in selected if item not in available]
        if missing:
            raise ValueError(f"Unknown image test case(s): {', '.join(missing)}.")

    timeout = int(timeout_sec)
    if timeout <= 0:
        raise ValueError("image_plan.timeout_sec must be positive.")
    return {
        "provider": provider,
        "endpoint": get_image_endpoint(config, provider, transport),
        "auth_mode": get_image_auth_mode(config, provider, transport),
        "model": model,
        "family": family,
        "family_suite": suite_id,
        "api_form": api_form,
        "route_profile": capability.get("route_profile"),
        "transport": transport,
        "suite": suite,
        "include_2k": include_2k,
        "include_4k": include_4k,
        "quality": quality_value,
        "output_format": output_format_value,
        "no_negative": no_negative,
        "no_cross_control": no_cross_control,
        "visual_forensics": visual_forensics,
        "cases": selected,
        "estimated_case_count": len(selected),
        "model_capability_profile": capability_profile_snapshot(
            "image",
            family,
            model,
            available,
            api_form=api_form,
            route_profile=str(capability.get("route_profile") or ""),
            provider_override=(
                (model_cfg.get("routes") or {})
                .get(str(capability.get("route_profile") or ""), {})
                .get("api_forms", {})
                .get(api_form, {})
            ),
        ),
        "timeout_sec": timeout,
    }


def make_job_spec(
    *,
    job_type: str,
    provider: str,
    model: str,
    workload: str,
    request_mode: str,
    target_rpm: float,
    target_tpm: float,
    model_family: str | None = None,
    api_form: str | None = None,
    route_profile: str | None = None,
    model_profile_id: str | None = None,
    transport: str | None = None,
    staircase_plan: dict[str, Any] | None = None,
    cache_plan: dict[str, Any] | None = None,
    soak_plan: dict[str, Any] | None = None,
    image_plan: dict[str, Any] | None = None,
    reference_source: str | None = None,
    reference_route_profile: str | None = None,
    model_capability_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": JOB_SPEC_VERSION,
        "type": job_type,
        "provider": provider,
        "model": model,
        "model_family": model_family,
        "api_form": api_form,
        "route_profile": route_profile,
        "model_profile_id": model_profile_id,
        "transport": transport,
        "workload": workload,
        "request_mode": request_mode,
        "target_rpm": target_rpm,
        "target_tpm": target_tpm,
        "staircase_plan": staircase_plan,
        "cache_plan": cache_plan,
        "soak_plan": soak_plan,
        "image_plan": image_plan,
        "reference_source": reference_source,
        "reference_route_profile": reference_route_profile,
        "model_capability_profile": model_capability_profile,
    }


def load_job_spec(path: str | Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    target = Path(path)
    if not target.exists():
        raise RuntimeError(f"Job spec does not exist: {target}")
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) not in {1, 2, JOB_SPEC_VERSION}:
        raise RuntimeError(f"Unsupported job spec: {target}")
    return payload


def _positive_int(value: Any, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer.") from exc
    if result <= 0:
        raise ValueError(f"{label} must be positive.")
    return result


def _strict_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    if key not in payload:
        return default
    value = payload[key]
    if not isinstance(value, bool):
        raise ValueError(f"image_plan.{key} must be a boolean.")
    return value


def _non_negative_int(value: Any, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer.") from exc
    if result < 0:
        raise ValueError(f"{label} must not be negative.")
    return result


def _validate_range(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"cache_plan {label} must contain min and max.")
    minimum = _positive_int(value.get("min"), f"{label}.min")
    maximum = _positive_int(value.get("max"), f"{label}.max")
    if maximum < minimum:
        raise ValueError(f"cache_plan {label}.max must be >= min.")
