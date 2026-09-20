"""Freeze or execute source-scoped Kimi/MiniMax Chat controls once.

The CLI prepares shared workflows from public references: the original 33 controls,
three explicit fresh Kimi baselines and six existing M2.5 JSON controls. The explicit
--variant stream subset has 36 requests. Preparation never requires private history.
Historical package helpers remain available separately; their batches cannot be
resumed through this CLI. --refresh applies only to undispatched shared packages.
"""
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
sys.path[:0] = [str(ROOT), str(ROOT / "packages/model-profile-db")]
import requests
import yaml
from lib.credential_security import ProviderCredential
from lib.model_profile_catalog import get_model_profile_catalog
from lib.parameter_output_limit import enforce_parameter_test_output_limit
from lib.report_retention import DEFAULT_REPORT_ROOT, initialize_report_retention, _batch_directory, _read_manifest, _snapshot
from scripts import kimi_minimax_stream_evidence as evidence
from scripts.prepare_zai_general_reference import (
    Reports, canonical_bytes, digest, read_frozen_package, response_deadline, strict_json_loads, utc_now,
)

REFERENCE = "references/kimi_minimax_stream_request_reference_20260908.json"
REFERENCE_SHA256 = "76da77dbcdf005a8c20d036f3181a51dc01ce2b3343c9e20fe3b78387c9de67b"
TARGETS = ("kimi_k3", "kimi_k27_code", "kimi_k26", "minimax_m3", "minimax_m27", "minimax_m25")
FORM = "openai_chat_completions"
LABELS = ("stream_default", "usage_on", "usage_off", "invalid_stream_type", "invalid_usage_type")
REQUEST_CAP, TIMEOUT, MAX_BYTES = 33, 150, 2 * 1024 * 1024
PROVIDERS = {"kimi_official": {"source_id": "moonshot", "family_id": "kimi", "base_url": "https://api.moonshot.ai/v1", "key_env": "KIMI_API_KEY"},
             "minimax_official": {"source_id": "minimax", "family_id": "minimax", "base_url": "https://api.minimax.io/v1", "key_env": "MINIMAX_API_KEY"}}
POLICY = {"approval_code": "R7-ALL-MODELS-EFFECT-PROOF", "request_cap": 33, "concurrency": 1,
    "retries": 0, "timeout_seconds": TIMEOUT, "response_byte_cap": MAX_BYTES, "allow_redirects": False,
    "report_retention": "P1M", "api_total_cost_cap": None, "output_caps": [1024, 4096, 8192],
    "retained_kimi_raw_baselines": 3, "new_minimax_raw_baselines": 3, "stream_controls_per_target": 5,
    "tools": False, "media": False, "state_resources": False, "pressure": False,
    "automatic_retry_or_output_growth": False, "obfuscation_requests": 0,
    "type_rejection_assumed": False, "usage_absence_assumed_for_false_or_omitted": False,
    "stream_semantic_pass_is_usage_option_proof": False, "full_parameter_matrix_verified": False}
CODE_FILES = ("scripts/run_kimi_minimax_stream_reference.py", "scripts/kimi_minimax_stream_evidence.py",
    "scripts/prepare_zai_general_reference.py", "lib/credential_security.py", "lib/gemini_api_version.py",
    "lib/parameter_output_limit.py", "lib/report_retention.py", "lib/model_profile_catalog.py", "lib/config.py")
CASE_FIELDS = ("case_id", "target_key", "source_id", "provider_id", "model", "api_form", "label", "url",
               "auth_mode", "body", "body_sha256", "snapshot_sha256", "output_cap", "expectation")
GLOBAL_SNAPSHOT_FIELDS = {"catalog_version", "catalog_digest", "test_extension_digest"}


def reference():
    raw = (ROOT / REFERENCE).read_bytes()
    if hashlib.sha256(raw).hexdigest() != REFERENCE_SHA256:
        raise ValueError("Reviewed Kimi/MiniMax request reference changed")
    value = strict_json_loads(raw)
    if [r["target_key"] for r in value["targets"]] != list(TARGETS):
        raise ValueError("Kimi/MiniMax target scope changed")
    return value


