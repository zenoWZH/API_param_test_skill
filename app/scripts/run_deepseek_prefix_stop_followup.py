#!/usr/bin/env python3
"""Four sequential beta stop controls with a nonconflicting assistant prefix."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.deepseek_beta_prefix import build_prefix_request
from lib.deepseek_prefix_live import TARGETS
from lib.live_stateful_runners import _response_json
from lib.model_profile_catalog import get_model_profile_catalog
from lib.report_retention import initialize_report_retention, register_report_files
from scripts.deepseek_prefix_stop_followup_candidate import (
    CASE_IDS, PREFIX, TARGET_JSON, STOP_LITERAL, candidate_sha256, expected_candidate, stop_cases,
)
from scripts.run_deepseek_prefix_approved_matrix import (
     canonical, read_official_key, resolve_cases, validate_generation,
)


def exact_target(text: str) -> bool:
    try:
        value = _response_json(text.encode())
        return value == TARGET_JSON and type(value.get("answer")) is int
    except (ValueError, UnicodeError):
        return False


def binding_descriptor(resolved: dict) -> dict:
    fields = ("source_id", "profile_id", "interface_id", "contract_id", "test_binding_id",
              "parameter_test_binding_id", "catalog_digest", "test_extension_digest")
    return {**{name: resolved[name] for name in fields}, "api_form": "deepseek_beta_chat_prefix", "api_version": "beta"}


def resolve_stop_cases(catalog, iid: str) -> tuple[dict, list[dict]]:
    if iid not in TARGETS:
        raise ValueError("stop follow-up requires an exact approved beta interface")
    resolved, _ = resolve_cases(catalog, iid)
    cases = stop_cases(TARGETS[iid][0])
    for record in (resolved["test_binding"], resolved["parameter_test_binding"]):
        if any(case_id not in record.get("test_cases", []) or case_id in record.get("excluded_test_profiles", []) for case_id in CASE_IDS):
            raise ValueError("stop follow-up case is absent or excluded")
        for field in ("expectations", "default_expectations"):
            overrides = record.get(field, {})
            if not isinstance(overrides, dict) or any(case_id in overrides and overrides[case_id] != "supported" for case_id in CASE_IDS):
                raise ValueError("stop follow-up expectation changed")
    definitions = resolved["parameter_test_binding"].get("case_definitions", [])
    for case in cases:
        if canonical([item for item in definitions if item.get("case_id") == case["case_id"]]) != canonical([case]):
            raise ValueError("stop follow-up frozen case changed or duplicated")
        body = case["body"]
        request = build_prefix_request(source_id="deepseek", family_id="deepseek", api_form="deepseek_beta_chat_prefix",
                                       model=body["model"], messages=body["messages"], max_tokens=512,
                                       stop=body.get("stop"), thinking_enabled=False)
        if canonical(request.body) != canonical(body) or len(canonical(body)) > 32768:
            raise ValueError("stop follow-up request is outside the bounded beta contract")
    return resolved, cases


def build_package(catalog=None) -> dict:
    catalog = catalog or get_model_profile_catalog()
    package = expected_candidate()
    package.update(created_at=datetime.now(timezone.utc).isoformat(), candidate_sha256=candidate_sha256(), requests=[])
    for iid, (model, _) in TARGETS.items():
        resolved, cases = resolve_stop_cases(catalog, iid)
        package["catalog_digest"], package["test_extension_digest"] = resolved["catalog_digest"], resolved["test_extension_digest"]
        for case in cases:
            package["requests"].append({"case_id": case["case_id"], "model_id": model,
                                        "target_binding": binding_descriptor(resolved), "body": case["body"],
                                        "request_sha256": hashlib.sha256(canonical(case["body"])).hexdigest()})
    if len(package["requests"]) != 4:
        raise ValueError("stop follow-up requires exactly four requests")
    return package


def send_one(request, item: dict, key: str, *, catalog=None) -> dict:
    if not isinstance(key, str) or not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise ValueError("approved in-memory credential is missing or invalid")
    catalog = catalog or get_model_profile_catalog()
    binding = item.get("target_binding", {})
    iid = binding.get("interface_id")
    if iid not in TARGETS or item.get("model_id") != TARGETS[iid][0]:
        raise ValueError("stop follow-up model or source selection changed")
    resolved, cases = resolve_stop_cases(catalog, iid)
    matching = [case for case in cases if case["case_id"] == item.get("case_id")]
    actual = canonical(item.get("body"))
    if (canonical(binding) != canonical(binding_descriptor(resolved)) or len(matching) != 1 or
            actual != canonical(matching[0]["body"]) or
            item.get("request_sha256") != hashlib.sha256(actual).hexdigest()):
        raise ValueError("stop follow-up request differs from its exact frozen binding")
    from lib.test_runner.adapters.deepseek_beta import execute_bound_observation
    record = execute_bound_observation(item, group="stop", api_key=key, catalog=catalog, request=request)
    if record.get("generation"):
        record["generation"] = {name: record["generation"][name] for name in
                                ("returned_model_id", "finish_reason", "usage", "content", "response_valid")}
    return record


def judge_pair(baseline: dict, stopped: dict) -> dict:
    result = {"stop_effect_observed": False, "status": "insufficient_evidence",
              "actual_baseline_contains_literal": False, "baseline_exact_json": False}
    if (baseline.get("status_code") != 200 or stopped.get("status_code") != 200 or
            canonical(baseline.get("target_binding")) != canonical(stopped.get("target_binding"))):
        return result
    good, controlled = copy.deepcopy(baseline["request"]), copy.deepcopy(stopped["request"])
    if controlled.pop("stop", None) != [STOP_LITERAL] or canonical(good) != canonical(controlled) or "stop" in good:
        return result
    try:
        bg, sg = validate_generation(good, baseline["response"]), validate_generation(stopped["request"], stopped["response"])
    except (ValueError, TypeError, KeyError):
        return result
    base_text, stopped_text = bg["content"], sg["content"]
    mode = ("full_prefix_and_continuation" if base_text.startswith(PREFIX) and exact_target(base_text)
            else "continuation_only" if exact_target(PREFIX + base_text) else "unrecognized")
    result.update(returned_content_mode=mode, actual_baseline_contains_literal=STOP_LITERAL in base_text,
                  baseline_exact_json=mode != "unrecognized")
    if mode == "unrecognized" or STOP_LITERAL not in base_text:
        result["reason"] = "actual unstopped response did not supply the required exact JSON and literal positive control"
        return result
    wanted = base_text.split(STOP_LITERAL, 1)[0]
    # No whitespace trimming: this boundary is inside the tail string.
    effect = bool(wanted and stopped_text == wanted and STOP_LITERAL not in stopped_text)
    result.update(stop_effect_observed=effect, status="observed" if effect else "not_observed",
                  expected_stopped_text=wanted, actual_stopped_text=stopped_text,
                  actual_baseline_text=base_text,
                  scope="one same-input beta stop comparison; no claim about all stop values or token accounting")
    return result


def summarize(package: dict, records: list[dict]) -> dict:
    result = {"source_id": "deepseek", "api_form": "deepseek_beta_chat_prefix", "api_version": "beta",
              "requests_sent": len(records), "request_cap": 4,
              "http_statuses": [record.get("status_code") for record in records],
              "catalog_digest": package["catalog_digest"], "test_extension_digest": package["test_extension_digest"],
              "models": {}, "first_eight_observations_preserved": True, "full_parameter_matrix_certified": False}
    for iid, (model, _) in TARGETS.items():
        selected = {record["case_id"]: record for record in records if record["model_id"] == model}
        result["models"][model] = {"interface_id": iid, **judge_pair(selected.get(CASE_IDS[0], {}), selected.get(CASE_IDS[1], {}))}
    result["both_models_stop_effect_observed"] = all(row["stop_effect_observed"] for row in result["models"].values())
    return result


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    from lib.test_runner.adapters.deepseek_beta import freeze_legacy_package, execute_legacy_package, records_from_report
    package = freeze_legacy_package(build_package(), 'stop')
    if not args.execute:
        print(json.dumps({key: package[key] for key in ("candidate_sha256", "catalog_digest", "test_extension_digest", "request_count")}))
        return
    key = read_official_key(ROOT / ".env")
    if not isinstance(key, str) or not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise ValueError("approved official credential unavailable")
    created = datetime.now(timezone.utc)
    batch = ROOT / "reports/approved_live_20260907" / ("deepseek_beta_stop_followup_" + created.strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:6])
    batch.mkdir(mode=0o700)
    initialize_report_retention(batch, created_at=created)
    def save(name, value):
        path = batch / name
        with path.open("x", encoding="utf-8") as handle:
            os.chmod(path, 0o600)
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        register_report_files(batch, [name])
    save("request_package.json", package)
    records = []
    reports = []
    def completed(report):
        reports.append(report)
        save(f"workflow_{len(reports):02d}.json", report)
        for record in records_from_report(report):
            records.append(record)
            number = len(records)
            save(f"{number:02d}_observation.json", record)
            print(json.dumps({"sequence": number, "model": record["model_id"], "case": record["case_id"],
                              "status": record.get("status_code"), "validation_error": record.get("validation_error")}), flush=True)
    try:
        execute_legacy_package(package, 'stop', api_key=key, evidence_dir=batch / "workflow_runs", on_report=completed)
    finally:
        summary = summarize(package, records)
        save("summary.json", summary)
        print(json.dumps({"batch": str(batch.relative_to(ROOT)), "summary": summary}), flush=True)


if __name__ == "__main__":
    run()
