#!/usr/bin/env python3
"""Prepare three Z.ai General controls; dispatch only with --execute --batch.

Preparation reads only public configuration and MPDB artifacts. The Coding provider is
preserved as the registered reference; General is an explicit execution target
whose parameter behavior remains unverified until separately dispatched.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml
import requests

from lib.config import _normalize_provider_config, get_provider_config, get_provider_interface
from lib.credential_security import ProviderCredential
from lib.model_profile_catalog import get_model_profile_catalog, resolve_runtime_parameter_config
from lib.parameter_output_limit import enforce_parameter_test_output_limit
from lib.report_retention import DEFAULT_REPORT_ROOT, initialize_report_retention, register_report_files
from lib.report_retention import _batch_directory, _read_manifest, _snapshot

MODEL, PROVIDER, SOURCE = "glm-5.3", "zhipu_official", "zhipu"
FORM, OBSERVATION_SCOPE = "openai_chat_completions", "zai_general"
PROFILE = "text/zhipu/glm/glm-5.3"
INTERFACE = PROFILE + "#openai-chat-default"
CONTRACT = "zhipu_glm_5_3_openai_compat"
CODING_BASE = "https://api.z.ai/api/coding/paas/v4"
GENERAL_URL = "https://api.z.ai/api/paas/v4/chat/completions"
CREDENTIAL_ENV = "ZHIPU_API_KEY"
REQUEST_CAP, OUTPUT_CAP = 3, 2048
RESPONSE_BYTE_CAP, RESPONSE_SECONDS_CAP = 2 * 1024 * 1024, 150
LABELS = ("format_text", "format_json", "format_invalid")
FORMAT_TYPES = ("text", "json_object", "invalid_reference_probe")
PROMPT = (
    "Calculate 6*7. Follow the API response format. In plain-text mode, return exactly "
    'NOT_JSON:42, which is not JSON. If JSON format is required, return exactly either '
    'the JSON number 42 or the JSON object {"answer":42}. No other JSON answers are allowed. '
    'Do not include commentary or Markdown.'
)
DOCS = {
    "checked_at": "2026-09-07",
    "endpoint_and_bearer_auth": "https://docs.z.ai/api-reference/introduction",
    "general_chat": "https://docs.z.ai/api-reference/llm/chat-completion",
    "errors": "https://docs.z.ai/api-reference/api-code",
    "documented_controls": {
        "response_format.type": ["text", "json_object"],
        "glm-5.3.thinking.type": ["enabled"],
        "glm-5.3.reasoning_effort": ["low", "high", "max"],
    },
}


def canonical_bytes(value: object) -> bytes:
    """Canonical wire JSON preserves bool/int/float distinctions; rejects NaN."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def public_config() -> dict:
    # Deliberately avoid load_config(): it reads .env and private overlays.
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    _normalize_provider_config(config, prune_unknown_models=True)
    return config


