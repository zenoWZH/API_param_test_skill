"""Portable access to current MPDB matrices and frozen functional test plans."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_env

skill_env.configure_skill_env()


def _directory(kind: str) -> Path:
    from lib.config import default_reports_root

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return default_reports_root() / "jobs" / f"{stamp}_{kind}_{uuid.uuid4().hex[:8]}"


def _outside_bundle(directory: Path) -> Path:
    directory = directory.expanduser().resolve()
    root = skill_env.SKILL_ROOT.resolve()
    if directory == root or root in directory.parents:
        raise ValueError("Plans and reports must be outside the installed skill directory")
    return directory


def _save(path: Path, payload: dict) -> None:
    skill_env.atomic_write_private_text(path, json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _list(args) -> dict:
    from lib.model_profile_catalog import get_model_profile_catalog
    from lib.test_runner.inventory import build_workflow_inventory

    catalog = get_model_profile_catalog()
    profiles = catalog.list_profiles(source=args.source, model=args.model)
    ids = {row["profile_id"] for row in profiles}
    inventory = build_workflow_inventory(catalog)
    bindings = [row for row in inventory["bindings"]
                if any(target["profile_id"] in ids for target in row["reference_targets"])]
    return {"schema": "llm-api-test.matrix-inventory.v1", "database": catalog.database_info(),
            "profiles": profiles, "bindings": bindings, "live_verification": "not_performed"}


def _preview(args) -> dict:
    from lib.config import load_config
    from lib.test_runner.service import preview_test_plan

    payload = {"type": args.type, "provider": args.provider, "model": args.model,
               "runs": args.runs, "tool_validation_mode": args.tool_validation_mode}
    for field in ("api_form", "route_profile", "reference_contract_id", "parameter_suite",
                  "workflow_id", "workflow_binding_id", "plan_seed", "timeout_sec"):
        value = getattr(args, field)
        if value is not None:
            payload[field] = value
    selection = {"suite": args.suite}
    if args.case is not None:
        selection["cases"] = args.case
    if args.type == "image_param_test":
        if args.no_cross_control is not None:
            selection["no_cross_control"] = args.no_cross_control
        payload["image_plan"] = selection
    else:
        payload.update(selection)
    preview = preview_test_plan(load_config(), payload)
    directory = _outside_bundle(args.output_dir or _directory("matrix_plan"))
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("Preview requires a new or empty output directory")
    _save(directory / "job_spec.json", preview["job_spec"])
    plan = preview["job_spec"]["execution_plan"]
    return {"schema": "llm-api-test.matrix-preview.v1", "mode": "offline_preview",
            "job_spec": str(directory / "job_spec.json"), "plan_digest": plan["plan_digest"],
            "target": plan["target"], "selected_cases": plan["selected_cases"],
            "run_count": plan["run_count"], "request_cap": preview["request_cap"],
            "cleanup_request_cap": preview.get("cleanup_request_cap", 0),
            "plan_seed": preview.get("plan_seed"), "preconditions": preview.get("preconditions", [])}


def _run(args) -> tuple[dict, int]:
    if not args.yes:
        raise ValueError("Execution requires --yes for the reviewed frozen plan")
    from lib.config import load_config
    from lib.job_spec import load_job_spec
    from scripts.workflow_test import execute_job

    job = load_job_spec(args.job_spec)
    if not isinstance(job, dict) or job.get("schema_version") != 6 or "execution_plan" not in job:
        raise ValueError("Expected a frozen functional JobSpec v6 from matrix preview")
    directory = _outside_bundle(args.output_dir or args.job_spec.parent)
    if args.cleanup_only and directory != args.job_spec.resolve().parent:
        raise ValueError("Cleanup recovery must use the original job directory")
    # Record the owning process for the existing jobs/result tools. Recovery
    # keeps original business metadata unchanged; its evidence is independent.
    run = {"pid": os.getpid(), "pid_marker": "matrix.py", "signal_scope": "process", "type": job["type"],
           "provider": job["provider"], "model": job["model"], "created_at": time.time(),
           "started_at": time.time(), "finished_at": None, "returncode": None}
    if not args.cleanup_only:
        if (directory / "dispatch_started.json").exists() or (directory / "run.json").exists():
            raise ValueError("This batch already entered execution; create a new plan for another run")
        _save(directory / "run.json", run)
    code = 2
    try:
        result = execute_job(job, load_config(), directory, cleanup_run_id=args.cleanup_only)
        passed = (result.get("workflow_result", {}).get("status") == "passed"
                  if args.cleanup_only else result.get("pass") is True)
        code = 0 if passed else 1
        return {"job_id": directory.name, "report_dir": str(directory), "pass": passed,
                "cleanup_only": bool(args.cleanup_only), "result_validation": result.get("result_validation"),
                "workflow_status": result.get("workflow_result", {}).get("status")}, code
    finally:
        if not args.cleanup_only:
            try:
                existing = json.loads((directory / "run.json").read_text())
                if existing.get("stop_requested"):
                    run["stop_requested"] = True
            except (OSError, ValueError, AttributeError):
                pass
            run.update(finished_at=time.time(), returncode=code)
            _save(directory / "run.json", run)


def main(argv=None) -> int:
    from lib.parameter_job_controls import TOOL_VALIDATION_MODES

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    listing = commands.add_parser("list", help="inspect matrices without credentials or traffic")
    listing.add_argument("--source")
    listing.add_argument("--model")
    preview = commands.add_parser("preview", help="freeze an offline functional plan")
    preview.add_argument("--type", choices=("param_test", "image_param_test"), default="param_test")
    preview.add_argument("--provider", required=True)
    preview.add_argument("--model", required=True)
    for flag in ("api-form", "route-profile", "reference-contract-id", "parameter-suite",
                 "workflow-id", "workflow-binding-id", "plan-seed"):
        preview.add_argument("--" + flag)
    preview.add_argument("--suite", choices=("full", "smoke", "resolution"), default="full")
    preview.add_argument("--case", action="append")
    preview.add_argument("--no-cross-control", action="store_true", default=None,
                         help="image plans: explicitly omit resolution-alias cross controls")
    preview.add_argument("--runs", type=int, default=1)
    preview.add_argument("--tool-validation-mode", choices=sorted(TOOL_VALIDATION_MODES), default="auto")
    preview.add_argument("--timeout-sec", type=int)
    preview.add_argument("--output-dir", type=Path)
    run = commands.add_parser("run", help="execute the same frozen plan or recover its owned resources")
    run.add_argument("--job-spec", type=Path, required=True)
    run.add_argument("--output-dir", type=Path)
    run.add_argument("--cleanup-only", metavar="RUN_ID")
    run.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "list":
            result, code = _list(args), 0
        elif args.action == "preview":
            result, code = _preview(args), 0
        else:
            result, code = _run(args)
    except (KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
