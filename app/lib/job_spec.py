from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

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
    gemini_flash_31_lite_image_profile_cases,
    gpt_image_2_cases,
    grok_imagine_cases,
)
from .gpt_image_25 import gpt_image_25_cases, is_gpt_image_25
from .banana_generate_content import banana_gc_comparison_metadata, build_banana_generate_content_cases, has_exact_banana_gc_reference
from .gemini_api_version import require_ai_studio_v1beta_url, resolve_gemini_api_version
from .parameter_job_controls import (
    DEFAULT_PARAM_TEST_RUNS, PARAMETER_JOB_SPEC_VERSION, WORKFLOW_JOB_SPEC_VERSION,
    WORKFLOW_JOB_TYPES, freeze_workflow_job, make_parameter_execution,
    parameter_execution_from_job, parameter_execution_result_state,
    workflow_execution_result_state,
)
from .reference_specs import capability_profile_snapshot, load_model_capability_profile


# Pressure jobs retain v4; functional jobs with a frozen workflow use v6.
# Historical parameter jobs without workflow plans retain their v5 controls.
JOB_SPEC_VERSION = 4
RESULT_CONTRACT_SCHEMA_VERSION = 1
CURRENT_TOKEN_AUDIT_SCHEMA_VERSION = 4
CURRENT_MPDB_SNAPSHOT_SCHEMA_VERSION = 1
CURRENT_CACHE_VERDICT_SCHEMA_VERSION = 1
PARAMETER_RESULT_JOB_TYPES = frozenset({"param_test", "image_param_test"})
TOKEN_POLICY_SUMMARY_FIELDS = (
    "enabled",
    "evidence_policy",
    "relative_tolerance",
    "input_absolute_tolerance",
    "output_absolute_tolerance",
    "gross_input_ratio",
    "gross_output_ratio",
    "gross_input_absolute_tolerance",
    "gross_output_absolute_tolerance",
    "gross_output_limit_multiplier",
)
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
SCHEMA_V4_SNAPSHOT_OPTIONAL_JOB_TYPES = frozenset({"trace_test"})
PRESSURE_JOB_TYPES = frozenset({"quick_load", "cache_suite", "staircase", "soak"})
NON_PRESSURE_MODALITIES = frozenset({"image", "video"})
IMAGE_PRESSURE_REQUEST_FIELDS = frozenset(
    {
        "workload",
        "request_mode",
        "concurrency",
        "users",
        "spawn_rate",
        "duration",
        "rate",
        "rpm",
        "tpm",
        "target_rpm",
        "target_tpm",
        "cache_measured_requests",
        "staircase_plan",
        "cache_plan",
        "soak_plan",
    }
)
IMAGE_PLAN_PRESSURE_FIELDS = frozenset(
    {
        "concurrency",
        "users",
        "spawn_rate",
        "duration",
        "rate",
        "rpm",
        "tpm",
        "target_rpm",
        "target_tpm",
        "staircase_plan",
        "cache_plan",
        "soak_plan",
    }
)
MAX_CACHE_REQUESTS = 1000
LARGE_CACHE_REQUESTS = 100
GEMINI_IMAGE_TRANSPORTS = {"gemini-interactions", "gemini-generate-content"}
GEMINI_API_VERSIONS = {"v1", "v1beta"}


def _require_current_mpdb_snapshot_schema(snapshot: Any) -> None:
    if not isinstance(snapshot, dict) or not snapshot:
        raise ValueError(
            "Schema-v4 executable Job requires an immutable MPDB snapshot."
        )
    version = snapshot.get("snapshot_schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != CURRENT_MPDB_SNAPSHOT_SCHEMA_VERSION
    ):
        raise ValueError(
            "Schema-v4 executable Job requires MPDB snapshot schema "
            f"{CURRENT_MPDB_SNAPSHOT_SCHEMA_VERSION}; found {version!r}."
        )


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


