#!/usr/bin/env python3
"""Freeze and run a resource-free Vertex-reference implicit-cache trio.

The selected execution provider is inferenceai_gemini. This sample preserves the
existing non-Pro source/family controls and does not require cachedContents APIs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib import config as config_module
from lib.config import load_config
from lib.credential_security import credential_from_config
from lib.report_retention import initialize_report_retention, register_report_files
from lib.test_runner import CancellationContext, digest_json, execute_plan
from lib.test_runner.adapters.gemini_implicit_cache import (
    MODEL, PROVIDER, credential_scope_digest, make_implicit_dispatcher,
    prepare_implicit_cache_plan, registry_for_implicit_plan, summarize_implicit_report,
)

KIND = "designated_vertex_gateway_implicit_cache_controls"


def _code_hashes():
    names = ("scripts/run_inferenceai_vertex_implicit_cache.py", "lib/report_retention.py",
             "lib/test_runner/adapters/gemini_implicit_cache.py", "lib/cache_acceptance.py", "lib/cache_coverage.py")
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}


def _package(config, nonces, account_scope, created_at, catalog=None):
    plan, _ = prepare_implicit_cache_plan(config, model=MODEL, catalog=catalog, nonces=nonces,
                                         credential_scope_sha256=account_scope)
    value = {"schema_version": 1, "kind": KIND, "created_at": created_at,
             "provider": PROVIDER, "model": MODEL, "nonces": list(nonces),
             "credential_scope_sha256": account_scope, "execution_plan": plan,
             "code_sha256": _code_hashes(), "request_cap": 3, "cleanup_request_cap": 0,
             "retries": 0, "state_resources": False, "cache_mode": "implicit_prefix",
             "reference_source_id": "google_vertex", "google_direct": False,
             "physical_upstream_verified": False, "full_parameter_certification": False,
             "independent_token_accuracy_verified": False, "report_retention": "P1M"}
    value["package_sha256"] = digest_json(value)
    return value


def _write_fresh(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def prepare_package(*, config=None, output_dir=None, credential_factory=credential_from_config, catalog=None):
    config = config if config is not None else load_config()
    nonces = [secrets.token_hex(16), secrets.token_hex(16)]
    provisional = hashlib.sha256(b"preflight-only-not-an-account").hexdigest()
    prepare_implicit_cache_plan(config, catalog=catalog, nonces=nonces, credential_scope_sha256=provisional)
    key = credential_factory(config, PROVIDER)
    created = datetime.now(timezone.utc)
    package = _package(config, nonces, credential_scope_digest(key), created.isoformat(), catalog)
    report_root = (config_module.default_reports_root() if hasattr(config_module, "default_reports_root")
                   else ROOT / "reports")
    batch = (Path(output_dir) if output_dir is not None else report_root / "jobs" /
             ("vertex_implicit_cache_" + created.strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]))
    if not batch.is_absolute():
        batch = ROOT / batch
    if batch.resolve() != batch.absolute():
        raise ValueError("The output directory cannot traverse a symbolic link")
    batch.mkdir(mode=0o700, parents=True, exist_ok=False)
    initialize_report_retention(batch, root=batch.parent, created_at=created)
    _write_fresh(batch / "request_package.json", package)
    _write_fresh(batch / "execution_plan.json", package["execution_plan"])
    register_report_files(batch, [batch / "request_package.json", batch / "execution_plan.json"], root=batch.parent)
    return batch, package


def execute_package(batch, package_sha256, *, config=None, catalog=None,
                    credential_factory=credential_from_config, dispatcher=None):
    batch = Path(batch).absolute()
    if batch.resolve() != batch or (batch / "request_package.json").is_symlink():
        raise ValueError("Unsafe prepared package path")
    package = json.loads((batch / "request_package.json").read_text())
    if package.get("kind") != KIND or package.get("package_sha256") != package_sha256:
        raise ValueError("The exact prepared implicit-cache package hash is required")
    if digest_json({k: v for k, v in package.items() if k != "package_sha256"}) != package_sha256:
        raise ValueError("Prepared package checksum mismatch")
    config = config if config is not None else load_config()
    plan = package["execution_plan"]
    registry = registry_for_implicit_plan(config, plan, catalog=catalog)
    if _package(config, package["nonces"], package["credential_scope_sha256"], package["created_at"], catalog) != package:
        raise ValueError("Frozen source, route or implicit-cache controls changed; prepare a new package")
    key = credential_factory(config, PROVIDER)
    if credential_scope_digest(key) != package["credential_scope_sha256"]:
        raise ValueError("The selected gateway credential account changed")
    sender = dispatcher if dispatcher is not None else make_implicit_dispatcher(
        config, plan, credential_factory=lambda *_args, **_kwargs: key)
    run_id = uuid.uuid4().hex
    _write_fresh(batch / "dispatch_started.json", {"plan_digest": plan["plan_digest"],
        "target": plan["target"], "package_sha256": package_sha256, "run_id": run_id})
    register_report_files(batch, [batch / "dispatch_started.json"], root=batch.parent)
    evidence = batch / "workflow_runs"
    try:
        cancellation = CancellationContext()
        with cancellation.install_signal_handlers():
            report = execute_plan(plan, registry, sender, cancellation=cancellation,
                                  evidence_dir=evidence, run_id=run_id)
        summary = summarize_implicit_report(plan, report)
        result = {"workflow_result": report, "cache_summary": summary,
                  "pass": report["status"] == "passed" and summary.get("cache_outcome") == "verified",
                  "package_sha256": package_sha256, "provider": PROVIDER,
                  "proof_scope": KIND, "cache_mode": "implicit_prefix", "state_resources": False,
                  "google_direct": False, "physical_upstream_verified": False,
                  "independent_token_accuracy_verified": False, "full_parameter_certification": False}
        _write_fresh(batch / "result.json", result)
        register_report_files(batch, [batch / "result.json"], root=batch.parent)
        return result
    finally:
        files = [evidence / run_id / "ledger.json", evidence / run_id / "run.lock"]
        present = [path for path in files if path.is_file()]
        if present:
            register_report_files(batch, present, root=batch.parent)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--execute", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--package-sha256")
    args = parser.parse_args(argv)
    if args.prepare:
        if args.package_sha256:
            parser.error("Preparation cannot reuse an old package hash")
        batch, package = prepare_package(output_dir=args.output_dir)
        print(json.dumps({"batch": str(batch), "package_sha256": package["package_sha256"],
                          "request_cap": 3, "cleanup_request_cap": 0, "network_requests": 0}))
        return 0
    if not args.package_sha256 or args.output_dir:
        parser.error("Execution requires the original hash and forbids changing its output directory")
    result = execute_package(args.execute, args.package_sha256)
    report, summary = result["workflow_result"], result["cache_summary"]
    print(json.dumps({"workflow_status": report["status"], "pass": result["pass"],
                      "cache_outcome": summary.get("cache_outcome"), "acceptance": summary["acceptance"],
                      "business_requests": sum(run["business_request_count"] for run in report["runs"]),
                      "cleanup_requests": sum(run["cleanup_request_count"] for run in report["runs"]),
                      "resources_created": 0}, ensure_ascii=False))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
