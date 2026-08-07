from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.config import (
    default_reports_root,
    ensure_dir,
    get_active_provider_name,
    get_selected_model,
    load_config,
    parse_duration_seconds,
    resolve_threshold_config,
)
from lib.job_spec import load_job_spec
from lib.credential_security import build_provider_child_env
from lib.metrics import write_json
from lib.threshold import (
    check_staircase,
    staircase_step_quality_failures,
    summarize_records_file,
)


def main() -> int:
    config = load_config()
    job_spec = load_job_spec(os.getenv("LOADTEST_JOB_SPEC"))
    staircase_cfg = (
        dict(job_spec.get("staircase_plan") or {})
        if job_spec
        else dict(config.get("staircase") or {})
    )
    threshold_cfg = resolve_threshold_config(
        config,
        "staircase",
        get_active_provider_name(config),
        get_selected_model(config),
        staircase_cfg.get("thresholds") if isinstance(staircase_cfg, dict) else None,
    )
    config.setdefault("thresholds", {})["staircase"] = threshold_cfg
    target_rpm_override = os.getenv("LOADTEST_TARGET_RPM")
    if target_rpm_override and float(target_rpm_override) > 0:
        threshold_cfg["target_business_rpm_min"] = float(target_rpm_override)
    target_tpm_override = os.getenv("LOADTEST_TARGET_TPM")
    if target_tpm_override and float(target_tpm_override) > 0:
        threshold_cfg["target_total_tpm_min"] = float(target_tpm_override)
    output_root = ensure_dir(Path(os.getenv("LOADTEST_REPORT_DIR") or default_reports_root() / "staircase"))
    step_duration = str(staircase_cfg.get("step_duration", "5m"))
    step_duration_sec = parse_duration_seconds(step_duration)
    spawn_rate = int(staircase_cfg.get("spawn_rate", 5))
    workload = str((job_spec or {}).get("workload") or os.getenv("LOADTEST_WORKLOAD") or staircase_cfg.get("workload", "throughput"))
    if workload == "mixed_compat" or not workload.startswith("throughput"):
        raise RuntimeError("Staircase requires a deterministic throughput workload.")

    configured_steps = [
        int(step.get("users")) if isinstance(step, dict) else int(step)
        for step in staircase_cfg.get("steps", [])
    ]
    if not configured_steps:
        raise RuntimeError("config.staircase.steps must contain at least one step.")

    auto_extend = staircase_cfg.get("auto_extend") or {}
    auto_enabled = bool(auto_extend.get("enabled", False))
    increment_users = int(auto_extend.get("increment_users", 30))
    max_users = int(auto_extend.get("max_users", configured_steps[-1]))
    target_rpm = float(threshold_cfg.get("target_business_rpm_min", 500))
    target_tpm = float(threshold_cfg.get("target_total_tpm_min", 0))
    target_tokens_per_request = (
        target_tpm / target_rpm if target_rpm > 0 and target_tpm > 0 else 0.0
    )

    step_summaries: list[dict[str, Any]] = []
    step_users = list(configured_steps)
    warmup_cfg = staircase_cfg.get("warmup") or config.get("warmup") or {}
    warmup_per_step = bool(warmup_cfg.get("per_step", False))
    if warmup_cfg.get("enabled", False) and not warmup_per_step:
        run_locust(
            config=config,
            report_dir=output_root / "warmup",
            users=int(warmup_cfg.get("users", min(configured_steps[0], 10))),
            spawn_rate=spawn_rate,
            duration=str(warmup_cfg.get("duration", "1m")),
            workload=str(warmup_cfg.get("workload") or workload),
            phase="warmup",
            staircase_step=0,
            target_rpm=target_rpm,
            target_tpm=target_tpm,
            target_tokens_per_request=target_tokens_per_request,
        )
    index = 0
    while index < len(step_users):
        users = step_users[index]
        step_number = index + 1
        print(f"[staircase] step {step_number}: users={users}, duration={step_duration}")

        step_dir = ensure_dir(output_root / f"step_{step_number:02d}")
        if warmup_cfg.get("enabled", False) and warmup_per_step:
            warmup_workload = str(warmup_cfg.get("workload") or workload)
            run_locust(
                config=config,
                report_dir=step_dir / "warmup",
                users=int(warmup_cfg.get("users", min(users, 10))),
                spawn_rate=spawn_rate,
                duration=str(warmup_cfg.get("duration", "1m")),
                workload=warmup_workload,
                phase="warmup",
                staircase_step=step_number,
                target_rpm=target_rpm,
                target_tpm=target_tpm,
                target_tokens_per_request=(
                    target_tokens_per_request
                    if warmup_workload == workload
                    else 0
                ),
            )

        measured_dir = step_dir / "measure"
        run_locust(
            config=config,
            report_dir=measured_dir,
            users=users,
            spawn_rate=spawn_rate,
            duration=step_duration,
            workload=workload,
            phase="measure",
            staircase_step=step_number,
            target_rpm=target_rpm,
            target_tpm=target_tpm,
            target_tokens_per_request=target_tokens_per_request,
        )

        summary = summarize_records_file(
            measured_dir / config.get("metrics", {}).get("records_file", "request_records.jsonl"),
            config,
            duration_sec=step_duration_sec,
            business_group=str(threshold_cfg.get("count_profile_group", "throughput_profiles")),
        )
        summary["step"] = step_number
        summary["users"] = users
        quality_failures = staircase_step_quality_failures(summary, threshold_cfg)
        summary["quality_pass"] = not quality_failures
        summary["quality_failures"] = quality_failures
        summary["target_rpm_pass"] = float(summary.get("business_rpm") or 0) >= target_rpm
        summary["target_tpm_pass"] = target_tpm <= 0 or float(summary.get("total_tpm") or 0) >= target_tpm
        summary["qualified"] = bool(
            summary["quality_pass"]
            and summary["target_rpm_pass"]
            and summary["target_tpm_pass"]
        )
        step_summaries.append(summary)
        write_json(
            output_root / "staircase_progress.json",
            {
                "phase": "measured",
                "completed_steps": len(step_summaries),
                "planned_steps": len(step_users),
                "latest_step": summary,
                "steps": step_summaries,
            },
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))

        if quality_failures:
            print(f"[staircase] stopping after quality failure at step {step_number}")
            break

        targets_met = any(bool(item.get("qualified")) for item in step_summaries)
        is_last_configured = index >= len(configured_steps) - 1
        all_quality_pass = all(bool(item.get("quality_pass")) for item in step_summaries)
        if is_last_configured and not targets_met and all_quality_pass and auto_enabled:
            next_users = users + increment_users
            if next_users <= max_users:
                step_users.append(next_users)
            else:
                print(f"[staircase] auto_extend stopped at max_users={max_users}")
        index += 1

    verdict = check_staircase(step_summaries, config, output_root)
    verdict["effective_staircase_plan"] = staircase_cfg
    write_json(output_root / "verdict.json", verdict)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if verdict["pass"] else 1


