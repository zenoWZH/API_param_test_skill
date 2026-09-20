from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_env

skill_env.configure_skill_env()
sys.path.insert(0, str(skill_env.APP_ROOT))


def _candidate_plan(provider: str, requested: list[str]) -> dict[str, Any]:
    from lib.config import (
        get_model_api_form,
        get_model_family,
        get_model_route_profile,
        get_provider_config,
        load_config,
    )
    from lib.model_profile_catalog import resolve_runtime_parameter_config

    config = load_config()
    provider_cfg = get_provider_config(config, provider)
    configured = list((provider_cfg.get("models") or {}).get("candidates") or [])
    candidates = list(dict.fromkeys(requested or configured))
    rows: list[dict[str, Any]] = []
    for model in candidates:
        row: dict[str, Any] = {"model": model, "configured": model in configured}
        if "latest" in model.casefold():
            row.update(eligible=False, reason="mutable latest aliases are excluded")
            rows.append(row)
            continue
        try:
            family = get_model_family(config, model, provider)
            route = get_model_route_profile(config, model, provider)
            api_form = get_model_api_form(
                config, model, provider, route_profile=route
            )
            parameter_config = resolve_runtime_parameter_config(
                config,
                provider,
                model,
                family,
                route,
                api_form,
            )
            policy = parameter_config["test_binding"]
            contract = parameter_config["contract"]
            eligible = bool(
                row["configured"]
                and policy.get("pressure_test_enabled") is True
            )
            row.update(
                family=family,
                route_profile=route,
                api_form=api_form,
                source_id=parameter_config.get("source_id"),
                profile_id=parameter_config.get("profile_id"),
                interface_id=parameter_config.get("interface_id"),
                reference_contract_id=contract.get("contract_id"),
                test_binding_id=parameter_config.get("test_binding_id"),
                parameter_test_binding_id=(
                    (parameter_config.get("parameter_test_binding") or {}).get(
                        "test_binding_id"
                    )
                ),
                pressure_test_enabled=policy.get("pressure_test_enabled"),
                eligible=eligible,
            )
            if not eligible:
                row["reason"] = policy.get("disabled_reason") or "not executable for pressure testing"
        except Exception as exc:
            row.update(eligible=False, reason=str(exc))
        rows.append(row)
    return {
        "schema": "llm-api-test.model-sweep-plan.v1",
        "provider": provider,
        "provider_label": provider_cfg.get("label") or provider,
        "configured_candidates": configured,
        "models": rows,
        "all_requested_eligible": bool(rows) and all(row.get("eligible") for row in rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan or explicitly execute the bundled fixed-rate model sweep."
    )
    parser.add_argument("--provider", required=True)
    parser.add_argument(
        "--models",
        default="",
        help="comma-separated explicit model list; omitted means configured candidates for planning only",
    )
    parser.add_argument("--target-rpm", type=float)
    parser.add_argument("--duration")
    parser.add_argument("--users", type=int)
    parser.add_argument("--spawn-rate", type=int)
    parser.add_argument("--workload", default="throughput_rpm")
    parser.add_argument("--report-dir")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--yes", action="store_true", help="confirm real, potentially billable traffic")
    args = parser.parse_args()

    requested = list(
        dict.fromkeys(
            item.strip() for item in args.models.split(",") if item.strip()
        )
    )
    try:
        plan = _candidate_plan(args.provider, requested)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    plan["execution"] = {
        "target_rpm_per_model": args.target_rpm,
        "duration": args.duration,
        "users_per_model": args.users,
        "spawn_rate_per_model": args.spawn_rate,
        "workload": args.workload,
        "report_dir": args.report_dir,
    }
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    missing = [
        name
        for name, value in (
            ("--models", requested),
            ("--target-rpm", args.target_rpm),
            ("--duration", args.duration),
            ("--users", args.users),
            ("--spawn-rate", args.spawn_rate),
        )
        if value in (None, "", [])
    ]
    if missing or not args.yes:
        print(
            json.dumps(
                {
                    "error": "execution requires explicit model/rate/duration/client settings and --yes",
                    "missing": missing + ([] if args.yes else ["--yes"]),
                    "plan": plan,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    if not plan["all_requested_eligible"]:
        print(
            json.dumps({"error": "one or more requested models are ineligible", "plan": plan}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    try:
        from lib.config import parse_duration_seconds
        from lib.load_rate import validate_fixed_rate_sweep_plan

        duration_sec = parse_duration_seconds(str(args.duration))
        validate_fixed_rate_sweep_plan(
            target_rpm=float(args.target_rpm),
            duration_sec=duration_sec,
            users=int(args.users),
            spawn_rate=int(args.spawn_rate),
            workload=str(args.workload),
        )
    except (TypeError, ValueError) as exc:
        print(
            json.dumps(
                {"error": f"invalid sweep execution plan: {exc}", "plan": plan},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    skill_env.ensure_skill_env()
    os.environ.update(
        {
            "LOADTEST_PROVIDER": args.provider,
            "LOADTEST_MODELS": ",".join(requested),
            "LOADTEST_TARGET_RPM": str(args.target_rpm),
            "LOADTEST_SWEEP_DURATION": str(args.duration),
            "LOADTEST_SWEEP_USERS": str(args.users),
            "LOADTEST_SWEEP_SPAWN_RATE": str(args.spawn_rate),
            "LOADTEST_SWEEP_WORKLOAD": args.workload,
        }
    )
    if args.report_dir:
        os.environ["LOADTEST_REPORT_DIR"] = args.report_dir
    sys.path.insert(0, str(skill_env.APP_ROOT / "scripts"))
    import run_model_sweep

    return int(run_model_sweep.main())


if __name__ == "__main__":
    raise SystemExit(main())