def build_result_validation_contract(
    config: dict[str, Any], *, image_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze the token gate used to decide whether a result is current."""

    test_cases = config.get("test_cases")
    test_cases = test_cases if isinstance(test_cases, dict) else {}
    raw_policy = test_cases.get("token_accuracy")
    raw_policy = raw_policy if isinstance(raw_policy, dict) else {}
    token_policy = {
        field: copy.deepcopy(raw_policy[field])
        for field in TOKEN_POLICY_SUMMARY_FIELDS
        if field in raw_policy
    }
    token_policy.setdefault("enabled", True)
    token_policy.setdefault("evidence_policy", "exact_only")
    from .gpt_image_25_web_audit import AUDIT_SCHEMA_VERSION, POLICY_NAME, eligible_image_plan
    if eligible_image_plan(image_plan):
        return {
            "schema_version": RESULT_CONTRACT_SCHEMA_VERSION,
            "token_audit_schema_version": AUDIT_SCHEMA_VERSION,
            "token_validation_field": "token_validation_pass",
            "validation_policy": POLICY_NAME,
            "token_policy": {
                "enabled": token_policy["enabled"],
                "evidence_policy": POLICY_NAME,
                "exact_media_input_required": False,
                "exact_image_output_required": False,
            },
        }
    return {
        "schema_version": RESULT_CONTRACT_SCHEMA_VERSION,
        "token_audit_schema_version": CURRENT_TOKEN_AUDIT_SCHEMA_VERSION,
        "token_validation_field": "token_validation_pass",
        "compatibility_fallback_fields": [
            "token_accuracy_pass",
            "token_audit_summary.pass",
        ],
        "token_policy": token_policy,
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


def _versioned_gemini_image_endpoint(
    endpoint: str,
    *,
    transport: str,
    model: str,
    api_version: str,
) -> str:
    require_ai_studio_v1beta_url(endpoint)
    resolve_gemini_api_version(endpoint, api_version)
    if api_version not in GEMINI_API_VERSIONS:
        raise ValueError(
            "image_plan.api_version must be v1 or v1beta for native Gemini images."
        )
    route = (
        r"/(?:v1|v1beta)/interactions$"
        if transport == "gemini-interactions"
        else r"/(?:v1|v1beta)/models/[^/]+:generateContent$"
    )
    match = re.search(route, endpoint)
    if match is None:
        raise ValueError(f"Unexpected {transport} image endpoint: {endpoint!r}.")
    if transport == "gemini-interactions":
        suffix = f"/{api_version}/interactions"
    else:
        selected_model = str(model).removeprefix("models/")
        suffix = (
            f"/{api_version}/models/{quote(selected_model, safe='')}:generateContent"
        )
    return endpoint[: match.start()] + suffix


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
    # Both API payload shapes describe the same source/interface selection.
    # An explicit selection must never silently fall back to the model default.
    for field in ("route_profile", "api_form"):
        selections = []
        for owner, values in (("", payload), ("image_plan.", requested)):
            if field not in values:
                continue
            value = values[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{owner}{field} must be a non-empty string.")
            selections.append(value.strip())
        if len(selections) == 2 and selections[0] != selections[1]:
            raise ValueError(f"{field} conflicts with image_plan.{field}.")
        if selections:
            requested[field] = selections[0]
    pressure_fields = sorted(
        field for field in IMAGE_PRESSURE_REQUEST_FIELDS if field in payload
    )
    nested_pressure_fields = sorted(
        field for field in IMAGE_PLAN_PRESSURE_FIELDS if field in requested
    )
    if pressure_fields or nested_pressure_fields:
        fields = [
            *pressure_fields,
            *(f"image_plan.{field}" for field in nested_pressure_fields),
        ]
        raise ValueError(
            "Image parameter tests do not accept pressure-test fields: "
            + ", ".join(fields)
            + "."
        )
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
    from .model_profile_catalog import (
        capability_profile_from_database_snapshot,
        resolve_runtime_parameter_config,
    )
    parameter_config = resolve_runtime_parameter_config(
        config, provider, model, family,
        str(model_cfg.get("route_profile") or ""), api_form, modality="image",
    )
    model_profile_database = parameter_config["model_profile_database"]
    capability = capability_profile_from_database_snapshot(model_profile_database)
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
        transport
        in {"chat-completions", "gemini-interactions", "gemini-generate-content"}
        and family != "banana"
    ):
        raise ValueError(
            f"{transport} image transport supports only Banana models."
        )
    if transport == "gemini-generate-content" and (
        capability.get("parameter_test_enabled") is not True
        or capability.get("test_policy_parameter_test_enabled") is not True
    ):
        raise ValueError(
            f"Image parameter testing is disabled for {family}/{model}: "
            f"{capability.get('disabled_reason') or 'model profile policy'}."
        )

    suite = str(requested.get("suite") or "full")
    if suite not in {"smoke", "resolution", "full"}:
        raise ValueError("image_plan.suite must be smoke, resolution, or full.")
    include_2k = _strict_bool(requested, "include_2k", suite == "full" and suite_id == "grok_imagine")
    include_4k = _strict_bool(requested, "include_4k", suite == "full" and suite_id != "grok_imagine")
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
    requested_api_version = requested.get("api_version")
    if requested_api_version is not None and not isinstance(
        requested_api_version, str
    ):
        raise ValueError("image_plan.api_version must be a string.")
    if transport in GEMINI_IMAGE_TRANSPORTS:
        api_version: str | None = str(
            resolve_gemini_api_version(
                get_image_endpoint(config, provider, transport), requested_api_version
            ) or "v1"
        )
        if api_version not in GEMINI_API_VERSIONS:
            raise ValueError(
                "image_plan.api_version must be v1 or v1beta for native Gemini images."
            )
    else:
        if requested_api_version is not None:
            raise ValueError(
                "image_plan.api_version applies only to native Gemini image transports."
            )
        api_version = None
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
    quality_choices = {"low", "medium", "high", "auto"}
    if is_gpt_image_25(model):
        quality_choices.update({"xhigh", "max"})
    if quality not in quality_choices:
        raise ValueError("image_plan.quality is not supported by the exact image model.")
    if output_format not in {"png", "jpeg", "webp"}:
        raise ValueError("image_plan.output_format must be png, jpeg, or webp.")
    if transport == "gemini-interactions" and output_format != "jpeg":
        raise ValueError(
            "Gemini Interactions image output currently supports only jpeg."
        )
    if (
        transport
        in {"chat-completions", "gemini-interactions", "gemini-generate-content"}
        or family == "grok-imagine"
    ):
        quality_value: str | None = None
    else:
        quality_value = quality
    if (
        transport
        in {"chat-completions", "gemini-interactions", "gemini-generate-content"}
        or family == "grok-imagine"
    ):
        output_format_value: str | None = None
    else:
        output_format_value = output_format

    if is_gpt_image_25(model):
        quality_value, output_format_value = "low", "png"

    try:
        if suite_id == "banana":
            if has_exact_banana_gc_reference(model, capability):
                matrix = build_banana_generate_content_cases(
                    model, suite, include_4k=include_4k,
                    include_negative=not no_negative, capability_profile=capability,
                )
            elif (
                model == "gemini-3.1-flash-lite-image"
                and str(capability.get("route_profile") or "")
                == "google_ai_studio"
                and api_form == "gemini_generate_content"
            ):
                matrix = gemini_flash_31_lite_image_profile_cases(
                    suite,
                    api_form=api_form,
                    include_4k=include_4k,
                    include_negative=not no_negative,
                    capability_profile=capability,
                )
            else:
                matrix = banana_variant_cases(
                    suite,
                    model_template=model,
                    include_4k=include_4k,
                    include_cross_control=not no_cross_control,
                    include_negative=not no_negative,
                    transport=(
                        "gemini-interactions"
                        if transport == "gemini-generate-content"
                        else transport
                    ),
                )
        elif is_gpt_image_25(model):
            if transport == "openai-responses-image":
                from .gpt_image_25_responses import responses_image_cases, CURRENT_EXPECTATION_POLICY
                matrix = responses_image_cases(model, suite, include_4k=include_4k, include_negative=not no_negative,
                                               expectation_policy=CURRENT_EXPECTATION_POLICY)
            else:
                matrix = gpt_image_25_cases(model, suite,
                    operation="edit" if transport == "images-edits" else "generation",
                    include_4k=include_4k, include_negative=not no_negative)
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
    by_name = {case.name: case for case in matrix}
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

    responses_request_cap = None
    if is_gpt_image_25(model) and transport == "openai-responses-image":
        from .gpt_image_25_responses import select_responses_image_cases, responses_image_request_cap
        expanded_cases = select_responses_image_cases(model, matrix, selected)
        selected = [case.name for case in expanded_cases]
        responses_request_cap = responses_image_request_cap(model, expanded_cases)

    selected_profiles = list(
        dict.fromkeys(
            str(by_name[name].metadata.get("test_profile") or name)
            for name in selected
        )
    )
    available_profiles = list(
        dict.fromkeys(
            str(case.metadata.get("test_profile") or case.name)
            for case in matrix
        )
    )

    timeout = int(timeout_sec)
    if timeout <= 0:
        raise ValueError("image_plan.timeout_sec must be positive.")
    endpoint = get_image_endpoint(config, provider, transport)
    if transport in GEMINI_IMAGE_TRANSPORTS:
        endpoint = _versioned_gemini_image_endpoint(
            endpoint,
            transport=transport,
            model=model,
            api_version=str(api_version),
        )
    return {
        "provider": provider,
        **banana_gc_comparison_metadata(model, capability, endpoint),
        "endpoint": endpoint,
        "auth_mode": get_image_auth_mode(config, provider, transport),
        "model": model,
        "family": family,
        "family_suite": suite_id,
        "api_form": api_form,
        "route_profile": capability.get("route_profile"),
        "transport": transport,
        "api_version": api_version,
        "suite": suite,
        "include_2k": include_2k,
        "include_4k": include_4k,
        "quality": quality_value,
        "output_format": output_format_value,
        "no_negative": no_negative,
        "no_cross_control": no_cross_control,
        "visual_forensics": visual_forensics,
        "cases": selected,
        "test_profiles": selected_profiles,
        "available_test_profiles": available_profiles,
        "estimated_case_count": len(selected),
        **({"request_cap": responses_request_cap} if responses_request_cap is not None else {}),
        "model_capability_profile": {**capability_profile_snapshot(
            "image",
            family,
            model,
            available_profiles,
            api_form=api_form,
            route_profile=str(capability.get("route_profile") or ""),
            provider_override=(
                (model_cfg.get("routes") or {})
                .get(str(capability.get("route_profile") or ""), {})
                .get("api_forms", {})
                .get(api_form, {})
            ),
            model_profile_database=model_profile_database,
        ), **banana_gc_comparison_metadata(model, capability, endpoint)},
        "model_profile_database": copy.deepcopy(model_profile_database),
        "timeout_sec": timeout,
    }


def _validate_job_snapshot_consistency(payload: dict[str, Any]) -> None:
    """Fail closed when a schema-v4 job mixes independently resolved identities."""

    from .model_profile_catalog import binding_from_database_snapshot

    database = payload.get("model_profile_database")
    if not isinstance(database, dict):
        raise ValueError("Schema-v4 jobs require an immutable MPDB snapshot.")
    binding = binding_from_database_snapshot(database)
    profile = binding["profile"]
    interface = binding["interface"]
    target = binding.get("execution_target") or {}
    identity = {
        "source_id": binding.get("source_id"),
        "profile_id": binding.get("profile_id"),
        "interface_id": binding.get("interface_id"),
        "test_binding_id": binding.get("test_binding_id"),
        "reference_contract_id": binding.get("reference_contract_id"),
        "parameter_test_binding_id": binding.get("parameter_test_binding_id"),
    }

    top_expected = {
        **identity,
        "model_profile_id": binding.get("interface_id"),
        "modality": database.get("modality"),
        "model_family": binding.get("suite_family_id"),
        "provider": target.get("provider_id"),
        "model": target.get("request_model_id"),
        "route_profile": target.get("route_profile"),
        "api_form": target.get("api_form") or interface.get("api_form"),
        "transport": target.get("transport")
        or interface.get("transport_adapter_id"),
    }
    top_conflicts = [
        field
        for field, expected in top_expected.items()
        if payload.get(field) not in (None, "", expected)
    ]
    if top_conflicts:
        category = (
            "Job execution target conflicts with immutable MPDB snapshot: "
            if set(top_conflicts)
            & {"provider", "model", "route_profile", "api_form"}
            else "Job fields conflict with immutable MPDB snapshot: "
        )
        raise ValueError(
            category
            + ", ".join(sorted(set(top_conflicts)))
        )

    capability_expected = {
        "source_id": identity["source_id"],
        "profile_id": identity["profile_id"],
        "interface_id": identity["interface_id"],
        "model_api_profile_id": identity["interface_id"],
        "test_binding_id": identity["test_binding_id"],
        "reference_contract_id": identity["reference_contract_id"],
        "reference_source": identity["reference_contract_id"],
        "parameter_test_binding_id": identity["parameter_test_binding_id"],
        "modality": database.get("modality"),
        "family": binding.get("suite_family_id"),
        "suite_family_id": binding.get("suite_family_id"),
        "canonical_family_id": profile.get("family_id"),
        "model": target.get("request_model_id"),
        "canonical_model_slug": profile.get("model_slug"),
        "route_profile": target.get("route_profile"),
        "api_form": target.get("api_form") or interface.get("api_form"),
        "transport": target.get("transport")
        or interface.get("transport_adapter_id"),
    }
    capability_execution_expected = {
        "provider_id": target.get("provider_id"),
        "request_model_id": target.get("request_model_id"),
        "route_profile": target.get("route_profile"),
        "api_form": target.get("api_form") or interface.get("api_form"),
        "transport": target.get("transport")
        or interface.get("transport_adapter_id"),
    }
    capability = payload.get("model_capability_profile")
    if isinstance(capability, dict):
        capability_conflicts = [
            field
            for field, expected in capability_expected.items()
            if capability.get(field) not in (None, "", expected)
        ]
        nested = capability.get("model_profile_database")
        if isinstance(nested, dict):
            try:
                nested_binding = binding_from_database_snapshot(nested)
            except ValueError as exc:
                raise ValueError(
                    "Job capability conflicts with immutable MPDB snapshot: "
                    "model_profile_database"
                ) from exc
            for field, expected in identity.items():
                if nested_binding.get(field) != expected:
                    capability_conflicts.append(f"model_profile_database.{field}")
            if nested.get("snapshot_digest") != database.get("snapshot_digest"):
                capability_conflicts.append(
                    "model_profile_database.snapshot_digest"
                )
        reference_identity = capability.get("reference_identity")
        if isinstance(reference_identity, dict):
            for field, expected in identity.items():
                if reference_identity.get(field) not in (None, "", expected):
                    capability_conflicts.append(f"reference_identity.{field}")
        capability_target = capability.get("execution_target")
        if isinstance(capability_target, dict):
            for field, expected in capability_execution_expected.items():
                if capability_target.get(field) not in (None, "", expected):
                    capability_conflicts.append(f"execution_target.{field}")
        for field in (
            "profile",
            "interface",
            "test_binding",
            "reference_contract",
            "parameter_test_binding",
        ):
            if field in capability and capability.get(field) != database.get(field):
                capability_conflicts.append(field)
        if capability_conflicts:
            raise ValueError(
                "Job capability conflicts with immutable MPDB snapshot: "
                + ", ".join(sorted(set(capability_conflicts)))
            )

    image_plan = payload.get("image_plan")
    if not isinstance(image_plan, dict):
        return
    image_plan_expected = {
        "provider": target.get("provider_id"),
        "model": target.get("request_model_id"),
        "family": binding.get("suite_family_id"),
        "route_profile": target.get("route_profile"),
        "api_form": target.get("api_form") or interface.get("api_form"),
        "transport": target.get("transport")
        or interface.get("transport_adapter_id"),
    }
    image_plan_conflicts = [
        field
        for field, expected in image_plan_expected.items()
        if image_plan.get(field) not in (None, "", expected)
    ]
    plan_database = image_plan.get("model_profile_database")
    if isinstance(plan_database, dict):
        try:
            plan_binding = binding_from_database_snapshot(plan_database)
        except ValueError as exc:
            raise ValueError(
                "Job image_plan conflicts with immutable MPDB snapshot: "
                "model_profile_database"
            ) from exc
        for field, expected in identity.items():
            if plan_binding.get(field) != expected:
                image_plan_conflicts.append(f"model_profile_database.{field}")
        if plan_database.get("snapshot_digest") != database.get("snapshot_digest"):
            image_plan_conflicts.append("model_profile_database.snapshot_digest")
    plan_capability = image_plan.get("model_capability_profile")
    if isinstance(plan_capability, dict):
        for field, expected in capability_expected.items():
            if plan_capability.get(field) not in (None, "", expected):
                image_plan_conflicts.append(f"model_capability_profile.{field}")
        plan_capability_database = plan_capability.get("model_profile_database")
        if isinstance(plan_capability_database, dict):
            try:
                plan_capability_binding = binding_from_database_snapshot(
                    plan_capability_database
                )
            except ValueError as exc:
                raise ValueError(
                    "Job image_plan conflicts with immutable MPDB snapshot: "
                    "model_capability_profile.model_profile_database"
                ) from exc
            for field, expected in identity.items():
                if plan_capability_binding.get(field) != expected:
                    image_plan_conflicts.append(
                        "model_capability_profile.model_profile_database."
                        + field
                    )
            if plan_capability_database.get("snapshot_digest") != database.get(
                "snapshot_digest"
            ):
                image_plan_conflicts.append(
                    "model_capability_profile.model_profile_database.snapshot_digest"
                )
        plan_reference_identity = plan_capability.get("reference_identity")
        if isinstance(plan_reference_identity, dict):
            for field, expected in identity.items():
                if plan_reference_identity.get(field) not in (None, "", expected):
                    image_plan_conflicts.append(
                        f"model_capability_profile.reference_identity.{field}"
                    )
        plan_capability_target = plan_capability.get("execution_target")
        if isinstance(plan_capability_target, dict):
            for field, expected in capability_execution_expected.items():
                if plan_capability_target.get(field) not in (None, "", expected):
                    image_plan_conflicts.append(
                        f"model_capability_profile.execution_target.{field}"
                    )
        for field in (
            "profile",
            "interface",
            "test_binding",
            "reference_contract",
            "parameter_test_binding",
        ):
            if field in plan_capability and plan_capability.get(
                field
            ) != database.get(field):
                image_plan_conflicts.append(
                    f"model_capability_profile.{field}"
                )
    if image_plan_conflicts:
        raise ValueError(
            "Job image_plan conflicts with immutable MPDB snapshot: "
            + ", ".join(sorted(set(image_plan_conflicts)))
        )


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
    source_id: str | None = None,
    profile_id: str | None = None,
    interface_id: str | None = None,
    test_binding_id: str | None = None,
    reference_contract_id: str | None = None,
    model_profile_database: dict[str, Any] | None = None,
    transport: str | None = None,
    staircase_plan: dict[str, Any] | None = None,
    cache_plan: dict[str, Any] | None = None,
    soak_plan: dict[str, Any] | None = None,
    image_plan: dict[str, Any] | None = None,
    reference_source: str | None = None,
    reference_route_profile: str | None = None,
    model_capability_profile: dict[str, Any] | None = None,
    result_contract: dict[str, Any] | None = None,
    param_test_runs: int | None = None,
    tool_validation_mode: str = "auto",
    execution_plan: dict[str, Any] | None = None,
    parameter_suite: str | None = None,
) -> dict[str, Any]:
    from .model_profile_catalog import binding_from_database_snapshot

    snapshot_required = job_type not in SCHEMA_V4_SNAPSHOT_OPTIONAL_JOB_TYPES
    if snapshot_required and isinstance(model_profile_database, dict):
        _require_current_mpdb_snapshot_schema(model_profile_database)
    elif model_profile_database is not None and not isinstance(
        model_profile_database, dict
    ):
        raise ValueError("model_profile_database must be an object when provided.")

    snapshot_identity = {
        "source_id": source_id,
        "profile_id": profile_id,
        "interface_id": interface_id,
        "test_binding_id": test_binding_id,
        "reference_contract_id": reference_contract_id,
    }
    target = {
        "provider_id": provider,
        "request_model_id": model,
        "route_profile": route_profile,
        "api_form": api_form,
    }
    if isinstance(model_profile_database, dict):
        snapshot_binding = binding_from_database_snapshot(model_profile_database)
        snapshot_identity = {
            "source_id": snapshot_binding.get("source_id"),
            "profile_id": snapshot_binding.get("profile_id"),
            "interface_id": snapshot_binding.get("interface_id"),
            "test_binding_id": snapshot_binding.get("test_binding_id"),
            "reference_contract_id": snapshot_binding.get(
                "reference_contract_id"
            ),
        }
        supplied_identity = {
            "source_id": source_id,
            "profile_id": profile_id,
            "interface_id": interface_id,
            "test_binding_id": test_binding_id,
            "reference_contract_id": reference_contract_id,
        }
        conflicts = [
            field
            for field, supplied in supplied_identity.items()
            if supplied not in (None, "", snapshot_identity[field])
        ]
        if conflicts:
            raise ValueError(
                "Job identity conflicts with immutable MPDB snapshot: "
                + ", ".join(conflicts)
            )
        target = snapshot_binding.get("execution_target") or {}
        target_expected = {
            "provider_id": provider,
            "request_model_id": model,
            "route_profile": route_profile,
            "api_form": api_form,
        }
        target_conflicts = [
            field
            for field, expected in target_expected.items()
            if expected not in (None, "") and target.get(field) != expected
        ]
        if target_conflicts:
            raise ValueError(
                "Job execution target conflicts with immutable MPDB snapshot: "
                + ", ".join(target_conflicts)
            )
    capability_modality = (
        str(model_capability_profile.get("modality") or "")
        if isinstance(model_capability_profile, dict)
        else ""
    )
    payload = {
        "schema_version": PARAMETER_JOB_SPEC_VERSION if job_type == "param_test" else JOB_SPEC_VERSION,
        "type": job_type,
        "modality": capability_modality
        or str((model_profile_database or {}).get("modality") or "")
        or ("image" if job_type == "image_param_test" else "text"),
        "provider": provider,
        "model": model,
        "model_family": model_family
        or (model_profile_database or {}).get("suite_family_id")
        or (model_profile_database or {}).get("family_id"),
        "api_form": api_form or target.get("api_form"),
        "route_profile": route_profile or target.get("route_profile"),
        "model_profile_id": model_profile_id,
        "source_id": snapshot_identity["source_id"],
        "profile_id": snapshot_identity["profile_id"],
        "interface_id": snapshot_identity["interface_id"],
        "test_binding_id": snapshot_identity["test_binding_id"],
        "reference_contract_id": snapshot_identity["reference_contract_id"],
        "transport": transport,
        "workload": workload,
        "request_mode": request_mode,
        "target_rpm": target_rpm,
        "target_tpm": target_tpm,
        "staircase_plan": staircase_plan,
        "cache_plan": cache_plan,
        "soak_plan": soak_plan,
        "image_plan": image_plan,
        # reference_source is retained only inside historical schema 1-3
        # readers.  New schema-v4 jobs persist the stable Contract ID above.
        "reference_route_profile": reference_route_profile,
        "model_capability_profile": model_capability_profile,
    }
    if job_type == "param_test":
        payload["parameter_execution"] = make_parameter_execution(
            DEFAULT_PARAM_TEST_RUNS if param_test_runs is None else param_test_runs,
            tool_validation_mode,
        )
    if result_contract is not None:
        if not isinstance(result_contract, dict):
            raise ValueError("result_contract must be an object when provided.")
        payload["result_contract"] = copy.deepcopy(result_contract)
    if model_profile_database is not None:
        payload["model_profile_database"] = copy.deepcopy(
            model_profile_database
        )
    if parameter_suite is not None:
        from .fixed_parameter_specs import build_fixed_plan
        from .anthropic_cache_reference import SUITE_IDS as CACHE_SUITE_IDS
        cache_selected = parameter_suite in CACHE_SUITE_IDS
        if cache_selected and execution_plan is not None:
            from .test_runner.adapters.fixed_parameter import fixed_domain_from_plan
            from .anthropic_cache_reference import CachePlan
            frozen_fixed = fixed_domain_from_plan(execution_plan)
            if (type(frozen_fixed) is not CachePlan or frozen_fixed.suite_id != parameter_suite
                    or frozen_fixed.snapshot != model_profile_database):
                raise ValueError("Frozen workflow does not describe this exact cache suite and source snapshot")
            fixed = build_fixed_plan(model_profile_database, suite_id=parameter_suite, job_type=job_type,
                                     frozen_plan=frozen_fixed.frozen_payload)
        else:
            fixed = build_fixed_plan(model_profile_database, suite_id=parameter_suite, job_type=job_type,
                                     create_runtime_nonce=cache_selected)
        payload["parameter_suite"] = parameter_suite
        if cache_selected:
            payload["fixed_parameter_plan"] = fixed.frozen_payload
    if execution_plan is not None:
        if not isinstance(execution_plan, dict):
            raise ValueError("execution_plan must be an object when provided.")
        if param_test_runs is not None and param_test_runs != execution_plan.get("run_count"):
            raise ValueError("param_test_runs conflicts with immutable execution_plan")
        payload = freeze_workflow_job(payload, execution_plan, tool_validation_mode)
    parameter_execution_from_job(payload)
    validate_pressure_job_spec(payload)
    if snapshot_required:
        _require_current_mpdb_snapshot_schema(model_profile_database)
    if isinstance(model_profile_database, dict):
        _validate_job_snapshot_consistency(payload)
    return payload


def _result_database_snapshot(payload: dict[str, Any]) -> dict[str, Any] | None:
    database = payload.get("model_profile_database")
    if isinstance(database, dict) and database:
        return database
    capability = payload.get("model_capability_profile")
    if isinstance(capability, dict):
        nested = capability.get("model_profile_database")
        if isinstance(nested, dict) and nested:
            return nested
    return None


def _result_token_validation(
    result: dict[str, Any],
    token_audit_summary: dict[str, Any],
) -> tuple[bool | None, str | None]:
    if "token_validation_pass" in result:
        value = result.get("token_validation_pass")
        return (value, None) if isinstance(value, bool) else (None, None)
    legacy_value = result.get("token_accuracy_pass")
    if isinstance(legacy_value, bool):
        return legacy_value, "token_accuracy_pass"
    if (
        token_audit_summary.get("schema_version")
        == CURRENT_TOKEN_AUDIT_SCHEMA_VERSION
        and isinstance(token_audit_summary.get("pass"), bool)
    ):
        return bool(token_audit_summary["pass"]), "token_audit_summary.pass"
    return None, None


def _current_token_summary_integrity(
    summary: dict[str, Any],
    *,
    require_required_exchange: bool,
) -> tuple[bool, list[str]]:
    """Validate the persisted summary instead of trusting its schema label."""

    reasons: list[str] = []
    if summary.get("schema_version") != CURRENT_TOKEN_AUDIT_SCHEMA_VERSION:
        reasons.append("token_audit_schema_not_current")
    if summary.get("pass") is not True:
        reasons.append("token_audit_summary_not_pass")

    count_fields = (
        "exchange_count",
        "required_exchange_count",
        "validated_exchange_count",
        "validation_failure_count",
        "missing_audit_result_count",
        "invalid_audit_result_count",
        "missing_usage_count",
        "gross_check_count",
        "gross_failure_count",
        "gross_partial_count",
        "completion_check_count",
        "completion_failure_count",
        "completion_unverified_count",
        "arithmetic_check_count",
        "arithmetic_failure_count",
    )
    counts: dict[str, int] = {}
    for field in count_fields:
        value = summary.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            reasons.append(f"token_audit_summary_invalid_{field}")
        else:
            counts[field] = value

    if len(counts) == len(count_fields):
        exchanges = counts["exchange_count"]
        required = counts["required_exchange_count"]
        validated = counts["validated_exchange_count"]
        if exchanges < 1:
            reasons.append("token_audit_summary_empty")
        if required > exchanges:
            reasons.append("token_audit_summary_required_exceeds_exchanges")
        if validated != required:
            reasons.append("token_audit_summary_required_not_fully_validated")
        if counts["gross_check_count"] != required:
            reasons.append("token_audit_summary_gross_check_count_mismatch")
        if counts["completion_check_count"] != required:
            reasons.append("token_audit_summary_completion_check_count_mismatch")
        if counts["arithmetic_check_count"] != exchanges:
            reasons.append("token_audit_summary_arithmetic_check_count_mismatch")
        failure_fields = (
            "validation_failure_count",
            "missing_audit_result_count",
            "invalid_audit_result_count",
            "missing_usage_count",
            "gross_failure_count",
            "gross_partial_count",
            "completion_failure_count",
            "completion_unverified_count",
            "arithmetic_failure_count",
        )
        if any(counts[field] != 0 for field in failure_fields):
            reasons.append("token_audit_summary_contains_failures")
        expected_statuses = {"pass"} if required else {"not_applicable"}
        if summary.get("validation_status") not in expected_statuses:
            reasons.append("token_audit_summary_invalid_validation_status")
        if require_required_exchange and required < 1:
            reasons.append("successful_result_without_required_token_exchange")

    return not reasons, sorted(set(reasons))


def _result_identity_matches(
    job_spec: dict[str, Any],
    result: dict[str, Any],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if (job_spec.get("parameter_suite") or None) != (result.get("parameter_suite") or None):
        reasons.append("parameter_suite_mismatch")
    if any(value is not None and not isinstance(value, str)
           for value in (job_spec.get("parameter_suite"), result.get("parameter_suite"))):
        reasons.append("parameter_suite_invalid_type")
    if job_spec.get("parameter_suite"):
        from .fixed_parameter_specs import build_fixed_plan, fixed_suite_descriptor
        try:
            plan = build_fixed_plan(job_spec.get("model_profile_database"), suite_id=job_spec["parameter_suite"],
                                    job_type=job_spec.get("type"), frozen_plan=job_spec.get("fixed_parameter_plan"))
            from .anthropic_cache_reference import SUITE_IDS as CACHE_SUITE_IDS, CachePlan, canonical_bytes as cache_bytes
            if job_spec["parameter_suite"] in CACHE_SUITE_IDS and type(plan) is not CachePlan:
                raise ValueError("Cache history requires its frozen versioned job plan")
            descriptor = fixed_suite_descriptor(job_spec["parameter_suite"])
            observation = result.get(descriptor["observation_key"]) or {}
            if (observation.get("suite_id") != job_spec["parameter_suite"]
                    or observation.get("snapshot_digest") != plan.snapshot["snapshot_digest"]
                    or result.get("planned_requests") != plan.request_cap):
                reasons.append("fixed_suite_observation_identity_mismatch")
            if type(plan) is CachePlan and (observation.get("fixed_plan_digest") != plan.plan_digest
                    or result.get("fixed_parameter_plan_digest") != plan.plan_digest):
                reasons.append("fixed_cache_plan_digest_mismatch")
            if type(plan) is CachePlan and plan.schema_version == 2:
                if (type(observation.get("plan_schema_version")) is not int or observation.get("plan_schema_version") != 2
                        or cache_bytes(observation.get("cache_evaluation_policy")) != cache_bytes(plan.frozen_payload["cache_evaluation_policy"])):
                    reasons.append("fixed_cache_evaluation_policy_mismatch")
            if result.get("pass") is True and (observation.get("complete") is not True or observation.get("pass") is not True
                    or [r.get("case_id") for r in observation.get("case_results", [])] != [r.case_id for r in plan.requests]
                    or any(r.get("pass") is not True for r in observation.get("case_results", []))):
                reasons.append("fixed_suite_results_incomplete")
        except (KeyError, TypeError, ValueError, RuntimeError, AttributeError):
            reasons.append("invalid_fixed_suite_snapshot")
    job_database = _result_database_snapshot(job_spec)
    result_database = _result_database_snapshot(result)
    if job_database is None:
        return False, ["job_missing_mpdb_snapshot"]
    if result_database is None:
        return False, ["result_missing_mpdb_snapshot"]

    from .model_profile_catalog import binding_from_database_snapshot

    try:
        job_binding = binding_from_database_snapshot(job_database)
    except (KeyError, RuntimeError, TypeError, ValueError):
        return False, ["job_invalid_mpdb_snapshot"]
    try:
        result_binding = binding_from_database_snapshot(result_database)
    except (KeyError, RuntimeError, TypeError, ValueError):
        return False, ["result_invalid_mpdb_snapshot"]

    job_digest = str(job_database.get("snapshot_digest") or "")
    result_digest = str(result_database.get("snapshot_digest") or "")
    job_snapshot_version = job_database.get("snapshot_schema_version")
    job_snapshot_version = (
        job_snapshot_version
        if isinstance(job_snapshot_version, int)
        and not isinstance(job_snapshot_version, bool)
        else 0
    )
    result_snapshot_version = result_database.get("snapshot_schema_version")
    result_snapshot_version = (
        result_snapshot_version
        if isinstance(result_snapshot_version, int)
        and not isinstance(result_snapshot_version, bool)
        else 0
    )
    if job_snapshot_version < 1 or not job_digest:
        reasons.append("job_mpdb_snapshot_not_current")
    if result_snapshot_version < 1 or not result_digest:
        reasons.append("result_mpdb_snapshot_not_current")
    if job_digest and result_digest and job_digest != result_digest:
        reasons.append("mpdb_snapshot_digest_mismatch")

    binding_fields = (
        "source_id",
        "profile_id",
        "interface_id",
        "test_binding_id",
        "reference_contract_id",
    )
    for field in binding_fields:
        expected = str(job_binding.get(field) or "")
        actual = str(result_binding.get(field) or "")
        if expected != actual:
            reasons.append(f"mpdb_{field}_mismatch")
        declared = result.get(field)
        if declared not in (None, "") and str(declared) != expected:
            reasons.append(f"result_{field}_mismatch")

    job_target = job_binding.get("execution_target")
    job_target = job_target if isinstance(job_target, dict) else {}
    result_target = result_binding.get("execution_target")
    result_target = result_target if isinstance(result_target, dict) else {}
    target_fields = {
        "provider": "provider_id",
        "model": "request_model_id",
        "route_profile": "route_profile",
        "api_form": "api_form",
    }
    for public_field, target_field in target_fields.items():
        expected = str(job_target.get(target_field) or job_spec.get(public_field) or "")
        actual = str(result_target.get(target_field) or "")
        if expected != actual:
            reasons.append(f"mpdb_{public_field}_mismatch")
        declared = result.get(public_field)
        if declared not in (None, "") and str(declared) != expected:
            reasons.append(f"result_{public_field}_mismatch")

    return not reasons, sorted(set(reasons))


def _current_job_snapshot_integrity(
    job_spec: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Validate every schema-v4 Job identity view against its MPDB snapshot."""

    if "test_workflow_snapshot" in job_spec:
        if job_spec.get("schema_version") != WORKFLOW_JOB_SPEC_VERSION:
            return False, ["workflow_snapshot_requires_schema_v6"]
        from .test_runner.snapshot import validate_workflow_snapshot
        try:
            validate_workflow_snapshot(job_spec["test_workflow_snapshot"], job=job_spec)
        except (KeyError, TypeError, ValueError):
            return False, ["job_invalid_test_workflow_snapshot"]
        return True, []

    reasons: list[str] = []
    if job_spec.get("schema_version") != JOB_SPEC_VERSION and not (
        job_spec.get("schema_version") == PARAMETER_JOB_SPEC_VERSION
        and job_spec.get("type") == "param_test"
    ) and not (
        job_spec.get("schema_version") == WORKFLOW_JOB_SPEC_VERSION
        and job_spec.get("type") in WORKFLOW_JOB_TYPES
    ):
        reasons.append("job_schema_not_current")

    top_declared = "model_profile_database" in job_spec
    top_snapshot = job_spec.get("model_profile_database")
    capability = job_spec.get("model_capability_profile")
    capability = capability if isinstance(capability, dict) else {}
    nested_declared = "model_profile_database" in capability
    nested_snapshot = capability.get("model_profile_database")
    for label, declared, snapshot in (
        ("top", top_declared, top_snapshot),
        ("nested", nested_declared, nested_snapshot),
    ):
        if declared and (not isinstance(snapshot, dict) or not snapshot):
            reasons.append(f"job_{label}_mpdb_snapshot_invalid")
    if (
        top_declared
        and nested_declared
        and isinstance(top_snapshot, dict)
        and isinstance(nested_snapshot, dict)
        and top_snapshot != nested_snapshot
    ):
        reasons.append("job_mpdb_snapshot_views_conflict")

    snapshot = (
        top_snapshot
        if isinstance(top_snapshot, dict) and top_snapshot
        else nested_snapshot
        if isinstance(nested_snapshot, dict) and nested_snapshot
        else None
    )
    if not isinstance(snapshot, dict):
        reasons.append("job_missing_mpdb_snapshot")
        return False, sorted(set(reasons))
    version = snapshot.get("snapshot_schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != CURRENT_MPDB_SNAPSHOT_SCHEMA_VERSION
    ):
        reasons.append("job_mpdb_snapshot_schema_not_current")

    from .model_profile_catalog import binding_from_database_snapshot

    try:
        binding = binding_from_database_snapshot(snapshot)
    except (KeyError, RuntimeError, TypeError, ValueError):
        reasons.append("job_invalid_mpdb_snapshot")
        return False, sorted(set(reasons))

    identity_fields = (
        "source_id",
        "profile_id",
        "interface_id",
        "test_binding_id",
        "reference_contract_id",
    )
    for field in identity_fields:
        if str(job_spec.get(field) or "") != str(binding.get(field) or ""):
            reasons.append(f"job_{field}_mismatch")
    reference_source = job_spec.get("reference_source")
    if reference_source not in (None, "") and str(reference_source) != str(
        binding.get("reference_contract_id") or ""
    ):
        reasons.append("job_reference_source_mismatch")

    target = binding.get("execution_target")
    target = target if isinstance(target, dict) else {}
    interface = binding.get("interface")
    interface = interface if isinstance(interface, dict) else {}
    public_target_fields = {
        "provider": "provider_id",
        "model": "request_model_id",
        "route_profile": "route_profile",
        "api_form": "api_form",
    }
    for public_field, target_field in public_target_fields.items():
        if str(job_spec.get(public_field) or "") != str(
            target.get(target_field) or ""
        ):
            reasons.append(f"job_{public_field}_mismatch")
    snapshot_transport = str(
        target.get("transport") or interface.get("transport_adapter_id") or ""
    )
    if job_spec.get("transport") not in (None, "") and str(
        job_spec.get("transport")
    ) != snapshot_transport:
        reasons.append("job_transport_mismatch")

    execution_view = job_spec.get("execution_target")
    if execution_view is not None:
        if not isinstance(execution_view, dict):
            reasons.append("job_execution_target_invalid")
        else:
            expected_execution = {
                target_field: target.get(target_field)
                for target_field in public_target_fields.values()
            }
            expected_execution["transport"] = job_spec.get("transport")
            for field, expected in expected_execution.items():
                if (
                    field not in execution_view
                    or execution_view.get(field) != expected
                ):
                    reasons.append(f"job_execution_target_{field}_mismatch")

    reference_view = job_spec.get("reference_identity")
    if reference_view is not None:
        if not isinstance(reference_view, dict):
            reasons.append("job_reference_identity_invalid")
        else:
            for field in identity_fields:
                expected = job_spec.get(field)
                if (
                    field not in reference_view
                    or reference_view.get(field) != expected
                ):
                    reasons.append(f"job_reference_identity_{field}_mismatch")

    reasons = sorted(set(reasons))
    return not reasons, reasons


def _current_cache_verdict_integrity(result: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate the persisted cache verdict produced by the current runner."""

    reasons: list[str] = []
    schema_version = result.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != CURRENT_CACHE_VERDICT_SCHEMA_VERSION
    ):
        reasons.append("cache_verdict_schema_not_current")
    if result.get("stage") != "cache":
        reasons.append("cache_verdict_stage_invalid")
    if result.get("completed") is not True:
        reasons.append("cache_verdict_not_completed")
    if result.get("latency_evidence_only") is not True:
        reasons.append("cache_verdict_latency_policy_invalid")
    if result.get("official_usage_required_for_hit_rate") is not True:
        reasons.append("cache_verdict_usage_policy_invalid")

    mode = result.get("mode")
    if mode not in {"observe", "gate", "hard_fail"}:
        reasons.append("cache_verdict_mode_invalid")
    threshold_pass = result.get("threshold_pass")
    if not isinstance(threshold_pass, bool):
        reasons.append("cache_verdict_threshold_pass_invalid")
    failures = result.get("failures")
    if not isinstance(failures, list) or any(
        not isinstance(item, dict) for item in failures
    ):
        reasons.append("cache_verdict_failures_invalid")
        failures = None
    if isinstance(threshold_pass, bool) and failures is not None:
        if threshold_pass != (len(failures) == 0):
            reasons.append("cache_verdict_threshold_inconsistent")

    summary = result.get("summary")
    if not isinstance(summary, dict) or not summary:
        reasons.append("cache_verdict_summary_missing")
        summary = {}

    count_fields = (
        "record_count",
        "business_record_count",
        "business_success_count",
        "cache_usage_fields_seen",
        "cache_usage_accuracy_record_count",
        "cache_usage_accuracy_failure_count",
        "cache_control_positive_warm_count",
        "cache_control_negative_count",
    )
    counts: dict[str, int] = {}
    for field in count_fields:
        value = summary.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            reasons.append(f"cache_verdict_summary_invalid_{field}")
        else:
            counts[field] = value
    if len(counts) == len(count_fields):
        if counts["record_count"] < 1:
            reasons.append("cache_verdict_summary_empty")
        if counts["business_record_count"] > counts["record_count"]:
            reasons.append("cache_verdict_business_count_exceeds_records")
        if counts["business_success_count"] > counts["business_record_count"]:
            reasons.append("cache_verdict_success_count_exceeds_business_count")
        if counts["cache_usage_fields_seen"] > counts["record_count"]:
            reasons.append("cache_verdict_usage_count_exceeds_records")
        if counts["cache_usage_accuracy_record_count"] > counts["record_count"]:
            reasons.append("cache_verdict_audit_count_exceeds_records")

    accuracy_status = summary.get("cache_usage_accuracy_status")
    if accuracy_status not in {"pass", "partial", "not_available", "fail"}:
        reasons.append("cache_verdict_accuracy_status_invalid")
    accuracy_pass = summary.get("cache_usage_accuracy_pass")
    if not isinstance(accuracy_pass, bool):
        reasons.append("cache_verdict_accuracy_pass_invalid")
    elif accuracy_status in {"pass", "partial", "not_available", "fail"}:
        if accuracy_pass != (accuracy_status != "fail"):
            reasons.append("cache_verdict_accuracy_status_inconsistent")
    accuracy_failures = counts.get("cache_usage_accuracy_failure_count")
    if accuracy_failures is not None and isinstance(accuracy_pass, bool):
        if (accuracy_failures > 0) != (accuracy_pass is False):
            reasons.append("cache_verdict_accuracy_failure_count_inconsistent")

    controls_present = summary.get("cache_controls_present")
    if not isinstance(controls_present, bool):
        reasons.append("cache_verdict_controls_presence_invalid")
    positive_count = counts.get("cache_control_positive_warm_count")
    negative_count = counts.get("cache_control_negative_count")
    if (
        isinstance(controls_present, bool)
        and positive_count is not None
        and negative_count is not None
        and controls_present != (positive_count > 0 and negative_count > 0)
    ):
        reasons.append("cache_verdict_controls_presence_inconsistent")

    def valid_ratio(value: Any) -> bool:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
            and 0.0 <= float(value) <= 1.0
        )

    reported_ratio = summary.get(
        "cached_input_token_ratio", summary.get("cache_hit_rate")
    )
    usage_evidence = (
        counts.get("cache_usage_fields_seen", 0) > 0
        and valid_ratio(reported_ratio)
    )
    control_metrics = summary.get("cache_control_metrics")
    control_metrics = control_metrics if isinstance(control_metrics, dict) else {}
    positive_control = control_metrics.get("positive_long_prefix")
    negative_control = control_metrics.get("negative_unique_prefix")
    control_evidence = bool(
        controls_present is True
        and positive_count
        and negative_count
        and isinstance(positive_control, dict)
        and isinstance(negative_control, dict)
        and valid_ratio(positive_control.get("cached_input_token_ratio"))
        and valid_ratio(negative_control.get("cached_input_token_ratio"))
    )
    reported_pass = result.get("pass")
    if reported_pass is True and not (usage_evidence or control_evidence):
        reasons.append("cache_verdict_missing_usage_or_control_evidence")

    if (
        isinstance(reported_pass, bool)
        and isinstance(threshold_pass, bool)
        and isinstance(accuracy_pass, bool)
        and mode in {"observe", "gate", "hard_fail"}
    ):
        expected_pass = (
            False
            if result.get("completed") is not True or accuracy_pass is False
            or (summary.get("cache_evaluation_policy") == "scenario_expectations_v1" and summary.get("cache_expectations_pass") is not True)
            else threshold_pass
            if mode in {"gate", "hard_fail"}
            else True
        )
        if reported_pass != expected_pass:
            reasons.append("cache_verdict_pass_inconsistent")

    reasons = sorted(set(reasons))
    return not reasons, reasons


def classify_cache_result(
    job_spec: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    """Classify a persisted cache verdict without reinterpreting old jobs."""

    spec = job_spec if isinstance(job_spec, dict) else {}
    output = result if isinstance(result, dict) else {}
    reasons: list[str] = []
    if spec.get("type") != "cache_suite":
        reasons.append("job_type_not_cache_suite")
    if spec.get("schema_version") != JOB_SPEC_VERSION:
        reasons.append("cache_job_schema_not_current")
    if not output:
        reasons.append("missing_cache_verdict")

    job_integrity, job_reasons = _current_job_snapshot_integrity(spec)
    result_identity_match, identity_reasons = _result_identity_matches(spec, output)
    identity_match = job_integrity and result_identity_match
    reasons.extend(job_reasons)
    reasons.extend(identity_reasons)
    for label, payload in (("job", spec), ("result", output)):
        database = _result_database_snapshot(payload)
        version = (
            database.get("snapshot_schema_version")
            if isinstance(database, dict)
            else None
        )
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != CURRENT_MPDB_SNAPSHOT_SCHEMA_VERSION
        ):
            reasons.append(f"{label}_mpdb_snapshot_schema_not_current")

    reported_pass = output.get("pass")
    if not isinstance(reported_pass, bool):
        reasons.append("missing_boolean_cache_verdict_pass")
    verdict_current, verdict_reasons = _current_cache_verdict_integrity(output)
    if not verdict_current:
        reasons.extend(verdict_reasons)

    reasons = sorted(set(reasons))
    current = not reasons
    return {
        "status": (
            "current_pass"
            if current and reported_pass is True
            else "current_fail"
            if current
            else "legacy_unverified"
        ),
        "pass": current and reported_pass is True,
        "tested": current,
        "legacy": not current,
        "reported_pass": reported_pass if isinstance(reported_pass, bool) else None,
        "identity_match": identity_match,
        "reasons": reasons,
    }


def classify_parameter_result(
    job_spec: dict[str, Any] | None,
    result: dict[str, Any] | None,
    *,
    job_type: str | None = None,
) -> dict[str, Any]:
    """Classify persisted parameter/image output without trusting legacy PASS."""

    spec = job_spec if isinstance(job_spec, dict) else {}
    output = result if isinstance(result, dict) else {}
    declared_type = str(job_type or spec.get("type") or "")
    control_status, control_reasons = parameter_execution_result_state(spec, output)
    reasons: list[str] = list(control_reasons)
    fallbacks: list[str] = []
    verdict_pass = output.get("pass")
    if not isinstance(verdict_pass, bool):
        verdict_pass = None
        reasons.append("missing_boolean_pass")

    summary = output.get("token_audit_summary")
    summary = summary if isinstance(summary, dict) else {}
    audit_schema = summary.get("schema_version")
    if isinstance(audit_schema, bool) or not isinstance(audit_schema, int):
        audit_schema = None
    token_validation_pass, fallback = _result_token_validation(output, summary)
    if fallback:
        fallbacks.append(fallback)
        reasons.append("token_validation_compatibility_fallback")
    primary_token_validation = (
        output.get("token_validation_pass")
        if isinstance(output.get("token_validation_pass"), bool)
        else None
    )
    if primary_token_validation is None:
        reasons.append("missing_boolean_token_validation_pass")

    contract = spec.get("result_contract")
    contract = contract if isinstance(contract, dict) else {}
    token_policy = contract.get("token_policy")
    from .gpt_image_25_web_audit import AUDIT_SCHEMA_VERSION, POLICY_NAME, validate_policy_summary
    if contract.get("validation_policy") == POLICY_NAME:
        # An explicit scoped policy has its own audit schema. Never reinterpret
        # these records as generic text-token schema 4 evidence.
        policy_ok, policy_reasons = validate_policy_summary(spec, output)
        contract_ok = (
            spec.get("schema_version") in {JOB_SPEC_VERSION, WORKFLOW_JOB_SPEC_VERSION}
            and contract.get("schema_version") == RESULT_CONTRACT_SCHEMA_VERSION
            and contract.get("token_audit_schema_version") == AUDIT_SCHEMA_VERSION
            and contract.get("token_validation_field") == "token_validation_pass"
            and isinstance(token_policy, dict)
            and token_policy.get("enabled") is True
            and token_policy.get("evidence_policy") == POLICY_NAME
            and token_policy.get("exact_media_input_required") is False
            and token_policy.get("exact_image_output_required") is False
        )
        job_integrity, job_reasons = _current_job_snapshot_integrity(spec)
        identity, identity_reasons = _result_identity_matches(spec, output)
        identity_match = job_integrity and identity
        policy_reasons.extend(control_reasons + job_reasons + identity_reasons)
        if not contract_ok:
            policy_reasons.append("dedicated_image_result_contract_invalid")
        if declared_type != "image_param_test":
            policy_reasons.append("unsupported_result_type")
        if not isinstance(verdict_pass, bool):
            policy_reasons.append("missing_boolean_pass")
        if primary_token_validation is not True:
            policy_reasons.append("token_validation_not_pass")
        complete = (policy_ok and contract_ok and identity_match and not control_reasons
                    and declared_type == "image_param_test" and isinstance(verdict_pass, bool)
                    and primary_token_validation is True)
        status = ("current_pass" if verdict_pass else "current_fail") if complete else (
            "identity_mismatch" if not identity_match else "token_validation_failed")
        return {
            "schema_version": RESULT_CONTRACT_SCHEMA_VERSION, "status": status,
            "current": complete, "verified": complete, "tested": complete,
            "pass": bool(complete and verdict_pass), "legacy": False,
            "verdict_pass": verdict_pass, "validation_policy": POLICY_NAME,
            "token_audit_schema_version": audit_schema,
            "token_validation_pass": primary_token_validation,
            "primary_token_validation_pass": primary_token_validation,
            "token_audit_summary_current": policy_ok,
            "parameter_execution_status": control_status, "identity_match": identity_match,
            "compatibility_fallbacks": [], "reasons": sorted(set(policy_reasons)),
        }
    spec_schema = spec.get("schema_version")
    spec_schema = (
        spec_schema
        if isinstance(spec_schema, int) and not isinstance(spec_schema, bool)
        else 0
    )
    contract_current = (
        spec_schema >= JOB_SPEC_VERSION
        and contract.get("schema_version") == RESULT_CONTRACT_SCHEMA_VERSION
        and contract.get("token_audit_schema_version")
        == CURRENT_TOKEN_AUDIT_SCHEMA_VERSION
        and contract.get("token_validation_field") == "token_validation_pass"
        and isinstance(token_policy, dict)
        and token_policy.get("enabled") is True
    )
    if not contract_current:
        reasons.append("legacy_or_missing_result_contract")
    if audit_schema != CURRENT_TOKEN_AUDIT_SCHEMA_VERSION:
        reasons.append("token_audit_schema_not_current")
    if token_validation_pass is not True:
        reasons.append("token_validation_not_pass")
    summary_current, summary_reasons = _current_token_summary_integrity(
        summary,
        require_required_exchange=verdict_pass is True,
    )
    reasons.extend(summary_reasons)

    job_integrity, job_reasons = _current_job_snapshot_integrity(spec)
    result_identity_match, identity_reasons = _result_identity_matches(spec, output)
    identity_match = job_integrity and result_identity_match
    reasons.extend(job_reasons)
    reasons.extend(identity_reasons)
    if declared_type not in PARAMETER_RESULT_JOB_TYPES:
        reasons.append("unsupported_result_type")
    if not output:
        reasons.append("missing_result")

    complete = (
        declared_type in PARAMETER_RESULT_JOB_TYPES
        and not control_reasons
        and bool(output)
        and contract_current
        and audit_schema == CURRENT_TOKEN_AUDIT_SCHEMA_VERSION
        and primary_token_validation is True
        and summary_current
        and identity_match
        and verdict_pass is not None
    )
    legacy = (
        not contract_current
        or audit_schema is None
        or audit_schema < CURRENT_TOKEN_AUDIT_SCHEMA_VERSION
        or fallback is not None
    )
    if complete:
        status = "current_pass" if verdict_pass is True else "current_fail"
    elif legacy:
        status = "legacy_unverified"
    elif not output:
        status = "unverified"
    elif not identity_match:
        status = "identity_mismatch"
    elif control_reasons:
        status = "parameter_execution_mismatch"
    elif token_validation_pass is not True or not summary_current:
        status = "token_validation_failed"
    else:
        status = "unverified"
    return {
        "schema_version": RESULT_CONTRACT_SCHEMA_VERSION,
        "status": status,
        "current": complete,
        "verified": complete,
        "tested": complete,
        "pass": bool(complete and verdict_pass is True),
        "legacy": legacy,
        "verdict_pass": verdict_pass,
        "token_audit_schema_version": audit_schema,
        "token_validation_pass": token_validation_pass,
        "primary_token_validation_pass": primary_token_validation,
        "token_audit_summary_current": summary_current,
        "parameter_execution_status": control_status,
        "identity_match": identity_match,
        "compatibility_fallbacks": fallbacks,
        "reasons": sorted(set(reasons)),
    }


def classify_workflow_result(
    job_spec: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    """Classify workflow evidence independently from protocol/token audit fields."""
    spec = job_spec if isinstance(job_spec, dict) else {}
    output = result if isinstance(result, dict) else {}
    control_status, reasons = workflow_execution_result_state(spec, output)
    reasons = list(reasons)
    if spec.get("schema_version") != WORKFLOW_JOB_SPEC_VERSION:
        reasons.append("workflow_job_schema_not_current")
    identity_match, identity_reasons = _current_job_snapshot_integrity(spec)
    reasons.extend(identity_reasons)
    report = output.get("workflow_result", output)
    report = report if isinstance(report, dict) else {}
    reported_status = report.get("status")
    current = control_status == "frozen" and identity_match and not reasons
    if current:
        status = "current_pass" if reported_status == "passed" else "current_fail"
    elif control_status == "incomplete":
        status = "cleanup_incomplete"
    elif not identity_match:
        status = "identity_mismatch"
    else:
        status = "workflow_execution_mismatch"
    return {"schema_version": RESULT_CONTRACT_SCHEMA_VERSION, "status": status,
            "current": current, "verified": current, "tested": current,
            "pass": current and reported_status == "passed", "legacy": False,
            "reported_status": reported_status, "identity_match": identity_match,
            "parameter_execution_status": control_status,
            "reasons": sorted(set(reasons))}


def validate_pressure_job_spec(payload: dict[str, Any]) -> None:
    """Reject media profile/interface markers for pressure job types."""
    if str(payload.get("type") or "") not in PRESSURE_JOB_TYPES:
        return

    schema_version = int(payload.get("schema_version") or 0)
    if schema_version >= JOB_SPEC_VERSION:
        declared_modality = str(payload.get("modality") or "").strip().casefold()
        if declared_modality != "text":
            raise ValueError(
                "Schema-v4 pressure job specs must declare modality='text'."
            )

    capability = payload.get("model_capability_profile")
    capability = capability if isinstance(capability, dict) else {}
    database = payload.get("model_profile_database")
    if not isinstance(database, dict):
        nested = capability.get("model_profile_database")
        database = nested if isinstance(nested, dict) else {}

    interface_candidates = [
        payload.get("interface"),
        capability.get("interface"),
        database.get("interface"),
    ]
    interfaces = [row for row in interface_candidates if isinstance(row, dict)]
    profile_candidates = [
        payload.get("profile"),
        capability.get("profile"),
        database.get("profile"),
    ]
    profiles = [row for row in profile_candidates if isinstance(row, dict)]
    modality_markers = {
        str(value).strip().casefold()
        for value in (
            payload.get("modality"),
            capability.get("modality"),
            database.get("modality"),
            *(row.get("modality") for row in interfaces),
            *(row.get("modality") for row in profiles),
        )
        if str(value or "").strip()
    }
    identifiers = [
        payload.get("profile_id"),
        payload.get("interface_id"),
        capability.get("profile_id"),
        capability.get("interface_id"),
        database.get("profile_id"),
        database.get("interface_id"),
        *(row.get("profile_id") for row in interfaces),
        *(row.get("interface_id") for row in interfaces),
        *(row.get("profile_id") for row in profiles),
    ]
    for identifier in identifiers:
        prefix = str(identifier or "").split("/", 1)[0].strip().casefold()
        if prefix:
            modality_markers.add(prefix)
    media = sorted(modality_markers & NON_PRESSURE_MODALITIES)
    if media:
        raise ValueError(
            "Pressure job specs cannot target image/video modalities: "
            + ", ".join(media)
            + "."
        )


def load_job_spec(path: str | Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    target = Path(path)
    if not target.exists():
        raise RuntimeError(f"Job spec does not exist: {target}")
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) not in {
        1,
        2,
        3,
        JOB_SPEC_VERSION,
        PARAMETER_JOB_SPEC_VERSION,
        WORKFLOW_JOB_SPEC_VERSION,
    }:
        raise RuntimeError(f"Unsupported job spec: {target}")
    try:
        validate_pressure_job_spec(payload)
    except ValueError as exc:
        raise RuntimeError(f"Invalid pressure job spec in {target}: {exc}") from exc
    try:
        parameter_execution_from_job(payload)
    except ValueError as exc:
        raise RuntimeError(f"Invalid parameter execution controls in {target}: {exc}") from exc
    schema_version = int(payload.get("schema_version") or 0)
    if "test_workflow_snapshot" in payload:
        if schema_version != WORKFLOW_JOB_SPEC_VERSION:
            raise RuntimeError("test_workflow_snapshot requires a schema-v6 functional Job")
        from .test_runner.snapshot import validate_workflow_snapshot
        try:
            validate_workflow_snapshot(payload["test_workflow_snapshot"], job=payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid test workflow snapshot in {target}: {exc}") from exc
        return payload
    capability = payload.get("model_capability_profile")
    has_database_snapshot = "model_profile_database" in payload or (
        isinstance(capability, dict) and "model_profile_database" in capability
    )
    snapshot_required = (
        schema_version >= JOB_SPEC_VERSION
        and str(payload.get("type") or "")
        not in SCHEMA_V4_SNAPSHOT_OPTIONAL_JOB_TYPES
    )
    if snapshot_required and not has_database_snapshot:
        raise RuntimeError(
            f"Schema-v4 executable Job in {target} requires an immutable MPDB "
            "snapshot."
        )
    if snapshot_required:
        raw_snapshot = payload.get("model_profile_database")
        if not isinstance(raw_snapshot, dict) or not raw_snapshot:
            raw_snapshot = (
                capability.get("model_profile_database")
                if isinstance(capability, dict)
                else None
            )
        if isinstance(raw_snapshot, dict) and raw_snapshot:
            try:
                _require_current_mpdb_snapshot_schema(raw_snapshot)
            except ValueError as exc:
                raise RuntimeError(
                    f"Invalid model profile database snapshot in {target}: {exc}"
                ) from exc
    if schema_version >= JOB_SPEC_VERSION:
        resolved = resolve_job_model_profile_snapshot(payload)
        if resolved.get("resolution_status") == "snapshot_invalid":
            raise RuntimeError(
                f"Invalid model profile database snapshot in {target}: "
                f"{resolved.get('error') or resolved.get('resolution_status')}"
            )
        if snapshot_required and resolved.get("resolution_status") != "snapshot":
            raise RuntimeError(
                f"Invalid model profile database snapshot in {target}: "
                f"{resolved.get('error') or resolved.get('resolution_status')}"
            )
        if resolved.get("resolution_status") == "snapshot":
            try:
                _validate_job_snapshot_consistency(payload)
            except ValueError as exc:
                raise RuntimeError(f"Invalid job spec in {target}: {exc}") from exc
    return payload


def resolve_job_model_profile_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Restore only immutable job data; never reinterpret through providers."""

    if "test_workflow_snapshot" in payload:
        if payload.get("schema_version") != WORKFLOW_JOB_SPEC_VERSION:
            return {"resolution_status": "snapshot_invalid", "legacy_unresolved": True,
                    "error": "test_workflow_snapshot requires a schema-v6 functional Job"}
        from .test_runner.snapshot import validate_workflow_snapshot
        try:
            identity = validate_workflow_snapshot(payload["test_workflow_snapshot"], job=payload)
        except (KeyError, TypeError, ValueError) as exc:
            return {"resolution_status": "snapshot_invalid", "legacy_unresolved": True,
                    "error": str(exc)}
        return {**copy.deepcopy(identity), "resolution_status": "snapshot",
                "legacy_unresolved": False}

    schema_version = int(payload.get("schema_version") or 0)
    top_level_snapshot_declared = "model_profile_database" in payload
    database = payload.get("model_profile_database")
    capability = payload.get("model_capability_profile")
    capability_snapshot = (
        copy.deepcopy(capability) if isinstance(capability, dict) else {}
    )
    nested_snapshot_declared = "model_profile_database" in capability_snapshot
    nested_database = capability_snapshot.get("model_profile_database")
    if schema_version >= JOB_SPEC_VERSION:
        malformed_views = [
            label
            for label, declared, value in (
                ("model_profile_database", top_level_snapshot_declared, database),
                (
                    "model_capability_profile.model_profile_database",
                    nested_snapshot_declared,
                    nested_database,
                ),
            )
            if declared and (not isinstance(value, dict) or not value)
        ]
        if malformed_views:
            return {
                "resolution_status": "snapshot_invalid",
                "source_id": payload.get("source_id"),
                "profile_id": payload.get("profile_id"),
                "interface_id": payload.get("interface_id"),
                "legacy_unresolved": True,
                "error": (
                    "Schema-v4 Job contains an empty or malformed snapshot: "
                    + ", ".join(malformed_views)
                ),
            }
        if (
            top_level_snapshot_declared
            and nested_snapshot_declared
            and database != nested_database
        ):
            return {
                "resolution_status": "snapshot_invalid",
                "source_id": payload.get("source_id"),
                "profile_id": payload.get("profile_id"),
                "interface_id": payload.get("interface_id"),
                "legacy_unresolved": True,
                "error": (
                    "Top-level and nested model profile database snapshots conflict."
                ),
            }
    if not isinstance(database, dict) or not database:
        if isinstance(nested_database, dict) and nested_database:
            database = nested_database
    if isinstance(database, dict) and database:
        from .model_profile_catalog import binding_from_database_snapshot

        try:
            binding_from_database_snapshot(database)
        except ValueError as exc:
            return {
                "resolution_status": "snapshot_invalid",
                "source_id": database.get("source_id"),
                "profile_id": database.get("profile_id"),
                "interface_id": database.get("interface_id"),
                "legacy_unresolved": True,
                "error": str(exc),
            }
        identity_fields = (
            "source_id",
            "profile_id",
            "interface_id",
            "test_binding_id",
            "reference_contract_id",
        )
        conflicts = [
            field
            for field in identity_fields
            if payload.get(field) not in (None, "", database.get(field))
        ]
        if conflicts:
            return {
                "resolution_status": "snapshot_invalid",
                "source_id": database.get("source_id"),
                "profile_id": database.get("profile_id"),
                "interface_id": database.get("interface_id"),
                "legacy_unresolved": True,
                "error": (
                    "Job identity conflicts with immutable MPDB snapshot: "
                    + ", ".join(conflicts)
                ),
            }
        execution_target = database.get("execution_target")
        execution_target = (
            execution_target if isinstance(execution_target, dict) else {}
        )
        target_fields = {
            "provider_id": payload.get("provider"),
            "request_model_id": payload.get("model"),
            "route_profile": payload.get("route_profile"),
            "api_form": payload.get("api_form"),
        }
        target_conflicts = [
            field
            for field, expected in target_fields.items()
            if expected not in (None, "")
            and execution_target.get(field) != expected
        ]
        if target_conflicts:
            return {
                "resolution_status": "snapshot_invalid",
                "source_id": database.get("source_id"),
                "profile_id": database.get("profile_id"),
                "interface_id": database.get("interface_id"),
                "legacy_unresolved": True,
                "error": (
                    "Job execution target conflicts with immutable MPDB snapshot: "
                    + ", ".join(target_conflicts)
                ),
            }
        result = copy.deepcopy(database)
        result["resolution_status"] = "snapshot"
        result["legacy_unresolved"] = False
        return result

    # Historical schema 1-3 capability blobs are themselves the only
    # immutable evidence available.  Keep them displayable, but do not query
    # today's catalog or provider routes to manufacture a current identity.
    if capability_snapshot:
        return {
            "resolution_status": "legacy_snapshot",
            "legacy_profile_id": payload.get("model_profile_id")
            or capability_snapshot.get("model_api_profile_id"),
            "source_id": payload.get("source_id"),
            "profile_id": payload.get("profile_id"),
            "interface_id": payload.get("interface_id"),
            "test_binding_id": payload.get("test_binding_id"),
            "reference_contract_id": payload.get("reference_contract_id")
            or payload.get("reference_source"),
            "legacy_unresolved": not bool(payload.get("interface_id")),
            "legacy_capability_snapshot": capability_snapshot,
        }
    if any(payload.get(field) for field in ("profile_id", "interface_id")):
        return {
            "resolution_status": "legacy_job_identity",
            "source_id": payload.get("source_id"),
            "profile_id": payload.get("profile_id"),
            "interface_id": payload.get("interface_id"),
            "test_binding_id": payload.get("test_binding_id"),
            "reference_contract_id": payload.get("reference_contract_id"),
            "legacy_unresolved": not bool(payload.get("interface_id")),
        }
    return {
        "resolution_status": "legacy_unresolved",
        "legacy_profile_id": payload.get("model_profile_id"),
        "interface_id": None,
        "legacy_unresolved": True,
    }


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