def registered_reference(config: dict | None = None) -> dict:
    config = copy.deepcopy(config) if config is not None else public_config()
    provider = get_provider_config(config, PROVIDER)
    route = get_provider_interface(config, "chat_completions", PROVIDER)
    models = provider.get("models") or {}
    if (provider.get("name") != PROVIDER or provider.get("base_url") != CODING_BASE
            or provider.get("api_key_env") != CREDENTIAL_ENV
            or route.get("base_url") != CODING_BASE or route.get("path") != "/chat/completions"
            or route.get("auth") != "bearer" or MODEL not in models.get("candidates", [])
            or (models.get("reference_source_ids") or {}).get(MODEL) != SOURCE
            or (models.get("reference_model_ids") or {}).get(MODEL) != MODEL):
        raise ValueError("registered Coding reference or credential environment name changed")
    result = resolve_runtime_parameter_config(config, PROVIDER, MODEL, "glm", "vendor_direct", FORM)
    snapshot = copy.deepcopy(result["model_profile_database"])
    exact = {
        "source_id": SOURCE, "reference_source_id": SOURCE,
        "runtime_provider_id": PROVIDER, "runtime_model_id": MODEL,
        "reference_model_id": MODEL, "runtime_route_profile": "vendor_direct",
        "modality": "text", "family_id": "glm", "model_slug": MODEL,
        "profile_id": PROFILE, "interface_id": INTERFACE, "api_form": FORM,
        "reference_contract_id": CONTRACT,
        "test_binding_id": "interface/" + PROFILE + "/openai-chat-default",
        "parameter_test_binding_id": "parameter/" + CONTRACT,
    }
    if any(snapshot.get(key) != value for key, value in exact.items()):
        raise ValueError("registered reference is not the exact GLM-5.3 source/interface/contract")
    interface, contract = snapshot["interface"], snapshot["reference_contract"]
    policy, parameter = snapshot["test_binding"], snapshot["parameter_test_binding"]
    if (snapshot.get("catalog_resolved") is not True or snapshot.get("legacy_fallback") is not False
            or snapshot.get("reference_unresolved") is not False
            or interface.get("enabled") is not True or interface.get("executable") is not True
            or interface.get("source_id") != SOURCE or interface.get("interface_id") != INTERFACE
            or interface.get("api_form") != FORM or interface.get("routing_mode") != "vendor_direct"
            or contract.get("source_id") != SOURCE or contract.get("contract_id") != CONTRACT
            or contract.get("family_id") != "glm" or contract.get("api_form") != FORM
            or policy.get("parameter_test_enabled") is not True
            or policy.get("source_id") != SOURCE or policy.get("interface_id") != INTERFACE
            or policy.get("contract_id") != CONTRACT
            or parameter.get("source_id") != SOURCE or parameter.get("contract_id") != CONTRACT
            or parameter.get("extension_type") != "parameter" or not parameter.get("test_cases")):
        raise ValueError("exact parameter/interface bindings must remain enabled")
    capabilities = {**contract.get("parameter_capabilities", {}),
                    **interface.get("parameter_capabilities", {})}
    required = ("model", "messages", "stream", "max_tokens", "thinking.type", "reasoning_effort", "response_format")
    if any(capabilities.get(field, {}).get("state") != "supported" for field in required):
        raise ValueError("documented probe fields are not supported in the registered reference")
    source = copy.deepcopy(get_model_profile_catalog().payload["sources"][SOURCE])
    if (source.get("source_type") != "official_direct" or source.get("authority") != "origin_vendor"
            or "docs.z.ai" not in source.get("official_domains", [])
            or DOCS["general_chat"] not in source.get("official_sources", [])
            or DOCS["general_chat"] not in contract.get("official_sources", [])):
        raise ValueError("registered official source does not cover the General chat document")
    for key in ("catalog_digest", "test_extension_digest", "snapshot_digest"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(snapshot.get(key, ""))):
            raise ValueError("missing immutable catalog or snapshot digest")
    unhashed = {key: value for key, value in snapshot.items() if key != "snapshot_digest"}
    if snapshot["snapshot_digest"] != digest(unhashed):
        raise ValueError("registered snapshot content no longer matches its digest")
    return {
        "source": source, "source_sha256": digest(source),
        "configured_provider": {"provider_id": PROVIDER, "endpoint": CODING_BASE + "/chat/completions",
                                "auth": "bearer", "credential_env_name": CREDENTIAL_ENV},
        "model_profile_database": snapshot, "snapshot_sha256": digest(snapshot),
    }


def body_for(label: str) -> dict:
    if label not in LABELS:
        raise ValueError("case outside the three frozen response-format controls")
    return {
        "model": MODEL,
        "messages": [{"role": "system", "content": PROMPT},
                     {"role": "user", "content": "Give the answer."}],
        "stream": False, "max_tokens": OUTPUT_CAP,
        "thinking": {"type": "enabled"}, "reasoning_effort": "low",
        "response_format": {"type": FORMAT_TYPES[LABELS.index(label)]},
    }