def retained(ref):
    path = ROOT / ref["path"]
    if path.parent.parent != DEFAULT_REPORT_ROOT or path.is_symlink():
        raise ValueError("Baseline is outside the exact retained report root")
    with _batch_directory(path.parent) as fd:
        now = datetime.now(timezone.utc)
        manifest = _read_manifest(fd, path.parent, now)
        created = datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(manifest["expires_at"].replace("Z", "+00:00"))
        if not created <= now < expires:
            raise ValueError("Retained baseline is outside its valid P1M period")
        owned = {r["path"]: r for r in manifest["owned_files"]}
        if path.name not in owned or _snapshot(fd, path.name) != owned[path.name]:
            raise ValueError("Baseline ownership changed")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref["file_sha256"] or _snapshot(fd, path.name) != owned[path.name]:
            raise ValueError("Retained baseline bytes changed")
    value = strict_json_loads(raw)
    for key in ref.get("json_pointer", "").lstrip("/").split("/") if ref.get("json_pointer") else []:
        key = key.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if type(value) is list else value[key]
    return value, manifest["expires_at"]


def history():
    result = []
    for row in reference()["targets"]:
        body, expiry = retained(row["request_reference"])
        payload, _ = retained(row["response_reference"])
        if (canonical_bytes(body) != canonical_bytes(row["baseline_body"])
                or digest(body) != row["baseline_body_sha256"]
                or digest(payload) != row["historical_response_canonical_sha256"]):
            raise ValueError("Native nonstream baseline changed")
        if row.get("historical_request_package_reference"):
            packaged, _ = retained(row["historical_request_package_reference"])
            if canonical_bytes(packaged) != canonical_bytes(body):
                raise ValueError("Historical request package differs from actual baseline")
        if row.get("historical_observation_reference"):
            observed, _ = retained(row["historical_observation_reference"])
            if type(observed.get("status_code")) is not int or observed["status_code"] != 200:
                raise ValueError("Historical baseline HTTP success is missing")
        verdict = evidence.observe_message(payload, row["source_id"], row["request_model_id"], row["output_cap"], row["expectation"])
        if verdict["pass"] is not True or verdict["usage_verified"] is not True:
            raise ValueError("Retained complete native baseline no longer replays")
        result.append({**copy.deepcopy(row), "historical_baseline_replay": verdict, "historical_report_expires_at": expiry})
    return result


def source_selection(row, config=None):
    spec = PROVIDERS[row["provider_id"]]
    # Public config only: do not call load_config or inspect private overlays.
    config = config if config is not None else yaml.safe_load((ROOT / "config.yaml").read_text())
    provider = config["providers"][row["provider_id"]]
    route = provider.get("api_interfaces", {}).get("chat_completions", {})
    model = row["request_model_id"]
    models = provider.get("models", {})
    if (provider.get("reference_source_id") != spec["source_id"] or provider.get("base_url") != spec["base_url"]
            or provider.get("api_key_env") != spec["key_env"] or provider.get("api_key")
            or provider.get("default_transport") != "chat_completions"
            or route.get("base_url", provider["base_url"]) != spec["base_url"]
            or route.get("path") != "/chat/completions" or route.get("auth") != "bearer" or route.get("api_key")
            or model not in models.get("candidates", [])
            or models.get("reference_source_ids", {}).get(model) != spec["source_id"]
            or models.get("reference_model_ids", {}).get(model) != model
            or models.get("default_routes", {}).get(model) != "vendor_direct"
            or models.get("default_api_forms", {}).get(model, {}).get("vendor_direct") != FORM
            or row["endpoint"] != spec["base_url"] + "/chat/completions" or row["auth_mode"] != "bearer"
            or any(provider.get(k) is False for k in ("enabled", "executable")) or provider.get("disabled_reason")):
        raise ValueError("Exact saved official source selection changed")
    return {"provider_id": row["provider_id"], "source_id": spec["source_id"], "origin": spec["base_url"],
            "endpoint": row["endpoint"], "credential_env": spec["key_env"], "auth_mode": "bearer"}


