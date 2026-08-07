from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.client import OpenAICompatibleClient
from lib.config import default_reports_root, ensure_dir, get_provider_config, load_config, parse_duration_seconds
from lib.deepseek_params import ensure_minimum_prompt_text
from lib.credential_security import build_provider_child_env
from lib.metrics import write_json
from lib.threshold import summarize_records_file


def main() -> int:
    config = load_config()
    provider = os.getenv("LOADTEST_PROVIDER", "inferenceai")
    provider_cfg = get_provider_config(config, provider)
    target_rpm = float(os.getenv("LOADTEST_TARGET_RPM", "600") or 600)
    duration = os.getenv("LOADTEST_SWEEP_DURATION", "10m")
    duration_sec = parse_duration_seconds(duration)
    users = int(os.getenv("LOADTEST_SWEEP_USERS", "120"))
    spawn_rate = int(os.getenv("LOADTEST_SWEEP_SPAWN_RATE", "30"))
    workload = os.getenv("LOADTEST_SWEEP_WORKLOAD", "throughput")
    output_root = ensure_dir(
        Path(os.getenv("LOADTEST_REPORT_DIR") or default_reports_root() / "jobs" / "model_sweep_600rpm_10m")
    )

    explicit_models = [item.strip() for item in os.getenv("LOADTEST_MODELS", "").split(",") if item.strip()]
    candidates = explicit_models or list((provider_cfg.get("models") or {}).get("candidates") or [])
    models = [model for model in candidates if "latest" not in model.lower()]
    excluded = [model for model in candidates if "latest" in model.lower()]

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
            "report_dir": str(output_root / _slug(model)),
        }
        preflight = _preflight(config, provider, model)
        row["preflight"] = preflight
        if not preflight.get("success"):
            row["status"] = "preflight_failed"
            sweep["results"].append(row)
            write_json(output_root / "sweep_results.json", sweep)
            print(f"[sweep] ({index}/{len(models)}) skip {model}: {preflight.get('failure')}", flush=True)
            continue

        report_dir = ensure_dir(output_root / _slug(model))
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
        )
        summary = summarize_records_file(
            report_dir / (config.get("metrics", {}).get("records_file", "request_records.jsonl")),
            config,
            duration_sec=duration_sec,
            business_group="throughput_profiles",
        )
        passed = _summary_pass(summary, target_rpm)
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


def _preflight(config: dict[str, Any], provider: str, model: str) -> dict[str, Any]:
    client = OpenAICompatibleClient.from_config(config, provider)
    prompt = ensure_minimum_prompt_text(config, "只输出 pong。")
    result = client.chat_completion(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
    )
    return {
        "success": result.success,
        "status_code": result.status_code,
        "latency_ms": result.latency_ms,
        "failure": result.failure_classification or result.error_type,
        "finish_reason": result.finish_reason,
        "response_model": result.response_json.get("model"),
        "raw": (result.raw_text or "")[:1000],
    }


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
) -> int:
    env = build_provider_child_env(config, provider)
    env.update(
        {
            "LOADTEST_PROVIDER": provider,
            "LOADTEST_MODEL": model,
            "LOADTEST_WORKLOAD": workload,
            "LOADTEST_PHASE": "measure",
            "LOADTEST_TARGET_RPM": str(target_rpm),
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
        "--csv",
        str(report_dir / "locust"),
        "--html",
        str(report_dir / "report.html"),
    ]
    completed = subprocess.run(cmd, cwd=PROJECT_ROOT, env=env, check=False)
    return completed.returncode


def _summary_pass(summary: dict[str, Any], target_rpm: float) -> bool:
    return (
        int(summary.get("business_record_count") or 0) > 0
        and float(summary.get("business_rpm") or 0) >= target_rpm * 0.95
        and float(summary.get("success_rate") or 0) >= 0.99
        and float(summary.get("error_429_ratio") or 0) <= 0.01
        and float(summary.get("error_5xx_ratio") or 0) <= 0.01
    )


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