def run_locust(
    *,
    config: dict[str, Any],
    report_dir: Path,
    users: int,
    spawn_rate: int,
    duration: str,
    workload: str,
    phase: str,
    staircase_step: int,
    target_rpm: float | None = None,
    target_tpm: float | None = None,
    target_tokens_per_request: float | None = None,
) -> None:
    ensure_dir(report_dir)
    env = build_provider_child_env(config, get_active_provider_name(config))
    env.update(
        {
            "LOADTEST_WORKLOAD": workload,
            "LOADTEST_PHASE": phase,
            "LOADTEST_REPORT_DIR": str(report_dir),
            "LOADTEST_USERS": str(users),
            "LOADTEST_STAIRCASE_STEP": str(staircase_step),
        }
    )
    if target_rpm is None:
        target_rpm = float(os.getenv("LOADTEST_TARGET_RPM", "0") or 0)
    if target_tpm is None:
        target_tpm = float(os.getenv("LOADTEST_TARGET_TPM", "0") or 0)
    # Staircase RPM/TPM values are qualification goals, not request-rate
    # limits. Keep their ratio below for adaptive sizing, but disable the
    # Locust limiters so each step can expose the provider's attempted peak.
    env.pop("LOADTEST_TARGET_RPM", None)
    env.pop("LOADTEST_TARGET_TPM", None)
    if target_tokens_per_request is None:
        target_tokens_per_request = (
            target_tpm / target_rpm if target_rpm > 0 and target_tpm > 0 else 0.0
        )
    if target_tokens_per_request > 0:
        env["LOADTEST_TARGET_TOKENS_PER_REQUEST"] = str(target_tokens_per_request)
    else:
        env.pop("LOADTEST_TARGET_TOKENS_PER_REQUEST", None)
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
        duration,
        "--exit-code-on-error",
        "0",
        "--csv",
        str(report_dir / "locust"),
        "--html",
        str(report_dir / "report.html"),
    ]
    completed = subprocess.run(cmd, cwd=PROJECT_ROOT, env=env, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Locust failed for {phase} run in {report_dir} with exit {completed.returncode}")


if __name__ == "__main__":
    raise SystemExit(main())
