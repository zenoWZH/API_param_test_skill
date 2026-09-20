#!/usr/bin/env python3
"""Prepare or execute registered cross-model workflows through the shared engine."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import datetime, timezone
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.config import load_config
from lib.job_spec import load_job_spec, classify_workflow_result
from lib.parameter_job_controls import bind_parameter_execution
from lib.report_retention import initialize_report_retention, register_report_files
from lib.test_runner import CancellationContext, execute_plan, digest_json
from lib.test_runner.service import preview_test_plan, validate_current_workflow_job
from lib.test_runner.transport import HttpDispatcher


def _write(path: Path, value) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".workflow-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_once(path: Path, value) -> None:
    """Never replace an unchanged, already registered immutable report."""
    if path.is_symlink():
        raise ValueError("Workflow evidence cannot be a symbolic link")
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != value:
            raise ValueError("Workflow evidence belongs to a different frozen job")
        return
    _write(path, value)


def case_result_rows(plan: dict, report: dict, job: dict, proof_scope: str) -> list[dict]:
    """One row per case/run, preserving observations beside semantic assertions."""
    cases = {row["id"]: row for row in plan["definition"]["cases"]}
    rows = []
    for run in report["runs"]:
        for case_id in plan["selected_cases"]:
            steps = [(step["id"], run["steps"][step["id"]]) for step in plan["ordered_steps"]
                     if step["case_id"] == case_id and step["id"] in run["steps"]]
            if not steps:
                continue
            terminal_id, terminal = next(((key, value) for key, value in steps if key == case_id), steps[-1])
            record = next((value["record"] for _, value in reversed(steps) if isinstance(value.get("record"), dict)), {})
            states = [value["status"] for _, value in steps]
            status = next((state for state in ("failed", "inconclusive", "cancelled", "blocked", "skipped") if state in states), terminal["status"])
            rows.append({"run_id": run["run_id"], "run_index": run["run_index"], "step_id": terminal_id,
                         "step_ids": [key for key, _ in steps], "profile": cases[case_id].get("name", case_id),
                         "name": case_id, "provider": job["provider"], "model": job["model"],
                         "status": "pass" if status == "passed" else status,
                         "workflow_status": status, "pass": status == "passed",
                         "request_body": record.get("request"), "response_json": record.get("response"),
                         "status_code": record.get("http_status", record.get("status_code")),
                         "verdict": terminal.get("verdict", record.get("verdict")),
                         "semantic_effect": terminal.get("semantic_effect"),
                         "public_result": terminal.get("public_result", record.get("public_result")),
                         "reason": terminal.get("reason"), "proof_scope": proof_scope})
    return rows


def _owned_ledger(path: Path, job: dict, run_id: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Owned run ledger is missing or unsafe")
    raw = path.read_bytes()
    state = json.loads(raw)
    checksum = state.pop("ledger_digest", None)
    plan = job["execution_plan"]
    if (checksum != digest_json(state) or state.get("ledger_schema_version") != 1
            or state.get("run_id") != run_id or state.get("plan_digest") != plan["plan_digest"]
            or state.get("target") != plan["target"] or type(state.get("run_index")) is not int
            or not 1 <= state["run_index"] <= plan["run_count"]):
        raise ValueError("Cleanup ledger does not belong to the original frozen run")
    steps = {row["id"] for row in plan["ordered_steps"]}
    attempts = {row["id"]: row for row in state["attempts"]}
    creations = {row["attempt_id"]: row for row in state["creations"]}
    for item in state["resources"]:
        creation = creations.get(item.get("creation_attempt_id"), {})
        attempt = attempts.get(item.get("creation_attempt_id"), {})
        if (item.get("run_id") != run_id or item.get("step_id") not in steps
                or creation.get("run_id") != run_id or creation.get("step_id") != item["step_id"]
                or attempt.get("step_id") != item["step_id"] or attempt.get("phase") != "business"
                or item.get("resource_id") not in creation.get("resource_ids", [])):
            raise ValueError("Cleanup resource lacks an original owned creation attempt")
    return raw


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_recovery_ledger(path: Path, raw: bytes) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _create_recovery_lock(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _repair_unstarted_legacy_recovery(directory: Path, job: dict, run_id: str, claim: dict, source_raw: bytes) -> None:
    """Repair only an old fork that demonstrably never entered the core.

    The core creates its lock before opening or changing its ledger, and journals
    every cleanup send before dispatch. A missing lock plus an absent or exact
    source copy therefore permits preparation, never replay of business work.
    Changed, partial, foreign or modern atomic-publication evidence is rejected.
    """
    runs = directory / "workflow_runs"
    target = runs / run_id
    if any(path.is_symlink() or not path.is_dir() for path in (runs, target)):
        raise ValueError("Unsafe cleanup recovery run directory")
    ledger, lock = target / "ledger.json", target / "run.lock"
    if lock.is_symlink() or (lock.exists() and not lock.is_file()):
        raise ValueError("Unsafe cleanup recovery lock")
    if lock.exists():
        if ledger.is_symlink() or not ledger.is_file():
            raise ValueError("An initialized cleanup recovery lost its ledger")
        return
    if claim.get("publication_protocol") is not None:
        raise ValueError("Published atomic cleanup recovery lost its core lock")
    if ({path.name for path in directory.iterdir()} != {"recovery_claim.json", "workflow_runs"}
            or {path.name for path in runs.iterdir()} != {run_id}
            or {path.name for path in target.iterdir()} - {"ledger.json"}):
        raise ValueError("Incomplete legacy cleanup recovery contains foreign evidence")
    if ledger.is_symlink() or (ledger.exists() and not ledger.is_file()):
        raise ValueError("Unsafe incomplete legacy cleanup ledger")
    if ledger.exists():
        if _owned_ledger(ledger, job, run_id) != source_raw:
            raise ValueError("Legacy cleanup ledger changed without its core lock")
    else:
        # Write outside the published lineage, then atomically install the full
        # original bytes. Another kill cannot leave a partially copied ledger.
        staged = Path(tempfile.mkdtemp(prefix=".recovery-ledger-", dir=directory.parent.parent))
        prepared = staged / "ledger.json"
        _write_recovery_ledger(prepared, source_raw)
        _fsync_directory(staged)
        if ledger.exists() or ledger.is_symlink():
            raise ValueError("Legacy cleanup ledger appeared during repair")
        prepared.rename(ledger)
        _fsync_directory(target)
        staged.rmdir()
    _create_recovery_lock(lock)
    _fsync_directory(target)


def _publish_recovery_directory(lineage: Path, destination: Path, run_id: str, claim: dict, raw: bytes) -> Path:
    """Publish a complete fork only; unpublished preparations cannot execute.

    Preparations are siblings of per-run lineages, not numbered recovery entries.
    A crash leaves them intact for inspection while the last published ledger
    remains authoritative. No request is made before publication and yielding.
    """
    staged = Path(tempfile.mkdtemp(prefix=".recovery-prepare-", dir=lineage.parent))
    runs = staged / "workflow_runs"
    target = runs / run_id
    target.mkdir(parents=True, mode=0o700)
    _write_once(staged / "recovery_claim.json", {**claim, "publication_protocol": "atomic-v1"})
    _write_recovery_ledger(target / "ledger.json", raw)
    _create_recovery_lock(target / "run.lock")
    for directory in (target, runs, staged):
        _fsync_directory(directory)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Cleanup recovery destination already exists")
    staged.rename(destination)
    _fsync_directory(lineage)
    _fsync_directory(lineage.parent)
    return destination / "workflow_runs" / run_id


@contextmanager
def _recovery_output(output_dir: Path, job: dict, run_id: str | None):
    """Fork cleanup evidence while locking the original run's immutable lineage.

    Each completed or interrupted ledger is registered once before the next
    recovery copies it. Old P1M hashes and business results never change.
    """
    if run_id is None:
        yield output_dir, output_dir / "workflow_runs"
        return
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", run_id):
        raise ValueError("Unsafe cleanup run ID")
    plan = job["execution_plan"]
    claim_path = output_dir / "dispatch_started.json"
    if claim_path.is_symlink() or not claim_path.is_file():
        raise ValueError("Cleanup requires the original dispatch claim")
    claim = json.loads(claim_path.read_text())
    if claim.get("plan_digest") != plan["plan_digest"] or claim.get("target") != plan["target"]:
        raise ValueError("Cleanup claim belongs to a different frozen plan or target")
    original = output_dir / "workflow_runs" / run_id
    if any(path.is_symlink() for path in (output_dir / "workflow_runs", original)) or not original.is_dir():
        raise ValueError("Original cleanup run directory is missing or unsafe")
    # The same lock excludes the original business process and every recovery.
    descriptor = os.open(original / "run.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        source = original / "ledger.json"
        raw = _owned_ledger(source, job, run_id)
        register_report_files(output_dir, [claim_path, source, original / "run.lock"], root=output_dir.parent)
        lineage = output_dir / "cleanup_recoveries" / run_id
        if any(path.is_symlink() for path in (lineage.parent, lineage)):
            raise ValueError("Unsafe cleanup recovery directory")
        lineage.mkdir(parents=True, exist_ok=True, mode=0o700)
        existing = sorted(lineage.iterdir())
        for ordinal, directory in enumerate(existing, 1):
            if directory.name != f"{ordinal:06d}" or directory.is_symlink() or not directory.is_dir():
                raise ValueError("Cleanup recovery lineage is not contiguous")
            marker = directory / "recovery_claim.json"
            if marker.is_symlink() or not marker.is_file():
                raise ValueError("Cleanup recovery is missing its immutable source claim")
            previous = json.loads(marker.read_text())
            if (type(previous.get("schema_version")) is not int or previous["schema_version"] != 1
                    or previous.get("publication_protocol") not in (None, "atomic-v1")
                    or previous.get("run_id") != run_id or previous.get("plan_digest") != plan["plan_digest"]
                    or previous.get("source_ledger") != str(source.relative_to(output_dir))
                    or previous.get("source_sha256") != hashlib.sha256(raw).hexdigest()):
                raise ValueError("Cleanup recovery source lineage changed")
            register_report_files(output_dir, [marker], root=output_dir.parent)
            _repair_unstarted_legacy_recovery(directory, job, run_id, previous, raw)
            source = directory / "workflow_runs" / run_id / "ledger.json"
            raw = _owned_ledger(source, job, run_id)
            register_report_files(output_dir, [source, source.parent / "run.lock"], root=output_dir.parent)
        destination = lineage / f"{len(existing) + 1:06d}"
        claim = {"schema_version": 1, "run_id": run_id, "plan_digest": plan["plan_digest"],
                        "source_ledger": str(source.relative_to(output_dir)),
                        "source_sha256": hashlib.sha256(raw).hexdigest(),
                        "created_at": datetime.now(timezone.utc).isoformat()}
        target = _publish_recovery_directory(lineage, destination, run_id, claim, raw)
        marker = destination / "recovery_claim.json"
        register_report_files(output_dir, [marker], root=output_dir.parent)
        try:
            yield destination, destination / "workflow_runs"
        finally:
            # Even an ordinary exception leaves an immutable, recoverable source;
            # SIGKILL evidence is checked and registered on the next takeover.
            _owned_ledger(target / "ledger.json", job, run_id)
            files = [target / "ledger.json"]
            if (target / "run.lock").is_file():
                files.append(target / "run.lock")
            register_report_files(output_dir, files, root=output_dir.parent)
    finally:
        os.close(descriptor)


def execute_job(job: dict, config: dict, output_dir: Path, *, cleanup_run_id: str | None = None,
                dispatcher=None, catalog=None) -> dict:
    factory_id = job.get("execution_plan", {}).get("definition", {}).get("factory", {}).get("factory_id")
    if factory_id in {"legacy_parameter", "source_fixed_parameter"}:
        if cleanup_run_id is not None:
            raise ValueError("The text parameter plan creates no cleanup resources")
        if dispatcher is not None:
            raise ValueError("Legacy CLI injection uses its reviewed client adapter")
        from lib.test_runner.service import validate_current_parameter_job
        from scripts import param_test
        if factory_id == "legacy_parameter":
            validate_current_parameter_job(config, job)
        else:
            from lib.job_spec import _current_job_snapshot_integrity
            valid, reasons = _current_job_snapshot_integrity(job)
            if not valid:
                raise ValueError("Invalid fixed workflow identity: " + ", ".join(reasons))
            from lib.test_runner.adapters.fixed_parameter import fixed_domain_from_plan
            from lib.test_runner.fixed_service import validate_fixed_configuration
            domain = fixed_domain_from_plan(job["execution_plan"])
            if domain.snapshot != job.get("model_profile_database"):
                raise ValueError("Fixed workflow source snapshot differs from its job")
            validate_fixed_configuration(config, domain)
        output_dir = Path(output_dir).absolute()
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_once(output_dir / "job_spec.json", job)
        param_test.main(config=config, job_spec=job, output_dir=output_dir)
        return json.loads((output_dir / "verdict.json").read_text(encoding="utf-8"))
    image_job = factory_id in {"image_parameter", "responses_image"}
    if image_job:
        from lib.test_runner.service import validate_current_image_job
        registry = validate_current_image_job(config, job, output_dir=output_dir, cleanup_only=cleanup_run_id is not None)
    else:
        registry = validate_current_workflow_job(config, job, cleanup_only=cleanup_run_id is not None, catalog=catalog)
    bind_parameter_execution(config, job, os.environ)
    plan = job["execution_plan"]
    output_dir = Path(output_dir).absolute()
    output_dir.mkdir(parents=True, exist_ok=True)
    existing_job = output_dir / "job_spec.json"
    if existing_job.exists() and json.loads(existing_job.read_text(encoding="utf-8")) != job:
        raise ValueError("Report directory belongs to a different frozen job")
    initialize_report_retention(output_dir, root=output_dir.parent)
    if not cleanup_run_id:
        claim = {"plan_digest": plan["plan_digest"], "target": plan["target"], "created_at": datetime.now(timezone.utc).isoformat()}
        descriptor = os.open(output_dir / "dispatch_started.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(claim, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
    _write_once(output_dir / "job_spec.json", job)
    _write_once(output_dir / "execution_plan.json", plan)
    register_report_files(output_dir, [output_dir / name for name in ("job_spec.json", "execution_plan.json", "dispatch_started.json")], root=output_dir.parent)
    if image_job and not cleanup_run_id:
        if dispatcher is not None:
            raise ValueError("Image CLI injection uses its reviewed protocol adapter")
        from scripts import image_param_test
        from lib.test_runner.service import image_cli_arguments
        image_param_test.main(image_cli_arguments(job["image_plan"], output_dir), config=config, job_spec=job, _dispatch_claimed=True)
        result = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
        report = result["workflow_result"]
        files = [output_dir / name for name in ("summary.json", "plan.json", "workflow_report.json", "case_results.json", "model_check.json", "attempt_results.json")]
        files.extend(Path(run["ledger_path"]) for run in report["runs"])
        files.extend(Path(run["ledger_path"]).parent / "run.lock" for run in report["runs"])
        for row in json.loads((output_dir / "case_results.json").read_text(encoding="utf-8")):
            files.extend(output_dir / path for path in row.get("artifacts", []) if isinstance(path, str))
        register_report_files(output_dir, [path for path in files if path.is_file()], root=output_dir.parent)
        return result
    if dispatcher is None:
        if image_job:
            from lib.test_runner.adapters.image import make_image_dispatcher
            dispatcher = make_image_dispatcher(config, plan)
        elif factory_id == "aws_bedrock_mantle_json":
            from lib.test_runner.adapters.mantle import make_dispatcher
            dispatcher = make_dispatcher(config)
        elif factory_id == "deepseek_beta_prefix":
            from lib.config import get_api_key
            from lib.test_runner.adapters.deepseek_beta import BetaDispatcher
            dispatcher = BetaDispatcher(plan, api_key=get_api_key(config, job["provider"]))
        elif factory_id == "media_input":
            from lib.test_runner.adapters.media_input import make_dispatcher
            dispatcher = make_dispatcher(config, plan)
        else:
            from lib.test_runner.adapters.fable import http_operation_auth
            execution = plan["target"]["execution_target"]
            public_urls = tuple(dict.fromkeys(
                extra["request"]["url"]
                for step in plan["ordered_steps"]
                for extra in step.get("inputs", {}).get("plan", {}).get("extra_api_steps", [])
                if extra.get("kind") == "public_http"))
            dispatcher = HttpDispatcher(config, execution["provider_id"], execution["transport_adapter_id"],
                                        public_urls=public_urls, operation_auth=http_operation_auth(execution["provider_id"]),
                                        public_response_byte_limit=32 * 1024 * 1024)
    with _recovery_output(output_dir, job, cleanup_run_id) as (result_dir, evidence_dir):
        cancellation = CancellationContext()
        with cancellation.install_signal_handlers():
            report = execute_plan(plan, registry, dispatcher, cancellation=cancellation,
                                  evidence_dir=evidence_dir, cleanup_only=cleanup_run_id is not None,
                                  run_id=cleanup_run_id)
        result = {"schema_version": 1, "pass": report["status"] == "passed" and not report["cleanup_only"],
                  "proof_scope": plan["definition"].get("proof_scope", "workflow_execution_and_domain_validators"), "workflow_result": report,
                  "provider": job["provider"], "model": job["model"], "api_form": job["api_form"],
                  "reference_contract_id": job["reference_contract_id"], "param_test_runs": plan["run_count"],
                  "tool_validation_mode": job["parameter_execution"]["tool_validation_mode"],
                  "native_aws_certification": False, "full_parameter_certification": False}
        result["result_validation"] = classify_workflow_result(job, result)
        result["pass"] = result["result_validation"]["pass"]
        filename = "cleanup_result.json" if cleanup_run_id else ("summary.json" if job["type"] == "image_param_test" else "verdict.json")
        _write_once(result_dir / filename, result)
        if not cleanup_run_id:
            _write(output_dir / "workflow_result.json", report)
            rows = case_result_rows(plan, report, job, result["proof_scope"])
            _write(output_dir / "param_results.json", rows)
        files = [output_dir / name for name in ("job_spec.json", "execution_plan.json", "dispatch_started.json")]
        files.append(result_dir / filename)
        if not cleanup_run_id:
            files.extend(output_dir / name for name in ("workflow_result.json", "param_results.json"))
        files.extend(Path(run["ledger_path"]) for run in report["runs"])
        files.extend(Path(run["ledger_path"]).parent / "run.lock" for run in report["runs"])
        files = [path for path in files if path.is_file()]
        # Registry owns explicit files only; it never takes ownership of another job.
        register_report_files(output_dir, files, root=output_dir.parent)
        return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-spec", type=Path)
    parser.add_argument("--type", choices=("param_test", "image_param_test"), dest="job_type")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--workflow-id")
    parser.add_argument("--workflow-binding-id")
    parser.add_argument("--api-form")
    parser.add_argument("--route-profile")
    parser.add_argument("--reference-contract-id")
    parser.add_argument("--plan-seed")
    parser.add_argument("--parameter-suite")
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--suite")
    parser.add_argument("--fixture-evidence", type=Path)
    parser.add_argument("--mcp-fixture", type=Path)
    parser.add_argument("--runs", type=int)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--cleanup-only", metavar="RUN_ID")
    args = parser.parse_args(argv)
    if args.job_spec and any((args.provider, args.model, args.workflow_id, args.workflow_binding_id, args.cases,
                              args.fixture_evidence, args.mcp_fixture, args.api_form, args.route_profile,
                              args.reference_contract_id, args.plan_seed, args.parameter_suite, args.suite, args.job_type, args.runs is not None)):
        parser.error("Frozen jobs cannot be combined with mutable target/case selectors")
    if args.cleanup_only and not args.job_spec:
        parser.error("Cleanup recovery requires the original frozen --job-spec")
    config = load_config()
    if args.job_spec:
        job = load_job_spec(args.job_spec)
        if not isinstance(job, dict) or job.get("schema_version") != 6 or "execution_plan" not in job:
            parser.error("Expected a frozen functional workflow JobSpec")
        preview = {"plan_digest": job["execution_plan"]["plan_digest"], "target": job["execution_plan"]["target"],
                   "selected_cases": job["execution_plan"]["selected_cases"], "job_spec": job}
    else:
        payload = {"type": args.job_type or "param_test", "provider": args.provider, "model": args.model,
                   "workflow_id": args.workflow_id, "workflow_binding_id": args.workflow_binding_id,
                   "cases": args.cases, "suite": args.suite or "full",
                   "runs": args.runs if args.runs is not None else 1,
                   "reference_contract_id": args.reference_contract_id, "plan_seed": args.plan_seed,
                   "parameter_suite": args.parameter_suite,
                   "fixture_evidence": json.loads(args.fixture_evidence.read_text()) if args.fixture_evidence else None,
                   "mcp_fixture": json.loads(args.mcp_fixture.read_text()) if args.mcp_fixture else None}
        for field in ("api_form", "route_profile"):
            value = getattr(args, field)
            if value is not None:
                payload[field] = value
        if payload["type"] == "image_param_test":
            # Image selection is resolved and validated from this nested plan.
            payload["image_plan"] = {field: payload.pop(field) for field in ("suite", "cases")}
        preview = preview_test_plan(config, payload)
        job = preview["job_spec"]
    if args.preview:
        if args.output_dir:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            _write_once(args.output_dir / "job_spec.json", job)
        print(json.dumps({key: value for key, value in preview.items() if key not in {"plan", "resolved_workflow", "job_spec", "test_workflow_snapshot"}}, ensure_ascii=False, indent=2))
        return 0
    output = args.output_dir or (args.job_spec.parent if args.job_spec else ROOT / "reports" / "jobs" /
                                ("workflow_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]))
    result = execute_job(job, config, output, cleanup_run_id=args.cleanup_only)
    print(json.dumps({"pass": result["pass"], "status": result["workflow_result"]["status"], "report_dir": str(output),
                      "plan_digest": result["workflow_result"]["plan_digest"]}, ensure_ascii=False))
    if args.cleanup_only:
        return 0 if result["workflow_result"]["status"] == "passed" else 1
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