def binding(row, catalog=None, config=None):
    catalog = catalog or get_model_profile_catalog()
    t, model = row["selection"], row["request_model_id"]
    spec = PROVIDERS[row["provider_id"]]
    slug = model.lower() if row["source_id"] == "minimax" else model
    s = catalog.resolve_parameter_config(source_id=row["source_id"], modality="text", family_id=spec["family_id"],
        model_slug=slug, interface_id=t["interface_id"], api_form=FORM, contract_id=t["contract_id"],
        test_binding_id=t["test_binding_id"], parameter_test_binding_id=t["parameter_test_binding_id"])
    p, i, c, policy, parameter = (s[k] for k in ("profile", "interface", "contract", "test_binding", "parameter_test_binding"))
    fields = {"model", "messages", "stream", "stream_options.include_usage"}
    fields.update(k for k in ("max_tokens", "max_completion_tokens", "reasoning_effort", "reasoning_split") if k in row["baseline_body"])
    caps = {**c.get("parameter_capabilities", {}), **i.get("parameter_capabilities", {})}
    if (any(s.get(k) != t[k] for k in ("profile_id", "interface_id", "contract_id", "test_binding_id", "parameter_test_binding_id", "api_form"))
            or any(obj.get("source_id") != row["source_id"] for obj in (s, p, i, c, policy, parameter))
            or p.get("model_slug") != slug or p.get("request_model_ids") != [model]
            or p.get("profile_state") != "executable" or p.get("lifecycle") != "active"
            or i.get("lifecycle", p["lifecycle"]) != "active" or i.get("api_form") != FORM
            or i.get("routing_mode") != "vendor_direct" or i.get("transport_adapter_id") != "chat_completions"
            or i.get("enabled") is not True or i.get("executable") is not True
            or i.get("default_contract_id") != t["contract_id"] or i.get("contract_ids") != [t["contract_id"]]
            or i.get("request_model_ids", [model]) != [model]
            or c.get("source_ids") != [row["source_id"]] or c.get("api_form") != FORM
            or c.get("routing_mode") != "vendor_direct" or c.get("lifecycle") == "retired"
            or policy.get("parameter_test_enabled") is not True or not parameter.get("test_cases")
            or any(obj.get("disabled_reason") for obj in (p, i, c, policy, parameter))
            or any(obj.get(k) is False for obj in (p, i, c, policy, parameter) for k in ("enabled", "executable", "runner_enabled", "parameter_test_enabled"))
            or any(caps.get(k, {}).get("state") != "supported" for k in fields)
            or any(policy.get("parameter_expectations", {}).get(k) == "unsupported" for k in fields)):
        raise ValueError("Exact Kimi/MiniMax source/model/form or field gate is unavailable")
    source = catalog.payload["sources"][row["source_id"]]
    if source.get("source_type") != "official_direct" or source.get("authority") != "origin_vendor":
        raise ValueError("Reference source is no longer the direct official vendor")
    return {"resolved": s, "source": copy.deepcopy(source), "configured_source": source_selection(row, config)}


def labels(row):
    return (["nonstream"] if row["requires_new_nonstream_bytes_baseline"] else []) + list(LABELS)


