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
from lib.threshold import check_soak, load_history_file, summarize_records_file


def main() -> int:
    config = load_config()
    job_spec = load_job_spec(os.getenv("LOADTEST_JOB_SPEC"))
    soak_cfg = (
        dict(job_spec.get("soak_plan") or {})
        if job_spec
        else dict(config.get("soak") or {})
    )
    config.setdefault("thresholds", {})["soak_1h"] = resolve_threshold_config(
        config,
        "soak_1h",
        get_active_provider_name(config),
        get_selected_model(config),
        soak_cfg.get("thresholds") if isinstance(soak_cfg, dict) else None,
    )
    metrics_cfg = config.get("metrics") or {}
    output_root = ensure_dir(Path(os.getenv("LOADTEST_REPORT_DIR") or default_reports_root() / "soak_1h"))
    duration = str(soak_cfg.get("duration", "1h"))
    spawn_rate = int(soak_cfg.get("spawn_rate", 5))
    users = int(soak_cfg.get("users", 80))
    workload = str((job_spec or {}).get("workload") or soak_cfg.get("workload", "throughput"))
    if workload == "mixed_compat" or not workload.startswith("throughput"):
        raise RuntimeError("Soak requires a deterministic throughput workload.")

    warmup_cfg = config.get("warmup") or {}
    if warmup_cfg.get("enabled", False):
        run_locust(
            config=config,
            report_dir=output_root / "warmup",
            users=int(warmup_cfg.get("users", min(users, 10))),
            spawn_rate=spawn_rate,
            duration=str(warmup_cfg.get("duration", "1m")),
            workload=str(
                warmup_cfg.get("workload")
                or workload
            ),
            phase="warmup",
        )

    run_dir = output_root / "run"
    run_locust(
        config=config,
        report_dir=run_dir,
        users=users,
        spawn_rate=spawn_rate,
        duration=duration,
        workload=workload,
        phase="measure",
    )

    summary = summarize_records_file(
        run_dir / metrics_cfg.get("records_file", "request_records.jsonl"),
        config,
        duration_sec=parse_duration_seconds(duration),
        business_group="throughput_profiles",
    )
    history = load_history_file(run_dir / metrics_cfg.get("history_file", "history.jsonl"))
    verdict = check_soak(summary, history, config, output_root)
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
) -> None:
    ensure_dir(report_dir)
    env = build_provider_child_env(config, get_active_provider_name(config))
    env.update(
        {
            "LOADTEST_WORKLOAD": workload,
            "LOADTEST_PHASE": phase,
            "LOADTEST_REPORT_DIR": str(report_dir),
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
