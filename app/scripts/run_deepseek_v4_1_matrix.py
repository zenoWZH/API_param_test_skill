#!/usr/bin/env python3
"""Freeze or sequentially execute the official DeepSeek V4.1 Flash matrix.

Each report directory is a single immutable batch. Planning makes no network
calls. Execution needs --execute and refuses source drift or an entered batch.
"""
from __future__ import annotations

import argparse
import calendar
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from lib.config import get_api_key, get_provider_config, load_config
from lib.deepseek_v4_1_matrix import MODEL, build_cases
from lib.deepseek_v4_1_validation import strict_json, validate_response

PROVIDER = "deepseek_official"
ORIGIN = "https://api.deepseek.com"
TIMEOUT_SECONDS = 120
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
SOURCE_FILES = (
    "scripts/run_deepseek_v4_1_matrix.py", "lib/deepseek_v4_1_matrix.py",
    "lib/deepseek_v4_1_validation.py", "lib/config.py", "config.yaml",
    "references/deepseek_v4_1_flash_chat_sources_20260917.json",
    "references/deepseek_v4_1_flash_compat_sources_20260917.json",
)
PATHS = {
    "openai_chat_completions": "/chat/completions", "openai_responses": "/responses",
    "anthropic_messages": "/anthropic/v1/messages", "deepseek_beta_chat_prefix": "/beta/chat/completions",
    "openai_fim_completions_beta": "/beta/completions",
}
HEADER_ALLOWLIST = frozenset({"content-type", "date", "server", "request-id", "x-request-id", "x-ds-trace-id", "retry-after"})


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_hashes() -> dict:
    return {name: sha256((ROOT / name).read_bytes()) for name in SOURCE_FILES}


def verify_source_snapshot(output_dir: Path, package: dict) -> None:
    snapshot = output_dir / "source_snapshot"
    for name, expected in package["source_sha256"].items():
        path = snapshot / name
        if not path.is_file() or sha256(path.read_bytes()) != expected:
            raise ValueError("frozen source snapshot missing or modified: " + name)


def write_json(path: Path, value) -> None:
    encoded = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("xb") as handle:
        os.chmod(temporary, 0o600)
        handle.write(encoded)
    temporary.replace(path)


def retention(now: datetime) -> dict:
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    expiry = now.replace(year=year, month=month, day=min(now.day, calendar.monthrange(year, month)[1]))
    return {"duration": "P1M", "created_at": now.isoformat(), "expires_at": expiry.isoformat(),
            "contains_raw_provider_responses": True, "credentials_persisted": False}


def validate_case(case: dict) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", case.get("id", "")):
        raise ValueError("case id must be safe for a report filename")
    allowed_paths = {PATHS.get(case.get("api_form"))}
    if (case.get("api_form") == "openai_chat_completions" and case.get("id") == "chat_tool_strict_stream"
            and any(tool.get("function", {}).get("strict") is True for tool in case.get("body", {}).get("tools", []))):
        allowed_paths.add("/beta/chat/completions")
    if case.get("path") not in allowed_paths:
        raise ValueError("case form/path is outside the frozen official route allowlist")
    body = case.get("body", {})
    if body.get("model") != MODEL or MODEL != "deepseek-flash":
        raise ValueError("case request model is not deepseek-flash")
    cap_key = "max_output_tokens" if case["api_form"] == "openai_responses" else "max_tokens"
    cap = body.get(cap_key)
    if type(cap) is not int or cap < 256:
        raise ValueError("every executable parameter case must explicitly request at least 256 output tokens")
    if len(canonical(body)) > 2 * 1024 * 1024:
        raise ValueError("request exceeds bounded matrix input size")
    expected = case.get("expected", {})
    if expected.get("kind") not in {"text", "json", "tool", "prefix", "fim", "image", "error"}:
        raise ValueError("unknown response validator kind")
    if expected["kind"] == "error" and not expected.get("error_fields"):
        raise ValueError("negative cases require parameter error attribution")