def case_for(row, label, snapshot):
    if label not in labels(row):
        raise ValueError("Unknown fixed Kimi/MiniMax stream control")
    body = copy.deepcopy(row["baseline_body"])
    body["stream"] = False if label == "nonstream" else 17 if label == "invalid_stream_type" else True
    if label in ("usage_on", "usage_off", "invalid_usage_type"):
        body["stream_options"] = {"include_usage": True if label == "usage_on" else False if label == "usage_off" else 17}
    limited = copy.deepcopy(body)
    enforce_parameter_test_output_limit(limited, FORM)
    if canonical_bytes(limited) != canonical_bytes(body):
        raise ValueError("Output floor would mutate the frozen source-specific cap")
    return {"case_id": row["target_key"] + "/" + label, "target_key": row["target_key"], "source_id": row["source_id"],
        "provider_id": row["provider_id"], "model": row["request_model_id"], "api_form": FORM, "label": label,
        "url": row["endpoint"], "auth_mode": "bearer", "body": body, "body_sha256": digest(body),
        "snapshot_sha256": digest(snapshot), "output_cap": row["output_cap"], "expectation": copy.deepcopy(row["expectation"])}


def code_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in CODE_FILES}


def build_package(*, created_at=None, refreshed_from=None):
    timestamp = created_at or utc_now()
    if type(timestamp) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", timestamp):
        raise ValueError("Package timestamp must be UTC seconds")
    datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    rows = history()
    catalog = get_model_profile_catalog()
    config = yaml.safe_load((ROOT / "config.yaml").read_text())
    snapshots = {r["target_key"]: binding(r, catalog, config) for r in rows}
    return {"schema_version": 1, "status": "prepared_only", "created_at": timestamp, "requests_sent": 0,
        "request_reference": {"path": REFERENCE, "sha256": REFERENCE_SHA256}, "policy": copy.deepcopy(POLICY),
        "official_document_review": copy.deepcopy(reference()["official_document_review"]), "targets": rows,
        "snapshots": snapshots, "cases": [case_for(r, label, snapshots[r["target_key"]]) for r in rows for label in labels(r)],
        "code_sha256": code_hashes(), "refreshed_from": copy.deepcopy(refreshed_from)}


def validate_package(package, *, current=True):
    if type(package) is not dict or type(package.get("created_at")) is not str:
        raise ValueError("Invalid frozen stream package")
    expected = build_package(created_at=package["created_at"], refreshed_from=package.get("refreshed_from"))
    if not current:
        # Still compare every historical source entity below when refreshing;
        # permit only these three global snapshot metadata fields to differ.
        for key in TARGETS:
            old, new = package["snapshots"][key]["resolved"], expected["snapshots"][key]["resolved"]
            for field in GLOBAL_SNAPSHOT_FIELDS:
                new[field] = old[field]
        expected["cases"] = [case_for(r, label, expected["snapshots"][r["target_key"]]) for r in expected["targets"] for label in labels(r)]
    if len(package.get("cases", [])) != REQUEST_CAP or canonical_bytes(package) != canonical_bytes(expected):
        raise ValueError("Frozen stream package code, source entities, policy or wire cases changed")


def _new_batch(root, timestamp):
    return Path(root).absolute() / ("kimi_minimax_stream_" + timestamp.replace("-", "").replace(":", "") + "_" + uuid.uuid4().hex[:8])


def prepare(*, root=DEFAULT_REPORT_ROOT):
    package = build_package()
    batch = _new_batch(root, package["created_at"])
    initialize_report_retention(batch, root=root)
    Reports(batch, root=root).json("request_package.json", package)
    return batch, package


def refresh(old_batch, *, root=DEFAULT_REPORT_ROOT):
    old = read_frozen_package(old_batch, root=root)
    reports = Reports(old_batch, root=root)
    reports.require_fresh()
    validate_package(old, current=False)
    if old.get("refreshed_from") is not None:
        raise ValueError("Nested refresh requires a separately audited chain")
    parent = {"batch": str(Path(old_batch).absolute()), "package_sha256": digest(old), "requests_sent": 0}
    new = build_package(refreshed_from=parent)
    new_batch = _new_batch(root, new["created_at"])
    initialize_report_retention(new_batch, root=root)
    Reports(new_batch, root=root).json("request_package.json", new)
    # The new package cannot execute until these parent claims exist. A racing
    # dispatcher wins this O_EXCL claim or the refresh does; they cannot both win.
    reports.json("dispatch_started.json", {"mode": "superseded_without_requests", "created_at": utc_now(),
        "package_sha256": digest(old), "replacement_batch": str(new_batch), "replacement_package_sha256": digest(new)}, claim=True)
    reports.json("dispatch_finished.json", {"terminal_state": "superseded_without_requests", "requests_sent": 0,
        "replacement_batch": str(new_batch), "replacement_package_sha256": digest(new), "created_at": utc_now()})
    return new_batch, new


