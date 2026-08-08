from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_env

DATA_DIR = skill_env.ensure_skill_env()
APP_ROOT = skill_env.APP_ROOT
sys.path.insert(0, str(APP_ROOT))

from lib.config import (  # noqa: E402
    default_reports_root,
    ensure_dir,
    get_model_family,
    get_active_provider_name,
    get_selected_model,
    load_config,
)
from lib.credential_security import build_provider_child_env  # noqa: E402
from lib.job_spec import make_job_spec  # noqa: E402
from lib.metrics import write_json  # noqa: E402

JOB_TYPES = {
    "param_test",
    "cache_suite",
    "image_param_test",
    "quick_load",
    "staircase",
    "soak",
    "trace_test",
}


def _new_job_id(job_type: str, provider: str, model: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{job_type}_{provider}_{model}")
    return f"{stamp}_{safe}_{uuid.uuid4().hex[:8]}"


def _background_fork() -> tuple[bool, int | None]:
    r, w = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid > 0:
        os.close(w)
        data = b""
        while True:
            chunk = os.read(r, 65536)
            if not chunk:
                break
            data += chunk
        os.close(r)
        try:
            info = json.loads(data.decode()) if data else {}
        except Exception:
            info = {}
        if not info:
            info = {"error": "background launch failed"}
        print(json.dumps(info, ensure_ascii=False))
        return True, None
    os.close(r)
    os.setsid()
    return False, w


def _background_child_stdio(w: int, info: dict) -> None:
    os.write(w, json.dumps(info, ensure_ascii=False).encode())
    os.close(w)
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)


def _pid_marker(command: list[str]) -> str:
    for token in command:
        if token.endswith(".py"):
            return os.path.basename(token)
    return ""


def _write_run_json(report_dir: Path, payload: dict) -> None:
    write_json(report_dir / "run.json", payload)


def _finalize_run_json(report_dir: Path, returncode: int) -> None:
    path = report_dir / "run.json"
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        run = {}
    run["returncode"] = returncode
    if returncode < 0:
        run["stop_requested"] = True
    run["finished_at"] = time.time()
    write_json(path, run)


def _payload_from_args(args: argparse.Namespace) -> dict:
    payload: dict = {"type": args.type}
    mapping = {
        "provider": "provider",
        "model": "model",
        "route_profile": "route_profile",
        "api_form": "api_form",
        "reference_source": "reference_source",
        "workload": "workload",
        "users": "users",
        "spawn_rate": "spawn_rate",
        "duration": "duration",
        "runs": "param_test_runs",
        "tool_validation_mode": "tool_validation_mode",
        "cache_measured_requests": "cache_measured_requests",
        "target_rpm": "target_rpm",
        "target_tpm": "target_tpm",
        "timeout_sec": "timeout_sec",
    }
    for arg_name, key in mapping.items():
        value = getattr(args, arg_name, None)
        if value is not None:
            payload[key] = value
    if args.extra_json:
        payload.update(json.loads(args.extra_json))
    return payload


def _run_via_console_manager(args: argparse.Namespace) -> int:
    background = bool(args.background)
    pipe_w = -1
    if background:
        is_parent, pipe_w_result = _background_fork()
        if is_parent:
            return 0
        pipe_w = pipe_w_result if pipe_w_result is not None else -1
    os.environ.setdefault("LLM_API_TEST_SKIP_HISTORY", "1")
    sys.path.insert(0, str(APP_ROOT / "scripts"))
    import web_console

    payload = _payload_from_args(args)
    try:
        job = web_console.JOB_MANAGER.create(payload)
    except Exception as exc:
        if background:
            _background_child_stdio(pipe_w, {"error": str(exc)})
            os._exit(2)
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    run_payload = {
        "pid": job.pid,
        "pid_marker": _pid_marker(job.command),
        "type": job.type,
        "provider": job.provider,
        "provider_label": job.provider_label,
        "model": job.model,
        "model_family": job.model_family,
        "workload": job.workload,
        "api_form": job.api_form,
        "route_profile": job.route_profile,
        "command": job.command,
        "created_at": job.created_at,
        "started_at": job.started_at or time.time(),
        "returncode": None,
        "finished_at": None,
    }
    _write_run_json(job.report_dir, run_payload)
    if background:
        _background_child_stdio(
            pipe_w,
            {
                "job_id": job.id,
                "pid": job.pid,
                "report_dir": str(job.report_dir),
                "status": "running",
            },
        )
        assert job.process is not None
        returncode = job.process.wait()
        _finalize_run_json(job.report_dir, returncode)
        os._exit(returncode)
    assert job.process is not None
    returncode = job.process.wait()
    _finalize_run_json(job.report_dir, returncode)
    print(
        json.dumps(
            {
                "job_id": job.id,
                "status": "completed" if returncode == 0 else "failed",
                "returncode": returncode,
                "report_dir": str(job.report_dir),
            },
            ensure_ascii=False,
        )
    )
    return returncode