def case_for(label: str, snapshot: dict) -> dict:
    body = body_for(label)
    limited = copy.deepcopy(body)
    enforce_parameter_test_output_limit(limited, FORM)
    if canonical_bytes(limited) != canonical_bytes(body):
        raise ValueError("token floor would change the frozen request")
    expectations = {
        "format_text": {"content_exact": "NOT_JSON:42", "valid_json": False},
        "format_json": {"valid_json": True, "semantic_answer": 42, "required_object_shape": None,
                        "allowed_json_answers": [42, {"answer": 42}], "boolean_answer_allowed": False},
        "format_invalid": {"rejection_field": "response_format", "field_attribution_required": True,
                           "generic_http_error_is_evidence": False},
    }
    if label != "format_invalid":
        expectations[label]["response_contract"] = {
            "http_status_exact": 200, "model_exact": MODEL,
            "id_nonempty_string": True, "choices_count": 1, "choice_index_integer_exact": 0,
            "message_role_exact": "assistant", "finish_reason_exact": "stop",
            "tool_or_function_call_allowed": False, "refusal_allowed": False,
            "usage": {
                "required_integer_fields": ["prompt_tokens", "completion_tokens", "total_tokens"],
                "minimum_value": 1, "total_equals_prompt_plus_completion": True,
                "completion_tokens_max": OUTPUT_CAP,
            },
        }
    return {
        "case_id": OBSERVATION_SCOPE + "/" + MODEL + "/" + label, "label": label,
        "method": "POST", "endpoint": GENERAL_URL, "api_form": FORM, "model": MODEL,
        "observation_source_scope": OBSERVATION_SCOPE,
        "registered_reference_snapshot_sha256": digest(snapshot),
        "body": body, "body_sha256": digest(body),
        "expected_observation": expectations[label],
    }


def build_package(config: dict | None = None, *, created_at: str | None = None) -> dict:
    reference = registered_reference(config)
    snapshot = reference["model_profile_database"]
    timestamp = created_at or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", timestamp):
        raise ValueError("created_at must be a UTC timestamp with seconds")
    datetime.fromisoformat(timestamp[:-1] + "+00:00")
    return {
        "schema_version": 1, "status": "prepared_only", "created_at": timestamp,
        "requests_sent": 0, "request_cap": REQUEST_CAP, "generation_output_cap": OUTPUT_CAP,
        "concurrency": 1, "retries": 0, "report_retention": "P1M", "api_total_cost_cap": None,
        "response_byte_cap": RESPONSE_BYTE_CAP, "response_elapsed_seconds_cap": RESPONSE_SECONDS_CAP,
        "allow_redirects": False,
        "registered_reference": reference,
        "execution_target": {"endpoint": GENERAL_URL, "method": "POST", "model": MODEL,
                             "api_form": FORM, "auth": "bearer", "credential_env_name": CREDENTIAL_ENV},
        "source_scope": {"registered_source_id": SOURCE, "observed_source_id": OBSERVATION_SCOPE,
                         "general_live_verified": False, "coding_equivalence_claimed": False,
                         "mainland_equivalence_claimed": False, "full_parameter_matrix_verified": False},
        "catalog_digest": snapshot["catalog_digest"], "test_extension_digest": snapshot["test_extension_digest"],
        "official_documents": copy.deepcopy(DOCS),
        "cases": [case_for(label, snapshot) for label in LABELS],
    }


def validate_package(package: dict, config: dict | None = None) -> None:
    """Compare full canonical content with current reference, never loose ==."""
    if type(package) is not dict or type(package.get("created_at")) is not str:
        raise ValueError("invalid frozen package")
    expected = build_package(config, created_at=package["created_at"])
    if canonical_bytes(package) != canonical_bytes(expected):
        raise ValueError("package types, controls, source scope, or current full binding changed")


def prepare_report(package: dict, batch: Path, *, root: Path = DEFAULT_REPORT_ROOT,
                   config: dict | None = None) -> Path:
    """Register a new immutable package for P1M; existing batches are refused."""
    validate_package(package, config)
    batch, root = Path(batch).absolute(), Path(root).absolute()
    if batch.parent != root or batch.name.startswith(".") or batch.exists():
        raise ValueError("preparation requires a new direct-child report batch")
    initialize_report_retention(batch, root=root,
                                created_at=datetime.fromisoformat(package["created_at"].replace("Z", "+00:00")))
    path = batch / "request_package.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as report:
            report.write(canonical_bytes(package) + b"\n")
    finally:
        # The with block closes even a partial write before P1M registration.
        register_report_files(batch, [path], root=root)
    return path