def validate_refresh_parent(package, batch, *, root):
    parent = package.get("refreshed_from")
    if parent is None: return
    if (type(parent) is not dict or set(parent) != {"batch", "package_sha256", "requests_sent"}
            or type(parent["requests_sent"]) is not int or parent["requests_sent"] != 0):
        raise ValueError("Invalid zero-dispatch refresh parent")
    old_batch = Path(parent["batch"])
    old = read_frozen_package(old_batch, root=root)
    if digest(old) != parent["package_sha256"]:
        raise ValueError("Refresh parent package changed")
    with _batch_directory(old_batch) as fd:
        m = _read_manifest(fd, old_batch, datetime.now(timezone.utc))
        owned = {r["path"]: r for r in m["owned_files"]}
        if any(name.startswith("case_") for name in os.listdir(fd)):
            raise ValueError("Refresh parent contains attempted requests")
        for name in ("dispatch_started.json", "dispatch_finished.json"):
            if name not in owned or _snapshot(fd, name) != owned[name]:
                raise ValueError("Refresh parent has no immutable supersession claim")
            value = strict_json_loads((old_batch / name).read_bytes())
            if (value.get("mode", value.get("terminal_state")) != "superseded_without_requests"
                    or value.get("replacement_batch") != str(Path(batch).absolute())
                    or value.get("replacement_package_sha256") != digest(package)
                    or _snapshot(fd, name) != owned[name]):
                raise ValueError("Refresh parent belongs to a different dispatch")


def credential(provider):
    if provider not in PROVIDERS: raise ValueError("Unselected credential provider")
    name = PROVIDERS[provider]["key_env"]
    selected = None
    path = ROOT / ".env"
    if path.is_file():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                match = re.match(r"^\s*(?:export\s+)?" + re.escape(name) + r"\s*=\s*(.*)$", line)
                if match:
                    if selected is not None: raise ValueError("Ambiguous selected credential entry")
                    try: words = shlex.split(match[1], comments=True)
                    except ValueError: raise ValueError("Invalid selected literal credential") from None
                    if len(words) != 1: raise ValueError("Selected credential is not one literal")
                    selected = words[0]
    if selected is None: selected = os.environ.get(name)
    if type(selected) is not str or not selected or not selected.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in selected):
        raise ValueError("Selected official credential is unavailable")
    return ProviderCredential.create(provider=provider, secret=selected, base_urls=[PROVIDERS[provider]["base_url"]])


def terminal_error(payload):
    if type(payload) is not dict: return False
    error = payload.get("error")
    if type(error) is not dict:
        native = payload.get("base_resp")
        error = {"message": native.get("status_msg")} if type(native) is dict and native.get("status_code") != 0 else {}
    if error.get("type") in ("authentication_error", "permission_error", "rate_limit_error", "overloaded_error"):
        return True
    message = error.get("message")
    return type(message) is str and bool(re.search(
        r"quota|insufficient\s+(balance|credit)|billing|rate.?limit|unauthenticated|authentication|invalid\s+(api.?key|credential)|permission.denied|server.overload", message, re.I))