def select_cases(suite: str, selectors: list[str] | None = None) -> list[dict]:
    all_cases = build_cases()
    ids = [case["id"] for case in all_cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate matrix case IDs")
    for case in all_cases:
        validate_case(case)
    if selectors and set(selectors) - set(ids):
        raise ValueError("unknown case selector: " + ", ".join(sorted(set(selectors) - set(ids))))
    cases = [case for case in all_cases if (suite == "matrix" or case.get("smoke"))
             and (not selectors or case["id"] in selectors)]
    if not cases or selectors and set(selectors) - {case["id"] for case in cases}:
        raise ValueError("selected case is not in the requested suite, or suite is empty")
    return cases


def build_package(suite: str, selectors: list[str] | None = None) -> dict:
    cases = select_cases(suite, selectors)
    for case in cases:
        case["request_sha256"] = sha256(canonical(case["body"]))
    return {"schema_version": 1, "kind": "deepseek_v4_1_official_parameter_matrix", "source": "deepseek",
            "provider": PROVIDER, "model": MODEL, "profile_model": "deepseek-v4.1-flash", "origin": ORIGIN,
            "suite": suite, "selectors": selectors or [], "cases": cases,
            "source_sha256": source_hashes(), "policy": {"automatic_retries": 0, "allow_redirects": False,
            "concurrency": 1, "output_token_floor": 256, "timeout_seconds": TIMEOUT_SECONDS,
            "max_response_bytes": MAX_RESPONSE_BYTES, "pressure_test": False, "stateful_lifecycle": False}}


def prepare(output_dir: Path, suite: str, selectors: list[str] | None = None) -> dict:
    package = build_package(suite, selectors)
    package_path, manifest_path = output_dir / "package.json", output_dir / "manifest.json"
    if package_path.exists() or manifest_path.exists():
        if not package_path.exists() or not manifest_path.exists():
            raise ValueError("partial package already exists; choose a new output directory")
        frozen = strict_json(package_path.read_bytes())
        manifest = strict_json(manifest_path.read_bytes())
        if (frozen != package or manifest.get("package_sha256") != sha256(canonical(frozen))
                or manifest.get("sources") != frozen["source_sha256"]):
            raise ValueError("frozen package or source drift; preserve evidence and prepare a new directory")
        verify_source_snapshot(output_dir, frozen)
        return frozen
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("output directory is not empty")
    output_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(output_dir, 0o700)
    for name, expected in package["source_sha256"].items():
        source = (ROOT / name).read_bytes()
        if sha256(source) != expected:
            raise ValueError("source drift while freezing package")
        path = output_dir / "source_snapshot" / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("xb") as handle:
            os.chmod(path, 0o600)
            handle.write(source)
    verify_source_snapshot(output_dir, package)
    write_json(package_path, package)
    write_json(manifest_path, {"schema_version": 1, "package_sha256": sha256(canonical(package)),
                             "sources": package["source_sha256"], "retention": retention(datetime.now(timezone.utc)),
                             "request_order": [case["id"] for case in package["cases"]]})
    return package


def official_provider(config: dict) -> dict:
    provider = get_provider_config(config, PROVIDER)
    parsed = urlsplit(provider.get("base_url", ""))
    if (parsed.scheme != "https" or parsed.hostname != "api.deepseek.com" or parsed.port not in {None, 443}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip("/") not in {"", "/v1"}):
        raise ValueError("deepseek_official configuration is not the exact official HTTPS API origin")
    return provider


@contextmanager
def deadline(seconds: int):
    """Also bound slow streams whose chunks individually meet read timeout."""
    def expire(_signum, _frame):
        raise TimeoutError("matrix response wall-clock deadline exceeded")
    prior = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prior)


def fetch(session, method: str, path: str, key: str, *, body: dict | None = None) -> dict:
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json"}
    if path == "/anthropic/v1/messages":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    if body and body.get("stream"):
        headers["Accept"] = "text/event-stream"
    started = time.monotonic()
    with deadline(TIMEOUT_SECONDS):
        with session.request(method, ORIGIN + path, headers=headers, data=canonical(body) if body is not None else None,
                             timeout=(15, TIMEOUT_SECONDS), stream=True, allow_redirects=False) as response:
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=8192):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ValueError("response exceeds matrix byte limit")
                chunks.append(chunk)
            raw = b"".join(chunks).decode("utf-8", errors="strict")
            # Providers must not echo credentials; redact any such unexpected echo.
            safe_raw = raw.replace(key, "[REDACTED_SECRET]") if key else raw
            safe_headers = {name.lower(): value.replace(key, "[REDACTED_SECRET]") for name, value in response.headers.items()
                            if name.lower() in HEADER_ALLOWLIST}
            return {"status_code": response.status_code, "headers": safe_headers, "raw": safe_raw,
                    "response_sha256": sha256(safe_raw.encode("utf-8")), "response_bytes": len(safe_raw.encode("utf-8")),
                    "credential_echo_redacted": safe_raw != raw, "elapsed_seconds": round(time.monotonic() - started, 6)}


def summary_for(package: dict, results: list[dict], *, discovery_sha256: str | None = None, terminal: bool = False) -> dict:
    passed = sum(result["validation"]["passed"] is True for result in results)
    return {"schema_version": 1, "source": "deepseek", "provider": PROVIDER, "model": MODEL,
            "profile_model": "deepseek-v4.1-flash", "suite": package["suite"], "selectors": package["selectors"],
            "package_sha256": sha256(canonical(package)), "source_sha256": package["source_sha256"],
            "discovery_sha256": discovery_sha256, "total": len(package["cases"]), "completed": len(results),
            "passed": passed, "failed": len(results) - passed, "terminal": terminal, "executed": True,
            "all_pass": terminal and len(results) == len(package["cases"]) and passed == len(results),
            "results": [{key: result[key] for key in ("case_id", "api_form", "request_sha256", "response_sha256", "status_code", "validation")}
                        for result in results]}