def _run_trace(args: argparse.Namespace) -> int:
    try:
        config = load_config()
        provider = args.provider or get_active_provider_name(config)
        model = args.model or get_selected_model(config, provider)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    family = ""
    try:
        family = get_model_family(config, model, provider)
    except Exception:
        pass
    pipe_w = -1
    if args.background:
        is_parent, pipe_w_result = _background_fork()
        if is_parent:
            return 0
        pipe_w = pipe_w_result if pipe_w_result is not None else -1
    job_id = _new_job_id("trace_test", provider, model)
    report_dir = ensure_dir(default_reports_root() / "jobs" / job_id)
    command = [
        str(skill_env.venv_python()),
        "scripts/trace_test.py",
        "compare",
        "--provider",
        provider,
        "--model",
        model,
        "--report-dir",
        str(report_dir),
    ]
    if args.expect:
        command += ["--expect", args.expect]
    job_spec = make_job_spec(
        job_type="trace_test",
        provider=provider,
        model=model,
        workload="trace",
        request_mode="fixed",
        target_rpm=0.0,
        target_tpm=0.0,
        model_family=family,
    )
    write_json(report_dir / "job_spec.json", job_spec)
    try:
        env = build_provider_child_env(
            config,
            provider,
            {
                "LOADTEST_PROVIDER": provider,
                "LOADTEST_MODEL": model,
                "LOADTEST_WORKLOAD": "trace",
                "LOADTEST_REPORT_DIR": str(report_dir),
                "LOADTEST_JOB_SPEC": str(report_dir / "job_spec.json"),
                "PYTHONUNBUFFERED": "1",
            },
        )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    log_fh = (report_dir / "job.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=APP_ROOT,
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    log_fh.close()
    _write_run_json(
        report_dir,
        {
            "pid": process.pid,
            "pid_marker": "trace_test.py",
            "type": "trace_test",
            "provider": provider,
            "model": model,
            "model_family": family,
            "workload": "trace",
            "command": command,
            "created_at": time.time(),
            "started_at": time.time(),
            "returncode": None,
            "finished_at": None,
        },
    )
    if args.background:
        _background_child_stdio(
            pipe_w,
            {
                "job_id": job_id,
                "pid": process.pid,
                "report_dir": str(report_dir),
                "status": "running",
            },
        )
        returncode = process.wait()
        _finalize_run_json(report_dir, returncode)
        os._exit(returncode)
    returncode = process.wait()
    _finalize_run_json(report_dir, returncode)
    verdict_path = report_dir / "verdict.json"
    verdict = {}
    if verdict_path.exists():
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "job_id": job_id,
                "status": "completed" if returncode == 0 else "failed",
                "returncode": returncode,
                "report_dir": str(report_dir),
                "verdict": verdict or None,
            },
            ensure_ascii=False,
        )
    )
    return returncode


def _list_providers() -> int:
    config = load_config()
    from lib.config import get_provider_config

    active = ""
    try:
        active = get_active_provider_name(config)
    except Exception:
        pass
    output = []
    for name in sorted((config.get("providers") or {})):
        try:
            cfg = get_provider_config(config, name)
        except Exception:
            continue
        models_cfg = cfg.get("models") or {}
        output.append(
            {
                "provider": name,
                "label": cfg.get("label"),
                "active": name == active,
                "default_model": models_cfg.get("default"),
                "models": models_cfg.get("candidates") or [],
                "families": models_cfg.get("families") or {},
            }
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launch an LLM test job directly (registered so the web console can see it)."
    )
    parser.add_argument("--type", choices=sorted(JOB_TYPES))
    parser.add_argument(
        "--list-providers",
        action="store_true",
        help="list configured providers and their models, then exit",
    )
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--route-profile", default=None)
    parser.add_argument("--api-form", default=None)
    parser.add_argument("--reference-source", default=None)
    parser.add_argument("--workload", default=None)
    parser.add_argument("--users", type=int, default=None)
    parser.add_argument("--spawn-rate", type=int, default=None)
    parser.add_argument("--duration", default=None)
    parser.add_argument("--runs", type=int, default=None)
    parser.add_argument("--tool-validation-mode", default=None)
    parser.add_argument("--cache-measured-requests", type=int, default=None)
    parser.add_argument("--target-rpm", type=float, default=None)
    parser.add_argument("--target-tpm", type=float, default=None)
    parser.add_argument("--timeout-sec", type=int, default=None)
    parser.add_argument(
        "--expect", default=None, help="trace_test: expected upstream id"
    )
    parser.add_argument(
        "--extra-json",
        default=None,
        help="extra job payload fields as JSON (e.g. image suite options)",
    )
    parser.add_argument("--background", action="store_true")
    args = parser.parse_args()
    if args.list_providers:
        try:
            return _list_providers()
        except Exception as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
    if not args.type:
        parser.error("--type is required (or use --list-providers)")
    if args.type == "trace_test":
        return _run_trace(args)
    return _run_via_console_manager(args)


if __name__ == "__main__":
    raise SystemExit(main())