def observe(case, record, *, positive_control=False):
    base = {"semantic_observation": None, "stream_semantic_verified": False, "usage_verified": False,
            "field_type_rejection_verified": False, "documented_usage_true_reported": None,
            "outcome": "incomplete_response", "full_parameter_matrix_verified": False,
            "type_rejection_required_by_document_assumed": False}
    if record.get("response_complete") is not True or record.get("failure_type"):
        return base
    raw = record["response_bytes"]
    payload = record.get("response_json")
    negative = case["label"].startswith("invalid_")
    field = "stream" if case["label"] == "invalid_stream_type" else "stream_options.include_usage"
    if negative and not terminal_error(payload) and evidence.attributed_type_rejection(payload, case["source_id"], record["status_code"], field):
        base.update(outcome="attributed_type_rejection" if positive_control is True else "rejection_without_positive_control",
                    field_type_rejection_verified=positive_control is True, positive_control_verified=positive_control is True,
                    rejection_field=field)
        return base
    if record.get("status_code") != 200 or terminal_error(payload):
        base["outcome"] = "unattributed_error"
        return base
    kind = record.get("content_type", "").split(";")[0].strip().lower()
    if kind == "text/event-stream":
        result = evidence.observe_stream(raw, case["source_id"], case["model"], case["output_cap"], case["expectation"], transport_complete=True)
    elif kind == "application/json":
        result = evidence.observe_message(payload, case["source_id"], case["model"], case["output_cap"], case["expectation"])
    else:
        base["outcome"] = "unexpected_content_type"
        return base
    base.update(semantic_observation=result, usage_verified=result["usage_verified"],
                stream_semantic_verified=result["pass"] and result["stream_envelope_verified"],
                outcome="native_semantics_verified" if result["pass"] else "native_or_semantic_mismatch")
    if negative:
        base["outcome"] = "nonboolean_value_accepted_with_verified_native_semantics" if result["pass"] else "nonboolean_value_not_rejected_response_unverified"
        base["positive_control_verified"] = positive_control is True
    elif case["label"] != "nonstream" and kind != "text/event-stream":
        base["outcome"] = "stream_true_returned_nonstream_response"
    if case["label"] == "usage_on":
        base["documented_usage_true_reported"] = base["stream_semantic_verified"] and result["usage_verified"]
    return base