def strict_json_loads(raw: str | bytes) -> object:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON object key")
            value[key] = item
        return value
    def invalid(_value):
        raise ValueError("nonfinite JSON number")
    def finite(value):
        result = float(value)
        return result if math.isfinite(result) else invalid(value)
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid, parse_float=finite)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _batch_path(batch: Path, root: Path) -> Path:
    batch, root = Path(batch).absolute(), Path(root).absolute()
    if batch.parent != root or batch.name.startswith("."):
        raise ValueError("batch is not a visible direct child of the approved report root")
    return batch


def read_frozen_package(batch: Path, *, root: Path = DEFAULT_REPORT_ROOT) -> dict:
    batch = _batch_path(batch, root)
    with _batch_directory(batch) as directory:
        manifest = _read_manifest(directory, batch, datetime.now(timezone.utc))
        entries = [entry for entry in manifest["owned_files"] if entry["path"] == "request_package.json"]
        if len(entries) != 1 or _snapshot(directory, "request_package.json") != entries[0]:
            raise ValueError("request package differs from its registered immutable file")
        with os.fdopen(os.open("request_package.json", os.O_RDONLY | os.O_NOFOLLOW,
                               dir_fd=directory), "rb") as source:
            raw = source.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024 or _snapshot(directory, "request_package.json") != entries[0]:
            raise ValueError("request package grew or changed while reading")
    package = strict_json_loads(raw)
    if type(package) is not dict or raw != canonical_bytes(package) + b"\n":
        raise ValueError("request package is not the exact saved canonical JSON")
    return package


class Reports:
    def __init__(self, batch: Path, *, root: Path = DEFAULT_REPORT_ROOT):
        self.path, self.root = _batch_path(batch, root), Path(root).absolute()

    @staticmethod
    def _fresh(directory: int) -> None:
        if any(name.startswith(("dispatch_", "case_", "summary")) for name in os.listdir(directory)):
            raise ValueError("batch already has dispatch evidence; replay is forbidden")

    def require_fresh(self) -> None:
        with _batch_directory(self.path) as directory:
            self._fresh(directory)

    def bytes(self, name: str, value: bytes, *, claim: bool = False) -> None:
        if Path(name).name != name or name.startswith("."):
            raise ValueError("unsafe report filename")
        created = False
        try:
            with _batch_directory(self.path) as directory:
                if claim:
                    if name != "dispatch_started.json":
                        raise ValueError("only dispatch start may claim a fresh batch")
                    self._fresh(directory)
                descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=directory)
                created = True
                with os.fdopen(descriptor, "wb") as output:
                    output.write(value)
        finally:
            if created:
                register_report_files(self.path, [name], root=self.root)

    def json(self, name: str, value: object, *, claim: bool = False) -> None:
        self.bytes(name, canonical_bytes(value) + b"\n", claim=claim)


def designated_credential() -> ProviderCredential:
    """Read this literal .env entry only; never expand it or load overlays."""
    selected = None
    path = ROOT / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            matched = re.match(r"^\s*(?:export\s+)?ZHIPU_API_KEY\s*=\s*(.*)$", line)
            if matched:
                words = shlex.split(matched.group(1), comments=True)
                if len(words) != 1 or not words[0]:
                    raise ValueError("designated credential is not one nonempty literal")
                selected = words[0]
    if selected is None:
        selected = os.environ.get(CREDENTIAL_ENV)
    if not selected or any(char in selected for char in "\r\n\x00"):
        raise ValueError("designated ZHIPU_API_KEY is missing or invalid")
    return ProviderCredential.create(provider=PROVIDER, secret=selected,
                                     base_urls=["https://api.z.ai"])