def execute(output_dir: Path, package: dict, *, config: dict | None = None, session=None) -> dict:
    if source_hashes() != package["source_sha256"]:
        raise ValueError("source drift detected before execution")
    verify_source_snapshot(output_dir, package)
    config = load_config() if config is None else config
    official_provider(config)
    key = get_api_key(config, PROVIDER)
    if not key:
        raise ValueError("empty official API credential")
    with (output_dir / "execution_started.json").open("x", encoding="utf-8") as handle:
        os.chmod(output_dir / "execution_started.json", 0o600)
        json.dump({"started_at": datetime.now(timezone.utc).isoformat(), "package_sha256": sha256(canonical(package))}, handle)
    own_session = session is None
    if own_session:
        session = requests.Session()
        session.trust_env = False
        session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
    results, discovery_hash = [], None
    try:
        discovery = fetch(session, "GET", "/models", key)
        discovery_payload = strict_json(discovery["raw"])
        discovery["model_discovered"] = (discovery["status_code"] == 200 and isinstance(discovery_payload, dict)
                                         and any(isinstance(item, dict) and item.get("id") == MODEL for item in discovery_payload.get("data", [])))
        write_json(output_dir / "discovery.json", discovery)
        discovery_hash = sha256((output_dir / "discovery.json").read_bytes())
        if not discovery["model_discovered"]:
            raise ValueError("official GET /models did not discover deepseek-flash")
        (output_dir / "requests").mkdir(mode=0o700)
        (output_dir / "responses").mkdir(mode=0o700)
        (output_dir / "results").mkdir(mode=0o700)
        for case in package["cases"]:
            if source_hashes() != package["source_sha256"]:
                raise ValueError("source drift detected during execution; remaining requests were not sent")
            validate_case(case)
            case_id = case["id"]
            write_json(output_dir / "requests" / (case_id + ".json"),
                       {"case_id": case_id, "method": "POST", "url": ORIGIN + case["path"],
                        "body": case["body"], "request_sha256": case["request_sha256"]})
            entered = time.monotonic()
            try:
                response = fetch(session, "POST", case["path"], key, body=case["body"])
                raw_path = output_dir / "responses" / (case_id + (".sse" if case["body"].get("stream") else ".json"))
                raw_path.write_text(response.pop("raw"), encoding="utf-8")
                os.chmod(raw_path, 0o600)
                validation = validate_response(case, response["status_code"], raw_path.read_text(encoding="utf-8"),
                                               response["headers"].get("content-type", ""))
                if response["credential_echo_redacted"]:
                    validation["passed"] = False
                    validation["errors"].append("provider unexpectedly echoed a credential; stored evidence was redacted")
            except (requests.RequestException, TimeoutError, UnicodeError, ValueError) as exc:
                response = {"status_code": None, "response_sha256": None, "elapsed_seconds": round(time.monotonic() - entered, 6)}
                validation = {"passed": False, "errors": ["transport or capture failure: " + type(exc).__name__]}
            result = {"case_id": case_id, "api_form": case["api_form"], "target_parameters": case["target_parameters"],
                      "request_sha256": case["request_sha256"], **response, "validation": validation}
            write_json(output_dir / "results" / (case_id + ".json"), result)
            results.append(result)
            write_json(output_dir / "summary.json", summary_for(package, results, discovery_sha256=discovery_hash))
            print(json.dumps({"case_id": case_id, "status": result["status_code"], "passed": validation["passed"],
                              "errors": validation["errors"], "elapsed_seconds": result["elapsed_seconds"]}, ensure_ascii=False), flush=True)
        summary = summary_for(package, results, discovery_sha256=discovery_hash, terminal=True)
        write_json(output_dir / "summary.json", summary)
        return summary
    except BaseException as exc:
        summary = summary_for(package, results, discovery_sha256=discovery_hash, terminal=True)
        summary["execution_error"] = type(exc).__name__ + ": " + str(exc).replace(key, "[REDACTED_SECRET]")
        summary["all_pass"] = False
        write_json(output_dir / "summary.json", summary)
        raise
    finally:
        if own_session:
            session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("smoke", "matrix"), default="smoke")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--case", action="append", dest="selectors")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        output_dir = args.output_dir.resolve()
        package = prepare(output_dir, args.suite, args.selectors)
        if args.execute:
            summary = execute(output_dir, package)
            print(json.dumps({key: summary[key] for key in ("total", "passed", "failed", "all_pass")}, ensure_ascii=False))
            return 0 if summary["all_pass"] else 1
        print(json.dumps({"mode": "plan", "suite": args.suite, "cases": len(package["cases"]),
                          "package_sha256": sha256(canonical(package)), "output_dir": str(output_dir)}, ensure_ascii=False))
        return 0
    except (ValueError, RuntimeError, OSError, requests.RequestException) as exc:
        # Deliberately avoid printing config or request headers.
        print("DeepSeek V4.1 matrix refused or failed: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