def send_one(session, package, case, key, reports, record, *, positive_control=False):
    validate_package(package)
    ordinal = record["ordinal"]
    if type(ordinal) is not int or not 1 <= ordinal <= REQUEST_CAP or canonical_bytes(case) != canonical_bytes(package["cases"][ordinal - 1]):
        raise ValueError("Outbound case is not the exact next frozen case")
    expected_origin = PROVIDERS[case["provider_id"]]["base_url"].removesuffix("/v1")
    if key.provider != case["provider_id"] or key.allowed_origins != frozenset({expected_origin}):
        raise ValueError("Selected credential binding changed")
    record.update({k: copy.deepcopy(case[k]) for k in CASE_FIELDS})
    record.update(client_entered=False, response_complete=False, status_code=None, failure_type=None)
    prefix = f"case_{ordinal:02d}"
    reports.json(prefix + "_attempt.json", {"ordinal": ordinal, "created_at": utc_now(), "case": case,
                 "request_package_sha256": digest(package), "client_entry_pending": True})
    raw, response, chunks, received, started = bytearray(), None, [], 0, time.monotonic()
    try:
        with response_deadline(TIMEOUT):
            headers = key.auth_headers(url=case["url"], auth_mode="bearer")
            record["client_entered"] = True
            response = session.request("POST", case["url"], headers=headers, data=canonical_bytes(case["body"]),
                timeout=(15, TIMEOUT), stream=True, allow_redirects=False)
            record.update(status_code=response.status_code, content_type=response.headers.get("Content-Type", ""),
                          content_encoding=response.headers.get("Content-Encoding"))
            for chunk in response.iter_content(chunk_size=1024):
                if type(chunk) is not bytes: raise ValueError("Response chunk is not bytes")
                received += len(chunk)
                if len(raw) + len(chunk) > MAX_BYTES:
                    raw.extend(chunk[:MAX_BYTES - len(raw)])
                    record["response_byte_cap_exceeded"] = True
                    raise ValueError("Response byte cap exceeded")
                raw.extend(chunk)
                if chunk: chunks.append({"end_byte": len(raw), "elapsed_seconds": round(time.monotonic() - started, 6)})
            record["response_complete"] = True
            raw.decode("utf-8")
            kind = record["content_type"].split(";")[0].strip().lower()
            if kind == "text/event-stream":
                record["parsed_events"] = evidence.parse_frames(bytes(raw))
            else:
                record["response_json"] = strict_json_loads(bytes(raw))
    except (Exception, KeyboardInterrupt) as exc:
        record.update(failure_type=type(exc).__name__, interrupted=isinstance(exc, KeyboardInterrupt))
    finally:
        if response is not None:
            try: response.close()
            except Exception as exc: record["failure_type"] = record["failure_type"] or type(exc).__name__
    record["response_bytes"] = bytes(raw)
    record["verdict"] = observe(case, record, positive_control=positive_control)
    record.pop("response_bytes")
    record.update(elapsed_seconds=round(time.monotonic() - started, 3), response_bytes_received=received,
                  response_bytes_captured=len(raw), transport_chunks=chunks,
                  raw_byte_scope="requests_iter_content_decoded_http_entity",
                  response_sha256_before_redaction=hashlib.sha256(raw).hexdigest())
    status = record["status_code"]
    sse_errors = any(terminal_error(f.get("data")) for f in record.get("parsed_events", []) if type(f) is dict)
    native = record["verdict"].get("semantic_observation") or {}
    record["stop_batch"] = bool(record["failure_type"] or type(status) is not int or status in (401, 402, 403, 404, 408, 429)
        or type(status) is int and (300 <= status < 400 or status >= 500) or terminal_error(record.get("response_json")) or sse_errors
        or case["label"] == "nonstream" and not (native.get("pass") is True and native.get("usage_verified") is True))
    retained_raw = key.redact(bytes(raw).decode("latin-1")).encode("latin-1")
    record.update(raw_redacted=retained_raw != bytes(raw), response_raw_file=prefix + "_response.bin",
                  response_sha256=hashlib.sha256(retained_raw).hexdigest())
    reports.bytes(record["response_raw_file"], retained_raw)
    public = key.redact(record)
    record.clear()
    record.update(public)
    reports.json(prefix + "_observation.json", record)


def usage_comparisons(package, records):
    out = []
    for target in TARGETS:
        rows = {r.get("label"): r for r in records if r.get("target_key") == target}
        group = {label: rows.get(label, {}).get("verdict", {}) for label in ("stream_default", "usage_on", "usage_off")}
        semantic = all(v.get("stream_semantic_verified") is True for v in group.values())
        reported = {label: v.get("usage_verified") if v else None for label, v in group.items()}
        changes = semantic and reported == {"stream_default": False, "usage_on": True, "usage_off": False}
        out.append({"target_key": target, "all_three_stream_semantics_verified": semantic,
                    "native_usage_verified_by_control": reported,
                    "documented_usage_true_reported": group["usage_on"].get("documented_usage_true_reported"),
                    "usage_presence_changes_with_option_observed": changes,
                    "false_or_omitted_absence_required_by_document": False,
                    "token_counter_value_change_is_option_effect": False,
                    "control_observations": {label: {"usage_distribution": (v.get("semantic_observation") or {}).get("usage_distribution"),
                        "outcome": v.get("outcome")} for label, v in group.items()}})
    return out


