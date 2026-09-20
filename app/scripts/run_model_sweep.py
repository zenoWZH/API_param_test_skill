from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.client import OpenAICompatibleClient
from lib.adaptive_load import filter_context_unsafe_profiles
from lib.config import (
    default_reports_root,
    ensure_dir,
    get_model_api_form,
    get_model_family,
    get_model_route_profile,
    get_provider_interface,
    get_provider_config,
    get_timeout_sec,
    load_config,
    parse_duration_seconds,
)
from lib.credential_security import build_provider_child_env
from lib.deepseek_params import build_request, weighted_workload_profiles
from lib.load_rate import ConstantThroughputPlan, validate_fixed_rate_sweep_plan
from lib.job_spec import load_job_spec, make_job_spec
from lib.metrics import (
    classify_pressure_failure,
    load_records,
    percentile,
    record_in_measurement_window,
    write_json,
)
from lib.model_identity import audit_model_identity, allowed_model_identities
from lib.model_profile_catalog import resolve_runtime_parameter_config
from lib.threshold import summarize_records_file


_ROUTING_ENV_NAMES = (
    "LOADTEST_PROVIDER",
    "LOADTEST_MODEL",
    "LOADTEST_ROUTE_PROFILE",
    "LOADTEST_API_FORM",
)


def main() -> int:
    config = load_config()
    provider = os.getenv("LOADTEST_PROVIDER", "inferenceai")
    provider_cfg = get_provider_config(config, provider)
    try:
        target_rpm = float(os.getenv("LOADTEST_TARGET_RPM", "600") or 600)
        duration = os.getenv("LOADTEST_SWEEP_DURATION", "10m")
        duration_sec = parse_duration_seconds(duration)
        users = int(os.getenv("LOADTEST_SWEEP_USERS", "120"))
        spawn_rate = int(os.getenv("LOADTEST_SWEEP_SPAWN_RATE", "30"))
        workload = os.getenv("LOADTEST_SWEEP_WORKLOAD", "throughput_rpm")
        validate_fixed_rate_sweep_plan(
            target_rpm=target_rpm,
            duration_sec=duration_sec,
            users=users,
            spawn_rate=spawn_rate,
            workload=workload,
        )

        explicit_models = [
            item.strip()
            for item in os.getenv("LOADTEST_MODELS", "").split(",")
            if item.strip()
        ]
        candidates = list(
            dict.fromkeys(
                explicit_models
                or list((provider_cfg.get("models") or {}).get("candidates") or [])
            )
        )
        models = [model for model in candidates if "latest" not in model.lower()]
        excluded = [model for model in candidates if "latest" in model.lower()]
        if not models:
            raise ValueError("sweep has no immutable model candidates")
        model_slugs = [_slug(model) for model in models]
        if len(set(model_slugs)) != len(model_slugs):
            raise ValueError("model identifiers collide after report-path normalization")

        prepared = {
            model: _prepare_model_execution(config, provider, model, workload)
            for model in models
        }
        job_specs = {
            model: _sweep_job_spec(
                prepared[model],
                provider=provider,
                model=model,
                workload=workload,
                target_rpm=target_rpm,
            )
            for model in models
        }
        output_root = _reserve_output_root(os.getenv("LOADTEST_REPORT_DIR"))
        report_dirs = {
            model: _reserve_model_report_dir(output_root, _slug(model))
            for model in models
        }
        job_spec_paths: dict[str, Path] = {}
        for model in models:
            job_spec_path = report_dirs[model] / "job_spec.json"
            write_json(job_spec_path, job_specs[model])
            job_spec_paths[model] = job_spec_path
    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
        print(
            json.dumps({"error": f"invalid sweep plan: {exc}"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2

    sweep: dict[str, Any] = {
        "provider": provider,
        "provider_label": provider_cfg.get("label") or provider,
        "base_url": provider_cfg.get("base_url"),
        "target_rpm": target_rpm,
        "duration": duration,
        "duration_sec": duration_sec,
        "users": users,
        "spawn_rate": spawn_rate,
        "workload": workload,
        "excluded_models": excluded,
        "models": models,
        "results": [],
    }
    write_json(output_root / "sweep_results.json", sweep)

    print(f"[sweep] provider={provider} target_rpm={target_rpm:g} duration={duration} users={users}", flush=True)
    if excluded:
        print(f"[sweep] excluded latest models: {', '.join(excluded)}", flush=True)
    print(f"[sweep] models: {', '.join(models)}", flush=True)

    for index, model in enumerate(models, start=1):
        print(f"[sweep] ({index}/{len(models)}) preflight {model}", flush=True)
        row: dict[str, Any] = {
            "model": model,
            "status": "preflight",
            "report_dir": str(report_dirs[model]),
        }
        preflight = _preflight(
            config,
            provider,
            model,
            prepared=prepared[model],
        )
        row["preflight"] = preflight
        if not preflight.get("success"):
            row["status"] = "preflight_failed"
            sweep["results"].append(row)
            write_json(output_root / "sweep_results.json", sweep)
            print(f"[sweep] ({index}/{len(models)}) skip {model}: {preflight.get('failure')}", flush=True)
            continue

        report_dir = report_dirs[model]
        print(f"[sweep] ({index}/{len(models)}) run {model} -> {report_dir}", flush=True)
        return_code = _run_locust(
            config=config,
            provider=provider,
            model=model,
            report_dir=report_dir,
            users=users,
            spawn_rate=spawn_rate,
            duration=duration,
            workload=workload,
            target_rpm=target_rpm,
            prepared=prepared[model],
            job_spec_path=job_spec_paths[model],
        )
        records_path = report_dir / (
            config.get("metrics", {}).get("records_file", "request_records.jsonl")
        )
        summary = summarize_records_file(
            records_path,
            config,
            duration_sec=duration_sec,
            business_group="throughput_profiles",
        )
        summary["model_identity_audit"] = _recorded_model_identity_audit(
            records_path,
            provider_cfg,
            model,
        )
        expected_profiles = [
            str(profile)
            for profile, weight in (
                (config.get("profile_weights") or {}).get(workload) or {}
            ).items()
            if profile in (config.get("throughput_profiles") or {})
            and float(weight or 0) > 0
        ]
        profile_mix = _recorded_profile_mix(
            records_path,
            expected_profiles,
            require_full_coverage=workload == "throughput_rpm",
        )
        summary["profile_counts"] = profile_mix["profile_counts"]
        summary["profile_mix_audit"] = profile_mix
        passed = _run_passed(return_code, summary, target_rpm)
        row.update(
            {
                "status": _completion_status(return_code, passed),
                "return_code": return_code,
                "summary": summary,
                "pass": passed,
            }
        )
        sweep["results"].append(row)
        write_json(report_dir / "summary.json", summary)
        write_json(output_root / "sweep_results.json", sweep)
        print(
            "[sweep] ({}/{}) done {} rpm={:.2f} success={:.4f} p95={} failures={}".format(
                index,
                len(models),
                model,
                float(summary.get("business_rpm") or 0),
                float(summary.get("success_rate") or 0),
                summary.get("p95_latency_ms"),
                summary.get("business_failure_count"),
            ),
            flush=True,
        )

    write_json(output_root / "sweep_results.json", sweep)
    print(f"[sweep] complete -> {output_root / 'sweep_results.json'}", flush=True)
    failed = [item for item in sweep["results"] if not item.get("pass")]
    return 1 if failed else 0


def _reserve_output_root(requested: str | None) -> Path:
    if requested:
        target = Path(requested).expanduser()
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = (
            default_reports_root()
            / "jobs"
            / f"model_sweep_{stamp}_{uuid.uuid4().hex[:8]}"
        )
    if target.is_symlink() or target.exists():
        raise ValueError(
            f"sweep report directory must not already exist: {target}"
        )
    ensure_dir(target.parent)
    target.mkdir(mode=0o700)
    return target


def _reserve_model_report_dir(output_root: Path, slug: str) -> Path:
    target = output_root / slug
    if target.is_symlink() or target.exists():
        raise ValueError(f"model report directory already exists: {target}")
    target.mkdir(mode=0o700)
    return target


def _prepare_model_execution(
    config: dict[str, Any],
    provider: str,
    model: str,
    workload: str,
) -> dict[str, Any]:
    probe_config = copy.deepcopy(config)
    probe_config["active_provider"] = provider
    provider_models = (
        (probe_config.get("providers") or {}).get(provider, {}).setdefault(
            "models", {}
        )
    )
    provider_models["default"] = model
    with _cleared_environment(*_ROUTING_ENV_NAMES):
        family = get_model_family(probe_config, model, provider)
        route_profile = get_model_route_profile(probe_config, model, provider)
        api_form = get_model_api_form(
            probe_config,
            model,
            provider,
            route_profile=route_profile,
        )
        parameter_config = resolve_runtime_parameter_config(
            probe_config,
            provider,
            model,
            family,
            route_profile,
            api_form,
            modality="text",
        )
        probe_config["_model_profile_database"] = copy.deepcopy(
            parameter_config["model_profile_database"]
        )
        reference_contract_id = str(parameter_config["contract_id"])
        entries = weighted_workload_profiles(
            probe_config,
            workload,
            api_form=api_form,
            route_profile=route_profile,
            reference_source=reference_contract_id,
        )
        entries, _skipped = filter_context_unsafe_profiles(
            probe_config,
            get_provider_config(probe_config, provider),
            model,
            entries,
        )
        built_requests: dict[tuple[str, str], Any] = {}
        for group, profile, _weight in entries:
            if group == "control":
                continue
            built = build_request(
                probe_config,
                group,
                profile,
                api_form_override=api_form,
                route_profile_override=route_profile,
                reference_source=reference_contract_id,
            )
            transport = str(built.metadata.get("transport") or "")
            get_provider_interface(probe_config, transport, provider)
            built_requests[(group, profile)] = built
    preflight_request = built_requests.get(
        ("throughput_profiles", "standard_short")
    )
    if preflight_request is None:
        preflight_request = next(iter(built_requests.values()), None)
    if preflight_request is None:
        raise ValueError(f"model {model!r} has no executable workload profiles")
    return {
        "config": probe_config,
        "request": preflight_request,
        "model_profile_database": copy.deepcopy(
            parameter_config["model_profile_database"]
        ),
        "reference_contract_id": reference_contract_id,
        "family": family,
        "route_profile": route_profile,
        "api_form": api_form,
        "workload_profiles": sorted(
            f"{group}:{profile}" for group, profile in built_requests
        ),
    }


def _sweep_job_spec(
    prepared: dict[str, Any],
    *,
    provider: str,
    model: str,
    workload: str,
    target_rpm: float,
) -> dict[str, Any]:
    from lib.model_profile_catalog import capability_profile_from_database_snapshot

    database = copy.deepcopy(prepared["model_profile_database"])
    capability = capability_profile_from_database_snapshot(database)
    return make_job_spec(
        job_type="quick_load",
        provider=provider,
        model=model,
        model_family=str(prepared["family"]),
        api_form=str(prepared["api_form"]),
        route_profile=str(prepared["route_profile"]),
        model_profile_id=str(capability.get("interface_id") or ""),
        source_id=str(capability.get("source_id") or ""),
        profile_id=str(capability.get("profile_id") or ""),
        interface_id=str(capability.get("interface_id") or ""),
        test_binding_id=str(capability.get("test_binding_id") or ""),
        reference_contract_id=str(
            capability.get("reference_contract_id") or ""
        ),
        model_profile_database=database,
        transport=str(capability.get("transport") or ""),
        workload=workload,
        request_mode="fixed",
        target_rpm=target_rpm,
        target_tpm=0.0,
        model_capability_profile=capability,
    )


def _preflight(
    config: dict[str, Any],
    provider: str,
    model: str,
    *,
    prepared: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prepared = prepared or _prepare_model_execution(
        config,
        provider,
        model,
        "throughput_rpm",
    )
    client = OpenAICompatibleClient.from_config(prepared["config"], provider)
    built = prepared["request"]
    transport = str(built.metadata.get("transport") or "chat_completions")
    result = _dispatch_preflight(client, transport, model, built.body)
    configured_reasons = (config.get("metrics") or {}).get(
        "failure_finish_reasons"
    )
    failure = classify_pressure_failure(
        result.status_code,
        result.finish_reason,
        result.error_type,
        configured_reasons if isinstance(configured_reasons, list) else None,
    )
    identity_audit = audit_model_identity(
        requested_model=model,
        result=result,
        transport=transport,
        provider_cfg=get_provider_config(prepared["config"], provider),
        exchange="model_sweep_preflight",
        request_endpoint=str(built.metadata.get("request_endpoint") or ""),
    )
    identity_failure = (
        None
        if identity_audit.get("status") == "match"
        else f"model_identity:{identity_audit.get('status') or 'unverifiable'}"
    )
    return {
        "success": result.success and failure is None and identity_failure is None,
        "status_code": result.status_code,
        "latency_ms": result.latency_ms,
        "failure": (
            failure
            or result.failure_classification
            or result.error_type
            or identity_failure
        ),
        "finish_reason": result.finish_reason,
        "response_model": identity_audit.get("returned_model"),
        "model_identity_audit": identity_audit,
        "transport": transport,
        "raw": (result.raw_text or "")[:1000],
    }


def _dispatch_preflight(
    client: OpenAICompatibleClient,
    transport: str,
    model: str,
    body: dict[str, Any],
) -> Any:
    if transport == "gemini_generate_content":
        return client.gemini_generate_content(model, body)
    method_name = {
        "chat_completions": "chat_completion",
        "claude_messages": "claude_messages",
        "gemini_interactions": "gemini_interactions",
        "openai_responses": "openai_responses",
        "fim_completions": "fim_completion",
    }.get(transport)
    sender = getattr(client, method_name, None) if method_name else None
    if not callable(sender):
        raise ValueError(f"Unsupported sweep preflight transport: {transport!r}.")
    return sender(body)


@contextmanager
def _cleared_environment(*names: str) -> Iterator[None]:
    previous = {name: os.environ[name] for name in names if name in os.environ}
    for name in names:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        for name in names:
            os.environ.pop(name, None)
        os.environ.update(previous)


def _run_locust(
    *,
    config: dict[str, Any],
    provider: str,
    model: str,
    report_dir: Path,
    users: int,
    spawn_rate: int,
    duration: str,
    workload: str,
    target_rpm: float,
    prepared: dict[str, Any],
    job_spec_path: Path,
) -> int:
    route_profile = str(prepared["route_profile"])
    api_form = str(prepared["api_form"])
    if not job_spec_path.is_file():
        raise ValueError(f"sweep job spec does not exist: {job_spec_path}")
    job_spec = load_job_spec(job_spec_path)
    persisted_database = (
        job_spec.get("model_profile_database")
        if isinstance(job_spec, dict)
        else None
    )
    prepared_database = prepared.get("model_profile_database")
    if not isinstance(persisted_database, dict) or not isinstance(
        prepared_database, dict
    ):
        raise ValueError("sweep job spec is missing its prepared MPDB snapshot")
    expected_digest = str(prepared_database.get("snapshot_digest") or "")
    expected_contract = str(
        prepared_database.get("reference_contract_id") or ""
    )
    if (
        not expected_digest
        or persisted_database.get("snapshot_digest") != expected_digest
        or persisted_database.get("reference_contract_id") != expected_contract
    ):
        raise ValueError(
            "sweep job spec conflicts with the prepared MPDB snapshot"
        )
    env = build_provider_child_env(prepared["config"], provider)
    env.pop("LOADTEST_JOB_SPEC", None)
    env.pop("LOADTEST_HTTP_EXTRA_HEADERS", None)
    rate_plan = ConstantThroughputPlan.build(target_rpm, users)
    if rate_plan is not None and spawn_rate < rate_plan.aggregate_rps:
        raise ValueError("spawn_rate must be at least target_rpm / 60")
    measure_sec = parse_duration_seconds(duration)
    warmup_sec = rate_plan.user_period_sec if rate_plan is not None else 0.0
    env.update(
        {
            "LOADTEST_PROVIDER": provider,
            "LOADTEST_MODEL": model,
            "LOADTEST_ROUTE_PROFILE": route_profile,
            "LOADTEST_API_FORM": api_form,
            "LOADTEST_WORKLOAD": workload,
            "LOADTEST_PHASE": "measure",
            "LOADTEST_TARGET_RPM": str(target_rpm),
            "LOADTEST_TARGET_TPM": "0",
            "LOADTEST_TARGET_TOKENS_PER_REQUEST": "0",
            "LOADTEST_USERS": str(users),
            "LOADTEST_REQUEST_MODE": (
                "fixed" if workload == "throughput_rpm" else "unique"
            ),
            "LOADTEST_WARMUP_SEC": str(warmup_sec),
            "LOADTEST_MEASURE_DURATION_SEC": str(measure_sec),
            "LOADTEST_REPORT_DIR": str(report_dir),
            "LOADTEST_JOB_SPEC": str(job_spec_path),
            "LOADTEST_EXPECTED_MPDB_SNAPSHOT_DIGEST": expected_digest,
            "LOADTEST_EXPECTED_REFERENCE_CONTRACT_ID": expected_contract,
        }
    )
    cmd = [
        sys.executable,
        "-m",
        "locust",
        "-f",
        "locustfile.py",
        "--headless",
        "-u",
        str(users),
        "-r",
        str(spawn_rate),
        "-t",
        f"{measure_sec + warmup_sec:g}s",
        "--stop-timeout",
        str(get_timeout_sec(config)),
        "--csv",
        str(report_dir / "locust"),
        "--html",
        str(report_dir / "report.html"),
    ]
    completed = subprocess.run(cmd, cwd=PROJECT_ROOT, env=env, check=False)
    return completed.returncode


def _summary_pass(summary: dict[str, Any], target_rpm: float) -> bool:
    profile_mix = summary.get("profile_mix_audit")
    return (
        int(summary.get("business_record_count") or 0) > 0
        and float(summary.get("business_rpm") or 0) >= target_rpm * 0.95
        and float(summary.get("success_rate") or 0) >= 0.99
        and float(summary.get("error_429_ratio") or 0) <= 0.01
        and float(summary.get("error_5xx_ratio") or 0) <= 0.01
        and (summary.get("model_identity_audit") or {}).get("status") == "match"
        and isinstance(profile_mix, dict)
        and profile_mix.get("pass") is True
    )


def _run_passed(
    return_code: int,
    summary: dict[str, Any],
    target_rpm: float,
) -> bool:
    return return_code == 0 and _summary_pass(summary, target_rpm)


def _recorded_model_identity_audit(
    records_path: Path,
    provider_cfg: dict[str, Any],
    requested_model: str,
) -> dict[str, Any]:
    records = [
        item
        for item in load_records(records_path)
        if item.group == "throughput_profiles"
        and not item.is_retry
        and record_in_measurement_window(item)
        and item.status_code is not None
        and 200 <= item.status_code <= 299
    ]
    allowed = set(allowed_model_identities(provider_cfg, requested_model))
    returned = [
        str(item.extra.get("response_model"))
        for item in records
        if isinstance(item.extra, dict)
        and item.extra.get("response_model") not in (None, "")
    ]
    returned_counts = Counter(returned)
    missing_count = len(records) - len(returned)
    mismatched = sorted(model for model in returned_counts if model not in allowed)
    if mismatched:
        status = "mismatch"
    elif not records or missing_count:
        status = "unverifiable"
    else:
        status = "match"
    conflicts: list[str] = []
    if mismatched:
        conflicts.append(
            f"unallowed response models observed: {mismatched}"
        )
    if missing_count:
        conflicts.append(
            f"{missing_count} HTTP 2xx records omitted a response model identity"
        )
    if not records:
        conflicts.append("no measured HTTP 2xx records were available for identity audit")
    return {
        "schema_version": 1,
        "status": status,
        "pass": status == "match",
        "requested_model": requested_model,
        "allowed_identities": sorted(allowed),
        "http_2xx_record_count": len(records),
        "verified_record_count": len(returned),
        "missing_identity_count": missing_count,
        "returned_model_counts": dict(sorted(returned_counts.items())),
        "mismatched_models": mismatched,
        "conflicts": conflicts,
    }


def _recorded_profile_mix(
    records_path: Path,
    expected_profiles: list[str],
    *,
    require_full_coverage: bool = True,
) -> dict[str, Any]:
    records = [
        item
        for item in load_records(records_path)
        if item.group == "throughput_profiles"
        and not item.is_retry
        and record_in_measurement_window(item)
    ]
    expected = list(dict.fromkeys(str(item) for item in expected_profiles if item))
    profile_counts = Counter(item.profile for item in records)
    observed = sorted(profile_counts)
    missing = [profile for profile in expected if profile_counts[profile] <= 0]
    unexpected = [profile for profile in observed if profile not in expected]
    profiles: dict[str, dict[str, Any]] = {}
    for profile in [*expected, *unexpected]:
        selected = [item for item in records if item.profile == profile]
        count = len(selected)
        http_request_count = sum(
            1 for item in selected if item.status_code is not None
        )
        http_2xx = sum(
            1
            for item in selected
            if item.status_code is not None and 200 <= item.status_code <= 299
        )
        business_success = sum(1 for item in selected if item.success)
        refusal = sum(
            1
            for item in selected
            if str(item.finish_reason or "").strip().casefold() == "refusal"
            or str(item.failure_classification or "").strip().casefold()
            == "finish_reason:refusal"
        )
        http_5xx = sum(
            1
            for item in selected
            if item.status_code is not None and 500 <= item.status_code <= 599
        )
        profiles[profile] = {
            "request_count": count,
            "http_request_count": http_request_count,
            "http_2xx_count": http_2xx,
            "http_2xx_rate": (
                http_2xx / http_request_count if http_request_count else 0.0
            ),
            "business_success_count": business_success,
            "business_success_rate": business_success / count if count else 0.0,
            "refusal_count": refusal,
            "http_5xx_count": http_5xx,
            "http_502_count": sum(
                1 for item in selected if item.status_code == 502
            ),
            "p95_latency_ms": percentile(
                [item.latency_ms for item in selected if item.latency_ms is not None],
                95,
            ),
        }
    return {
        "schema_version": 1,
        "pass": (
            bool(records)
            and not unexpected
            and (not require_full_coverage or not missing)
        ),
        "coverage_required": require_full_coverage,
        "expected_profiles": expected,
        "observed_profiles": observed,
        "missing_profiles": missing,
        "unexpected_profiles": unexpected,
        "profile_counts": dict(sorted(profile_counts.items())),
        "profiles": profiles,
    }


def _completion_status(return_code: int, passed: bool) -> str:
    if return_code == 0:
        return "completed"
    if passed:
        return "completed_with_request_failures"
    return "process_failed"


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_") or "model"


if __name__ == "__main__":
    raise SystemExit(main())
