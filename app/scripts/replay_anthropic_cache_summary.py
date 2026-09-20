"""Offline main/history replay of retained cache jobs; never resend HTTP.

Original reports are read through their P1M ownership records and never written.
Only an aggregate replay receipt is kept; temporary main outputs are discarded.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from lib import approved_report_retention as retention
from lib import anthropic_cache_reference as cache
from lib.config import load_config
from lib.job_spec import classify_parameter_result
from lib.token_audit import summarize_token_audits
from scripts import param_test as cli


def read_retained(batch):
    kernel = retention.retention
    now = datetime.now(timezone.utc)
    with kernel._batch_directory(batch) as fd:
        meta = kernel._read_manifest(fd, batch, now)
        if now >= datetime.fromisoformat(meta["expires_at"].replace("Z", "+00:00")):
            raise ValueError("Original cache report is outside P1M")
        files, hashes = {}, {}
        for owner in meta["owned_files"]:
            name = owner["path"]
            if Path(name).name != name or kernel._snapshot(fd, name) != owner:
                raise ValueError("Original cache artifact ownership changed")
            with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd), "rb") as source:
                raw = source.read()
            digest = hashlib.sha256(raw).hexdigest()
            if kernel._snapshot(fd, name) != owner or digest != owner["sha256"]:
                raise ValueError("Original cache bytes changed during replay")
            files[name], hashes[name] = raw, digest
        if kernel._read_manifest(fd, batch, now) != meta:
            raise ValueError("Original cache retention metadata changed")
    return meta, files, hashes


def replay(batch, output_root=Path("/tmp/retained_replay")):
    batch, output_root = Path(batch), Path(output_root)
    meta, files, hashes = read_retained(batch)
    documents = {name: json.loads(raw) for name, raw in files.items() if name.endswith(".json")}
    job, original, rows = documents["job_spec.json"], documents["verdict.json"], documents["param_results.json"]
    plan = cache.build_cache_plan(job["model_profile_database"], suite_id=job["parameter_suite"],
                                  frozen_plan=job["fixed_parameter_plan"])
    records = [documents[f"anthropic_cache_{n:02d}_observation.json"] for n in range(1, 6)]
    observed = cache.evaluate_cache_plan(plan, records)
    if len(rows) != 5 or not observed["pass"]:
        raise ValueError("Retained cache job does not contain five qualified native controls")
    for request, row in zip(plan.requests, rows):
        if (row.get("name") != request.case_id or row.get("profile") != request.case_id
                or row.get("request_kind") != request.kind or cache.digest(row.get("request_body")) != request.body_sha256):
            raise ValueError("Retained parameter result differs from its frozen wire")
    expected_summary = summarize_token_audits(cache.generation_token_audit_results(plan, rows))
    saved_summary = summarize_token_audits(rows)
    if cache.canonical_bytes(saved_summary) != cache.canonical_bytes(original["token_audit_summary"]):
        raise ValueError("Original erroneous aggregate does not replay")
    output_root.mkdir(parents=True, exist_ok=True)
    captured = {}
    config = load_config()
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline cache replay cannot send HTTP, audit anew or create nonces")
    def replay_results(_config, _client, selected, _directory):
        if selected.frozen_payload != plan.frozen_payload:
            raise ValueError("Offline main replaced the original frozen cache plan")
        return copy.deepcopy(rows), copy.deepcopy(observed)
    with tempfile.TemporaryDirectory(prefix=".offline-main-", dir=output_root) as temporary:
        environment = {"LLM_API_TEST_REPORTS_DIR": temporary, "LOADTEST_REPORT_DIR": "",
            "LOADTEST_PROVIDER": "anthropic_official", "LOADTEST_MODEL": plan.model,
            "LOADTEST_API_FORM": cache.API_FORM, "LOADTEST_ROUTE_PROFILE": "vendor_direct",
            "LOADTEST_REFERENCE_SOURCE": cache.CONTRACT_ID, "LOADTEST_PARAMETER_SUITE": plan.suite_id,
            "LOADTEST_PARAM_TEST_RUNS": "1"}
        import requests
        with patch.dict(os.environ, environment), patch.object(cli, "load_config", lambda: copy.deepcopy(config)), \
                patch.object(cli, "load_job_spec", lambda *args: copy.deepcopy(job)), \
                patch.object(cli.DeepSeekClient, "from_config", lambda *args: object()), \
                patch.object(cli, "run_anthropic_cache_params", replay_results), \
                patch.object(cli, "run_identity_probe", forbidden), patch.object(cli, "run_param_tests", forbidden), \
                patch.object(cache.secrets, "token_hex", forbidden), patch.object(requests.sessions.Session, "request", forbidden), \
                patch.object(cli, "write_json", lambda path, value: captured.__setitem__(Path(path).name, copy.deepcopy(value))), \
                patch.object(cli, "_write_failed_cases", lambda *args: None), redirect_stdout(io.StringIO()):
            code = cli.main()
    repaired = captured["verdict.json"]
    if (cache.canonical_bytes(repaired["token_audit_summary"]) != cache.canonical_bytes(expected_summary)
            or len(captured["param_results.json"]) != 5 or repaired["bounded_cache_observations"]["case_results"] != observed["case_results"]):
        raise ValueError("Actual main replay changed cases or diverged from the scoped aggregate")
    validation = classify_parameter_result(job, repaired, job_type="param_test")
    _, after, after_hashes = read_retained(batch)
    if hashes != after_hashes or files != after:
        raise ValueError("Original report changed during offline replay")
    receipt = {"kind": "offline_retained_cache_main_summary_replay", "offline_only": True, "new_http_requests": 0,
        "source_batch": str(batch), "source_artifacts_sha256": hashes, "source_retention": "P1M",
        "source_expires_at": meta["expires_at"], "original_bytes_unchanged": True,
        "original_live_verdict_pass": original["pass"], "original_token_audit_summary": original["token_audit_summary"],
        "offline_main_exit_code": code, "offline_main_verdict_pass": repaired["pass"],
        "recomputed_token_audit_summary": repaired["token_audit_summary"], "history_result_validation": validation,
        "five_case_results_preserved": True, "cache_effect_verified": repaired["bounded_cache_observations"]["cache_effect_verified"],
        "independent_token_exactness_verified": False, "count_precision": "official_estimate",
        "original_live_verdict_rewritten": False, "replay_is_new_live_execution": False}
    destination = output_root / (batch.name + "_offline_summary.json")
    with destination.open("x", encoding="utf-8") as target:
        json.dump(receipt, target, ensure_ascii=False, sort_keys=True, indent=2)
        target.write("\n")
    return destination, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    parser.add_argument("--output-root", type=Path, default=Path("/tmp/retained_replay"))
    args = parser.parse_args()
    path, result = replay(args.batch, args.output_root)
    print(json.dumps({"receipt": str(path), "new_http_requests": 0, "offline_main_exit_code": result["offline_main_exit_code"],
        "offline_pass": result["offline_main_verdict_pass"], "history_status": result["history_result_validation"]["status"]}))


if __name__ == "__main__": main()