def summarize(package, records, *, fatal_error=None):
    sent = sum(r.get("client_entered") is True for r in records)
    stopped = bool(fatal_error or any(r.get("stop_batch") for r in records))
    complete = sent == REQUEST_CAP and len(records) == REQUEST_CAP
    return {"requests_sent": sent, "planned_requests": REQUEST_CAP, "all_planned_requests_dispatched": complete,
        "terminal_state": "stopped_early" if stopped else "completed" if complete else "incomplete",
        "fatal_error_type": fatal_error, "report_retention": "P1M", "api_total_cost_cap": None,
        "request_package_sha256": digest(package), "full_parameter_matrix_verified": False,
        "stream_semantics_verified": sum(r.get("verdict", {}).get("stream_semantic_verified") is True for r in records),
        "field_type_rejections_verified": sum(r.get("verdict", {}).get("field_type_rejection_verified") is True for r in records),
        "http_counts": {str(s): sum(r.get("status_code") == s for r in records) for s in sorted({r.get("status_code") for r in records}, key=str)},
        "usage_controls": usage_comparisons(package, records), "observations": records,
        "sent_count_semantics": "HTTP client entered once; server receipt is not guaranteed"}


def execute(batch, *, root=DEFAULT_REPORT_ROOT, credential_factory=None, session_factory=None):
    package = read_frozen_package(batch, root=root)
    validate_package(package)
    validate_refresh_parent(package, batch, root=root)
    reports = Reports(batch, root=root)
    reports.require_fresh()
    factory = credential_factory or credential
    keys = {provider: factory(provider) for provider in PROVIDERS}
    reports.json("dispatch_started.json", {"created_at": utc_now(), "request_package_sha256": digest(package), "request_cap": REQUEST_CAP}, claim=True)
    records, fatal_error = [], None
    try:
        with (session_factory or requests.Session)() as session:
            session.trust_env = False
            session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
            for ordinal, case in enumerate(package["cases"], 1):
                positive_label = "usage_on" if case["label"] == "invalid_usage_type" else "stream_default"
                positive = next((r["verdict"].get("stream_semantic_verified") is True for r in records
                                 if r.get("target_key") == case["target_key"] and r.get("label") == positive_label), False)
                record = {"ordinal": ordinal}
                records.append(record)
                send_one(session, package, case, keys[case["provider_id"]], reports, record, positive_control=positive)
                print(json.dumps({"ordinal": ordinal, "case_id": case["case_id"], "http": record["status_code"],
                    "outcome": record["verdict"]["outcome"], "usage_verified": record["verdict"]["usage_verified"]}), flush=True)
                if record["stop_batch"]: break
    except (Exception, KeyboardInterrupt) as exc:
        fatal_error = type(exc).__name__
    finally:
        result = summarize(package, records, fatal_error=fatal_error)
        for key in keys.values(): result = key.redact(result)
        reports.json("summary.json", result)
        reports.json("dispatch_finished.json", {"created_at": utc_now(), "requests_sent": result["requests_sent"],
                     "terminal_state": result["terminal_state"]})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--prepare", action="store_true")
    group.add_argument("--execute", action="store_true")
    group.add_argument("--refresh", action="store_true")
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--target", action="append", choices=TARGETS, dest="targets")
    parser.add_argument("--variant", choices=("stream", "all", "json"), default="all")
    args = parser.parse_args()
    if args.execute or args.refresh:
        if args.batch is None: parser.error("--execute/--refresh requires --batch")
    elif args.batch is not None: parser.error("--batch requires --execute or --refresh")
    if (args.execute or args.refresh) and args.targets:
        parser.error("--target is frozen during preparation")
    from lib.test_runner.adapters.kimi_minimax import execute_shared_batch, prepare_shared_batch, refresh_shared_batch
    if args.execute:
        result = execute_shared_batch(args.batch)
        print(json.dumps({k: result[k] for k in ("requests_sent", "terminal_state", "stream_semantics_verified", "field_type_rejections_verified", "pass")}))
        return 0 if result["pass"] else 1
    batch, package = refresh_shared_batch(args.batch) if args.refresh else prepare_shared_batch(target_keys=args.targets, variant=args.variant)
    print(json.dumps({"batch": str(batch), "request_package_sha256": digest(package), "requests_sent": 0,
                      "planned_requests": package["planned_requests"], "reference_sha256": REFERENCE_SHA256,
                      "historical_baseline_replayed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