@contextmanager
def response_deadline(seconds: float):
    if (threading.current_thread() is not threading.main_thread()
            or signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0)):
        raise ValueError("request requires an unused main-thread deadline")
    previous = signal.getsignal(signal.SIGALRM)
    def expired(_signum, _frame):
        raise TimeoutError("overall response deadline exceeded")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


STOP_CODES = frozenset({"1000", "1001", "1003", "1005", "1113", "1220", "1302", "1305"}
                       | {str(code) for code in range(1308, 1322)})


def error_payload(payload: object) -> dict:
    if type(payload) is not dict:
        return {}
    if type(payload.get("error")) is dict:
        return payload["error"]
    if "choices" not in payload and "code" in payload and isinstance(payload.get("message"), str):
        return payload
    return {}


def account_or_limit_error(payload: object) -> bool:
    error = error_payload(payload)
    code = error.get("code")
    if type(code) in (str, int) and str(code) in STOP_CODES:
        return True
    message = error.get("message")
    return isinstance(message, str) and bool(re.search(
        r"insufficient\s+(balance|credit)|no\s+resource\s+package|(?:balance|quota|usage|rate|spend).{0,25}"
        r"(?:exhaust|limit|insufficient)|permission|unauthori[sz]ed|authenticat|subscription.{0,30}"
        r"(?:expir|access)|余额|欠费|权限|无权|未授权|鉴权|认证失败|额度|限流|套餐.{0,8}过期",
        message, flags=re.IGNORECASE))


def observe(label: str, status: int | None, payload: object, *, complete: bool = True) -> dict:
    if label not in LABELS:
        raise ValueError("unknown observation control")
    verdict = {"label": label, "pass": False, "envelope_valid": False,
               "field_attributed": False, "valid_json": False, "semantic_answer_valid": False}
    if not complete or type(payload) is not dict or account_or_limit_error(payload):
        return verdict
    if label == "format_invalid":
        error = error_payload(payload)
        param = error.get("param", error.get("field", ""))
        message = error.get("message", "")
        attributed = ((type(param) is str and param in {"response_format", "response_format.type"})
                      or (type(message) is str and re.search(r"\bresponse[_ .-]?format\b", message, re.I)
                          and re.search(r"invalid|unsupported|not\s+(?:valid|supported|allowed)|must|expected|"
                                        r"unknown|unrecognized|非法|无效|不支持|必须|应为", message, re.I)))
        verdict["field_attributed"] = bool(status in (400, 422) and attributed)
        verdict["pass"] = verdict["field_attributed"]
        return verdict
    if (type(status) is not int or status != 200 or payload.get("error") is not None or error_payload(payload)
            or payload.get("model") != MODEL or type(payload.get("id")) is not str
            or not payload["id"].strip()):
        return verdict
    choices, usage = payload.get("choices"), payload.get("usage")
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict or type(usage) is not dict:
        return verdict
    choice, message = choices[0], choices[0].get("message")
    if (type(choice.get("index")) is not int or choice["index"] != 0 or choice.get("finish_reason") != "stop"
            or type(message) is not dict or message.get("role") != "assistant"
            or type(message.get("content")) is not str
            or message.get("tool_calls") not in (None, []) or message.get("function_call") is not None
            or message.get("refusal") not in (None, "")):
        return verdict
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    if (any(type(usage.get(key)) is not int or usage[key] <= 0 for key in fields)
            or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]
            or usage["completion_tokens"] > OUTPUT_CAP):
        return verdict
    verdict.update(envelope_valid=True, usage={key: usage[key] for key in fields}, returned_model=payload["model"])
    content = message["content"]
    try:
        parsed = strict_json_loads(content)
        verdict["valid_json"] = True
    except (ValueError, TypeError):
        parsed = None
    if label == "format_text":
        verdict["pass"] = content == "NOT_JSON:42" and not verdict["valid_json"]
    else:
        answer = parsed.get("answer") if type(parsed) is dict and set(parsed) == {"answer"} else parsed
        verdict["semantic_answer_valid"] = type(answer) in (int, float) and answer == 42
        verdict["pass"] = verdict["valid_json"] and verdict["semantic_answer_valid"]
    return verdict


