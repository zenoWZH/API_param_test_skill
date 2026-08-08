from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_env

skill_env.ensure_skill_env()
sys.path.insert(0, str(skill_env.APP_ROOT))

from lib.config import default_reports_root  # noqa: E402


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _pid_alive(pid: int, marker: str = "") -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    cmdline = Path(f"/proc/{pid}/cmdline")
    if marker and cmdline.exists():
        try:
            return marker in cmdline.read_bytes().decode("utf-8", "ignore")
        except OSError:
            return True
    return True


def _job_status(report_dir: Path) -> dict:
    job_spec = _read_json(report_dir / "job_spec.json") or {}
    run = _read_json(report_dir / "run.json")
    verdict = _read_json(report_dir / "verdict.json") or {}
    load_result = _read_json(report_dir / "load_result.json") or {}
    summary = _read_json(report_dir / "summary.json") or {}
    info = {
        "job_id": report_dir.name,
        "type": job_spec.get("type") or (run or {}).get("type"),
        "provider": job_spec.get("provider") or (run or {}).get("provider"),
        "model": job_spec.get("model") or (run or {}).get("model"),
        "report_dir": str(report_dir),
    }
    if isinstance(run, dict):
        info["created_at"] = run.get("created_at")
        returncode = run.get("returncode")
        if returncode is None and _pid_alive(
            int(run.get("pid") or 0), str(run.get("pid_marker") or "")
        ):
            info["status"] = "running"
            info["pid"] = run.get("pid")
        elif returncode is None:
            info["status"] = "failed"
            info["returncode"] = -1
        else:
            info["returncode"] = returncode
            info["status"] = (
                "stopped"
                if run.get("stop_requested")
                else ("completed" if returncode == 0 else "failed")
            )
    else:
        has_result = bool(verdict) or bool(load_result) or bool(summary)
        info["status"] = "finished" if has_result else "unknown"
        info["created_at"] = report_dir.stat().st_mtime
    if verdict:
        info["pass"] = verdict.get("pass")
    if load_result:
        info["pass"] = load_result.get("pass", load_result.get("threshold_pass"))
    if summary and "pass" in summary:
        info["pass"] = summary.get("pass")
    return info


def _stop_job(report_dir: Path) -> dict:
    run = _read_json(report_dir / "run.json")
    if not isinstance(run, dict):
        return {
            "job_id": report_dir.name,
            "stopped": False,
            "reason": "no run.json (console-launched job; stop via web console)",
        }
    if run.get("returncode") is not None:
        return {
            "job_id": report_dir.name,
            "stopped": False,
            "reason": "job already finished",
        }
    pid = int(run.get("pid") or 0)
    marker = str(run.get("pid_marker") or "")
    if not _pid_alive(pid, marker):
        return {
            "job_id": report_dir.name,
            "stopped": False,
            "reason": "process not alive",
        }
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        return {"job_id": report_dir.name, "stopped": False, "reason": str(exc)}
    run["stop_requested"] = True
    report_dir.joinpath("run.json").write_text(
        json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"job_id": report_dir.name, "stopped": True, "pid": pid}


def main() -> int:
    parser = argparse.ArgumentParser(description="List test jobs from the reports dir.")
    parser.add_argument("--running", action="store_true", help="only running jobs")
    parser.add_argument("--id", dest="job_id", default=None, help="single job id")
    parser.add_argument(
        "--stop",
        metavar="JOB_ID",
        default=None,
        help="stop a running job (SIGTERM to its process group)",
    )
    parser.add_argument(
        "--since-hours", type=float, default=None, help="only jobs newer than N hours"
    )
    args = parser.parse_args()
    jobs_root = default_reports_root() / "jobs"
    if args.stop:
        report_dir = jobs_root / args.stop
        if not report_dir.is_dir():
            print(json.dumps({"error": f"job not found: {args.stop}"}), file=sys.stderr)
            return 2
        print(json.dumps(_stop_job(report_dir), ensure_ascii=False))
        return 0
    jobs = []
    if jobs_root.exists():
        for report_dir in sorted(
            (p for p in jobs_root.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            if args.job_id and report_dir.name != args.job_id:
                continue
            info = _job_status(report_dir)
            if args.running and info["status"] != "running":
                continue
            if args.since_hours is not None:
                created = float(info.get("created_at") or 0)
                if created < time.time() - args.since_hours * 3600:
                    continue
            jobs.append(info)
    print(
        json.dumps(
            jobs if not args.job_id else (jobs[0] if jobs else None),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
