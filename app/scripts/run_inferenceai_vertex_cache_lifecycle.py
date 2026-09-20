#!/usr/bin/env python3
"""Bounded cached-content sample on the explicitly selected InferenceAI gateway.

Google Vertex supplies the reference identity; the gateway supplies the execution
endpoint. This sample never opens the ordinary direct-Vertex policy gate.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
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
from lib.test_runner.adapters.gemini_gateway_resources import (
    credential_scope_digest,
    make_gateway_dispatcher,
    prepare_gateway_cached_content_plan,
    registry_for_gateway_plan,
)
from scripts.workflow_test import _recovery_output

PROVIDER = "inferenceai_gemini"
MODEL = "gemini-3.7-flash"
KIND = "designated_vertex_gateway_cached_content_sample"
OUTPUT_CAP = 2048
TTL_SECONDS = 300
PROMPT = "Return only the value of Verification marker from the cached document. No other text."


def _code_hashes():
    names = (
        "scripts/run_inferenceai_vertex_cache_lifecycle.py",
        "scripts/workflow_test.py",
        "lib/report_retention.py",
        "lib/test_runner/adapters/gemini_gateway_resources.py",
    )
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}


def _cache_text(nonce):
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise ValueError("The sample requires one frozen 32-character hexadecimal marker")
    corpus = "\n".join(
        f"Fictional library shelf {number:03d} contains blue notebooks, cedar bookmarks and silver pencils."
        for number in range(512)
    )
    return f"Verification marker: {nonce}\n{corpus}\nThe marker is a synthetic test value."


def _plan(config, model, nonce, account_scope, catalog=None):
    return prepare_gateway_cached_content_plan(
        config, model, catalog=catalog, cache_text=_cache_text(nonce), prompt=PROMPT,
        expected_text=nonce, credential_scope_sha256=account_scope,
        ttl_seconds=TTL_SECONDS, max_output_tokens=OUTPUT_CAP,
        request_timeout_seconds=120, cleanup_timeout_seconds=30,
    )


def _package(config, model, nonce, account_scope, created_at, catalog=None):
    plan, _ = _plan(config, model, nonce, account_scope, catalog)
    value = {
        "schema_version": 1, "kind": KIND, "created_at": created_at,
        "provider": PROVIDER, "model": model, "nonce": nonce,
        "credential_scope_sha256": account_scope, "execution_plan": plan,
        "code_sha256": _code_hashes(), "request_cap": 3, "retries": 0,
        "reference_source_id": "google_vertex", "google_direct": False,
        "physical_upstream_verified": False, "full_parameter_certification": False,
        "independent_token_accuracy_verified": False,
        "report_retention": "P1M",
    }
    value["package_sha256"] = digest_json(value)
    return value


def _write_fresh(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def prepare_package(*, config=None, model=MODEL, output_dir=None,
                    credential_factory=credential_from_config, catalog=None):
    config = config if config is not None else load_config()
    nonce = secrets.token_hex(16)
    # Resolve the complete source, route and request before reading a credential.
    provisional_scope = hashlib.sha256(b"preflight-only-not-an-account").hexdigest()
    _plan(config, model, nonce, provisional_scope, catalog)
    key = credential_factory(config, PROVIDER)
    account_scope = credential_scope_digest(key)
    created = datetime.now(timezone.utc)
    package = _package(config, model, nonce, account_scope, created.isoformat(), catalog)
    report_root = (config_module.default_reports_root() if hasattr(config_module, "default_reports_root")
                   else ROOT / "reports")
    batch = (Path(output_dir) if output_dir is not None else
             report_root / "jobs" / ("vertex_gateway_cache_" + created.strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]))
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
                    cleanup_run_id=None, credential_factory=credential_from_config, dispatcher=None):
    batch = Path(batch).absolute()
    if batch.resolve() != batch or (batch / "request_package.json").is_symlink():
        raise ValueError("Unsafe prepared package path")
    package = json.loads((batch / "request_package.json").read_text())
    if package.get("kind") != KIND or package.get("package_sha256") != package_sha256:
        raise ValueError("The exact prepared package hash is required")
    if digest_json({k: v for k, v in package.items() if k != "package_sha256"}) != package_sha256:
        raise ValueError("Prepared package checksum mismatch")
    config = config if config is not None else load_config()
    plan = package["execution_plan"]
    registry = registry_for_gateway_plan(config, plan, catalog=catalog, cleanup_only=cleanup_run_id is not None)
    if cleanup_run_id is None:
        if _package(config, package["model"], package["nonce"], package["credential_scope_sha256"],
                    package["created_at"], catalog) != package:
            raise ValueError("Frozen source, routing, sample or request changed; prepare a new package")
    key = credential_factory(config, PROVIDER)
    if credential_scope_digest(key) != package["credential_scope_sha256"]:
        raise ValueError("The selected gateway credential account changed")
    sender = dispatcher if dispatcher is not None else make_gateway_dispatcher(
        config, plan, credential_factory=lambda *_args, **_kwargs: key,
        cleanup_only=cleanup_run_id is not None,
    )
    run_id = cleanup_run_id or uuid.uuid4().hex
    if cleanup_run_id is None:
        _write_fresh(batch / "dispatch_started.json", {
            "plan_digest": plan["plan_digest"], "target": plan["target"],
            "package_sha256": package_sha256, "run_id": run_id,
        })
        register_report_files(batch, [batch / "dispatch_started.json"], root=batch.parent)
    report = None
    with _recovery_output(batch, {"execution_plan": plan}, cleanup_run_id) as (output, evidence):
        try:
            cancellation = CancellationContext()
            with cancellation.install_signal_handlers():
                report = execute_plan(plan, registry, sender, cancellation=cancellation,
                                      evidence_dir=evidence, run_id=run_id,
                                      cleanup_only=cleanup_run_id is not None)
            result = {"workflow_result": report, "pass": report["status"] == "passed",
                      "package_sha256": package_sha256, "provider": PROVIDER,
                      "proof_scope": "cleanup_only" if cleanup_run_id else KIND,
                      "google_direct": False, "physical_upstream_verified": False,
                      "full_parameter_certification": False,
                      "independent_token_accuracy_verified": False}
            result_path = output / ("cleanup_result.json" if cleanup_run_id else "result.json")
            _write_fresh(result_path, result)
            register_report_files(batch, [result_path], root=batch.parent)
            return result
        finally:
            exact_files = [evidence / run_id / "ledger.json", evidence / run_id / "run.lock"]
            present = [path for path in exact_files if path.is_file()]
            if present:
                register_report_files(batch, present, root=batch.parent)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--execute", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--package-sha256")
    parser.add_argument("--cleanup-only", metavar="RUN_ID")
    args = parser.parse_args(argv)
    if args.prepare:
        if args.package_sha256 or args.cleanup_only:
            parser.error("Preparation cannot consume an old hash or cleanup run")
        batch, package = prepare_package(model=args.model or MODEL, output_dir=args.output_dir)
        print(json.dumps({"batch": str(batch), "package_sha256": package["package_sha256"],
                          "plan_digest": package["execution_plan"]["plan_digest"],
                          "request_cap": 3, "network_requests": 0}))
        return 0
    if not args.package_sha256 or args.model or args.output_dir:
        parser.error("Execution requires the original package hash and forbids changed selection/output")
    result = execute_package(args.execute, args.package_sha256, cleanup_run_id=args.cleanup_only)
    report = result["workflow_result"]
    print(json.dumps({"status": report["status"], "pass": result["pass"],
                      "run_ids": [run["run_id"] for run in report["runs"]],
                      "business_requests": sum(run["business_request_count"] for run in report["runs"]),
                      "cleanup_requests": sum(run["cleanup_request_count"] for run in report["runs"]),
                      "cleanup_statuses": [run["cleanup"]["status"] for run in report["runs"]]}, ensure_ascii=False))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