def send_one(session, package: dict, case: dict, reports: Reports, credential: ProviderCredential,
             record: dict, *, config: dict | None = None) -> None:
    ordinal = record.get("ordinal")
    if type(ordinal) is not int or not 1 <= ordinal <= REQUEST_CAP or case.get("label") != LABELS[ordinal - 1]:
        raise ValueError("outbound ordinal is outside the fixed three-control order")
    saved = read_frozen_package(reports.path, root=reports.root)
    validate_package(saved, config)
    if canonical_bytes(saved) != canonical_bytes(package):
        raise ValueError("saved package differs from dispatch snapshot")
    expected = case_for(case.get("label"), saved["registered_reference"]["model_profile_database"])
    if canonical_bytes(case) != canonical_bytes(expected):
        raise ValueError("outbound case differs from the saved exact control")
    wire = canonical_bytes(case["body"])
    if hashlib.sha256(wire).hexdigest() != case["body_sha256"]:
        raise ValueError("outbound wire bytes differ from the frozen body digest")
    headers = credential.auth_headers(url=GENERAL_URL, auth_mode="bearer")
    prefix = "case_" + str(record["ordinal"]).zfill(2) + "_" + case["label"]
    reports.json(prefix + "_attempt.json", {"created_at": utc_now(), "body_sha256": case["body_sha256"],
                 "endpoint": GENERAL_URL, "case_id": case["case_id"], "ordinal": record["ordinal"]})
    response, raw = None, bytearray()
    started = time.monotonic()
    record.update(status_code=None, client_entered=False, response_complete=False, transport_error_type=None)
    try:
        with response_deadline(RESPONSE_SECONDS_CAP):
            record["client_entered"] = True
            response = session.request("POST", GENERAL_URL, data=wire, headers=headers,
                                       timeout=(15, RESPONSE_SECONDS_CAP), stream=True, allow_redirects=False)
            record["status_code"] = response.status_code
            for chunk in response.iter_content(chunk_size=8192):
                if not isinstance(chunk, bytes):
                    raise ValueError("non-byte response chunk")
                remaining = RESPONSE_BYTE_CAP - len(raw)
                raw.extend(chunk[:remaining])
                if len(chunk) > remaining or time.monotonic() - started >= RESPONSE_SECONDS_CAP:
                    raise ValueError("response size or elapsed-time bound exceeded")
            record["response_complete"] = True
    except (Exception, KeyboardInterrupt) as exc:
        record["transport_error_type"] = type(exc).__name__
    finally:
        if response is not None:
            try:
                response.close()
            except Exception as exc:
                record["transport_error_type"] = record["transport_error_type"] or type(exc).__name__
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    raw_text = credential.redact(bytes(raw).decode("utf-8", errors="replace"))
    try:
        payload = strict_json_loads(bytes(raw).decode("utf-8"))
    except (ValueError, TypeError):
        payload = {}
        record["transport_error_type"] = record["transport_error_type"] or "InvalidJsonEnvelope"
    record["verdict"] = observe(case["label"], record["status_code"], payload,
                                complete=record["response_complete"] and not record["transport_error_type"])
    status = record["status_code"]
    record["stop_batch"] = (bool(record["transport_error_type"]) or not record["response_complete"]
                            or type(status) is not int or status in (401, 402, 403, 408, 429)
                            or (type(status) is int and (status >= 500 or 300 <= status < 400))
                            or account_or_limit_error(payload))
    record["response_raw_file"] = prefix + "_response.txt"
    record["response_sha256"] = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    record["response_sha256_before_redaction"] = hashlib.sha256(raw).hexdigest()
    record["response_bytes_captured_before_redaction"] = len(raw)
    reports.bytes(record["response_raw_file"], raw_text.encode("utf-8"))
    reports.json(prefix + "_observation.json", credential.redact(record))


