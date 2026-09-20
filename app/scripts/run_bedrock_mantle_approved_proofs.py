#!/usr/bin/env python3
"""Freeze exact AWS Mantle JSON workflows, with optional CountTokens helpers.

Requires installed source-local Mantle bindings. It never borrows Runtime or
direct Anthropic bindings, changes account settings, or discovers other routes.
Every closed report file is immediately registered for calendar-month retention.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import sys
import threading
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "model-profile-db"))
from model_profile_db import Catalog, load_catalog  # noqa: E402
from lib.report_retention import initialize_report_retention, register_report_files  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402

HOST = "https://bedrock-mantle.us-east-1.api.aws"
BASE_URL = HOST + "/anthropic/v1"
COUNT_PATH = "/anthropic/v1/messages/count_tokens"
MESSAGE_PATH = "/anthropic/v1/messages"
API_VERSION = "2023-06-01"
CONTRACT = "claude_aws_bedrock_mantle_messages"
INTERFACE_SLUG = "bedrock-mantle-messages"
CASE = "bedrock_mantle_messages_json"
REQUEST_CAP = 10
OUTPUT_CAP = 8192
RESPONSE_BYTES = 4 * 1024 * 1024
RESPONSE_SECONDS = 180
MODELS = {
    "anthropic.claude-haiku-4-5": "claude-haiku-4-5-20251001",
    "anthropic.claude-opus-4-7": "claude-opus-4-7",
    "anthropic.claude-opus-4-8": "claude-opus-4-8",
    "anthropic.claude-opus-5": "claude-opus-5",
    "anthropic.claude-sonnet-5": "claude-sonnet-5",
}
# Explicit public provider declarations, not a prefix-stripping algorithm and
# not an AWS guarantee of a particular physical backend or immutable revision.
RETURNED_IDENTITIES = {
    "anthropic.claude-haiku-4-5": ["anthropic.claude-haiku-4-5", "claude-haiku-4-5", "claude-haiku-4-5-20251001"],
    "anthropic.claude-opus-4-7": ["anthropic.claude-opus-4-7", "claude-opus-4-7"],
    "anthropic.claude-opus-4-8": ["anthropic.claude-opus-4-8", "claude-opus-4-8"],
    "anthropic.claude-opus-5": ["anthropic.claude-opus-5", "claude-opus-5"],
    "anthropic.claude-sonnet-5": ["anthropic.claude-sonnet-5", "claude-sonnet-5"],
}
PROMPT = 'Return exactly the JSON object {"color":"blue","count":2} without markdown or other final-answer text.'
EXPECTED = {"color": "blue", "count": 2}
EXPECTED_SCHEMA = {
    "type": "object", "properties": {"color": {"type": "string", "const": "blue"},
                                      "count": {"type": "integer", "const": 2}},
    "required": ["color", "count"], "additionalProperties": False,
}
SEMANTIC_VALIDATOR = Draft202012Validator(EXPECTED_SCHEMA)
OFFICIAL_SOURCES = [
    "https://docs.aws.amazon.com/bedrock/latest/userguide/inference-messages-api.html",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/count-tokens.html",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages-request-response.html",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def public_provider_snapshot() -> dict:
    import yaml
    provider = yaml.safe_load((ROOT / "config.yaml").read_text())["providers"]["bedrock_mantle"]
    interface = provider["api_interfaces"]["claude_messages"]
    models = provider["models"]
    if (provider.get("reference_source_id") != "aws_bedrock" or provider.get("base_url") != BASE_URL
            or provider.get("api_key_env") != "BEDROCK_API_KEY" or interface.get("path") != "/messages"
            or interface.get("auth") not in ("bearer", "anthropic")
            or interface.get("anthropic_version") != API_VERSION):
        raise ValueError("Public provider source/host/path/credential/version binding differs")
    for model, slug in MODELS.items():
        if (model not in models.get("candidates", []) or models.get("reference_source_ids", {}).get(model) != "aws_bedrock"
                or models.get("reference_model_ids", {}).get(model) != slug
                or models.get("default_routes", {}).get(model) != "aws_bedrock"
                or models.get("default_api_forms", {}).get(model, {}).get("aws_bedrock") != "anthropic_messages"
                or models.get("identity_aliases", {}).get(model) != RETURNED_IDENTITIES[model]):
            raise ValueError("Exact public model/route/finite identity declaration differs")
    return {"provider_id": "bedrock_mantle", "source_id": "aws_bedrock", "base_url": BASE_URL,
            "message_path": "/messages", "configured_auth": interface["auth"], "sender_auth": "x-api-key",
            "version_header": {"anthropic-version": API_VERSION}, "credential_env_name": "BEDROCK_API_KEY",
            "models": list(MODELS), "returned_identity_allowlists": RETURNED_IDENTITIES,
            "identity_rule_scope": "Literal public-provider declaration on this exact source/Interface, not physical model or immutable revision attestation."}


def exact_bindings(catalog: Catalog) -> dict:
    if not isinstance(catalog, Catalog):
        raise ValueError("Requires the real MPDB Catalog resolver")
    results = {}
    for model, slug in MODELS.items():
        profile_id = "text/aws_bedrock/claude/" + slug
        interface_id = profile_id + "#" + INTERFACE_SLUG
        policy_id = "interface/" + profile_id + "/" + INTERFACE_SLUG
        resolved = Catalog.resolve_parameter_config(catalog, source_id="aws_bedrock", modality="text", family_id="claude",
            model_slug=slug, interface_id=interface_id, api_form="anthropic_messages", contract_id=CONTRACT,
            test_binding_id=policy_id, parameter_test_binding_id="parameter/" + CONTRACT)
        interface, contract = resolved["interface"], resolved["contract"]
        policy, parameter = resolved["test_binding"], resolved["parameter_test_binding"]
        if (interface.get("request_model_ids") != [model] or interface.get("routing_mode") != "aws_bedrock"
                or interface.get("transport_adapter_id") != "claude_messages" or interface.get("contract_ids") != [CONTRACT]
                or interface.get("default_api_version") != API_VERSION
                or interface.get("protocol_constraints", {}).get("version_header") != {"name": "anthropic-version", "value": API_VERSION}
                or interface.get("api_versions", {}).get(API_VERSION, {}).get("path_template") != MESSAGE_PATH
                or interface.get("protocol_constraints", {}).get("native_input_count", {}).get("path_template") != COUNT_PATH):
            raise ValueError("Mantle Interface model/transport/version/count path is not exact")
        if (contract.get("source_id") != "aws_bedrock" or contract.get("source_ids") != ["aws_bedrock"]
                or contract.get("api_form") != "anthropic_messages"
                or policy.get("parameter_test_enabled") is not True or parameter.get("parameter_test_enabled") is not True
                or parameter.get("runner_enabled") is not True or CASE not in parameter.get("test_cases", [])
                or CASE not in resolved.get("test_cases", [])
                or CASE in resolved.get("excluded_test_profiles", [])):
            raise ValueError("Source-local Mantle contract/case execution binding unavailable")
        caps = {**contract.get("parameter_capabilities", {}), **interface.get("parameter_capabilities", {})}
        if any(caps.get(field, {}).get("state") != "supported" for field in ("model", "messages", "max_tokens", "stream")):
            raise ValueError("Minimal native request field is not documented on this Mantle binding")
        limits = interface.get("parameter_constraints", {}).get("max_tokens", {})
        if not (type(limits.get("inclusive_minimum")) is int and type(limits.get("inclusive_maximum")) is int
                and limits["inclusive_minimum"] <= OUTPUT_CAP <= limits["inclusive_maximum"]):
            raise ValueError("Frozen output cap is outside the exact model's documented limits")
        definition = [row for row in parameter.get("case_definitions", []) if row.get("case_id") == CASE]
        expected_body = {"model": "$REQUEST_MODEL_ID", "messages": [{"role": "user", "content": PROMPT}],
                         "max_tokens": OUTPUT_CAP, "stream": False}
        if (len(definition) != 1 or definition[0].get("expectation") != "supported"
                or digest(definition[0].get("body_template")) != digest(expected_body)):
            raise ValueError("Frozen case differs from the installed source-local parameter definition")
        results[model] = {"source_id": "aws_bedrock", "request_model_id": model, "profile_id": profile_id,
            "interface_id": interface_id, "api_form": "anthropic_messages", "contract_id": CONTRACT,
            "test_binding_id": policy_id, "parameter_test_binding_id": "parameter/" + CONTRACT,
            "api_version": API_VERSION, "resolved_sha256": digest(resolved)}
    return results


def cases() -> list[dict]:
    result = []
    for index, model in enumerate(MODELS, 1):
        common = {"model": model, "messages": [{"role": "user", "content": PROMPT}]}
        result.extend([{"id": f"m{index:02d}_count", "kind": "count", "path": COUNT_PATH, "body": common},
                       {"id": f"m{index:02d}_generate", "kind": "generation", "path": MESSAGE_PATH,
                        "body": {**copy.deepcopy(common), "max_tokens": OUTPUT_CAP, "stream": False}}])
    return result


def make_plan(catalog: Catalog) -> dict:
    return {"created_at": utc_now(), "source_id": "aws_bedrock", "provider": public_provider_snapshot(),
            "catalog_digest": catalog.digest, "test_extension_digest": catalog.payload.get("test_extension_digest"),
            "bindings": exact_bindings(catalog), "cases": cases(), "request_cap": REQUEST_CAP,
            "api_fee_cap": None, "api_fee_cap_policy": "user_explicit_no_total_cap_20260907", "report_retention": "P1M",
            "official_sources": OFFICIAL_SOURCES, "response_byte_cap": RESPONSE_BYTES,
            "response_elapsed_seconds_cap": RESPONSE_SECONDS, "automatic_retries": 0,
            "expected_json_schema": copy.deepcopy(EXPECTED_SCHEMA),
            "semantic_scope": "Prompted JSON fixture; no structured-output parameter or full parameter-effect proof."}


def validate_plan(plan: dict, catalog: Catalog) -> None:
    expected = make_plan(catalog)
    expected["created_at"] = plan.get("created_at")
    if not isinstance(plan.get("created_at"), str) or digest(plan) != digest(expected):
        raise ValueError("Frozen request/host/identity/binding/budget/retention package changed")
    for case in plan["cases"]:
        if case["kind"] == "generation" and (type(case["body"]["max_tokens"]) is not int or case["body"]["max_tokens"] != OUTPUT_CAP):
            raise ValueError("Generation must retain its reviewed integer cap of 8192")


class Reports:
    def __init__(self, path: Path, created_at: str):
        self.path = path.resolve()
        initialize_report_retention(self.path, created_at=datetime.fromisoformat(created_at.replace("Z", "+00:00")))
        self.closed_files: list[str] = []

    def bytes(self, name: str, value: bytes) -> None:
        if Path(name).name != name or name.startswith("."):
            raise ValueError("Unsafe or reserved report filename")
        path = self.path / name
        created = False
        try:
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as output:
                created = True
                output.write(value)
        finally:
            if created:
                register_report_files(self.path, [path])
        # Closed partial/success/failure artifacts are owned immediately; a
        # later process interruption cannot leave prior files unregistered.
        self.closed_files.append(name)

    def json(self, name: str, value: dict) -> None:
        self.bytes(name, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def credential() -> str:
    selected = os.environ.get("BEDROCK_API_KEY")
    if not selected:
        for line in (ROOT / ".env").read_text().splitlines():
            key, equal, value = line.removeprefix("export ").partition("=")
            if equal and key.strip() == "BEDROCK_API_KEY":
                words = shlex.split(value, comments=True)
                if len(words) == 1:
                    selected = words[0]
    if not selected or any(char in selected for char in "\r\n"):
        raise ValueError("Official Bedrock credential missing or invalid")
    return selected


def observe(case: dict, status: int | None, payload: dict, *, complete: bool = True) -> dict:
    model = case["body"]["model"]
    result = {"case_id": case["id"], "kind": case["kind"], "request_model_id": model,
              "status_code": status, "response_complete": complete, "envelope_valid": False,
              "request_body_sha256": digest(case["body"])}
    if status != 200 or payload.get("error"):
        result["error"] = payload.get("error")
    if case["kind"] == "count":
        count = payload.get("input_tokens")
        result.update({"input_count": count, "envelope_valid": bool(complete and status == 200 and not payload.get("error")
                        and type(count) is int and count >= 0), "exact_token_proof": False})
        return result
    usage = payload.get("usage")
    usage_valid = isinstance(usage, dict) and all(type(usage.get(f)) is int and usage[f] >= 0 for f in ("input_tokens", "output_tokens"))
    if usage_valid:
        usage_valid = all(type(usage.get(f, 0)) is int and usage.get(f, 0) >= 0 for f in ("cache_read_input_tokens", "cache_creation_input_tokens"))
        usage_valid = usage_valid and 0 < usage["output_tokens"] <= OUTPUT_CAP
    blocks = payload.get("content")
    def valid_block(block):
        if not isinstance(block, dict):
            return False
        field = {"text": "text", "thinking": "thinking", "redacted_thinking": "data"}.get(block.get("type"))
        return (field is not None and isinstance(block.get(field), str)
                and ("signature" not in block or isinstance(block["signature"], str)))
    blocks_valid = isinstance(blocks, list) and bool(blocks) and all(valid_block(b) for b in blocks)
    text_parts = [b.get("text") for b in blocks if isinstance(b, dict) and b.get("type") == "text"] if isinstance(blocks, list) else []
    text = "".join(text_parts) if all(isinstance(t, str) for t in text_parts) else None
    returned = payload.get("model")
    identity = type(returned) is str and returned in RETURNED_IDENTITIES[model]
    valid = (complete and status == 200 and not payload.get("error") and payload.get("type") == "message"
             and payload.get("role") == "assistant" and isinstance(payload.get("id"), str) and bool(payload["id"])
             and identity and blocks_valid and bool(text) and payload.get("stop_reason") == "end_turn" and usage_valid)
    try:
        parsed = strict_json_loads(text)
        semantic = SEMANTIC_VALIDATOR.is_valid(parsed)
    except (TypeError, ValueError):
        semantic = False
    result.update({"returned_model": returned, "allowed_returned_identities": RETURNED_IDENTITIES[model],
        "identity_matches_declared_binding": identity, "identity_guarantees_physical_revision": False,
        "usage": usage, "usage_valid": bool(usage_valid), "stop_reason": payload.get("stop_reason"),
        "text": text, "content_types": [b.get("type") for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else [],
        "envelope_valid": bool(valid), "json_semantic_valid": semantic, "sample_pass": bool(valid and semantic)})
    return result


def strict_json_loads(value: str) -> Any:
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result
    def invalid_constant(_):
        raise ValueError("Non-JSON numeric constant")
    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid_constant)


@contextmanager
def response_deadline(seconds: float):
    """Bound the entire socket exchange, including a stalled streamed read."""
    if threading.current_thread() is not threading.main_thread() or signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise ValueError("A dedicated main-thread request deadline is required")
    previous = signal.getsignal(signal.SIGALRM)
    def expired(_signum, _frame):
        raise TimeoutError("Response wall-clock bound exceeded")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def compare_pair(counter: dict, generated: dict, count_case: dict, generation_case: dict) -> dict:
    if not counter or not generated:
        return {"status": "not_compared", "exact_token_proof": False}
    if (counter.get("request_model_id") != generated.get("request_model_id")
            or counter.get("request_model_id") != count_case["body"]["model"]
            or generated.get("request_model_id") != generation_case["body"]["model"]
            or count_case["path"] != COUNT_PATH or generation_case["path"] != MESSAGE_PATH
            or counter.get("request_body_sha256") != digest(count_case["body"])
            or generated.get("request_body_sha256") != digest(generation_case["body"])):
        return {"status": "control_failed", "exact_token_proof": False}
    generation_input = {k: v for k, v in generation_case["body"].items() if k not in ("max_tokens", "stream")}
    if generation_input != count_case["body"]:
        return {"status": "control_failed", "reason": "Full structured counter/generation inputs differ", "exact_token_proof": False}
    if not counter.get("envelope_valid") or not generated.get("envelope_valid"):
        return {"status": "not_compared", "exact_token_proof": False}
    if generated.get("returned_model") not in RETURNED_IDENTITIES.get(generation_case["body"]["model"], []):
        return {"status": "control_failed", "reason": "Returned identity is outside the frozen finite declaration", "exact_token_proof": False}
    usage = generated["usage"]
    reported = sum(usage.get(f, 0) for f in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    count = counter["input_count"]
    return {"status": "compared", "counter_input_tokens": count, "generation_total_input_tokens": reported,
            "difference": reported - count, "counts_agree": reported == count, "returned_model": generated["returned_model"],
            "count_kind": "aws_mantle_source_local_input_count", "exact_token_proof": False,
            "limitation": "Numeric input comparison on this exact Mantle request only; no independent output, hidden-wrapper or physical model proof."}


def send_one(session: Any, case: dict, reports: Reports, secret: str, ordinal: int) -> tuple[dict, bool]:
    """Historical sender is deliberately non-executable; use a new frozen workflow."""
    raise ValueError("Historical Mantle sender is offline replay only; prepare a new shared workflow")


def run_live(plan: dict, catalog: Catalog, reports: Reports, *, session_factory=None, key_loader=None) -> dict:
    if plan.get("schema_version") != 2 or plan.get("kind") != "bedrock_mantle_shared_workflow":
        raise ValueError("Frozen historical Mantle packages are offline replay only")
    if session_factory is not None or key_loader is not None:
        raise ValueError("Shared Mantle execution accepts an injected dispatcher through execute_shared")
    return execute_shared(reports.path, package_sha256=digest(plan), catalog=catalog)


def prepare_shared(*, catalog=None, config=None, models=None, include_count=False, runs=1, batch=None, root=None):
    from lib.config import load_config
    from lib.test_runner.adapters.mantle import prepare_mantle_plan
    from scripts.prepare_zai_general_reference import Reports as FrozenReports
    import uuid
    catalog = catalog or load_catalog()
    config = config if config is not None else load_config()
    selected = list(MODELS) if models is None else models
    if (not isinstance(selected, list) or not selected or len(set(selected)) != len(selected)
            or any(model not in MODELS for model in selected)):
        raise ValueError("Select unique exact registered Mantle models")
    selected = [model for model in MODELS if model in selected]
    plans = [prepare_mantle_plan(config, model, catalog, include_count=include_count, runs=runs)[0] for model in selected]
    package = {"schema_version": 2, "kind": "bedrock_mantle_shared_workflow", "created_at": utc_now(),
               "provider": "bedrock_mantle", "models": selected, "include_count": include_count,
               "runs": runs, "plans": plans, "request_cap": sum(plan["limits"]["max_requests"] * runs for plan in plans),
               "automatic_retries": 0, "historical_business_resume": False, "iam_changes": False,
               "full_parameter_certification": False, "physical_identity_attestation": False}
    root = Path(root or ROOT / "reports/approved_live_20260907").absolute()
    batch = Path(batch).absolute() if batch is not None else root / ("bedrock_mantle_shared_" +
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8])
    if batch.parent != root or batch.exists() or batch.is_symlink() or not batch.name.startswith("bedrock_mantle_"):
        raise ValueError("Preparation needs a new direct-child bedrock_mantle_* batch")
    initialize_report_retention(batch, root=root)
    FrozenReports(batch, root=root).json("request_package.json", package)
    return batch, package


def execute_shared(batch, *, package_sha256, config=None, catalog=None, dispatcher=None, root=None):
    from lib.config import load_config
    from lib.test_runner import CancellationContext, IntegrityError, execute_plan
    from lib.test_runner.adapters.mantle import registry_for_mantle_plan, make_dispatcher
    from scripts.prepare_zai_general_reference import Reports as FrozenReports, read_frozen_package
    root = Path(root or ROOT / "reports/approved_live_20260907").absolute()
    batch = Path(batch).absolute()
    package = read_frozen_package(batch, root=root)
    if package.get("schema_version") != 2 or package.get("kind") != "bedrock_mantle_shared_workflow":
        raise ValueError("Historical Mantle packages are offline replay only; prepare a new shared workflow")
    if digest(package) != package_sha256:
        raise ValueError("Frozen Mantle package digest changed")
    reports = FrozenReports(batch, root=root)
    reports.require_fresh()
    config = config if config is not None else load_config()
    catalog = catalog or load_catalog()
    plans = package["plans"]
    if ([plan["target"]["execution_target"]["request_model_id"] for plan in plans] != package["models"]
            or len(set(package["models"])) != len(plans)
            or any(plan["run_count"] != package["runs"] or
                   plan["definition"]["factory_arguments"]["include_count"] != package["include_count"] for plan in plans)
            or package["request_cap"] != sum(plan["limits"]["max_requests"] * plan["run_count"] for plan in plans)):
        raise ValueError("Frozen Mantle model, helper or request budget differs from its plans")
    registries = [registry_for_mantle_plan(config, plan, catalog) for plan in plans]
    reports.json("dispatch_started.json", {"request_package_sha256": package_sha256,
                                           "request_cap": package["request_cap"], "created_at": utc_now()}, claim=True)
    transport = dispatcher if dispatcher is not None else make_dispatcher(config)
    def dispatch(request, **kwargs):
        if digest(read_frozen_package(batch, root=root)) != package_sha256:
            raise IntegrityError("Frozen Mantle package changed before dispatch")
        return transport(request, **kwargs)
    cancellation = CancellationContext()
    results, observations, model_results = [], [], []
    with cancellation.install_signal_handlers():
        for plan, registry in zip(plans, registries):
            result = execute_plan(plan, registry, dispatch, cancellation=cancellation,
                                  evidence_dir=batch / "workflow_runs")
            results.append(result)
            for run in result["runs"]:
                for step in run["steps"].values():
                    if step.get("observation"):
                        observations.append({**step["observation"], "run_id": run["run_id"], "run_index": run["run_index"]})
                generation = run["steps"].get(CASE, {})
                model_results.append({"request_model_id": plan["target"]["execution_target"]["request_model_id"],
                    "run_id": run["run_id"], "run_index": run["run_index"], "workflow_status": run["status"],
                    "json_sample_pass": generation.get("observation", {}).get("sample_pass", False),
                    "token_comparison": generation.get("verdict", {}).get("token_comparison", {"status": "not_compared", "exact_token_proof": False})})
            reports.json(f"workflow_{len(results):02d}.json", result)
            register_report_files(batch, [Path(run["ledger_path"]) for run in result["runs"]] +
                                  [Path(run["ledger_path"]).parent / "run.lock" for run in result["runs"]], root=root)
            if cancellation.cancelled or any(run["fatal_reason"] for run in result["runs"]):
                break
    attempted = sum(run["request_count"] for result in results for run in result["runs"])
    summary = {"schema_version": 2, "provider": "bedrock_mantle", "package_sha256": package_sha256,
        "pass": len(results) == len(plans) and all(result["status"] == "passed" for result in results),
        "requests_attempted": attempted, "request_cap": package["request_cap"],
        "count_requests_sent": sum(row["kind"] == "count" for row in observations),
        "observations": observations, "model_results": model_results, "workflow_results": results,
        "unattempted_models": package["models"][len(results):], "automatic_retries": 0,
        "terminal_reason": "planned_requests_completed" if len(results) == len(plans) and not cancellation.cancelled
                           and not any(run["fatal_reason"] for result in results for run in result["runs"]) else "source_or_transport_requires_inspection",
        "full_parameter_certification": False, "structured_output_parameter_proof": False,
        "physical_identity_attestation": False, "native_aws_runtime_certification": False, "iam_changes": False}
    reports.json("summary.json", summary)
    reports.json("dispatch_finished.json", {"created_at": utc_now(), "requests_attempted": attempted,
                                           "terminal_reason": summary["terminal_reason"]})
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--package-sha256")
    parser.add_argument("--model", action="append", choices=tuple(MODELS), dest="models")
    parser.add_argument("--include-count", action="store_true")
    parser.add_argument("--runs", type=int)
    args = parser.parse_args(argv)
    if args.live:
        if not args.package_sha256 or args.models or args.include_count or args.runs is not None:
            parser.error("Execution requires only --output --live and the frozen --package-sha256")
        result = execute_shared(args.output, package_sha256=args.package_sha256)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["pass"] else 1
    if args.package_sha256:
        parser.error("A package digest is only used with --live")
    batch, package = prepare_shared(batch=args.output, models=args.models, include_count=args.include_count,
                                    runs=1 if args.runs is None else args.runs)
    print(json.dumps({"prepared": str(batch), "request_cap": package["request_cap"],
                      "request_package_sha256": digest(package), "network_requests": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
