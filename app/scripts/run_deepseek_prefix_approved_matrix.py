#!/usr/bin/env python3
"""Eight sequential, exact-bound official DeepSeek beta prefix observations."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.deepseek_beta_prefix import build_prefix_request, validate_prefix_response
from lib.deepseek_prefix_live import POSITIVE_CASE, TARGETS, _bound_case
from lib.live_stateful_runners import _response_json
from lib.model_profile_catalog import get_model_profile_catalog
from lib.report_retention import initialize_report_retention, register_report_files
from scripts.deepseek_prefix_execution_candidate import (
    CASE_IDS, PREFIX, CONTROL_JSON, TARGET_JSON, STOP_LITERAL, SCOPE, candidate_sha256, expected_candidate, matrix_cases,
)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_INPUT_BYTES = 32 * 1024
TIMEOUT_SEC = 120


def canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def resolve_cases(catalog, iid: str) -> tuple[dict, list[dict]]:
    model, _ = TARGETS[iid]
    try:
        resolved, _ = _bound_case(catalog, iid, model, POSITIVE_CASE)
    except (KeyError, TypeError, AttributeError):
        raise ValueError("malformed beta prefix Catalog records") from None
    parameter, policy = resolved["parameter_test_binding"], resolved["test_binding"]
    interface, contract = resolved["interface"], resolved["contract"]
    definitions = parameter.get("case_definitions", [])
    cases = matrix_cases(model)
    for record in (interface, parameter, policy):
        if record.get("pressure_test_enabled") is not False or record.get("certification_scope") != SCOPE:
            raise ValueError("matrix requires frozen bounded execution scope and disabled pressure on every target record")
    if contract.get("certification_scope") != "official_documented_beta_prefix_live_unverified":
        raise ValueError("beta prefix Contract reference scope changed")
    capabilities = {}
    for record in (contract, interface):
        declared = record.get("parameter_capabilities", {})
        if not isinstance(declared, dict):
            raise ValueError("matrix capability declaration must be an object")
        capabilities.update(declared)
    for record in (parameter, policy):
        if record.get("api_version") != "beta" or record.get("pressure_test_enabled") is not False:
            raise ValueError("matrix requires explicit beta binding and disabled pressure")
        if any(case_id not in record["test_cases"] or case_id in record.get("excluded_test_profiles", []) for case_id in CASE_IDS):
            raise ValueError("reviewed matrix case absent or excluded")
        for field in ("expectations", "default_expectations"):
            overrides = record.get(field, {})
            if not isinstance(overrides, dict) or any(case["case_id"] in overrides and overrides[case["case_id"]] != case["expectation"] for case in cases):
                raise ValueError("reviewed matrix expectation changed")
    for case in cases:
        matching = [item for item in definitions if item.get("case_id") == case["case_id"]]
        if canonical(matching) != canonical([case]):
            raise ValueError("reviewed matrix case definition changed or duplicated")
        body = copy.deepcopy(case["body"])
        required_capabilities = {"model", "messages", "messages[].prefix", "max_tokens", "stream", "thinking.type",
                                 "response.model", "response.choices[].message.content",
                                 "response.choices[].finish_reason", "response.usage"}
        if "stop" in body:
            required_capabilities.add("stop")
        if any(not isinstance(capabilities.get(field), dict) or capabilities[field].get("state") != "supported"
               for field in required_capabilities):
            raise ValueError("matrix case uses a currently unsupported or malformed effective capability")
        if type(body["max_tokens"]) is not int or not 256 <= body["max_tokens"] <= 512:
            raise ValueError("matrix output token floor or cap violated")
        if len(canonical(body)) > MAX_INPUT_BYTES:
            raise ValueError("matrix request body too large")
        body["messages"][-1]["prefix"] = True
        kwargs = {key: value for key, value in body.items() if key in {"model", "messages", "max_tokens", "stop"}}
        normalized = build_prefix_request(source_id="deepseek", family_id="deepseek", api_form="deepseek_beta_chat_prefix",
                                          thinking_enabled=False, **kwargs)
        if normalized.body != body:
            raise ValueError("matrix body is outside the official beta builder contract")
    return resolved, cases


def build_package(catalog=None) -> dict:
    catalog = catalog or get_model_profile_catalog()
    package = expected_candidate()
    package["created_at"] = datetime.now(timezone.utc).isoformat()
    package["candidate_sha256"] = candidate_sha256()
    package["catalog_digest"] = catalog.digest
    package["requests"] = []
    for iid, (model, _) in TARGETS.items():
        resolved, cases = resolve_cases(catalog, iid)
        package["test_extension_digest"] = resolved["test_extension_digest"]
        binding = {key: resolved[key] for key in ("source_id", "profile_id", "interface_id", "contract_id",
                    "test_binding_id", "parameter_test_binding_id", "catalog_digest", "test_extension_digest")}
        binding.update(api_form="deepseek_beta_chat_prefix", api_version="beta")
        for case in cases:
            package["requests"].append({"case_id": case["case_id"], "model_id": model,
                                        "target_binding": binding, "body": case["body"],
                                        "request_sha256": hashlib.sha256(canonical(case["body"])).hexdigest()})
    if len(package["requests"]) != 8:
        raise ValueError("matrix must contain exactly eight requests")
    return package


def exact_json(text: str, target: dict = TARGET_JSON) -> bool:
    try:
        value = _response_json(text.encode())
        return value == target and type(value.get("answer")) is int
    except (ValueError, UnicodeError):
        return False


def validate_generation(body: dict, payload: dict) -> dict:
    valid = copy.deepcopy(body)
    valid["messages"][-1]["prefix"] = True
    request = build_prefix_request(source_id="deepseek", family_id="deepseek", api_form="deepseek_beta_chat_prefix",
                                  model=valid["model"], messages=valid["messages"], max_tokens=valid["max_tokens"],
                                  stop=valid.get("stop"), thinking_enabled=False)
    parsed = validate_prefix_response(request, status_code=200, payload=payload)
    usage, message = parsed["usage"], payload["choices"][0]["message"]
    if ("error" in payload or parsed["finish_reason"] != "stop" or message.get("tool_calls") or
            message.get("reasoning_content") or message.get("refusal") or
            any(type(usage.get(key)) is not int for key in ("prompt_tokens", "completion_tokens", "total_tokens")) or
            usage["prompt_tokens"] <= 0 or not 0 < usage["completion_tokens"] <= body["max_tokens"] or
            usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]):
        raise ValueError("native response termination, usage, refusal or unexpected thinking/tools invalid")
    cache = {key: usage[key] for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens") if key in usage}
    if (any(type(value) is not int or not 0 <= value <= usage["prompt_tokens"] for value in cache.values()) or
            len(cache) == 2 and sum(cache.values()) != usage["prompt_tokens"]):
        raise ValueError("native cache usage inconsistent")
    content = parsed["continuation"]
    return {"returned_model_id": parsed["model"], "finish_reason": parsed["finish_reason"],
            "usage": usage, "content": content, "response_valid": True,
            "response_alone_matches_control_json": exact_json(content, CONTROL_JSON),
            "response_alone_matches_json": exact_json(content),
            "prefix_plus_response_matches_json": exact_json(PREFIX + content),
            "returned_content_mode": ("full_prefix_and_continuation" if content.startswith(PREFIX) and exact_json(content)
                                      else "continuation_only" if exact_json(PREFIX + content) and not exact_json(content)
                                      else "control_json" if exact_json(content, CONTROL_JSON) else "unrecognized")}


def negative_attribution(positive: dict, negative: dict) -> dict:
    """Never turn an unrelated HTTP 400 or an error merely echoing a prompt into proof."""
    result = {"attributed": False, "method": None}
    if (not positive.get("generation", {}).get("response_valid") or positive.get("status_code") != 200 or
            negative.get("status_code") not in {400, 422} or positive.get("target_binding") != negative.get("target_binding")):
        return result
    good, bad = copy.deepcopy(positive.get("request", {})), copy.deepcopy(negative.get("request", {}))
    try:
        good_value = good["messages"][-1].pop("prefix")
        bad_value = bad["messages"][-1].pop("prefix")
    except (KeyError, IndexError, TypeError):
        return result
    if canonical(good) != canonical(bad) or good_value is not True or bad_value != "not-a-boolean":
        return result
    payload = negative.get("response", {})
    error = payload.get("error") if isinstance(payload, dict) else None
    if (not isinstance(error, dict) or error.get("type") not in {"invalid_request_error", "validation_error"} or
            any(field in payload for field in ("choices", "model", "usage"))):
        return result
    param = error.get("param")
    if param in {"messages[1].prefix", "messages.1.prefix", "messages[].prefix"}:
        return {"attributed": True, "method": "structured_prefix_field_and_single_mutation_control"}
    message = error.get("message", "")
    if not isinstance(message, str):
        return result
    # Official Rust JSON errors sometimes set param=null. Accept only the exact
    # request path plus the unique invalid string and an explicit boolean error.
    path = re.search(r"(?<![A-Za-z0-9_.])messages(?:\[1\]|\.1)\.prefix(?![A-Za-z0-9_.])", message)
    if (path and 'not-a-boolean' in message and
            re.search(r"(?:expected (?:a )?bool(?:ean)?|must be (?:a )?bool(?:ean)?)\b", message, re.I)):
        return {"attributed": True, "method": "exact_prefix_path_unique_invalid_string_boolean_error_and_single_mutation_control"}
    # Native serde reports the message object's path, rather than its prefix
    # member, in current beta responses. Attribute this narrower observation
    # only when the one invalid literal occurs once in the exact request, and
    # the positive request differs solely in that already-checked prefix field.
    object_error = re.fullmatch(
        r'Failed to deserialize the JSON body into the target type: messages\[1\]: '
        r'invalid type: string "not-a-boolean", expected a boolean at line 1 column [1-9][0-9]*', message)
    if (object_error and error.get("param") is None and
            canonical(negative["request"]).count(b'"not-a-boolean"') == 1):
        return {"attributed": True, "method": "native_message_object_boolean_error_unique_literal_and_single_prefix_mutation_control"}
    return result


def observed_prefix_shape(control: dict, positive: dict) -> dict:
    """Describe continuation structure without relaxing the frozen tail target."""
    result = {"controlled_continuation_shape_observed": False, "frozen_exact_tail_target_met": False}
    if (control.get("status_code") != 200 or positive.get("status_code") != 200 or
            control.get("target_binding") != positive.get("target_binding")):
        return result
    control_body, positive_body = copy.deepcopy(control["request"]), copy.deepcopy(positive["request"])
    control_prefix = control_body["messages"][-1].pop("prefix", None)
    positive_prefix = positive_body["messages"][-1].pop("prefix", None)
    if canonical(control_body) != canonical(positive_body) or control_prefix is not False or positive_prefix is not True:
        return result
    try:
        cg = validate_generation(control["request"], control["response"])
        pg = validate_generation(positive["request"], positive["response"])
        full = _response_json((PREFIX + pg["content"]).encode())
    except (ValueError, KeyError, TypeError):
        return result
    try:
        _response_json(pg["content"].encode())
    except (ValueError, UnicodeError):
        continuation_is_not_complete_json = True
    else:
        continuation_is_not_complete_json = False
    shape = (set(full) == {"answer", "tail"} and type(full.get("answer")) is int and full["answer"] == 2 and
             isinstance(full.get("tail"), str) and full["tail"].startswith("PREFILLED_") and
             len(full["tail"]) > len("PREFILLED_"))
    result.update(controlled_continuation_shape_observed=bool(cg["response_alone_matches_control_json"] and
                                                            continuation_is_not_complete_json and shape),
                  frozen_exact_tail_target_met=exact_json(PREFIX + pg["content"]),
                  returned_content_mode="continuation_only" if continuation_is_not_complete_json and shape else "unrecognized",
                  reconstructed_json=full,
                  stop_effect_proved=False,
                  limit="Observed prefix framing only; the original exact-tail matrix result remains unchanged.")
    return result


def summarize(package: dict, records: list[dict]) -> dict:
    result = {"source_id": "deepseek", "api_form": "deepseek_beta_chat_prefix", "api_version": "beta",
              "requests_sent": len(records), "request_cap": 8,
              "http_statuses": [record.get("status_code") for record in records],
              "catalog_digest": package["catalog_digest"], "test_extension_digest": package["test_extension_digest"],
              "models": {}, "stable_chat_or_fim_evidence_used": False,
              "full_parameter_matrix_certified": False}
    for iid, (model, _) in TARGETS.items():
        selected = {record["case_id"]: record for record in records if record["model_id"] == model}
        control, positive, stopped, negative = [selected.get(case_id, {}) for case_id in CASE_IDS]
        cg, pg, sg = [record.get("generation", {}) for record in (control, positive, stopped)]
        positive_mode = pg.get("returned_content_mode")
        prefix_effect = bool(cg.get("response_alone_matches_control_json") and not cg.get("response_alone_matches_json") and
                             positive_mode in {"continuation_only", "full_prefix_and_continuation"})
        stop_effect = False
        if positive_mode in {"continuation_only", "full_prefix_and_continuation"} and sg.get("response_valid"):
            raw = sg["content"]
            prefix_present = raw.startswith(PREFIX)
            continuation = raw[len(PREFIX):] if prefix_present else raw
            # Stop occurs inside a fixed JSON string. JSON parse ignores spacing
            # outside strings while literal CUT_HERE matching remains exact.
            stop_effect = (prefix_present == (positive_mode == "full_prefix_and_continuation") and
                           STOP_LITERAL not in raw and not exact_json(PREFIX + continuation) and
                           exact_json(PREFIX + continuation + 'CUT_HERE_OMEGA"}'))
        attribution = negative_attribution(positive, negative)
        result["models"][model] = {"interface_id": iid, "prefix_effect_observed": prefix_effect,
                                   "stop_effect_observed": stop_effect, "negative_type_attribution": attribution,
                                   "bounded_prefix_matrix_passed": prefix_effect and stop_effect and attribution["attributed"],
                                   "returned_content_modes": {record.get("case_id"): {
                                       key: record.get("generation", {}).get(key) for key in
                                       ("returned_content_mode", "response_alone_matches_control_json",
                                        "response_alone_matches_json", "prefix_plus_response_matches_json")}
                                       for record in (control, positive, stopped) if record}}
    result["both_model_bounded_prefix_matrices_passed"] = all(row["bounded_prefix_matrix_passed"] for row in result["models"].values())
    return result


def _redact(value, key: str):
    if isinstance(value, str):
        return value.replace(key, "[REDACTED]")
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    if isinstance(value, dict):
        return {_redact(name, key): _redact(item, key) for name, item in value.items()}
    return value


def read_official_key(path: Path) -> str | None:
    """Select only the approved variable; never execute or expand dotenv text."""
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    matches = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.strip().removeprefix("export ").partition("=")
        if not separator or name.strip() != "DEEPSEEK_API_KEY":
            continue
        try:
            words = shlex.split(value, comments=True, posix=True)
        except ValueError:
            raise ValueError("official DeepSeek credential declaration is malformed") from None
        if len(words) != 1:
            raise ValueError("official DeepSeek credential declaration must contain one value")
        matches.append(words[0])
    if len(matches) > 1:
        raise ValueError("official DeepSeek credential declaration is ambiguous")
    return matches[0] if matches else None


def send_one(request, item: dict, key: str, *, catalog=None) -> dict:
    if not isinstance(key, str) or not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise ValueError("in-memory official key is missing or invalid")
    catalog = catalog or get_model_profile_catalog()
    binding = item.get("target_binding", {})
    iid = binding.get("interface_id")
    if iid not in TARGETS or item.get("model_id") != TARGETS[iid][0]:
        raise ValueError("unreviewed beta source/model/interface selection")
    resolved, cases = resolve_cases(catalog, iid)
    expected_binding = {name: resolved[name] for name in ("source_id", "profile_id", "interface_id", "contract_id",
                        "test_binding_id", "parameter_test_binding_id", "catalog_digest", "test_extension_digest")}
    expected_binding.update(api_form="deepseek_beta_chat_prefix", api_version="beta")
    matching = [case for case in cases if case["case_id"] == item.get("case_id")]
    if (canonical(binding) != canonical(expected_binding) or len(matching) != 1 or
            canonical(item.get("body")) != canonical(matching[0]["body"]) or
            item.get("request_sha256") != hashlib.sha256(canonical(item.get("body"))).hexdigest() or
            item.get("request_sha256") != hashlib.sha256(canonical(matching[0]["body"])).hexdigest()):
        raise ValueError("request differs from the frozen exact beta matrix binding")
    from lib.test_runner.adapters.deepseek_beta import execute_bound_observation
    return execute_bound_observation(item, group='matrix', api_key=key, catalog=catalog, request=request)


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    from lib.test_runner.adapters.deepseek_beta import freeze_legacy_package, execute_legacy_package, records_from_report
    package = freeze_legacy_package(build_package(), 'matrix')
    if not args.execute:
        print(json.dumps({"ready": True, "request_count": len(package["requests"]),
                          "candidate_sha256": package["candidate_sha256"], "catalog_digest": package["catalog_digest"]}))
        return
    key = read_official_key(ROOT / ".env")
    if not isinstance(key, str) or not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise ValueError("the approved official DeepSeek credential is unavailable or invalid")
    created = datetime.now(timezone.utc)
    batch = ROOT / "reports/approved_live_20260907" / ("deepseek_beta_prefix_" + created.strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:6])
    batch.mkdir(mode=0o700)
    initialize_report_retention(batch, created_at=created)
    def save(name: str, value: dict):
        path = batch / name
        with path.open("x", encoding="utf-8") as handle:
            os.chmod(path, 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
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
        execute_legacy_package(package, 'matrix', api_key=key, evidence_dir=batch / "workflow_runs", on_report=completed)
    finally:
        summary = summarize(package, records)
        save("summary.json", summary)
        print(json.dumps({"batch": str(batch.relative_to(ROOT)), "summary": summary}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    run()