def summarize(package: dict, records: list[dict], *, fatal_error: str | None = None) -> dict:
    sent = [row for row in records if row.get("client_entered") is True]
    rows = {row["label"]: row.get("verdict", {}) for row in records}
    complete = len(sent) == REQUEST_CAP and len(rows) == REQUEST_CAP
    effect = (complete and all(rows.get(label, {}).get("pass") is True for label in LABELS)
              and rows["format_text"].get("valid_json") is False
              and rows["format_json"].get("valid_json") is True
              and rows["format_json"].get("semantic_answer_valid") is True
              and rows["format_invalid"].get("field_attributed") is True)
    stopped = fatal_error is not None or any(row.get("stop_batch") for row in records)
    return {"created_at": utc_now(), "source_scope": OBSERVATION_SCOPE, "model": MODEL,
            "planned_requests": REQUEST_CAP, "requests_sent": len(sent),
            "sent_count_semantics": "HTTP client entered once; server receipt is not guaranteed",
            "all_planned_requests_dispatched": complete,
            "terminal_state": "stopped_early" if stopped else "completed" if complete else "incomplete",
            "fatal_error_type": fatal_error, "response_format_effect_verified": bool(effect and not stopped),
            "full_parameter_matrix_verified": False, "coding_equivalence_claimed": False,
            "mainland_equivalence_claimed": False, "request_package_sha256": digest(package),
            "http_counts": {str(code): sum(row.get("status_code") == code for row in sent)
                            for code in {row.get("status_code") for row in sent}},
            "observations": records}


def execute(batch: Path, *, root: Path = DEFAULT_REPORT_ROOT, config: dict | None = None,
            credential_factory=None, session_factory=None) -> dict:
    package = read_frozen_package(batch, root=root)
    validate_package(package, config)
    reports = Reports(batch, root=root)
    reports.require_fresh()  # Refuse a replay before even reading credentials.
    credential = (credential_factory or designated_credential)()
    reports.json("dispatch_started.json", {"created_at": utc_now(), "request_package_sha256": digest(package),
                 "request_cap": REQUEST_CAP}, claim=True)
    records, fatal_error = [], None
    try:
        with (session_factory or requests.Session)() as session:
            session.trust_env = False
            session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
            for ordinal, case in enumerate(package["cases"], 1):
                record = {"ordinal": ordinal, "label": case["label"], "case_id": case["case_id"],
                          "request_body_sha256": case["body_sha256"]}
                records.append(record)
                send_one(session, package, case, reports, credential, record, config=config)
                if record["stop_batch"]:
                    break
    except (Exception, KeyboardInterrupt) as exc:
        fatal_error = type(exc).__name__
    finally:
        summary = summarize(package, records, fatal_error=fatal_error)
        reports.json("summary.json", credential.redact(summary))
        reports.json("dispatch_finished.json", {"created_at": utc_now(),
                     "terminal_state": summary["terminal_state"], "requests_sent": summary["requests_sent"]})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--prepare", action="store_true", help="write a new P1M package; no API dispatch")
    action.add_argument("--execute", action="store_true", help="dispatch a reviewed frozen batch once")
    parser.add_argument("--batch", type=Path, help="existing frozen batch directory; required with --execute")
    args = parser.parse_args()
    if args.execute:
        if args.batch is None:
            parser.error("--execute requires --batch")
        summary = execute(args.batch)
        print(json.dumps({key: summary[key] for key in ("terminal_state", "requests_sent", "response_format_effect_verified")}))
        return
    if args.batch is not None:
        parser.error("--batch is only accepted with --execute")
    package = build_package()
    validate_package(package)
    output = {"status": package["status"], "requests_sent": 0, "planned_requests": REQUEST_CAP,
              "endpoint": GENERAL_URL, "model": MODEL, "package_sha256": digest(package),
              "catalog_digest": package["catalog_digest"], "test_extension_digest": package["test_extension_digest"]}
    if args.prepare:
        stamp = package["created_at"].replace("-", "").replace(":", "")
        batch = DEFAULT_REPORT_ROOT / ("zai_general_" + stamp + "_" + uuid.uuid4().hex[:8])
        output["request_package"] = str(prepare_report(package, batch))
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
