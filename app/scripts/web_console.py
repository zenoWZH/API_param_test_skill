from __future__ import annotations

import json
import math
import copy
import hashlib
import hmac
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    render_template_string,
    request,
    send_from_directory,
    session,
)

from lib.adaptive_load import resolve_context_window
from lib.config import (
    api_form_for_transport,
    default_reports_root,
    ensure_dir,
    skill_data_dir,
    get_active_provider_name,
    get_image_model_config,
    get_image_provider_config,
    get_model_api_form,
    get_model_api_forms,
    get_model_family,
    get_model_route_profile,
    get_model_route_profiles,
    get_model_transport,
    get_provider_interface,
    get_provider_config,
    get_selected_model,
    get_timeout_sec,
    image_provider_has_api_key,
    list_image_providers,
    list_public_providers,
    load_config,
    parse_duration_seconds,
    provider_has_api_key,
)
from lib.credential_security import SELECTED_API_KEY_ENV, build_provider_child_env
from lib.cache_suite import (
    approved_cache_tool_profile,
    cache_plan_requires_tool_profile,
)
from lib.load_rate import ConstantThroughputPlan
from lib.metrics import (
    RequestRecord,
    build_time_series,
    load_history,
    load_records,
    percentile,
    record_in_measurement_window,
    summarize_records,
    write_json,
)
from lib.deepseek_params import build_request, weighted_workload_profiles
from lib.parameter_job_controls import (
    parameter_execution_from_job, historical_parameter_execution,
    WORKFLOW_JOB_SPEC_VERSION, workflow_termination_grace_seconds,
)
from lib.job_spec import (
    load_job_spec,
    SUPPORTED_JOB_TYPES,
    build_result_validation_contract,
    classify_cache_result,
    classify_parameter_result,
    classify_workflow_result,
    make_job_spec,
    resolve_job_model_profile_snapshot,
    resolve_cache_plan,
    resolve_image_plan,
    resolve_request_mode,
    resolve_soak_plan,
    resolve_staircase_plan,
    validate_workload,
)
from lib.model_profile_catalog import (
    capability_profile_from_database_snapshot,
    database_snapshot,
    require_official_reference_binding,
    resolve_runtime_parameter_config,
    resolve_runtime_profile_binding,
    resolve_runtime_test_policy,
)
from lib.reference_specs import (
    capability_profile_snapshot,
    default_reference_source_for_family,
    default_reference_source_for_model,
    get_reference_source,
    list_reference_sources,
    load_model_capability_profile,
    model_reference_spec_payload,
    pressure_test_runnable,
    reference_spec_payload,
    reference_sources_for_model,
    test_profiles_for_reference,
)


REPORTS_ROOT = default_reports_root()
JOBS_ROOT = REPORTS_ROOT / "jobs"
CONSOLE_AUTH_PATH = skill_data_dir() / "console_auth.json"
CONSOLE_SECRET_PATH = skill_data_dir() / "console_secret_key"
CONSOLE_PASSWORD_PATH = skill_data_dir() / "console_password"
DEFAULT_QUICK_USERS = 10
DEFAULT_QUICK_SPAWN_RATE = 2
DEFAULT_QUICK_DURATION = "2m"
DEFAULT_WORKLOAD = "throughput"
DEFAULT_PARAM_TEST_RUNS = 1
MAX_PARAM_TEST_RUNS = 1000
TOOL_VALIDATION_MODES = {"auto", "openai_compat", "gemini_native", "claude_native"}
MAX_CACHE_MEASURED_REQUESTS = 1000
DEFAULT_TARGET_RPM = 0.0
DEFAULT_TARGET_TPM = 0.0
LOAD_RESULT_SCHEMA_VERSION = 9


def _cache_tool_capability(
    policy: dict[str, Any], family: str, transport: str
) -> dict[str, Any]:
    """Project the exact cache-tool gate used by the runner."""
    try:
        profile = approved_cache_tool_profile(policy, family, transport)
    except ValueError as exc:
        return {
            "cache_tool_runnable": False,
            "cache_tool_profile": None,
            "cache_tool_disabled_reason": str(exc),
        }
    return {
        "cache_tool_runnable": True,
        "cache_tool_profile": profile,
        "cache_tool_disabled_reason": None,
    }


def _require_cache_plan_tool_capability(
    cache_plan: dict[str, Any] | None,
    policy: dict[str, Any],
    family: str,
    transport: str,
) -> None:
    if not cache_plan_requires_tool_profile(cache_plan):
        return
    capability = _cache_tool_capability(policy, family, transport)
    if capability["cache_tool_runnable"] is True:
        return
    scenario = str((cache_plan or {}).get("scenario") or "unknown")
    raise ValueError(
        f"Cache scenario {scenario!r} requires an MPDB-approved tool profile "
        f"for family={family!r}, transport={transport!r}: "
        f"{capability['cache_tool_disabled_reason']}"
    )


def _interface_contract_deprecated_aliases(
    replacements: dict[str, str],
) -> dict[str, dict[str, object]]:
    return {
        legacy: {
            "replacement": canonical,
            "semantic_type": "InterfaceContract",
            "deprecated": True,
            "legacy": True,
        }
        for legacy, canonical in replacements.items()
    }


def _select_interface_contract_id(
    canonical_value: Any,
    legacy_value: Any,
    default_value: Any = None,
) -> tuple[str, bool]:
    """Resolve a Contract ID while keeping the legacy name fail-closed."""

    canonical_present = canonical_value is not None
    legacy_present = legacy_value is not None
    canonical = str(canonical_value or "").strip()
    legacy = str(legacy_value or "").strip()
    if canonical_present and legacy_present and canonical != legacy:
        raise ValueError(
            "contract_id and deprecated reference_source alias must be equal "
            "when both are supplied."
        )
    return str(canonical or legacy or default_value or "").strip(), legacy_present


def _contract_first_param_payload(
    payload: dict[str, Any],
    contract_id: str,
    *,
    legacy_query_alias_used: bool,
) -> dict[str, Any]:
    result = dict(payload)
    result["contract_id"] = contract_id
    # Compatibility is intentionally value-identical and explicitly marked.
    result["reference_source"] = contract_id
    result["deprecated_aliases"] = _interface_contract_deprecated_aliases(
        {"reference_source": "contract_id"}
    )
    result["legacy_query_alias_used"] = legacy_query_alias_used
    return result


app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("WEB_CONSOLE_COOKIE_SECURE") == "1",
)


class AuthConfigurationError(RuntimeError):
    """Authentication is enabled but its credential source is unusable."""


def _auth_disabled() -> bool:
    return os.getenv("LLM_API_TEST_DISABLE_AUTH") == "1"


def _load_auth_credentials() -> tuple[str, str, str] | None:
    if _auth_disabled():
        return None
    user = os.getenv("WEB_CONSOLE_USER")
    password = os.getenv("WEB_CONSOLE_PASSWORD")
    if bool(user) != bool(password):
        raise AuthConfigurationError(
            "WEB_CONSOLE_USER and WEB_CONSOLE_PASSWORD must be set together"
        )
    if user and password:
        return user, "plain", password
    if not CONSOLE_AUTH_PATH.exists():
        raise AuthConfigurationError("console authentication file is missing")
    if CONSOLE_AUTH_PATH.is_symlink() or not CONSOLE_AUTH_PATH.is_file():
        raise AuthConfigurationError(
            "console authentication path is not a regular file"
        )
    CONSOLE_AUTH_PATH.parent.chmod(0o700)
    CONSOLE_AUTH_PATH.chmod(0o600)
    try:
        data = json.loads(CONSOLE_AUTH_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AuthConfigurationError(
            "console authentication file is unreadable or invalid"
        ) from exc
    if not all(data.get(key) for key in ("user", "salt", "password_pbkdf2")):
        raise AuthConfigurationError("console authentication file is incomplete")
    username = str(data["user"])
    salt = str(data["salt"])
    password_hash = str(data["password_pbkdf2"])
    try:
        salt_bytes = bytes.fromhex(salt)
        hash_bytes = bytes.fromhex(password_hash)
    except ValueError as exc:
        raise AuthConfigurationError(
            "console authentication file has invalid credential encoding"
        ) from exc
    if len(salt_bytes) != 16 or len(hash_bytes) != 32:
        raise AuthConfigurationError(
            "console authentication file has invalid credential lengths"
        )
    return username, salt, password_hash


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 100_000
    ).hex()


def _verify_password(password: str, creds: tuple[str, str, str]) -> bool:
    _user, salt, stored = creds
    if salt == "plain":
        return hmac.compare_digest(password, stored)
    return hmac.compare_digest(_hash_password(password, salt), stored)


def _atomic_private_text(path: Path, text: str) -> None:
    """Flush and atomically replace one console credential file as mode 0600."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        os.chmod(temp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        parent_fd = os.open(path.parent, flags)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise


def _write_auth_file(user: str, password: str) -> None:
    salt = secrets.token_hex(16)
    payload = {
        "user": user,
        "salt": salt,
        "password_pbkdf2": _hash_password(password, salt),
    }
    _atomic_private_text(CONSOLE_AUTH_PATH, json.dumps(payload))
    _atomic_private_text(CONSOLE_PASSWORD_PATH, password)


def _ensure_auth_configured() -> tuple[str, str] | None:
    if _auth_disabled():
        return None
    user = os.getenv("WEB_CONSOLE_USER")
    password = os.getenv("WEB_CONSOLE_PASSWORD")
    if user or password:
        _load_auth_credentials()
        return None
    if CONSOLE_AUTH_PATH.exists():
        _load_auth_credentials()
        if CONSOLE_PASSWORD_PATH.exists():
            if (
                CONSOLE_PASSWORD_PATH.is_symlink()
                or not CONSOLE_PASSWORD_PATH.is_file()
            ):
                raise AuthConfigurationError(
                    "stored console password path is not a regular file"
                )
            CONSOLE_PASSWORD_PATH.chmod(0o600)
        return None
    user = "admin"
    password = secrets.token_urlsafe(12)
    _write_auth_file(user, password)
    return user, password


def _ensure_secret_key() -> None:
    if CONSOLE_SECRET_PATH.exists():
        if CONSOLE_SECRET_PATH.is_symlink() or not CONSOLE_SECRET_PATH.is_file():
            raise AuthConfigurationError("console session secret is not a regular file")
        CONSOLE_SECRET_PATH.parent.chmod(0o700)
        CONSOLE_SECRET_PATH.chmod(0o600)
        try:
            secret = CONSOLE_SECRET_PATH.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AuthConfigurationError("console session secret is unreadable") from exc
        if not secret:
            raise AuthConfigurationError("console session secret is empty")
        app.secret_key = secret
        return
    secret = secrets.token_hex(32)
    _atomic_private_text(CONSOLE_SECRET_PATH, secret)
    app.secret_key = secret


_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_FAILURES_LOCK = threading.Lock()
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SEC = 300

LOGIN_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>LLM API Test Console - 登录</title>
<style>
body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
form{background:#1e293b;padding:2rem;border-radius:12px;width:300px;display:flex;flex-direction:column;gap:.8rem}
input{padding:.6rem;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0}
button{padding:.6rem;border:0;border-radius:6px;background:#3b82f6;color:#fff;cursor:pointer}
.err{color:#f87171;font-size:.85rem}
</style></head><body>
<form method="post" action="/login">
<h3>LLM API Test Console</h3>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<input name="username" placeholder="用户名" autocomplete="username" required>
<input name="password" type="password" placeholder="密码" autocomplete="current-password" required>
<button type="submit">登录</button>
</form></body></html>"""


@app.before_request
def _require_login() -> Any:
    if _auth_disabled():
        return None
    path = request.path
    try:
        creds = _load_auth_credentials()
    except AuthConfigurationError:
        if path.startswith("/api/") or path.startswith("/reports/"):
            return jsonify({"error": "authentication configuration unavailable"}), 503
        return "Authentication configuration unavailable.", 503
    if path in {"/login", "/favicon.ico"} or path.startswith("/static/"):
        return None
    if session.get("auth_user"):
        return None
    if path.startswith("/api/") or path.startswith("/reports/"):
        return jsonify({"error": "authentication required"}), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login() -> Any:
    if _auth_disabled():
        return redirect("/")
    try:
        creds = _load_auth_credentials()
    except AuthConfigurationError:
        return "Authentication configuration unavailable.", 503
    if request.method == "POST":
        client = request.remote_addr or "unknown"
        now = time.time()
        with _LOGIN_FAILURES_LOCK:
            failures = [
                t for t in _LOGIN_FAILURES.get(client, []) if now - t < LOGIN_WINDOW_SEC
            ]
            if len(failures) >= LOGIN_MAX_FAILURES:
                return render_template_string(
                    LOGIN_PAGE, error="尝试次数过多，请稍后再试"
                ), 429
        username = str(request.form.get("username") or "")
        password = str(request.form.get("password") or "")
        if hmac.compare_digest(username, creds[0]) and _verify_password(
            password, creds
        ):
            with _LOGIN_FAILURES_LOCK:
                _LOGIN_FAILURES.pop(client, None)
            session.clear()
            session["auth_user"] = username
            return redirect("/")
        with _LOGIN_FAILURES_LOCK:
            failures.append(now)
            _LOGIN_FAILURES[client] = failures
        return render_template_string(LOGIN_PAGE, error="用户名或密码错误"), 401
    return render_template_string(LOGIN_PAGE, error=None)


@app.get("/logout")
def logout() -> Any:
    session.clear()
    return redirect("/login")


@dataclass
class Job:
    id: str
    type: str
    provider: str
    provider_label: str
    model: str
    model_family: str
    workload: str
    users: int | None
    spawn_rate: int | None
    duration: str | None
    report_dir: Path
    command: list[str]
    reference_source: str | None = None
    reference_label: str | None = None
    api_form: str = ""
    route_profile: str = ""
    model_profile_id: str = ""
    param_test_runs: int = DEFAULT_PARAM_TEST_RUNS
    tool_validation_mode: str = "auto"
    cache_measured_requests: int = 50
    request_mode: str = "fixed"
    staircase_plan: dict[str, Any] | None = None
    cache_plan: dict[str, Any] | None = None
    soak_plan: dict[str, Any] | None = None
    image_plan: dict[str, Any] | None = None
    job_spec: dict[str, Any] = field(default_factory=dict)
    timeout_sec: int = 0
    target_rpm: float = DEFAULT_TARGET_RPM
    target_tpm: float = DEFAULT_TARGET_TPM
    target_tokens_per_request: float = 0.0
    context_window_tokens: int | None = None
    context_window_source: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    status: str = "queued"
    returncode: int | None = None
    pid: int | None = None
    stop_requested: bool = False
    external: bool = False
    process: subprocess.Popen[Any] | None = field(default=None, repr=False)

    @property
    def log_path(self) -> Path:
        return self.report_dir / "job.log"


def _fixed_cache_retention_suite(job: Job) -> str | None:
    if job.type != "param_test" or not isinstance(job.job_spec, dict):
        return None
    from lib.anthropic_cache_reference import SUITE_IDS
    suite = job.job_spec.get("parameter_suite")
    return suite if suite in SUITE_IDS else None


def _initialize_fixed_cache_retention(job: Job) -> None:
    suite = _fixed_cache_retention_suite(job)
    if suite:
        from lib.approved_report_retention import initialize_cache_report
        initialize_cache_report(job.report_dir, suite, reports_root=REPORTS_ROOT, on_exit=False,
                                created_at=datetime.fromtimestamp(job.created_at, timezone.utc))


def _finalize_fixed_cache_retention(job: Job) -> None:
    if _fixed_cache_retention_suite(job):
        from lib.approved_report_retention import finalize_cache_report
        finalize_cache_report(job.report_dir, reports_root=REPORTS_ROOT, include_job_log=True)


def _parameter_result_for_job(job: Job) -> dict[str, Any] | None:
    filename = "summary.json" if job.type == "image_param_test" else "verdict.json"
    result = _read_json(job.report_dir / filename)
    return result if isinstance(result, dict) else None


def _classify_functional_result(job_spec: dict[str, Any], result: Any,
                                *, job_type: str | None = None) -> dict[str, Any]:
    if (job_spec.get("schema_version") == WORKFLOW_JOB_SPEC_VERSION
            and "test_workflow_snapshot" in job_spec):
        return classify_workflow_result(job_spec, result)
    return classify_parameter_result(job_spec, result, job_type=job_type)


def _result_validation_for_job(job: Job) -> dict[str, Any] | None:
    if job.type == "cache_suite":
        return classify_cache_result(
            job.job_spec,
            _parameter_result_for_job(job),
        )
    if job.type not in {"param_test", "image_param_test"}:
        return None
    return _classify_functional_result(
        job.job_spec,
        _parameter_result_for_job(job),
        job_type=job.type,
    )


def _public_result_view(
    result: Any,
    validation: dict[str, Any] | None,
) -> Any:
    if not isinstance(result, dict) or validation is None:
        return result
    public_result = copy.deepcopy(result)
    public_result["reported_pass"] = (
        result.get("pass") if isinstance(result.get("pass"), bool) else None
    )
    public_result["pass"] = validation.get("pass") is True
    public_result["result_validation"] = copy.deepcopy(validation)
    if validation.get("legacy") is True:
        public_result["legacy_unverified"] = True
    return public_result


def _result_gated_completion(job: Job, returncode: int) -> tuple[int, str]:
    effective_returncode = int(returncode)
    validation = _result_validation_for_job(job)
    if (
        validation is not None
        and validation.get("pass") is not True
        and effective_returncode == 0
    ):
        effective_returncode = 1
    return (
        effective_returncode,
        "completed" if effective_returncode == 0 else "failed",
    )


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._last_discovery_at = 0.0
        self._discovery_config: dict[str, Any] | None = None
        if os.getenv("LLM_API_TEST_SKIP_HISTORY") == "1":
            return
        self._load_finished_jobs()
        self._discover_external_jobs()

    @staticmethod
    def _pid_alive(pid: int, marker: str = "") -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        if marker and cmdline_path.exists():
            try:
                content = cmdline_path.read_bytes().decode("utf-8", "ignore")
            except OSError:
                return True
            return marker in content
        return True

    def _get_discovery_config(self) -> dict[str, Any]:
        if self._discovery_config is None:
            self._discovery_config = load_config()
        return self._discovery_config

    def _discover_external_jobs(self) -> None:
        now = time.time()
        last = getattr(self, "_last_discovery_at", 0.0)
        if now - last < 2.0:
            return
        self._last_discovery_at = now
        if not JOBS_ROOT.exists():
            return
        with self._lock:
            for report_dir in sorted(JOBS_ROOT.iterdir()):
                if not report_dir.is_dir() or report_dir.name in self._jobs:
                    continue
                run = _read_json(report_dir / "run.json")
                if not isinstance(run, dict):
                    continue
                pid = int(run.get("pid") or 0)
                returncode = run.get("returncode")
                marker = str(run.get("pid_marker") or "")
                if returncode is None and self._pid_alive(pid, marker):
                    job_spec = _read_json(report_dir / "job_spec.json") or {}
                    provider = str(
                        run.get("provider") or job_spec.get("provider") or ""
                    )
                    try:
                        provider_label = (
                            str(
                                get_provider_config(
                                    self._get_discovery_config(), provider
                                ).get("label")
                                or provider
                            )
                            if provider
                            else ""
                        )
                    except Exception:
                        provider_label = provider
                    job = Job(
                        id=report_dir.name,
                        type=str(run.get("type") or job_spec.get("type") or ""),
                        provider=provider,
                        provider_label=provider_label,
                        model=str(run.get("model") or job_spec.get("model") or ""),
                        model_family=str(
                            run.get("model_family")
                            or job_spec.get("model_family")
                            or ""
                        ),
                        workload=str(
                            run.get("workload") or job_spec.get("workload") or ""
                        ),
                        users=None,
                        spawn_rate=None,
                        duration=None,
                        report_dir=report_dir,
                        command=[str(item) for item in run.get("command") or []],
                        api_form=str(
                            run.get("api_form") or job_spec.get("api_form") or ""
                        ),
                        route_profile=str(
                            run.get("route_profile")
                            or job_spec.get("route_profile")
                            or ""
                        ),
                        job_spec=job_spec if isinstance(job_spec, dict) else {},
                        created_at=float(
                            run.get("created_at") or report_dir.stat().st_mtime
                        ),
                        started_at=float(run.get("started_at") or time.time()),
                        status="running",
                        pid=pid,
                        external=True,
                    )
                    self._jobs[job.id] = job
                    threading.Thread(
                        target=self._watch_external, args=(job.id,), daemon=True
                    ).start()
                elif returncode is not None:
                    self._restore_external_finished(report_dir, run)
                elif not self._pid_alive(pid, marker):
                    crashed = dict(run)
                    crashed["returncode"] = -1
                    self._restore_external_finished(report_dir, crashed)

    def _restore_external_finished(self, report_dir: Path, run: dict[str, Any]) -> None:
        job_spec = _read_json(report_dir / "job_spec.json") or {}
        job_type = str(run.get("type") or job_spec.get("type") or "")
        if job_type == "image_param_test":
            self._restore_image_job(
                load_config(),
                report_dir,
                job_spec if isinstance(job_spec, dict) else {},
            )
            return
        returncode = int(run.get("returncode") or 0)
        provider = str(run.get("provider") or job_spec.get("provider") or "")
        try:
            provider_label = (
                str(
                    get_provider_config(self._get_discovery_config(), provider).get(
                        "label"
                    )
                    or provider
                )
                if provider
                else ""
            )
        except Exception:
            provider_label = provider
        finished_at = float(run.get("finished_at") or report_dir.stat().st_mtime)
        self._jobs[report_dir.name] = Job(
            id=report_dir.name,
            type=job_type,
            provider=provider,
            provider_label=provider_label,
            model=str(run.get("model") or job_spec.get("model") or ""),
            model_family=str(
                run.get("model_family") or job_spec.get("model_family") or ""
            ),
            workload=str(run.get("workload") or job_spec.get("workload") or ""),
            users=None,
            spawn_rate=None,
            duration=None,
            report_dir=report_dir,
            command=[str(item) for item in run.get("command") or []],
            api_form=str(run.get("api_form") or job_spec.get("api_form") or ""),
            route_profile=str(
                run.get("route_profile") or job_spec.get("route_profile") or ""
            ),
            job_spec=job_spec if isinstance(job_spec, dict) else {},
            created_at=float(run.get("created_at") or report_dir.stat().st_mtime),
            started_at=float(run.get("started_at") or finished_at),
            finished_at=finished_at,
            status="stopped"
            if run.get("stop_requested")
            else ("completed" if returncode == 0 else "failed"),
            returncode=returncode,
            pid=int(run.get("pid") or 0) or None,
            external=True,
        )
        restored = self._jobs[report_dir.name]
        if not run.get("stop_requested"):
            restored.returncode, restored.status = _result_gated_completion(
                restored,
                returncode,
            )

    def _monitor_external_termination(self, pid: int, marker: str, grace_seconds: float,
                                      *, signal_sent: bool = False, process_only: bool = False) -> None:
        # The process marker prevents a stale external-job PID from killing a new process.
        if not self._pid_alive(pid, marker):
            return
        if not signal_sent:
            try:
                (os.kill if process_only else os.killpg)(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                return
        deadline = time.monotonic() + grace_seconds
        while self._pid_alive(pid, marker):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    (os.kill if process_only else os.killpg)(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                return
            time.sleep(min(0.1, remaining))

    def _watch_external(self, job_id: str) -> None:
        while True:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or job.status not in {"running", "stopping"}:
                    return
                pid = job.pid or 0
                report_dir = job.report_dir
            run = _read_json(report_dir / "run.json")
            marker = str(run.get("pid_marker") or "") if isinstance(run, dict) else ""
            if not self._pid_alive(pid, marker):
                break
            if isinstance(run, dict) and run.get("returncode") is not None:
                break
            time.sleep(2)
        time.sleep(1)
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in {"running", "stopping"}:
                return
            run = _read_json(job.report_dir / "run.json") or {}
            returncode = run.get("returncode") if isinstance(run, dict) else None
            if returncode is None:
                returncode = -1
            job.finished_at = float(
                (run.get("finished_at") if isinstance(run, dict) else None)
                or time.time()
            )
            if job.stop_requested:
                job.returncode = int(returncode)
                job.status = "stopped"
            else:
                job.returncode, job.status = _result_gated_completion(
                    job,
                    int(returncode),
                )
            _ensure_load_result(job)

    def create(self, payload: dict[str, Any]) -> Job:
        config = load_config()
        workflow_requested = bool(payload.get("workflow_id") or payload.get("workflow_binding_id"))
        job_type = str(payload.get("type") or ("param_test" if workflow_requested else "quick_load"))
        if job_type not in SUPPORTED_JOB_TYPES:
            raise ValueError(f"Unsupported job type: {job_type}")
        if workflow_requested and job_type not in {"param_test", "image_param_test"}:
            raise ValueError("Workflow execution is only available for functional parameter/image jobs")
        if workflow_requested:
            return self._create_workflow_job(config, {**payload, "type": job_type})
        if job_type == "param_test":
            preview = _maybe_preview_workflow(config, {**payload, "type": job_type})
            if preview is not None:
                return self._create_workflow_job(config, {**payload, "type": job_type}, preview=preview)
        if job_type == "image_param_test":
            return self._create_image_job(config, payload)
        provider = str(payload.get("provider") or get_active_provider_name(config))
        provider_cfg = get_provider_config(config, provider)
        model = str(payload.get("model") or get_selected_model(config, provider))
        family = get_model_family(config, model, provider)
        route_profile = get_model_route_profile(
            config,
            model,
            provider,
            route_profile=str(payload.get("route_profile") or "") or None,
        )
        api_form = get_model_api_form(
            config,
            model,
            provider,
            route_profile=route_profile,
            api_form=str(payload.get("api_form") or "") or None,
        )
        workload_default = (
            "cache_suite" if job_type == "cache_suite" else DEFAULT_WORKLOAD
        )
        workload = str(payload.get("workload") or workload_default)
        validate_workload(config, job_type, workload)
        request_mode = resolve_request_mode(payload, job_type)
        if workload == "throughput_rpm" and "request_mode" not in payload:
            request_mode = "fixed"
        users = _optional_int(payload.get("users"))
        spawn_rate = _optional_int(payload.get("spawn_rate"))
        duration = str(payload.get("duration") or DEFAULT_QUICK_DURATION)
        requested_contract_id, _legacy_contract_alias_used = (
            _select_interface_contract_id(
                payload.get("reference_contract_id"),
                payload.get("reference_source"),
            )
        )
        if job_type == "cache_suite":
            binding = resolve_runtime_profile_binding(
                config,
                provider,
                model,
                family,
                route_profile,
                api_form,
                modality="text",
            )
            require_official_reference_binding(binding)
            policy = resolve_runtime_test_policy(binding)
            if policy.get("pressure_test_enabled") is not True:
                raise ValueError(
                    f"Pressure testing is disabled for {family}/{model}: "
                    f"{policy.get('disabled_reason') or 'MPDB model test policy'}."
                )
            default_contract_id = str(
                policy.get("default_reference_contract_id") or ""
            )
            if requested_contract_id and requested_contract_id != default_contract_id:
                raise ValueError(
                    "Cache jobs use the selected MPDB Interface's default Contract; "
                    f"requested={requested_contract_id!r}, "
                    f"default={default_contract_id!r}."
                )
            model_profile_database = database_snapshot(
                binding,
                include_parameter_binding=False,
            )
            contract_id = str(
                model_profile_database["reference_contract_id"]
            )
            parameter_config = {
                "source_id": binding["source_id"],
                "profile_id": binding["profile_id"],
                "interface_id": binding["interface_id"],
                "test_binding_id": policy["test_binding_id"],
                "contract_id": contract_id,
                "model_profile_database": model_profile_database,
            }
        else:
            parameter_config = resolve_runtime_parameter_config(
                config,
                provider,
                model,
                family,
                route_profile,
                api_form,
                modality="text",
                contract_id=requested_contract_id or None,
            )
        model_profile_database = copy.deepcopy(
            parameter_config["model_profile_database"]
        )
        reference_source = str(parameter_config["contract_id"])
        catalog_model = str(
            model_profile_database.get("model_slug")
            or ((model_profile_database.get("profile") or {}).get("model_slug"))
            or model
        )
        capability = capability_profile_from_database_snapshot(model_profile_database)
        capability["model_profile_database"] = copy.deepcopy(
            model_profile_database
        )
        if (
            capability.get("known_model") is not True
            or capability.get("known_api_profile") is not True
            or capability.get("route_profile_known") is not True
        ):
            raise ValueError(
                f"Missing registered text model/API/route profile for "
                f"{family}/{api_form}/{model}/{route_profile}."
            )
        if (
            job_type == "param_test"
            and capability.get("parameter_test_enabled") is not True
        ):
            raise ValueError(
                f"Text parameter testing is disabled for {family}/{model}: "
                f"{capability.get('disabled_reason') or 'model profile policy'}."
            )
        if (
            job_type in {"quick_load", "staircase", "soak", "cache_suite"}
            and not pressure_test_runnable(capability)
        ):
            raise ValueError(
                f"Pressure testing is disabled for {family}/{model}: "
                f"{capability.get('disabled_reason') or 'model profile policy'}."
            )
        reference = get_reference_source(reference_source)
        parameter_suite = payload.get("parameter_suite")
        if parameter_suite is not None and not isinstance(parameter_suite, str):
            raise ValueError("parameter_suite must be a registered suite ID string")
        parameter_suite = parameter_suite or None
        param_test_runs = min(
            max(
                _optional_int(payload.get("param_test_runs"))
                or DEFAULT_PARAM_TEST_RUNS,
                1,
            ),
            MAX_PARAM_TEST_RUNS,
        )
        if api_form == "deepseek_beta_chat_prefix":
            from lib.deepseek_beta_reference import build_deepseek_beta_reference_plan
            if provider != "deepseek_official":
                raise ValueError("The fixed beta suite requires deepseek_official")
            raw_runs = payload.get("param_test_runs", 1)
            if type(raw_runs) is not int or raw_runs != 1:
                raise ValueError("DeepSeek beta uses exactly five fixed cases with one run")
            route = get_provider_interface(config, "deepseek-beta-chat-prefix", provider)
            endpoint = str(route.get("base_url") or provider_cfg["base_url"]).rstrip("/") + str(route["path"])
            build_deepseek_beta_reference_plan(model_profile_database, endpoint=endpoint,
                                               runs=raw_runs, job_type=job_type)
            param_test_runs = 1
        if parameter_suite:
            from lib.fixed_parameter_specs import build_fixed_plan, fixed_suite_descriptor
            descriptor = fixed_suite_descriptor(parameter_suite)
            route = get_provider_interface(config, descriptor["transport"], provider)
            endpoint = str(route.get("base_url") or provider_cfg["base_url"]).rstrip("/") + str(route["path"])
            raw_runs = payload.get("param_test_runs", 1)
            build_fixed_plan(model_profile_database, suite_id=parameter_suite, endpoint=endpoint,
                           runs=raw_runs, job_type=job_type)
            param_test_runs = 1
        tool_validation_mode = str(payload.get("tool_validation_mode") or "auto")
        if tool_validation_mode not in TOOL_VALIDATION_MODES:
            raise ValueError(
                "tool_validation_mode must be auto, openai_compat, gemini_native, or claude_native"
            )
        staircase_plan = (
            resolve_staircase_plan(config, payload, provider, model)
            if job_type == "staircase"
            else None
        )
        cache_plan = (
            resolve_cache_plan(config, payload, provider, model)
            if job_type == "cache_suite"
            else None
        )
        if job_type == "cache_suite":
            _require_cache_plan_tool_capability(
                cache_plan,
                policy,
                family,
                get_model_transport(
                    config,
                    model,
                    provider,
                    route_profile=route_profile,
                    api_form=api_form,
                ),
            )
        soak_plan = (
            resolve_soak_plan(config, payload, provider, model)
            if job_type == "soak"
            else None
        )
        cache_measured_requests = int(
            (cache_plan or {}).get("estimated_request_count")
            or _resolve_cache_measured_requests(
                config, payload.get("cache_measured_requests")
            )
        )
        timeout_sec = _resolve_timeout_sec(config, payload.get("timeout_sec"))
        target_rpm = (
            _resolve_target_rpm(config, payload.get("target_rpm"))
            if job_type in {"quick_load", "staircase"}
            else DEFAULT_TARGET_RPM
        )
        target_tpm = (
            _resolve_target_tpm(config, payload.get("target_tpm"))
            if job_type in {"quick_load", "staircase"}
            else DEFAULT_TARGET_TPM
        )
        target_tokens_per_request = (
            target_tpm / target_rpm if target_rpm > 0 and target_tpm > 0 else 0.0
        )
        context_window_tokens, context_window_source = resolve_context_window(
            config, provider_cfg, model
        )

        parameter_preview = None
        if job_type == "param_test":
            parameter_preview = _preview_workflow(config, {
                **payload, "type": "param_test", "provider": provider, "model": model,
                "route_profile": route_profile, "api_form": api_form,
                "reference_contract_id": reference_source, "timeout_sec": timeout_sec,
                "param_test_runs": param_test_runs, "tool_validation_mode": tool_validation_mode,
            })
            timeout_sec = parameter_preview["job_spec"]["timeout_sec"]

        if not provider_has_api_key(config, provider):
            env_name = provider_cfg.get("api_key_env") or "api_key"
            raise ValueError(
                f"Missing API key for provider {provider!r}. Configure {env_name}."
            )
        if (
            target_tokens_per_request > 0
            and job_type in {"quick_load", "staircase"}
            and (
                not workload.startswith("throughput")
                or workload == "throughput_streaming"
            )
        ):
            raise ValueError(
                "Adaptive RPM+TPM sizing is unavailable for this workload; "
                "throughput_streaming keeps request lengths fixed, so clear one target."
            )
        _validate_model(provider_cfg, model)
        _preflight_job(
            config,
            provider,
            model,
            job_type,
            workload,
            reference_source,
            api_form,
            route_profile,
            model_profile_database=model_profile_database,
        )
        if job_type == "quick_load":
            users = users or DEFAULT_QUICK_USERS
            spawn_rate = spawn_rate or DEFAULT_QUICK_SPAWN_RATE
            parse_duration_seconds(duration)
        elif job_type == "soak":
            users = int((soak_plan or {}).get("users") or 0)
            spawn_rate = int((soak_plan or {}).get("spawn_rate") or 0)
            duration = str((soak_plan or {}).get("duration") or "1h")
        else:
            users = users if users is not None else None
            spawn_rate = spawn_rate if spawn_rate is not None else None

        with self._lock:
            running = [
                job
                for job in self._jobs.values()
                if self._refresh_locked(job).status in {"queued", "running", "stopping"}
            ]
            if running:
                raise ValueError(
                    f"Job {running[0].id} is still {running[0].status}; stop or wait before starting another job."
                )

            job_id = _new_job_id(job_type, provider, model)
            report_dir = ensure_dir(JOBS_ROOT / job_id)
            command = _command_for_job(
                job_type,
                report_dir,
                users,
                spawn_rate,
                duration,
                target_rpm,
                timeout_sec,
            )
            capability_snapshot = capability_profile_snapshot(
                "text",
                family,
                model,
                list(reference.get("test_profiles") or []),
                reference_source=reference_source,
                api_form=api_form,
                route_profile=route_profile,
                provider_override=get_model_api_forms(
                    config,
                    model,
                    provider,
                    route_profile=route_profile,
                )[api_form],
                model_profile_database=model_profile_database,
            )
            capability_snapshot["model_profile_database"] = copy.deepcopy(
                model_profile_database
            )
            job_spec = copy.deepcopy(parameter_preview["job_spec"]) if parameter_preview is not None else make_job_spec(
                job_type=job_type,
                provider=provider,
                model=model,
                model_family=family,
                api_form=api_form,
                route_profile=route_profile,
                model_profile_id=str(capability.get("model_api_profile_id") or ""),
                source_id=str(parameter_config["source_id"]),
                profile_id=str(parameter_config["profile_id"]),
                interface_id=str(parameter_config["interface_id"]),
                test_binding_id=str(parameter_config["test_binding_id"]),
                reference_contract_id=reference_source,
                model_profile_database=model_profile_database,
                transport=str(capability.get("transport") or ""),
                workload=workload,
                request_mode=request_mode,
                target_rpm=target_rpm,
                target_tpm=target_tpm,
                staircase_plan=staircase_plan,
                cache_plan=cache_plan,
                soak_plan=soak_plan,
                reference_route_profile=str(reference.get("route_profile") or ""),
                model_capability_profile=capability_snapshot,
                param_test_runs=param_test_runs,
                tool_validation_mode=tool_validation_mode,
                result_contract=(
                    build_result_validation_contract(config)
                    if job_type == "param_test"
                    else None
                ),
                parameter_suite=parameter_suite,
            )
            if parameter_preview is not None:
                job_spec = copy.deepcopy(parameter_preview["job_spec"])
                workload = job_spec["workload"]
                request_mode = job_spec["request_mode"]
            write_json(report_dir / "job_spec.json", job_spec)
            job = Job(
                id=job_id,
                type=job_type,
                provider=provider,
                provider_label=str(provider_cfg.get("label") or provider),
                model=model,
                model_family=family,
                workload=workload,
                users=users,
                spawn_rate=spawn_rate,
                duration=duration,
                report_dir=report_dir,
                command=command,
                reference_source=reference_source,
                reference_label=str(reference.get("label") or reference_source),
                api_form=api_form,
                route_profile=route_profile,
                model_profile_id=str(capability.get("model_api_profile_id") or ""),
                param_test_runs=param_test_runs,
                tool_validation_mode=tool_validation_mode,
                cache_measured_requests=cache_measured_requests,
                request_mode=request_mode,
                staircase_plan=staircase_plan,
                cache_plan=cache_plan,
                soak_plan=soak_plan,
                job_spec=job_spec,
                timeout_sec=timeout_sec,
                target_rpm=target_rpm,
                target_tpm=target_tpm,
                target_tokens_per_request=target_tokens_per_request,
                context_window_tokens=context_window_tokens,
                context_window_source=context_window_source,
            )
            self._jobs[job.id] = job
            self._start_locked(job)
            return job

    def _create_workflow_job(self, config: dict[str, Any], payload: dict[str, Any],
                             *, preview: dict[str, Any] | None = None) -> Job:
        """Use the exact reviewed workflow permission and its independent snapshot."""
        if preview is None:
            preview = _preview_workflow(config, payload)
        spec = copy.deepcopy(preview.get("job_spec"))
        if (not isinstance(spec, dict) or spec.get("schema_version") != WORKFLOW_JOB_SPEC_VERSION
                or "test_workflow_snapshot" not in spec):
            raise ValueError("Workflow preview did not produce a complete immutable JobSpec")
        from lib.test_runner.snapshot import validate_workflow_snapshot
        controls = parameter_execution_from_job(spec)
        validate_workflow_snapshot(spec["test_workflow_snapshot"], job=spec)
        if spec.get("type") != payload["type"]:
            raise ValueError("Workflow preview conflicts with the requested job type")
        expected_type = {"text": "param_test", "image": "image_param_test"}.get(spec.get("modality"))
        if spec["type"] != expected_type:
            raise ValueError("Workflow modality conflicts with the requested job type")
        provider = spec["provider"]
        model = spec["model"]
        provider_cfg = get_provider_config(config, provider)
        has_key = image_provider_has_api_key if spec["type"] == "image_param_test" else provider_has_api_key
        if not has_key(config, provider):
            env_name = provider_cfg.get("api_key_env") or "api_key"
            raise ValueError(f"Missing API key for provider {provider!r}. Configure {env_name}.")
        with self._lock:
            running = [job for job in self._jobs.values()
                       if self._refresh_locked(job).status in {"queued", "running", "stopping"}]
            if running:
                raise ValueError(f"Job {running[0].id} is still {running[0].status}; stop or wait before starting another job.")
            job_id = _new_job_id(spec["type"], provider, model)
            report_dir = ensure_dir(JOBS_ROOT / job_id)
            write_json(report_dir / "job_spec.json", spec)
            identities = {name: str(spec.get(name) or "")
                          for name in ("source_id", "profile_id", "interface_id", "test_binding_id")
                          if name in Job.__dataclass_fields__}
            job = Job(
                id=job_id, type=spec["type"], provider=provider,
                provider_label=str(provider_cfg.get("label") or provider), model=model,
                model_family=str(spec.get("model_family") or ""), workload="functional_workflow",
                users=None, spawn_rate=None, duration=None, report_dir=report_dir,
                command=[sys.executable, "scripts/workflow_test.py", "--job-spec", str(report_dir / "job_spec.json")],
                reference_source=spec.get("reference_contract_id"),
                reference_label=spec.get("reference_contract_id"),
                api_form=str(spec.get("api_form") or ""), route_profile=str(spec.get("route_profile") or ""),
                param_test_runs=controls["runs"], tool_validation_mode=controls["tool_validation_mode"],
                cache_measured_requests=0, request_mode="fixed", job_spec=spec,
                timeout_sec=spec["execution_plan"]["limits"]["request_timeout_seconds"],
                **identities,
            )
            self._jobs[job.id] = job
            self._start_locked(job)
            return job

    def _create_image_job(
        self,
        config: dict[str, Any],
        payload: dict[str, Any],
    ) -> Job:
        configured = list_image_providers(config)
        default_provider = configured[0]["name"] if configured else ""
        provider = str(payload.get("provider") or default_provider)
        if not provider:
            raise ValueError("No image provider is configured.")
        provider_cfg = get_provider_config(config, provider)
        image_cfg = get_image_provider_config(config, provider)
        model = str(payload.get("model") or image_cfg.get("default") or "")
        model_cfg = get_image_model_config(config, provider, model)
        timeout_sec = _resolve_image_timeout_sec(config, payload.get("timeout_sec"))
        image_plan = resolve_image_plan(
            config,
            payload,
            provider,
            model,
            timeout_sec,
        )
        image_family = str(model_cfg.get("family") or "")
        image_route = str(image_plan.get("route_profile") or "")
        image_form = str(image_plan.get("api_form") or "")
        parameter_config = resolve_runtime_parameter_config(
            config,
            provider,
            model,
            image_family,
            image_route,
            image_form,
            modality="image",
        )
        model_profile_database = copy.deepcopy(
            parameter_config["model_profile_database"]
        )
        image_plan["model_profile_database"] = copy.deepcopy(
            model_profile_database
        )
        image_capability = image_plan.get("model_capability_profile")
        if isinstance(image_capability, dict):
            image_capability["model_profile_database"] = copy.deepcopy(
                model_profile_database
            )
        preview = _preview_workflow(config, {**payload, "type": "image_param_test", "provider": provider,
                                              "model": model, "timeout_sec": timeout_sec})
        frozen_spec = copy.deepcopy(preview["job_spec"])
        controls = parameter_execution_from_job(frozen_spec)
        restored = resolve_job_model_profile_snapshot(frozen_spec)
        if restored.get("resolution_status") != "snapshot":
            raise ValueError("Invalid frozen image model profile snapshot: " + str(restored.get("error") or restored.get("resolution_status")))
        image_plan = copy.deepcopy(preview["image_plan"])
        if (frozen_spec.get("schema_version") != WORKFLOW_JOB_SPEC_VERSION
                or frozen_spec.get("type") != "image_param_test"
                or frozen_spec.get("provider") != provider or frozen_spec.get("model") != model):
            raise ValueError("Image preview conflicts with the immutable job target")
        if not image_provider_has_api_key(config, provider):
            env_name = provider_cfg.get("api_key_env") or "api_key"
            raise ValueError(
                f"Missing API key for image provider {provider!r}. Configure {env_name}."
            )

        with self._lock:
            running = [
                job
                for job in self._jobs.values()
                if self._refresh_locked(job).status in {"queued", "running", "stopping"}
            ]
            if running:
                raise ValueError(
                    f"Job {running[0].id} is still {running[0].status}; "
                    "stop or wait before starting another job."
                )

            job_id = _new_job_id("image_param_test", provider, model)
            report_dir = ensure_dir(JOBS_ROOT / job_id)
            command = _image_command_for_job(report_dir, image_plan)
            job_spec = frozen_spec
            write_json(report_dir / "job_spec.json", job_spec)
            job = Job(
                id=job_id,
                type="image_param_test",
                provider=provider,
                provider_label=str(provider_cfg.get("label") or provider),
                model=model,
                model_family=str(model_cfg.get("family") or ""),
                workload="image_param",
                users=None,
                spawn_rate=None,
                duration=None,
                report_dir=report_dir,
                command=command,
                reference_source=None,
                reference_label=None,
                api_form=str(image_plan.get("api_form") or ""),
                route_profile=str(image_plan.get("route_profile") or ""),
                model_profile_id=str(
                    (image_plan.get("model_capability_profile") or {}).get(
                        "model_api_profile_id"
                    )
                    or ""
                ),
                param_test_runs=controls["runs"],
                tool_validation_mode=controls["tool_validation_mode"],
                cache_measured_requests=0,
                request_mode="fixed",
                image_plan=image_plan,
                job_spec=job_spec,
                timeout_sec=timeout_sec,
                context_window_tokens=None,
                context_window_source=None,
            )
            self._jobs[job.id] = job
            self._start_locked(job)
            return job

    def list(self) -> list[dict[str, Any]]:
        self._discover_external_jobs()
        with self._lock:
            jobs = [self._refresh_locked(job) for job in self._jobs.values()]
        return [
            self.public(job, include_detail=False)
            for job in sorted(jobs, key=lambda item: item.created_at, reverse=True)
        ]

    def current_refs(self) -> dict[str, dict[str, Any] | None]:
        self._discover_external_jobs()
        with self._lock:
            jobs = sorted(
                (self._refresh_locked(job) for job in self._jobs.values()),
                key=lambda item: item.created_at,
                reverse=True,
            )
            active = next(
                (
                    job
                    for job in jobs
                    if job.status in {"queued", "running", "stopping"}
                ),
                None,
            )

        def ref(job: Job | None) -> dict[str, Any] | None:
            if job is None:
                return None
            return {
                "id": job.id,
                "type": job.type,
                "status": job.status,
                "created_at": job.created_at,
            }

        return {"active": ref(active), "newest": ref(jobs[0] if jobs else None)}

    def get(self, job_id: str) -> dict[str, Any]:
        self._discover_external_jobs()
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            job = self._refresh_locked(self._jobs[job_id])
        return self.public(job, include_detail=True)

    def latest_param_result(
        self,
        provider: str,
        model: str,
        route_profile: str,
        api_form: str,
        model_profile_id: str,
        reference_source: str,
        tool_validation_mode: str = "auto",
        parameter_suite: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            try:
                current_profiles = set(test_profiles_for_reference(reference_source))
            except KeyError:
                current_profiles = set()
            if parameter_suite is not None:
                from lib.fixed_parameter_specs import fixed_suite_descriptor
                selected = fixed_suite_descriptor(parameter_suite)
                if (provider != selected["provider"] or model != selected["model"]
                        or route_profile != "vendor_direct" or api_form != selected["api_form"] or reference_source != selected["contract_id"]):
                    raise ValueError("Fixed suite history requires its exact source/model/form/contract")
                current_profiles = set(selected["case_ids"])
            candidates = [
                self._refresh_locked(job)
                for job in self._jobs.values()
                if job.type == "param_test"
                and job.provider == provider
                and job.model == model
                and job.route_profile == route_profile
                and job.api_form == api_form
                and job.model_profile_id == model_profile_id
                and job.reference_source == reference_source
                and job.tool_validation_mode == tool_validation_mode
                and job.job_spec.get("parameter_suite") == parameter_suite
                and (
                    _job_param_profiles(job) == current_profiles
                    or parameter_suite is None and (not current_profiles or not _job_param_profiles(job))
                )
            ]
            finished = [
                job for job in candidates if job.status in {"completed", "failed"}
            ]
            latest = max(
                finished,
                key=lambda job: job.finished_at or job.created_at,
                default=None,
            )
        return self.public(latest, include_detail=True) if latest else None

    def latest_image_result(
        self,
        provider: str,
        model: str,
        route_profile: str,
        api_form: str,
        model_profile_id: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            candidates = [
                self._refresh_locked(job)
                for job in self._jobs.values()
                if job.type == "image_param_test"
                and job.provider == provider
                and job.model == model
                and job.route_profile == route_profile
                and job.api_form == api_form
                and job.model_profile_id == model_profile_id
                and job.status in {"completed", "failed"}
            ]
            latest = max(
                candidates,
                key=lambda job: job.finished_at or job.created_at,
                default=None,
            )
        return self.public(latest, include_detail=True) if latest else None

    def stop(self, job_id: str) -> dict[str, Any]:
        self._discover_external_jobs()
        termination = None
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            job = self._refresh_locked(self._jobs[job_id])
            if job.stop_requested:
                return self.public(job, include_detail=True)
            if job.process is not None and job.process.poll() is None:
                grace = _termination_grace_for_job(job)
                job.stop_requested = True
                job.status = "stopping"
                _signal_process_group(job.process)
                # Take the deadline after signalling, so a completed request cannot
                # race this read and start fresh business work with a shorter grace.
                try:
                    grace = _termination_grace_for_job(job)
                except (RuntimeError, ValueError):
                    pass  # Retain the previously validated conservative deadline.
                termination = threading.Thread(
                    target=_terminate_process_group, args=(job.process,),
                    kwargs={"grace_seconds": grace, "signal_sent": True}, daemon=True,
                )
            elif job.external and job.status == "running" and job.pid:
                grace = _termination_grace_for_job(job)
                job.stop_requested = True
                job.status = "stopping"
                if (_read_json(job.report_dir / "job_spec.json") or {}).get("schema_version") == WORKFLOW_JOB_SPEC_VERSION:
                    run = _read_json(job.report_dir / "run.json") or {}
                    marker = str(run.get("pid_marker") or "")
                    process_only = run.get("signal_scope") == "process"
                    write_json(job.report_dir / "run.json", {**run, "stop_requested": True})
                    if self._pid_alive(job.pid, marker):
                        try:
                            (os.kill if process_only else os.killpg)(job.pid, signal.SIGTERM)
                        except (ProcessLookupError, PermissionError, OSError):
                            pass
                    try:
                        grace = _termination_grace_for_job(job)
                    except (RuntimeError, ValueError):
                        pass
                    termination = threading.Thread(
                        target=self._monitor_external_termination,
                        args=(job.pid, marker, grace), kwargs={"signal_sent": True, "process_only": process_only}, daemon=True,
                    )
                else:
                    try:
                        os.killpg(job.pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
            if termination is not None:
                termination.start()
            response = self.public(job, include_detail=True)
        return response

    def public(self, job: Job, include_detail: bool = True) -> dict[str, Any]:
        result_validation = _result_validation_for_job(job)
        payload = {
            "id": job.id,
            "type": job.type,
            "status": job.status,
            "returncode": job.returncode,
            "pid": job.pid,
            "provider": job.provider,
            "provider_label": job.provider_label,
            "model": job.model,
            "model_family": job.model_family,
            "api_form": job.api_form,
            "route_profile": job.route_profile,
            "model_profile_id": job.model_profile_id,
            "workload": job.workload,
            "users": job.users,
            "spawn_rate": job.spawn_rate,
            "duration": job.duration,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "report_dir": str(job.report_dir),
            "command": job.command,
            "reference_contract_id": job.reference_source,
            "reference_source": job.reference_source,
            "deprecated_aliases": _interface_contract_deprecated_aliases(
                {"reference_source": "reference_contract_id"}
            ),
            "reference_label": job.reference_label,
            "param_test_runs": job.param_test_runs,
            "parameter_suite": job.job_spec.get("parameter_suite"),
            "tool_validation_mode": job.tool_validation_mode,
            "cache_measured_requests": job.cache_measured_requests,
            "request_mode": job.request_mode,
            "effective_staircase_plan": job.staircase_plan,
            "effective_cache_plan": job.cache_plan,
            "effective_soak_plan": job.soak_plan,
            "effective_image_plan": job.image_plan,
            "job_spec": job.job_spec or None,
            "timeout_sec": job.timeout_sec,
            "target_rpm": job.target_rpm or None,
            "target_tpm": job.target_tpm or None,
            "target_tokens_per_request": job.target_tokens_per_request or None,
            "context_window_tokens": job.context_window_tokens,
            "context_window_source": job.context_window_source,
            "report_files": _report_files(job.report_dir),
        }
        if result_validation is not None:
            payload["result_validation"] = result_validation
            if (
                job.status in {"completed", "failed"}
                and result_validation.get("pass") is not True
            ):
                payload["status"] = "failed"
                if payload["returncode"] in (None, 0):
                    payload["returncode"] = 1
        if include_detail:
            records = _load_result_records(job.report_dir)
            summary = _job_summary(job, records)
            time_series = _job_time_series(job, records)
            load_result = _ensure_load_result(job)
            verdict_raw = _read_json(job.report_dir / "verdict.json")
            verdict = _public_result_view(
                verdict_raw,
                (
                    result_validation
                    if job.type in {"param_test", "cache_suite"}
                    else None
                ),
            )
            param_results = _read_json(job.report_dir / "param_results.json")
            param_failed_cases = _read_json(job.report_dir / "param_failed_cases.json")
            param_failed_cases_log = _read_text(
                job.report_dir / "param_failed_cases.log"
            )
            cache_progress = _read_json(job.report_dir / "cache_progress.json")
            cache_result = _read_json(job.report_dir / "cache_results.json") or {}
            payload.update(
                {
                    "summary": summary,
                    "time_series": time_series,
                    "load_result": load_result,
                    "verdict": verdict,
                    "param_results": param_results,
                    "param_failed_cases": param_failed_cases,
                    "param_failed_cases_log": param_failed_cases_log,
                    "cache_progress": cache_progress,
                    "cache_result_schema_version": cache_result.get("schema_version"),
                    "cache_result_scenario": cache_result.get("scenario"),
                    "cache_actual_request_count": cache_result.get(
                        "actual_request_count"
                    ),
                    "cache_session_outcomes": cache_result.get("session_outcomes")
                    or [],
                    "progress": _job_progress(
                        job, summary, verdict, param_results, cache_progress
                    ),
                    "log_tail": _tail(job.log_path),
                }
            )
            if job.type == "image_param_test":
                image_plan = _read_json(job.report_dir / "plan.json")
                image_results_raw = _read_json(job.report_dir / "case_results.json")
                image_results = _public_image_results(job, image_results_raw)
                image_summary_raw = _read_json(job.report_dir / "summary.json")
                image_summary = _public_result_view(
                    image_summary_raw,
                    result_validation,
                )
                model_check = _read_json(job.report_dir / "model_check.json")
                payload.update(
                    {
                        "image_plan": image_plan,
                        "image_results": image_results,
                        "image_summary": image_summary,
                        "image_model_check": model_check,
                        "progress": _image_job_progress(
                            job,
                            image_plan,
                            image_results,
                            image_summary,
                        ),
                    }
                )
        if include_detail and job.type == "image_param_test":
            workflow_progress = _workflow_job_progress(job)
            if workflow_progress is not None:
                payload["progress"] = workflow_progress
        return payload

    def _load_finished_jobs(self) -> None:
        if not JOBS_ROOT.exists():
            return
        config = load_config()
        for report_dir in sorted(
            (path for path in JOBS_ROOT.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
        ):
            try:
                self._load_finished_job(config, report_dir)
            except Exception:
                continue

    def _load_finished_job(self, config: dict[str, Any], report_dir: Path) -> None:
        if (report_dir / "run.json").exists():
            return
        job_type = _job_type_from_report_dir(report_dir)
        if not job_type:
            return
        job_spec = _read_json(report_dir / "job_spec.json") or {}
        if job_type == "image_param_test":
            self._restore_image_job(config, report_dir, job_spec)
            return
        controls = historical_parameter_execution(job_spec)
        verdict = _read_json(report_dir / "verdict.json") or {}
        load_result = _read_json(report_dir / "load_result.json") or {}
        first_record = (
            _first_request_record(report_dir)
            if not verdict and not load_result
            else None
        )
        record_extra = first_record.extra if first_record else {}
        profile_history = resolve_job_model_profile_snapshot(job_spec)
        if (
            int(job_spec.get("schema_version") or 0) >= 4
            and job_type != "cache_suite"
            and profile_history.get("resolution_status") != "snapshot"
        ):
            raise ValueError(
                "Cannot restore schema-v4 job without a valid immutable MPDB snapshot."
            )
        profile_snapshot = profile_history.get("profile")
        if not isinstance(profile_snapshot, dict):
            profile_snapshot = {}
        interface_snapshot = profile_history.get("interface")
        if not isinstance(interface_snapshot, dict):
            interface_snapshot = {}
        execution_snapshot = profile_history.get("execution_target")
        if not isinstance(execution_snapshot, dict):
            execution_snapshot = {}
        provider = str(
            execution_snapshot.get("provider_id")
            or job_spec.get("provider")
            or verdict.get("provider")
            or load_result.get("provider")
            or record_extra.get("provider")
            or "legacy-unknown"
        )
        try:
            provider_cfg = get_provider_config(config, provider)
        except KeyError:
            provider_cfg = {"label": provider}
        model = str(
            execution_snapshot.get("request_model_id")
            or job_spec.get("model")
            or verdict.get("model")
            or load_result.get("model")
            or record_extra.get("requested_model")
            or profile_snapshot.get("model_slug")
            or "legacy-unknown"
        )
        family = str(
            profile_history.get("suite_family_id")
            or job_spec.get("model_family")
            or verdict.get("model_family")
            or load_result.get("model_family")
            or record_extra.get("model_family")
            or profile_snapshot.get("family_id")
            or "legacy-unknown"
        )
        route_profile = str(
            execution_snapshot.get("route_profile")
            or job_spec.get("route_profile")
            or verdict.get("route_profile")
            or load_result.get("route_profile")
            or record_extra.get("route_profile")
            or interface_snapshot.get("routing_mode")
            or ""
        )
        api_form = str(
            execution_snapshot.get("api_form")
            or job_spec.get("api_form")
            or verdict.get("api_form")
            or load_result.get("api_form")
            or record_extra.get("api_form")
            or interface_snapshot.get("api_form")
            or ""
        )
        reference_source = str(
            profile_history.get("reference_contract_id")
            or job_spec.get("reference_contract_id")
            or job_spec.get("reference_source")
            or verdict.get("reference_source")
            or "legacy-unresolved"
        )
        try:
            reference = get_reference_source(reference_source)
        except KeyError:
            reference = {"label": reference_source}
        if job_type in {"param_test", "cache_suite"}:
            result_validation = (
                _classify_functional_result(
                    job_spec,
                    verdict,
                    job_type=job_type,
                )
                if job_type == "param_test"
                else classify_cache_result(job_spec, verdict)
            )
            returncode = 0 if result_validation.get("pass") is True else 1
        elif verdict:
            returncode = 0 if verdict.get("pass") is True else 1
        elif load_result:
            returncode = int(load_result.get("returncode") or 0)
        else:
            log_tail = _tail(report_dir / "job.log", max_chars=20000)
            returncode = (
                1
                if "Traceback (most recent call last):" in log_tail
                or "Shutting down (exit code 1)" in log_tail
                else 0
            )
        if not verdict and not any(report_dir.iterdir()):
            return
        job = Job(
            id=report_dir.name,
            type=job_type,
            provider=provider,
            provider_label=str(
                verdict.get("provider_label")
                or load_result.get("provider_label")
                or record_extra.get("provider_label")
                or provider_cfg.get("label")
                or provider
            ),
            model=model,
            model_family=family,
            workload=str(
                verdict.get("workload")
                or load_result.get("workload")
                or record_extra.get("workload")
                or DEFAULT_WORKLOAD
            ),
            users=_optional_int(record_extra.get("configured_users")),
            spawn_rate=None,
            duration=None,
            report_dir=report_dir,
            command=[],
            reference_source=reference_source,
            reference_label=str(
                verdict.get("reference_label")
                or reference.get("label")
                or reference_source
            ),
            api_form=api_form,
            route_profile=route_profile,
            model_profile_id=str(
                job_spec.get("model_profile_id")
                or (job_spec.get("model_capability_profile") or {}).get(
                    "model_api_profile_id"
                )
                or ""
            ),
            param_test_runs=int(
                controls.get("runs") or verdict.get("param_test_runs") or DEFAULT_PARAM_TEST_RUNS
            ),
            tool_validation_mode=str(controls.get("tool_validation_mode") or verdict.get("tool_validation_mode") or "auto"),
            cache_measured_requests=_historical_cache_measured_requests(
                report_dir,
                config,
                verdict=verdict,
                job_spec=job_spec,
            ),
            request_mode=str(job_spec.get("request_mode") or "fixed"),
            staircase_plan=job_spec.get("staircase_plan"),
            cache_plan=job_spec.get("cache_plan"),
            soak_plan=job_spec.get("soak_plan"),
            job_spec=job_spec,
            created_at=report_dir.stat().st_mtime,
            started_at=None,
            finished_at=report_dir.stat().st_mtime,
            status=(
                ("completed" if returncode == 0 else "failed")
                if job_type in {"param_test", "cache_suite"}
                else str(
                    load_result.get("status")
                    or ("completed" if returncode == 0 else "failed")
                )
            ),
            returncode=returncode,
        )
        self._jobs[job.id] = job

    def _restore_image_job(
        self,
        config: dict[str, Any],
        report_dir: Path,
        job_spec: dict[str, Any],
    ) -> None:
        plan_raw = _read_json(report_dir / "plan.json")
        plan = plan_raw if isinstance(plan_raw, dict) else {}
        summary = _read_json(report_dir / "summary.json")
        provider = str(
            job_spec.get("provider")
            or plan.get("provider")
            or "legacy-unknown"
        )
        try:
            provider_cfg = get_provider_config(config, provider)
        except KeyError:
            provider_cfg = {"label": provider}
        image_plan = job_spec.get("image_plan")
        if not isinstance(image_plan, dict):
            case_rows = plan.get("cases") if isinstance(plan, dict) else []
            case_names = [
                str(item.get("name"))
                for item in case_rows or []
                if isinstance(item, dict) and item.get("name")
            ]
            image_plan = {
                "endpoint": plan.get("endpoint"),
                "model": plan.get("model"),
                "family": plan.get("family"),
                "transport": plan.get("transport"),
                "suite": plan.get("suite"),
                "include_2k": bool(plan.get("include_2k", False)),
                "include_4k": bool(plan.get("include_4k", False)),
                "visual_forensics": bool(plan.get("visual_forensics", True)),
                "cases": case_names,
                "estimated_case_count": len(case_names),
                "timeout_sec": get_timeout_sec(config),
            }
        profile_history = resolve_job_model_profile_snapshot(job_spec)
        if (
            int(job_spec.get("schema_version") or 0) >= 4
            and profile_history.get("resolution_status") != "snapshot"
        ):
            raise ValueError(
                "Cannot restore schema-v4 image job without a valid immutable MPDB snapshot."
            )
        profile_snapshot = profile_history.get("profile")
        if not isinstance(profile_snapshot, dict):
            profile_snapshot = {}
        interface_snapshot = profile_history.get("interface")
        if not isinstance(interface_snapshot, dict):
            interface_snapshot = {}
        execution_snapshot = profile_history.get("execution_target")
        if not isinstance(execution_snapshot, dict):
            execution_snapshot = {}
        provider = str(
            execution_snapshot.get("provider_id")
            or job_spec.get("provider")
            or provider
        )
        try:
            provider_cfg = get_provider_config(config, provider)
        except KeyError:
            provider_cfg = {"label": provider}
        model = str(
            execution_snapshot.get("request_model_id")
            or job_spec.get("model")
            or image_plan.get("model")
            or plan.get("model")
            or profile_snapshot.get("model_slug")
            or ""
        )
        family = str(
            profile_history.get("suite_family_id")
            or job_spec.get("model_family")
            or image_plan.get("family")
            or plan.get("family")
            or profile_snapshot.get("family_id")
            or "image"
        )
        if isinstance(summary, dict):
            result_validation = _classify_functional_result(
                job_spec,
                summary,
                job_type="image_param_test",
            )
            returncode = 0 if result_validation.get("pass") is True else 1
            status = "completed" if returncode == 0 else "failed"
        else:
            returncode = 1
            status = "failed"
        timestamp = report_dir.stat().st_mtime
        job = Job(
            id=report_dir.name,
            type="image_param_test",
            provider=provider,
            provider_label=str(provider_cfg.get("label") or provider),
            model=model,
            model_family=family,
            workload="image_param",
            users=None,
            spawn_rate=None,
            duration=None,
            report_dir=report_dir,
            command=[],
            reference_source=None,
            reference_label=None,
            api_form=str(
                execution_snapshot.get("api_form")
                or job_spec.get("api_form")
                or (image_plan.get("model_capability_profile") or {}).get("api_form")
                or interface_snapshot.get("api_form")
                or ""
            ),
            route_profile=str(
                execution_snapshot.get("route_profile")
                or job_spec.get("route_profile")
                or interface_snapshot.get("routing_mode")
                or ""
            ),
            model_profile_id=str(
                job_spec.get("model_profile_id")
                or (image_plan.get("model_capability_profile") or {}).get(
                    "model_api_profile_id"
                )
                or ""
            ),
            cache_measured_requests=0,
            request_mode="fixed",
            image_plan=image_plan,
            job_spec=job_spec,
            timeout_sec=int(image_plan.get("timeout_sec") or get_timeout_sec(config)),
            created_at=timestamp,
            started_at=None,
            finished_at=timestamp,
            status=status,
            returncode=returncode,
        )
        self._jobs[job.id] = job

    def _start_locked(self, job: Job) -> None:
        persisted_spec = None
        controls = None
        if job.type == "param_test" or job.job_spec.get("schema_version") == WORKFLOW_JOB_SPEC_VERSION:
            persisted_spec = load_job_spec(job.report_dir / "job_spec.json")
            controls = parameter_execution_from_job(persisted_spec)
            if (persisted_spec or {}).get("schema_version") == WORKFLOW_JOB_SPEC_VERSION and job.type == "image_param_test":
                from lib.test_runner.service import registry_for_plan
                registry_for_plan(persisted_spec["execution_plan"], config=load_config(), output_dir=job.report_dir)
            if (persisted_spec or {}).get("test_workflow_snapshot") is not None:
                from lib.test_runner.service import registry_for_plan
                registry_for_plan(persisted_spec["execution_plan"], config=load_config())
                for field in ("provider", "model", "route_profile", "api_form"):
                    if getattr(job, field) != persisted_spec.get(field):
                        raise ValueError(f"Workflow runtime {field} conflicts with immutable JobSpec")
        _initialize_fixed_cache_retention(job)
        rate_plan = (
            ConstantThroughputPlan.build(job.target_rpm, job.users)
            if job.type == "quick_load"
            else None
        )
        env = build_provider_child_env(
            load_config(),
            job.provider,
            {
                "LOADTEST_PROVIDER": job.provider,
                "LOADTEST_MODEL": job.model,
                "LOADTEST_API_FORM": job.api_form,
                "LOADTEST_ROUTE_PROFILE": job.route_profile,
                "LOADTEST_WORKLOAD": job.workload,
                "LOADTEST_REPORT_DIR": str(job.report_dir),
                "LOADTEST_TIMEOUT_SEC": str(job.timeout_sec or get_timeout_sec()),
                "LOADTEST_TARGET_RPM": str(job.target_rpm or 0),
                "LOADTEST_TARGET_TPM": str(job.target_tpm or 0),
                "LOADTEST_TARGET_TOKENS_PER_REQUEST": str(
                    job.target_tokens_per_request or 0
                ),
                "LOADTEST_REQUEST_MODE": job.request_mode,
                "LOADTEST_WARMUP_SEC": str(
                    rate_plan.user_period_sec if rate_plan is not None else 0
                ),
                "LOADTEST_MEASURE_DURATION_SEC": str(
                    parse_duration_seconds(job.duration) if job.duration else 0
                ),
                "LOADTEST_JOB_SPEC": str(job.report_dir / "job_spec.json"),
                "PYTHONUNBUFFERED": "1",
            },
        )
        if job.reference_source:
            env["LOADTEST_REFERENCE_SOURCE"] = job.reference_source
        if job.type == "param_test" or (persisted_spec or {}).get("schema_version") == WORKFLOW_JOB_SPEC_VERSION:
            if controls is not None:
                job.param_test_runs = controls["runs"]
                job.tool_validation_mode = controls["tool_validation_mode"]
            env["LOADTEST_PARAM_TEST_RUNS"] = str(job.param_test_runs)
            env["LOADTEST_TOOL_VALIDATION_MODE"] = job.tool_validation_mode
            if (persisted_spec or {}).get("schema_version") == WORKFLOW_JOB_SPEC_VERSION:
                env["LOADTEST_TEST_PLAN_DIGEST"] = controls["plan_digest"]
                env["LOADTEST_TEST_CASES"] = json.dumps(controls["requested_cases"])
        env.pop("LOADTEST_PARAMETER_SUITE", None)
        if job.job_spec.get("parameter_suite"):
            env["LOADTEST_PARAMETER_SUITE"] = job.job_spec["parameter_suite"]
        if job.type == "cache_suite" and job.cache_plan is None:
            env["LOADTEST_CACHE_MEASURED_REQUESTS"] = str(job.cache_measured_requests)
        if job.users:
            env["LOADTEST_USERS"] = str(job.users)
        job.started_at = time.time()
        job.status = "running"
        log_fh = job.log_path.open("w", encoding="utf-8")
        try:
            job.process = subprocess.Popen(
                job.command,
                cwd=PROJECT_ROOT,
                env=env,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except BaseException:
            log_fh.close()
            _finalize_fixed_cache_retention(job)
            raise
        finally:
            log_fh.close()
        job.pid = job.process.pid if job.process else None
        threading.Thread(target=self._monitor, args=(job.id,), daemon=True).start()

    def _monitor(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            process = job.process if job else None
        if process is None:
            return
        returncode = process.wait()
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.finished_at = time.time()
            if job.stop_requested:
                job.returncode = returncode
                job.status = "stopped"
            else:
                job.returncode, job.status = _result_gated_completion(
                    job,
                    returncode,
                )
            _ensure_load_result(job)
            _finalize_fixed_cache_retention(job)

    def _refresh_locked(self, job: Job) -> Job:
        if job.process is None:
            return job
        returncode = job.process.poll()
        if returncode is not None and job.status in {"queued", "running", "stopping"}:
            job.finished_at = job.finished_at or time.time()
            if job.stop_requested:
                job.returncode = returncode
                job.status = "stopped"
            else:
                job.returncode, job.status = _result_gated_completion(
                    job,
                    returncode,
                )
            _finalize_fixed_cache_retention(job)
        return job


@app.get("/")
def index() -> str:
    static_version = int(
        max(
            (PROJECT_ROOT / "scripts" / "static" / "web_console.js").stat().st_mtime,
            (PROJECT_ROOT / "scripts" / "static" / "web_console.css").stat().st_mtime,
        )
    )
    return render_template("web_console.html", static_version=static_version)


@app.get("/api/config")
def api_config() -> Any:
    config = load_config()
    provider = get_active_provider_name(config)
    model = get_selected_model(config, provider)
    family = get_model_family(config, model, provider)
    route_profile = get_model_route_profile(config, model, provider)
    api_form = get_model_api_form(config, model, provider, route_profile=route_profile)
    default_reference_contract_id = default_reference_source_for_model(
        config,
        family,
        model,
        provider,
        api_form=api_form,
        route_profile=route_profile,
    )
    reference_contracts = [
        {
            **row,
            "contract_id": str(row.get("contract_id") or row["id"]),
            "semantic_type": "InterfaceContract",
        }
        for row in list_reference_sources()
    ]
    capability_registry = _capability_registry_payload(config)
    return jsonify(
        {
            "active_provider": provider,
            "active_model": model,
            "model_family": family,
            "default_api_form": api_form,
            "route_profile": route_profile,
            "default_reference_contract_id": default_reference_contract_id,
            "reference_contracts": reference_contracts,
            # Value-identical compatibility aliases for pre-Gate14 clients.
            "default_reference_source": default_reference_contract_id,
            "reference_sources": reference_contracts,
            "deprecated_aliases": _interface_contract_deprecated_aliases(
                {
                    "default_reference_source": "default_reference_contract_id",
                    "reference_sources": "reference_contracts",
                }
            ),
            "model_capabilities": capability_registry["text"],
            "test_workflows": capability_registry.get("workflows", []),
            "providers": list_public_providers(config),
            "image_providers": list_image_providers(config),
            "image_model_capabilities": capability_registry["image"],
            "capability_summary": _capability_registry_summary(capability_registry),
            "image_defaults": {
                "suite": "full",
                "quality": "low",
                "output_format": "png",
                "include_2k": None,
                "include_4k": None,
                "no_negative": False,
                "no_cross_control": False,
                "visual_forensics": True,
                "timeout_sec": get_timeout_sec(config),
            },
            "cache_test": config.get("cache_test") or {},
            "staircase": config.get("staircase") or {},
            "soak": config.get("soak") or {},
            "warmup": config.get("warmup") or {},
            "adaptive_load": config.get("adaptive_load") or {},
            "test_cases": config.get("test_cases") or {},
            "defaults": {
                "users": DEFAULT_QUICK_USERS,
                "spawn_rate": DEFAULT_QUICK_SPAWN_RATE,
                "duration": DEFAULT_QUICK_DURATION,
                "workload": DEFAULT_WORKLOAD,
                "timeout_sec": get_timeout_sec(config),
                "target_rpm": _default_target_rpm(config),
                "target_tpm": _default_target_tpm(config),
                "param_test_runs": DEFAULT_PARAM_TEST_RUNS,
                "param_test_runs_max": MAX_PARAM_TEST_RUNS,
            },
        }
    )


def _capability_registry_payload(config: dict[str, Any]) -> dict[str, Any]:
    text: dict[str, dict[str, Any]] = {}
    image: dict[str, dict[str, Any]] = {}
    for provider, raw_provider in (config.get("providers") or {}).items():
        provider_cfg = raw_provider if isinstance(raw_provider, dict) else {}
        models_cfg = provider_cfg.get("models") or {}
        text_models: dict[str, Any] = {}
        for model in models_cfg.get("candidates") or []:
            model_id = str(model)
            family = get_model_family(config, model_id, provider)
            try:
                default_route = get_model_route_profile(config, model_id, provider)
                route_rows: dict[str, Any] = {}
                for route in get_model_route_profiles(config, model_id, provider):
                    default_form = get_model_api_form(
                        config,
                        model_id,
                        provider,
                        route_profile=route,
                    )
                    api_form_rows: dict[str, Any] = {}
                    for form_id in get_model_api_forms(
                        config,
                        model_id,
                        provider,
                        route_profile=route,
                    ):
                        fallback_transport: str | None = None
                        try:
                            fallback_transport = get_model_transport(
                                config,
                                model_id,
                                provider,
                                route_profile=route,
                                api_form=form_id,
                            )
                            from lib.config import get_model_reference_source
                            from lib.reference_specs import reference_sources_from_parameter_config
                            parameter_config = resolve_runtime_parameter_config(
                                config, str(provider), model_id, family, str(route), str(form_id),
                                modality="text", contract_id=get_model_reference_source(
                                    config, model_id, str(provider), route_profile=str(route), api_form=str(form_id)
                                ),
                            )
                            source = str(parameter_config["contract_id"])
                            policy = parameter_config["test_binding"]
                            capability = capability_profile_from_database_snapshot(
                                parameter_config["model_profile_database"]
                            )
                            allowed_sources = reference_sources_from_parameter_config(parameter_config, family, str(form_id))
                            cache_tool = _cache_tool_capability(
                                policy,
                                family,
                                str(capability.get("transport") or fallback_transport or ""),
                            )
                            api_form_rows[form_id] = {
                                "api_form": form_id,
                                "transport": capability.get("transport"),
                                "route_profile": route,
                                "default_reference_contract_id": source,
                                "reference_contract_ids": allowed_sources,
                                # Value-identical aliases for old console bundles.
                                "reference_source": source,
                                "reference_sources": allowed_sources,
                                "deprecated_aliases": _interface_contract_deprecated_aliases(
                                    {
                                        "reference_source": "default_reference_contract_id",
                                        "reference_sources": "reference_contract_ids",
                                    }
                                ),
                                "profile_id": capability.get("model_api_profile_id"),
                                "profile_status": capability.get("profile_status"),
                                "evidence": capability.get("evidence"),
                                "certification_scope": capability.get(
                                    "certification_scope"
                                ),
                                "route_stability_required": capability.get(
                                    "route_stability_required"
                                ),
                                "parameter_test_enabled": capability.get(
                                    "parameter_test_enabled"
                                ),
                                "pressure_test_enabled": capability.get(
                                    "pressure_test_enabled"
                                ),
                                "test_policy_pressure_test_enabled": capability.get(
                                    "test_policy_pressure_test_enabled"
                                ),
                                "pressure_test_runnable": pressure_test_runnable(
                                    capability
                                ),
                                **cache_tool,
                                "disabled_reason": capability.get("disabled_reason"),
                            }
                        except (KeyError, ValueError, RuntimeError) as exc:
                            # Keep a rejected Interface visible without hiding a
                            # runnable sibling on the same route/model.
                            api_form_rows[form_id] = {
                                "api_form": form_id,
                                "transport": fallback_transport,
                                "route_profile": route,
                                "default_reference_contract_id": "",
                                "reference_contract_ids": [],
                                "reference_source": "",
                                "reference_sources": [],
                                "deprecated_aliases": _interface_contract_deprecated_aliases(
                                    {
                                        "reference_source": "default_reference_contract_id",
                                        "reference_sources": "reference_contract_ids",
                                    }
                                ),
                                "profile_id": None,
                                "profile_status": "invalid",
                                "parameter_test_enabled": False,
                                "pressure_test_enabled": False,
                                "test_policy_pressure_test_enabled": False,
                                "pressure_test_runnable": False,
                                "cache_tool_runnable": False,
                                "cache_tool_profile": None,
                                "cache_tool_disabled_reason": str(exc),
                                "disabled_reason": str(exc),
                                "error": str(exc),
                            }
                    route_rows[route] = {
                        "route_profile": route,
                        "default_api_form": default_form,
                        "api_forms": api_form_rows,
                        **api_form_rows[default_form],
                    }
                default_row = route_rows[default_route]
                text_models[model_id] = {
                    "family": family,
                    "default_route_profile": default_route,
                    "routes": route_rows,
                    **default_row,
                }
            except (KeyError, ValueError, RuntimeError) as exc:
                text_models[model_id] = {
                    "family": family,
                    "profile_status": "invalid",
                    "error": str(exc),
                }
        if text_models:
            text[str(provider)] = text_models

        image_models: dict[str, Any] = {}
        image_cfg = provider_cfg.get("image") or {}
        for raw_model in image_cfg.get("models") or []:
            if not isinstance(raw_model, dict):
                continue
            model_id = str(raw_model.get("id") or "")
            family = str(raw_model.get("family") or "")
            if not model_id or not family:
                continue
            try:
                model_cfg = get_image_model_config(config, str(provider), model_id)
                default_route = str(model_cfg.get("route_profile") or "")
                route_rows: dict[str, Any] = {}
                for route, route_cfg in (model_cfg.get("routes") or {}).items():
                    route_model_cfg = get_image_model_config(
                        config,
                        str(provider),
                        model_id,
                        route_profile=str(route),
                    )
                    default_form = str(route_model_cfg.get("api_form") or "")
                    api_form_rows: dict[str, Any] = {}
                    for form_id in route_cfg.get("api_forms") or {}:
                        try:
                            exact_model_cfg = get_image_model_config(
                                config,
                                str(provider),
                                model_id,
                                route_profile=str(route),
                                api_form=str(form_id),
                            )
                            capability = capability_profile_snapshot(
                                "image",
                                family,
                                model_id,
                                [],
                                api_form=str(form_id),
                                route_profile=str(route),
                                model_profile_database=resolve_runtime_parameter_config(
                                    config, str(provider), model_id, family, str(route), str(form_id),
                                    modality="image",
                                )["model_profile_database"],
                            )
                            api_form_rows[str(form_id)] = {
                                "api_form": str(form_id),
                                "transport": exact_model_cfg.get("transport"),
                                "route_profile": str(route),
                                "profile_id": capability.get("profile_id"),
                                "profile_status": capability.get("profile_status"),
                                "evidence": capability.get("evidence"),
                                "certification_scope": capability.get(
                                    "certification_scope"
                                ),
                                "route_stability_required": capability.get(
                                    "route_stability_required"
                                ),
                                "parameter_test_enabled": capability.get("parameter_test_enabled"),
                                "pressure_test_enabled": capability.get(
                                    "pressure_test_enabled"
                                ),
                                "test_policy_pressure_test_enabled": capability.get(
                                    "test_policy_pressure_test_enabled"
                                ),
                                "pressure_test_runnable": pressure_test_runnable(capability),
                                "suite": capability.get("suite"),
                            }
                        except (KeyError, ValueError, RuntimeError) as exc:
                            # A gated Interactions sibling must not hide the exact
                            # Lite GenerateContent interface with official proof.
                            api_form_rows[str(form_id)] = {
                                "api_form": str(form_id),
                                "route_profile": str(route),
                                "profile_status": "invalid",
                                "parameter_test_enabled": False,
                                "pressure_test_enabled": False,
                                "test_policy_pressure_test_enabled": False,
                                "pressure_test_runnable": False,
                                "disabled_reason": str(exc),
                                "error": str(exc),
                            }
                    route_rows[str(route)] = {
                        "route_profile": str(route),
                        "default_api_form": default_form,
                        "api_forms": api_form_rows,
                        **api_form_rows[default_form],
                    }
                default_row = route_rows[default_route]
                image_models[model_id] = {
                    "family": family,
                    "default_route_profile": default_route,
                    "routes": route_rows,
                    **default_row,
                }
            except (KeyError, ValueError, RuntimeError) as exc:
                image_models[model_id] = {
                    "family": family,
                    "profile_status": "invalid",
                    "error": str(exc),
                }
        if image_models:
            image[str(provider)] = image_models
    return _add_workflow_capabilities(config, {"text": text, "image": image})


def _add_workflow_capabilities(config: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    """Expose separate exact workflow permissions without changing native gates."""
    from lib.model_profile_catalog import get_model_profile_catalog
    catalog = get_model_profile_catalog()
    available: list[dict[str, Any]] = []
    for binding in catalog.list_workflows(enabled=True):
        target = binding["execution_target"]
        provider, model, form = (target[key] for key in ("provider_id", "request_model_id", "api_form"))
        provider_cfg = (config.get("providers") or {}).get(provider) or {}
        configured = provider_cfg.get("models") or {}
        if model not in (configured.get("candidates") or []):
            continue
        try:
            routes = get_model_route_profiles(config, model, provider)
        except (KeyError, ValueError, RuntimeError):
            continue
        for route in routes:
            try:
                if form not in get_model_api_forms(config, model, provider, route_profile=route):
                    continue
                preview = _preview_workflow(config, {
                    "type": "param_test", "provider": provider, "model": model,
                    "route_profile": route, "api_form": form,
                    "workflow_binding_id": binding["test_binding_id"],
                    "reference_contract_id": binding["contract_id"], "runs": 1,
                })
            except (KeyError, TypeError, ValueError, RuntimeError):
                # The reviewed factory and exact route must still compile today.
                continue
            factory_id = str((binding.get("workflow") or {}).get("factory_id") or "")
            optional = factory_id == "media_input"
            if optional and not preview["plan"]["selected_cases"]:
                continue
            descriptor = {
                "factory_id": factory_id,
                "optional": optional,
                "label": "图片/视频/音频输入" if optional else {
                    "fable_research": "Fable 完整参数矩阵",
                    "aws_bedrock_mantle_json": "Bedrock Mantle 参数矩阵",
                    "deepseek_beta_prefix": "DeepSeek Beta 参数矩阵",
                }.get(factory_id, binding["workflow_id"]),
                "workflow_id": binding["workflow_id"],
                "workflow_binding_id": binding["test_binding_id"],
                "source_id": binding["source_id"], "profile_id": binding["profile_id"],
                "interface_id": binding["interface_id"], "reference_contract_id": binding["contract_id"],
                "provider": provider, "model": model, "route_profile": route, "api_form": form,
                "case_count": len(preview["plan"]["selected_cases"]), "default_runs": 1,
                "request_cap": preview["request_cap"], "cleanup_request_cap": preview["cleanup_request_cap"],
                "cases": [{key: row[key] for key in ("id", "name", "phase", "group") if key in row}
                          for row in preview["plan"]["definition"]["cases"] if row.get("enabled", True)],
            }
            available.append(descriptor)
            models = registry["text"].setdefault(provider, {})
            model_row = models.setdefault(model, {"family": get_model_family(config, model, provider),
                                                  "profile_status": "invalid", "parameter_test_enabled": False,
                                                  "pressure_test_runnable": False})
            model_row.setdefault("default_route_profile", str(route))
            route_row = model_row.setdefault("routes", {}).setdefault(route, {"route_profile": route})
            leaves = route_row.setdefault("api_forms", {})
            leaf = leaves.setdefault(form, {"api_form": form, "route_profile": route,
                                            "transport": target["transport_adapter_id"],
                                            "profile_status": "invalid", "parameter_test_enabled": False,
                                            "pressure_test_enabled": False, "pressure_test_runnable": False})
            for row in (leaf, route_row, model_row):
                row["workflow_available"] = True
                row.setdefault("test_workflows", []).append(copy.deepcopy(descriptor))
            if not optional:
                route_row.setdefault("workflow_default_api_form", form)
                model_row.setdefault("workflow_default_route_profile", route)
    registry["workflows"] = available
    return registry


def _capability_registry_summary(registry: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for modality in ("text", "image"):
        rows = [
            profile
            for models in (registry.get(modality) or {}).values()
            for profile in models.values()
        ]
        registered = sum(1 for row in rows if row.get("profile_status") == "registered")
        invalid = sum(1 for row in rows if row.get("profile_status") == "invalid")
        inherited = len(rows) - registered - invalid
        summary[modality] = {
            "configured_models": len(rows),
            "registered_models": registered,
            "inherited_models": inherited,
            "invalid_models": invalid,
            "complete": registered == len(rows),
        }
    return summary


@app.get("/api/param-specs")
def api_param_specs() -> Any:
    config = load_config()
    family_arg = request.args.get("family")
    family_only = bool(
        family_arg
        and not request.args.get("provider")
        and not request.args.get("model")
    )
    if family_only:
        family = str(family_arg)
        route_profile = str(request.args.get("route_profile") or "") or None
        api_form = str(request.args.get("api_form") or "") or None
        try:
            default_contract_id = default_reference_source_for_family(
                family,
                route_profile=route_profile,
                api_form=api_form,
            )
            contract_id, legacy_query_alias_used = _select_interface_contract_id(
                request.args.get("contract_id"),
                request.args.get("reference_source"),
                default_contract_id,
            )
            reference = get_reference_source(contract_id)
            if str(reference.get("model_family") or "") != family:
                raise ValueError(
                    f"Interface Contract {contract_id!r} does not belong to "
                    f"family {family!r}."
                )
            if api_form and str(reference.get("api_form") or "") != api_form:
                raise ValueError(
                    f"Interface Contract {contract_id!r} does not belong to "
                    f"API form {api_form!r}."
                )
            if (
                route_profile
                and str(reference.get("route_profile") or "") != route_profile
            ):
                raise ValueError(
                    f"Interface Contract {contract_id!r} does not belong to "
                    f"route profile {route_profile!r}."
                )
        except (KeyError, RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            _contract_first_param_payload(
                reference_spec_payload(contract_id),
                contract_id,
                legacy_query_alias_used=legacy_query_alias_used,
            )
        )

    provider = str(request.args.get("provider") or get_active_provider_name(config))
    model = str(request.args.get("model") or get_selected_model(config, provider))
    actual_family = get_model_family(config, model, provider)
    if family_arg and str(family_arg) != actual_family:
        return jsonify(
            {
                "error": (
                    f"Model {model!r} belongs to family {actual_family!r}, "
                    f"not {str(family_arg)!r}."
                )
            }
        ), 400
    family = actual_family
    route_profile = get_model_route_profile(
        config,
        model,
        provider,
        route_profile=str(request.args.get("route_profile") or "") or None,
    )
    api_form = get_model_api_form(
        config,
        model,
        provider,
        route_profile=route_profile,
        api_form=str(request.args.get("api_form") or "") or None,
    )
    default_contract_id = default_reference_source_for_model(
        config,
        family,
        model,
        provider,
        api_form=api_form,
        route_profile=route_profile,
    )
    try:
        contract_id, legacy_query_alias_used = _select_interface_contract_id(
            request.args.get("contract_id"),
            request.args.get("reference_source"),
            default_contract_id,
        )
        allowed_contract_ids = reference_sources_for_model(
            config,
            family,
            model,
            provider,
            api_form=api_form,
            route_profile=route_profile,
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    if contract_id not in allowed_contract_ids:
        return jsonify(
            {
                "error": (
                    f"Interface Contract {contract_id!r} is not part of the "
                    f"{family}/{model} family suite."
                ),
                "allowed_reference_contract_ids": allowed_contract_ids,
                "allowed_reference_sources": allowed_contract_ids,
                "deprecated_aliases": _interface_contract_deprecated_aliases(
                    {"allowed_reference_sources": "allowed_reference_contract_ids"}
                ),
            }
        ), 400
    selected_suite = request.args.get("parameter_suite") or None
    available_suites = []
    fixed_payload = None
    from lib.fixed_parameter_specs import candidate_fixed_suites, build_fixed_plan, fixed_suite_descriptor, fixed_spec_payload
    candidates = candidate_fixed_suites(provider, model, api_form, contract_id)
    if selected_suite and selected_suite not in {item["id"] for item in candidates}:
        return jsonify({"error": "Fixed parameter suite does not match the selected source/model/form/Contract"}), 400
    if candidates:
        try:
            parameter = resolve_runtime_parameter_config(config, provider, model, family, route_profile,
                api_form, modality="text", contract_id=contract_id)
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            if selected_suite:
                return jsonify({"error": str(exc)}), 400
            parameter = None
        for descriptor in candidates if parameter is not None else []:
            try:
                route = get_provider_interface(config, descriptor["transport"], provider)
                endpoint = str(route.get("base_url") or get_provider_config(config, provider)["base_url"]).rstrip("/") + str(route["path"])
                # Cache descriptions validate shared definitions but contain no
                # nonce or dispatch body. Only JobSpec creation freezes those.
                fixed_plan = build_fixed_plan(parameter["model_profile_database"], suite_id=descriptor["id"], endpoint=endpoint)
                available_suites.append({k: descriptor[k] for k in ("id", "label", "request_count", "runs")})
                if selected_suite == descriptor["id"]:
                    fixed_payload = fixed_spec_payload(fixed_plan)
            except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                if selected_suite == descriptor["id"]:
                    return jsonify({"error": str(exc)}), 400
    if fixed_payload is not None:
        return jsonify(_contract_first_param_payload({**fixed_payload, "available_parameter_suites": available_suites},
            contract_id, legacy_query_alias_used=legacy_query_alias_used))
    try:
        parameter_database = resolve_runtime_parameter_config(
            config, provider, model, family, route_profile, api_form,
            modality="text", contract_id=contract_id,
        )["model_profile_database"]
    except (KeyError, RuntimeError, ValueError):
        # The existing read-only observation projection still handles disabled
        # historical references, without manufacturing execution permission.
        parameter_database = None
    return jsonify(
        _contract_first_param_payload(
            {**model_reference_spec_payload(
                "text",
                family,
                model,
                contract_id,
                api_form=api_form,
                route_profile=route_profile,
                provider_override=get_model_api_forms(
                    config,
                    model,
                    provider,
                    route_profile=route_profile,
                )[api_form],
                model_profile_database=parameter_database,
            ), "available_parameter_suites": available_suites},
            contract_id,
            legacy_query_alias_used=legacy_query_alias_used,
        )
    )


@app.get("/api/param-results/latest")
def api_latest_param_result() -> Any:
    config = load_config()
    provider = str(request.args.get("provider") or get_active_provider_name(config))
    model = str(request.args.get("model") or get_selected_model(config, provider))
    family = get_model_family(config, model, provider)
    route_profile = get_model_route_profile(
        config,
        model,
        provider,
        route_profile=str(request.args.get("route_profile") or "") or None,
    )
    api_form = get_model_api_form(
        config,
        model,
        provider,
        route_profile=route_profile,
        api_form=str(request.args.get("api_form") or "") or None,
    )
    default_contract_id = default_reference_source_for_model(
        config,
        family,
        model,
        provider,
        api_form=api_form,
        route_profile=route_profile,
    )
    try:
        contract_id, _legacy_query_alias_used = _select_interface_contract_id(
            request.args.get("contract_id"),
            request.args.get("reference_source"),
            default_contract_id,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    tool_validation_mode = str(request.args.get("tool_validation_mode") or "auto")
    if tool_validation_mode not in TOOL_VALIDATION_MODES:
        return jsonify({"error": "invalid tool_validation_mode"}), 400
    try:
        parameter = resolve_runtime_parameter_config(
            config, provider, model, family, route_profile, api_form,
            modality="text", contract_id=contract_id,
        )
        capability = capability_profile_from_database_snapshot(parameter["model_profile_database"])
        result = JOB_MANAGER.latest_param_result(
            provider, model, route_profile, api_form,
            str(capability.get("model_api_profile_id") or ""), contract_id, tool_validation_mode,
            parameter_suite=request.args.get("parameter_suite") or None,
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"result": result})


def _maybe_preview_workflow(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    from lib.test_runner.service import maybe_preview_test_plan
    return maybe_preview_test_plan(config, payload)


def _preview_workflow(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from lib.test_runner.service import preview_test_plan
    return preview_test_plan(config, payload)


@app.post("/api/test-plan/preview")
def api_preview_test_plan() -> Any:
    """Compile the selected enabled workflow without credentials or dispatch."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Test plan request must be an object."}), 400
    try:
        preview = _preview_workflow(load_config(), payload)
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(preview)


@app.post("/api/image-plan/preview")
def api_preview_image_plan() -> Any:
    """Retain the old case-count field and expose the shared frozen image plan."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Image plan request must be an object."}), 400
    try:
        config = load_config()
        preview = _preview_workflow(config, {
            **payload, "type": "image_param_test",
            "timeout_sec": _resolve_image_timeout_sec(config, payload.get("timeout_sec")),
        })
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({**preview, "estimated_case_count": len(preview["plan"]["selected_cases"])})


@app.get("/api/image-results/latest")
def api_latest_image_result() -> Any:
    config = load_config()
    configured = list_image_providers(config)
    default_provider = configured[0]["name"] if configured else ""
    provider = str(request.args.get("provider") or default_provider)
    if not provider:
        return jsonify({"result": None})
    try:
        image_cfg = get_image_provider_config(config, provider)
        model = str(request.args.get("model") or image_cfg.get("default") or "")
        requested_form = str(request.args.get("api_form") or "").strip()
        transport = str(request.args.get("transport") or "").strip()
        legacy_transport_form = (
            api_form_for_transport(transport, modality="image") if transport else ""
        )
        if (
            requested_form
            and legacy_transport_form
            and requested_form != legacy_transport_form
        ):
            raise ValueError(
                f"api_form {requested_form!r} conflicts with legacy transport "
                f"{transport!r} ({legacy_transport_form!r})."
            )
        requested_form = requested_form or legacy_transport_form or None
        model_cfg = get_image_model_config(
            config,
            provider,
            model,
            route_profile=str(request.args.get("route_profile") or "") or None,
            api_form=requested_form,
        )
        route_profile = str(model_cfg.get("route_profile") or "")
        api_form = str(model_cfg.get("api_form") or "")
        capability = load_model_capability_profile(
            "image",
            str(model_cfg.get("family") or ""),
            model,
            route_profile=route_profile,
            api_form=api_form,
            provider_override=(
                (model_cfg.get("routes") or {})
                .get(route_profile, {})
                .get("api_forms", {})
                .get(api_form, {})
            ),
        )
        model_profile_id = str(capability.get("model_api_profile_id") or "")
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(
        {
            "result": JOB_MANAGER.latest_image_result(
                provider,
                model,
                route_profile,
                api_form,
                model_profile_id,
            )
        }
    )


@app.get("/api/jobs")
def api_jobs() -> Any:
    return jsonify({"jobs": JOB_MANAGER.list()})


@app.get("/api/jobs/current")
def api_current_job_refs() -> Any:
    return jsonify(JOB_MANAGER.current_refs())


@app.get("/api/results")
def api_results() -> Any:
    return jsonify({"results": _list_load_results()})


@app.get("/api/results/<path:result_id>")
def api_result_detail(result_id: str) -> Any:
    result = _load_result_by_id(result_id)
    if result is None:
        return jsonify({"error": "result not found"}), 404
    return jsonify(result)


@app.get("/api/result")
def api_result_detail_query() -> Any:
    result_id = str(request.args.get("id") or "")
    result = _load_result_by_id(result_id)
    if result is None:
        return jsonify({"error": "result not found"}), 404
    return jsonify(result)


@app.post("/api/jobs")
def api_create_job() -> Any:
    payload = request.get_json(silent=True) or {}
    try:
        job = JOB_MANAGER.create(payload)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(JOB_MANAGER.public(job, include_detail=True)), 201


@app.get("/api/jobs/<job_id>")
def api_job(job_id: str) -> Any:
    try:
        return jsonify(JOB_MANAGER.get(job_id))
    except KeyError:
        return jsonify({"error": "job not found"}), 404


@app.post("/api/jobs/<job_id>/stop")
def api_stop_job(job_id: str) -> Any:
    try:
        return jsonify(JOB_MANAGER.stop(job_id))
    except KeyError:
        return jsonify({"error": "job not found"}), 404
    except (RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/reports/<path:relpath>")
def reports_file(relpath: str) -> Any:
    return send_from_directory(REPORTS_ROOT, relpath)


def _command_for_job(
    job_type: str,
    report_dir: Path,
    users: int | None,
    spawn_rate: int | None,
    duration: str,
    target_rpm: float,
    timeout_sec: int,
) -> list[str]:
    if job_type == "param_test":
        return [sys.executable, "scripts/param_test.py"]
    if job_type == "cache_suite":
        return [sys.executable, "scripts/run_cache.py"]
    if job_type == "staircase":
        return [sys.executable, "scripts/run_staircase.py"]
    if job_type == "soak":
        return [sys.executable, "scripts/run_soak.py"]
    resolved_users = int(users or DEFAULT_QUICK_USERS)
    resolved_spawn_rate = int(spawn_rate or DEFAULT_QUICK_SPAWN_RATE)
    rate_plan = ConstantThroughputPlan.build(target_rpm, resolved_users)
    if rate_plan is not None and resolved_spawn_rate < rate_plan.aggregate_rps:
        raise ValueError(
            "spawn_rate must be at least target_rpm / 60 so users can reach "
            "their deterministic initial slots."
        )
    measure_sec = parse_duration_seconds(duration)
    warmup_sec = rate_plan.user_period_sec if rate_plan is not None else 0.0
    total_duration = f"{measure_sec + warmup_sec:g}s"
    return [
        sys.executable,
        "-m",
        "locust",
        "-f",
        "locustfile.py",
        "--headless",
        "-u",
        str(resolved_users),
        "-r",
        str(resolved_spawn_rate),
        "-t",
        total_duration,
        "--stop-timeout",
        str(timeout_sec),
        "--csv",
        str(report_dir / "locust"),
        "--html",
        str(report_dir / "report.html"),
    ]


def _image_command_for_job(report_dir: Path, image_plan: dict[str, Any]) -> list[str]:
    from lib.test_runner.service import image_cli_arguments
    return [sys.executable, "scripts/image_param_test.py", *image_cli_arguments(
        image_plan, report_dir, api_key_env=SELECTED_API_KEY_ENV)]


def _job_summary(
    job: Job, records: list[RequestRecord] | None = None
) -> dict[str, Any] | None:
    records = records if records is not None else _load_result_records(job.report_dir)
    if not records:
        return None
    config = load_config()
    metrics_cfg = config.get("metrics") or {}
    if job.type == "cache_suite":
        return summarize_records(
            records,
            business_prefix="cache:",
            business_group="cache_profiles",
            cache_min_prompt_tokens=int(
                metrics_cfg.get("cache_min_prompt_tokens", 4000)
            ),
        )
    return summarize_records(
        records,
        business_prefix=str(metrics_cfg.get("business_request_prefix", "chat:")),
        business_group=_business_group_for_workload(job.workload),
        cache_min_prompt_tokens=int(metrics_cfg.get("cache_min_prompt_tokens", 4000)),
    )


def _business_group_for_workload(workload: str) -> str:
    return (
        "compatibility_profiles"
        if workload == "mixed_compat"
        else "throughput_profiles"
    )


def _job_time_series(job: Job, records: list[RequestRecord]) -> list[dict[str, Any]]:
    if job.type not in {"quick_load", "staircase", "soak"} or not records:
        return []
    config = load_config()
    metrics_cfg = config.get("metrics") or {}
    series_now = None
    if job.status not in {"queued", "running", "stopping"}:
        series_now = job.finished_at or max(
            (item.timestamp for item in records), default=time.time()
        )
    return build_time_series(
        records,
        business_prefix=str(metrics_cfg.get("business_request_prefix", "chat:")),
        business_group=_business_group_for_workload(job.workload),
        cache_min_prompt_tokens=int(metrics_cfg.get("cache_min_prompt_tokens", 4000)),
        bucket_sec=int(metrics_cfg.get("live_chart_interval_sec", 10)),
        now=series_now,
    )


def _ensure_load_result(job: Job) -> dict[str, Any] | None:
    if job.type not in {"quick_load", "staircase", "soak"}:
        return None
    if job.status in {"queued", "running", "stopping"}:
        return None
    return _ensure_load_result_for_dir(
        job.report_dir,
        result_type=job.type,
        provider=job.provider,
        provider_label=job.provider_label,
        model=job.model,
        model_family=job.model_family,
        workload=job.workload,
        users=job.users,
        spawn_rate=job.spawn_rate,
        duration=job.duration,
        status=job.status,
        returncode=job.returncode,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        target_rpm=job.target_rpm,
        target_tpm=job.target_tpm,
        target_tokens_per_request=job.target_tokens_per_request,
        context_window_tokens=job.context_window_tokens,
        context_window_source=job.context_window_source,
    )


def _list_load_results() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[Path] = set()
    active_dirs = {
        job.report_dir.resolve()
        for job in JOB_MANAGER._jobs.values()
        if job.status in {"queued", "running", "stopping"}
    }

    for job in JOB_MANAGER._jobs.values():
        if job.type in {"quick_load", "staircase", "soak"}:
            result = _ensure_load_result(job)
            if result:
                results.append(_load_result_summary(result))
                seen.add(job.report_dir.resolve())

    if JOBS_ROOT.exists():
        evidence_paths = {
            *JOBS_ROOT.rglob("request_records.jsonl"),
            *JOBS_ROOT.rglob("request_attempts.jsonl"),
            *JOBS_ROOT.rglob("load_window.json"),
        }
        for evidence_path in sorted(evidence_paths):
            report_dir = evidence_path.parent.resolve()
            if any(
                report_dir == active or active in report_dir.parents
                for active in active_dirs
            ):
                continue
            if any(report_dir == root or root in report_dir.parents for root in seen):
                continue
            result = _ensure_load_result_for_dir(report_dir)
            if result:
                results.append(_load_result_summary(result))
                seen.add(report_dir)

    deduped = {item["id"]: item for item in results}
    return sorted(
        deduped.values(),
        key=lambda item: float(item.get("created_at") or 0),
        reverse=True,
    )


def _load_result_by_id(result_id: str) -> dict[str, Any] | None:
    report_dir = (REPORTS_ROOT / result_id).resolve()
    try:
        report_dir.relative_to(REPORTS_ROOT.resolve())
    except ValueError:
        return None
    if not report_dir.exists() or not report_dir.is_dir():
        return None
    return _ensure_load_result_for_dir(report_dir)


def _ensure_load_result_for_dir(
    report_dir: Path,
    *,
    result_type: str | None = None,
    provider: str | None = None,
    provider_label: str | None = None,
    model: str | None = None,
    model_family: str | None = None,
    workload: str | None = None,
    users: int | None = None,
    spawn_rate: int | None = None,
    duration: str | None = None,
    status: str | None = None,
    returncode: int | None = None,
    created_at: float | None = None,
    started_at: float | None = None,
    finished_at: float | None = None,
    target_rpm: float | None = None,
    target_tpm: float | None = None,
    target_tokens_per_request: float | None = None,
    context_window_tokens: int | None = None,
    context_window_source: str | None = None,
) -> dict[str, Any] | None:
    report_dir = report_dir.resolve()
    result_path = report_dir / "load_result.json"
    cached = _read_json(result_path)
    if (
        isinstance(cached, dict)
        and int(cached.get("schema_version") or 0) >= LOAD_RESULT_SCHEMA_VERSION
        and cached.get("summary")
        and isinstance(cached.get("profile_stats"), list)
        and isinstance(cached.get("time_series"), list)
    ):
        return cached

    records = _load_result_records(report_dir)
    attempt_rows = _load_result_attempt_rows(report_dir)
    load_window = _load_result_window(report_dir, records, attempt_rows)
    measured = _measured_load_records(records)
    if not measured and load_window is None:
        return None

    fallback_attempts = [
        item
        for item in attempt_rows
        if item.get("event") in {None, "admitted"}
        and isinstance(item.get("extra"), dict)
    ]
    fallback_extra = (
        dict(fallback_attempts[0].get("extra") or {}) if fallback_attempts else {}
    )
    fallback_timestamps = [
        float(item["timestamp"])
        for item in fallback_attempts
        if isinstance(item.get("timestamp"), (int, float))
    ]

    metadata = _infer_load_result_metadata(
        report_dir,
        measured,
        fallback_extra=fallback_extra,
        fallback_timestamps=fallback_timestamps,
        result_type=result_type,
        provider=provider,
        provider_label=provider_label,
        model=model,
        model_family=model_family,
        workload=workload,
        users=(
            users
            if users is not None
            else int(load_window.get("users") or 0) or None
            if load_window is not None
            else None
        ),
        spawn_rate=spawn_rate,
        duration=(
            duration
            or (
                f"{float(load_window.get('measure_sec')):g}s"
                if load_window is not None and load_window.get("measure_sec")
                else None
            )
        ),
        status=status,
        returncode=returncode,
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        target_rpm=(
            target_rpm
            if target_rpm is not None
            else load_window.get("target_rpm") if load_window is not None else None
        ),
        target_tpm=target_tpm,
        target_tokens_per_request=target_tokens_per_request,
        context_window_tokens=context_window_tokens,
        context_window_source=context_window_source,
    )
    config = load_config()
    metrics_cfg = config.get("metrics") or {}
    business_group = _business_group_for_workload(
        str(metadata.get("workload") or "throughput")
    )
    summary = summarize_records(
        records,
        business_prefix=str(metrics_cfg.get("business_request_prefix", "chat:")),
        business_group=business_group,
        cache_min_prompt_tokens=int(metrics_cfg.get("cache_min_prompt_tokens", 4000)),
        duration_sec=(
            float(load_window.get("measure_sec"))
            if load_window is not None and load_window.get("measure_sec")
            else None
        ),
    )
    summary = _merge_load_window_summary(summary, load_window)
    result = {
        **metadata,
        "schema_version": LOAD_RESULT_SCHEMA_VERSION,
        "id": report_dir.relative_to(REPORTS_ROOT.resolve()).as_posix(),
        "report_dir": str(report_dir),
        "summary": summary,
        "load_window": load_window,
        "profile_stats": _profile_stats(measured),
        "time_series": build_time_series(
            records,
            business_prefix=str(metrics_cfg.get("business_request_prefix", "chat:")),
            business_group=business_group,
            cache_min_prompt_tokens=int(
                metrics_cfg.get("cache_min_prompt_tokens", 4000)
            ),
            bucket_sec=int(metrics_cfg.get("live_chart_interval_sec", 10)),
            now=metadata.get("finished_at")
            or max((item.timestamp for item in records), default=time.time()),
        ),
        "history": _load_history_rows(report_dir),
        "report_files": _report_files(report_dir),
        "generated_at": time.time(),
    }
    write_json(result_path, result)
    return result


def _load_result_records(report_dir: Path) -> list[RequestRecord]:
    records: list[RequestRecord] = []
    for path in sorted(report_dir.rglob("request_records.jsonl")):
        records.extend(load_records(path))
    return records


def _load_result_attempt_rows(report_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(report_dir.rglob("request_attempts.jsonl")):
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def _attempt_is_measured(item: dict[str, Any]) -> bool:
    if item.get("is_warmup"):
        return False
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    try:
        elapsed = float(item.get("admitted_elapsed_sec"))
    except (TypeError, ValueError):
        elapsed = None
    try:
        warmup_sec = float(extra.get("warmup_sec") or 0)
        measure_sec = float(extra.get("measure_duration_sec") or 0)
    except (TypeError, ValueError):
        warmup_sec = measure_sec = 0.0
    if elapsed is not None and measure_sec > 0:
        return warmup_sec <= elapsed < warmup_sec + measure_sec
    try:
        timestamp = float(item.get("timestamp"))
    except (TypeError, ValueError):
        return False
    try:
        measure_start = float(extra.get("measure_started_at"))
    except (TypeError, ValueError):
        measure_start = None
    try:
        measure_end = float(extra.get("measure_ended_at"))
    except (TypeError, ValueError):
        measure_end = None
    return not (
        (measure_start is not None and timestamp < measure_start)
        or (measure_end is not None and timestamp >= measure_end)
    )


def _load_result_window(
    report_dir: Path,
    records: list[RequestRecord],
    attempt_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    stored = _read_json(report_dir / "load_window.json")
    if isinstance(stored, dict):
        return stored

    admitted = [
        item
        for item in attempt_rows
        if item.get("event") in {None, "admitted"}
        and item.get("group") == "throughput_profiles"
        and item.get("request_id")
        and _attempt_is_measured(item)
    ]
    if not admitted:
        return None
    admitted_ids = {str(item["request_id"]) for item in admitted}
    cancelled_ids = {
        str(item["request_id"])
        for item in attempt_rows
        if item.get("event") == "cancelled_before_send"
        and item.get("request_id")
        and str(item["request_id"]) in admitted_ids
    }
    started_ids = admitted_ids - cancelled_ids
    completed_records = [
        item
        for item in records
        if item.request_id and str(item.request_id) in started_ids
    ]
    completed_ids = {str(item.request_id) for item in completed_records}
    extra = admitted[0].get("extra") if isinstance(admitted[0].get("extra"), dict) else {}
    try:
        target_rpm = float(extra.get("target_rpm") or 0)
    except (TypeError, ValueError):
        target_rpm = 0.0
    try:
        measure_sec = float(extra.get("measure_duration_sec") or 0)
    except (TypeError, ValueError):
        measure_sec = 0.0
    try:
        users = int(extra.get("configured_users") or 0)
    except (TypeError, ValueError):
        users = 0
    planned = target_rpm * measure_sec / 60.0 if target_rpm > 0 and measure_sec > 0 else None
    return {
        "schema_version": 1,
        "status": "reconstructed_from_attempt_ledger",
        "scheduler": "locust.constant_throughput",
        "scheduler_semantics": "closed_loop_capped_throughput",
        "target_rpm": target_rpm or None,
        "users": users or None,
        "per_user_rps": target_rpm / (60.0 * users) if target_rpm > 0 and users > 0 else None,
        "warmup_sec": extra.get("warmup_sec"),
        "measure_sec": measure_sec or None,
        "measure_started_at": extra.get("measure_started_at"),
        "measure_ended_at": extra.get("measure_ended_at"),
        "planned_start_count": planned,
        "admitted_request_count": len(admitted_ids),
        "cancelled_before_send_count": len(cancelled_ids),
        "unresolved_admission_count": 0,
        "admission_conservation_ok": (
            len(admitted_ids) == len(started_ids) + len(cancelled_ids)
        ),
        "started_request_count": len(started_ids),
        "completed_request_count": len(completed_ids),
        "successful_request_count": sum(1 for item in completed_records if item.success),
        "unfinished_after_drain_count": len(started_ids - completed_ids),
        "completion_conservation_ok": (
            len(started_ids)
            == len(completed_ids) + len(started_ids - completed_ids)
        ),
        "unscheduled_count": max(planned - len(started_ids), 0.0) if planned is not None else None,
        "overrun_start_count": max(len(started_ids) - planned, 0.0) if planned is not None else None,
    }


def _merge_load_window_summary(
    summary: dict[str, Any], load_window: dict[str, Any] | None
) -> dict[str, Any]:
    if not isinstance(load_window, dict):
        return summary
    merged = dict(summary)
    try:
        measure_sec = float(load_window.get("measure_sec") or 0)
    except (TypeError, ValueError):
        measure_sec = 0.0
    minutes = max(measure_sec / 60.0, 1 / 60.0)
    planned = load_window.get("planned_start_count")
    try:
        planned = float(planned) if planned is not None else None
    except (TypeError, ValueError):
        planned = None
    started = int(load_window.get("started_request_count") or 0)
    completed = int(load_window.get("completed_request_count") or 0)
    merged.update(
        {
            "target_rpm": load_window.get("target_rpm"),
            "planned_request_count": planned,
            "admitted_request_count": int(load_window.get("admitted_request_count") or 0),
            "cancelled_before_send_count": int(load_window.get("cancelled_before_send_count") or 0),
            "unresolved_admission_count": int(load_window.get("unresolved_admission_count") or 0),
            "admission_conservation_ok": bool(load_window.get("admission_conservation_ok", True)),
            "started_request_count": started,
            "attempted_business_rpm": started / minutes,
            "started_business_rpm": started / minutes,
            "arrival_shortfall_count": max(planned - started, 0.0) if planned is not None else None,
            "arrival_coverage": started / planned if planned is not None and planned > 0 else None,
            "arrival_overrun_count": max(started - planned, 0.0) if planned is not None else None,
            "completed_business_request_count": completed,
            "completed_business_rpm": completed / minutes,
            "completion_shortfall_count": max(planned - completed, 0.0) if planned is not None else None,
            "completion_coverage": completed / planned if planned is not None and planned > 0 else None,
            "unfinished_after_drain_count": int(load_window.get("unfinished_after_drain_count") or 0),
            "completion_conservation_ok": bool(load_window.get("completion_conservation_ok", True)),
        }
    )
    return merged


def _measured_load_records(records: list[RequestRecord]) -> list[RequestRecord]:
    return [
        item
        for item in records
        if item.task_name.startswith("chat:")
        and item.group in {"throughput_profiles", "compatibility_profiles"}
        and record_in_measurement_window(item)
        and not item.is_retry
    ]


def _infer_load_result_metadata(
    report_dir: Path,
    measured: list[RequestRecord],
    fallback_extra: dict[str, Any] | None = None,
    fallback_timestamps: list[float] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    first = measured[0] if measured else None
    extra = (first.extra or {}) if first is not None else (fallback_extra or {})
    timestamps = [item.timestamp for item in measured if item.timestamp]
    if not timestamps:
        timestamps = list(fallback_timestamps or [])
    created_at = overrides.get("created_at") or (
        min(timestamps) if timestamps else report_dir.stat().st_mtime
    )
    finished_at = overrides.get("finished_at") or (
        max(timestamps) if timestamps else report_dir.stat().st_mtime
    )
    provider = overrides.get("provider") or extra.get("provider") or "unknown"
    model = overrides.get("model") or extra.get("requested_model") or "unknown"
    result_type = overrides.get("result_type") or _result_type_from_dir(report_dir)
    title = f"{_format_time(created_at)} · {provider} / {model} · {result_type}"
    return {
        "title": title,
        "type": result_type,
        "provider": provider,
        "provider_label": overrides.get("provider_label")
        or extra.get("provider_label")
        or provider,
        "model": model,
        "model_family": overrides.get("model_family")
        or extra.get("model_family")
        or "",
        "workload": overrides.get("workload") or extra.get("workload") or "throughput",
        "users": overrides.get("users"),
        "spawn_rate": overrides.get("spawn_rate"),
        "duration": overrides.get("duration"),
        "target_rpm": overrides.get("target_rpm")
        if overrides.get("target_rpm") is not None
        else extra.get("target_rpm"),
        "target_tpm": overrides.get("target_tpm")
        if overrides.get("target_tpm") is not None
        else extra.get("target_tpm"),
        "target_tokens_per_request": (
            overrides.get("target_tokens_per_request")
            if overrides.get("target_tokens_per_request") is not None
            else extra.get("target_tokens_per_request")
        ),
        "context_window_tokens": (
            overrides.get("context_window_tokens")
            if overrides.get("context_window_tokens") is not None
            else extra.get("context_window_tokens")
        ),
        "context_window_source": (
            overrides.get("context_window_source")
            if overrides.get("context_window_source") is not None
            else extra.get("context_window_source")
        ),
        "status": overrides.get("status") or "completed",
        "returncode": overrides.get("returncode"),
        "created_at": created_at,
        "started_at": overrides.get("started_at") or created_at,
        "finished_at": finished_at,
    }


def _result_type_from_dir(report_dir: Path) -> str:
    name = report_dir.name
    if "staircase" in report_dir.as_posix() or name == "measure":
        return "staircase"
    if "model_sweep" in report_dir.as_posix():
        return "model_sweep"
    if "soak" in report_dir.as_posix() or report_dir.name == "run":
        return "soak"
    return "quick_load"


def _profile_stats(records: list[RequestRecord]) -> list[dict[str, Any]]:
    duration_sec = _records_duration(records)
    rows = [
        _profile_stat_row(name, items, duration_sec)
        for name, items in _records_by_name(records).items()
    ]
    total = _profile_stat_row("Aggregated", records, duration_sec)
    return rows + [total]


def _records_by_name(records: list[RequestRecord]) -> dict[str, list[RequestRecord]]:
    groups: dict[str, list[RequestRecord]] = {}
    for item in records:
        groups.setdefault(item.task_name, []).append(item)
    return dict(sorted(groups.items()))


def _profile_stat_row(
    name: str, records: list[RequestRecord], duration_sec: float
) -> dict[str, Any]:
    latencies = [
        float(item.latency_ms or 0) for item in records if item.latency_ms is not None
    ]
    failures = [item for item in records if not item.success]
    status_counter = Counter(
        item.status_code for item in records if item.status_code is not None
    )
    failure_counter = Counter(
        item.failure_classification or item.error_type or "unknown" for item in failures
    )
    count = len(records)
    failure_count = len(failures)
    minutes = max(duration_sec / 60.0, 1 / 60.0)
    seconds = max(duration_sec, 1.0)
    return {
        "name": name,
        "request_count": count,
        "failure_count": failure_count,
        "success_rate": (count - failure_count) / count if count else 0,
        "median_ms": percentile(latencies, 50),
        "avg_ms": sum(latencies) / len(latencies) if latencies else None,
        "min_ms": min(latencies) if latencies else None,
        "max_ms": max(latencies) if latencies else None,
        "rpm": (count - failure_count) / minutes,
        "rps": (count - failure_count) / seconds,
        "failures_per_sec": failure_count / seconds,
        "p90_ms": percentile(latencies, 90),
        "p95_ms": percentile(latencies, 95),
        "p99_ms": percentile(latencies, 99),
        "status_code_counts": dict(status_counter),
        "failure_classification_counts": dict(failure_counter),
    }


def _records_duration(records: list[RequestRecord]) -> float:
    configured = [
        item.extra.get("measure_duration_sec")
        for item in records
        if isinstance(item.extra, dict)
        and item.extra.get("measure_duration_sec") is not None
    ]
    if configured:
        try:
            duration = float(Counter(configured).most_common(1)[0][0])
        except (TypeError, ValueError):
            duration = 0.0
        if duration > 0:
            return duration
    timestamps = [item.timestamp for item in records if item.timestamp]
    if len(timestamps) < 2:
        return 1.0
    return max(max(timestamps) - min(timestamps), 1.0)


def _load_history_rows(report_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(report_dir.rglob("history.jsonl")):
        rows.extend(load_history(path))
    return rows[-200:]


def _load_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": result.get("id"),
        "title": result.get("title"),
        "type": result.get("type"),
        "provider": result.get("provider"),
        "provider_label": result.get("provider_label"),
        "model": result.get("model"),
        "model_family": result.get("model_family"),
        "workload": result.get("workload"),
        "status": result.get("status"),
        "returncode": result.get("returncode"),
        "created_at": result.get("created_at"),
        "finished_at": result.get("finished_at"),
        "target_rpm": result.get("target_rpm"),
        "target_tpm": result.get("target_tpm"),
        "target_tokens_per_request": result.get("target_tokens_per_request"),
        "summary": result.get("summary"),
    }


def _format_time(timestamp: float | None) -> str:
    if not timestamp:
        return "unknown"
    return datetime.fromtimestamp(float(timestamp), timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%SZ"
    )


def _public_image_results(job: Job, raw_results: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_results, list):
        return []
    report_root = job.report_dir.resolve()
    reports_root = REPORTS_ROOT.resolve()
    public: list[dict[str, Any]] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        artifact_urls: list[str] = []
        for artifact in raw.get("artifacts") or []:
            value = str(artifact or "")
            relative = Path(value)
            if not value or relative.is_absolute() or ".." in relative.parts:
                continue
            target = (report_root / relative).resolve()
            try:
                target.relative_to(report_root)
                report_relative = target.relative_to(reports_root)
            except ValueError:
                continue
            if not target.is_file() or target.suffix.lower() not in {
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
            }:
                continue
            artifact_urls.append(
                "/reports/" + quote(report_relative.as_posix(), safe="/")
            )
        item["artifact_urls"] = artifact_urls
        public.append(item)
    return public


def _image_job_progress(
    job: Job,
    plan: Any,
    results: list[dict[str, Any]],
    summary: Any,
) -> dict[str, Any]:
    raw_cases = plan.get("cases") if isinstance(plan, dict) else None
    case_names = [
        str(item.get("name") if isinstance(item, dict) else item)
        for item in (raw_cases or (job.image_plan or {}).get("cases") or [])
        if (isinstance(item, str) and item)
        or (isinstance(item, dict) and item.get("name"))
    ]
    summary_case_count = (
        int(summary.get("case_count") or 0) if isinstance(summary, dict) else 0
    )
    total = (
        len(case_names)
        or int((job.image_plan or {}).get("estimated_case_count") or 0)
        or summary_case_count
    )
    completed = len(results) or summary_case_count
    passed = sum(
        1
        for item in results
        if item.get("overall_pass", item.get("pass")) is True
    )
    failed = sum(
        1
        for item in results
        if item.get("overall_pass", item.get("pass")) is False
    )
    if isinstance(summary, dict) and not results:
        passed = int(summary.get("pass_count") or 0)
        failed = int(summary.get("failure_count") or 0)
    percent = int(min(completed, total) * 100 / total) if total else 0
    if isinstance(summary, dict) and completed >= total:
        percent = 100
    current_case = (
        case_names[completed]
        if job.status in {"queued", "running", "stopping"}
        and completed < len(case_names)
        else None
    )
    last_latency = results[-1].get("latency_ms") if results else None
    if job.status in {"completed", "failed", "stopped"}:
        label = f"{completed}/{total or completed} cases · {job.status}"
        detail = f"passed {passed}, failed {failed}"
    else:
        label = f"{completed}/{total or '?'} cases"
        detail = f"current {current_case}" if current_case else "等待图片测试计划/结果"
    return {
        "percent": percent,
        "label": label,
        "detail": detail,
        "completed_cases": completed,
        "total_cases": total,
        "pass_count": passed,
        "failure_count": failed,
        "current_case": current_case,
        "last_latency_ms": last_latency,
    }


def _workflow_progress_json(path: Path) -> Any:
    """Read bounded local evidence; never follow an evidence symlink."""
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("unsafe_evidence_path")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("invalid_evidence_file")
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("invalid_evidence_object")
    return value


def _workflow_progress_run(raw: dict[str, Any], plan: dict[str, Any], *, ledger: bool) -> dict[str, Any]:
    """Validate ownership and return only public counters and enum-valued states."""
    if not isinstance(raw, dict):
        raise ValueError("run_evidence_invalid")
    steps_by_id = {step["id"]: step for step in plan["ordered_steps"]}
    terminal_steps = ("passed", "failed", "blocked", "skipped", "inconclusive", "cancelled")
    run_id, index = raw.get("run_id"), raw.get("run_index")
    if (not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", run_id)
            or type(index) is not int or not 1 <= index <= plan["run_count"]):
        raise ValueError("run_identity_invalid")
    status = raw.get("status")
    phase = raw.get("phase") if ledger else "complete"
    if (status not in ("running", "passed", "failed", "cancelled", "incomplete")
            or phase not in ("business", "cleanup", "complete")
            or (status == "running") != (phase != "complete")):
        raise ValueError("run_state_invalid")
    steps = raw.get("steps")
    if not isinstance(steps, dict) or not set(steps) <= set(steps_by_id):
        raise ValueError("steps_invalid")
    for step_id, result in steps.items():
        if (not isinstance(result, dict) or result.get("status") not in terminal_steps
                or result.get("status") == "passed" and type(result.get("accepted")) is not bool):
            raise ValueError("step_state_invalid")
        blocked = result.get("blocked_dependencies", [])
        if (not isinstance(blocked, list) or any(not isinstance(item, str) for item in blocked)
                or not set(blocked) <= set(steps_by_id[step_id]["depends_on"])):
            raise ValueError("blocked_dependencies_invalid")
    if status == "passed" and (set(steps) != set(steps_by_id)
                               or any(step["status"] != "passed" for step in steps.values())):
        raise ValueError("passed_run_steps_incomplete")

    attempts = raw.get("attempts")
    if not isinstance(attempts, list):
        raise ValueError("attempts_invalid")
    public_attempts = []
    attempt_counts: Counter[str] = Counter()
    phase_counts: Counter[str] = Counter()
    for number, attempt in enumerate(attempts, 1):
        if (not isinstance(attempt, dict) or attempt.get("id") != f"{run_id}:attempt:{number}"
                or attempt.get("step_id") not in steps_by_id
                or attempt.get("phase") not in ("business", "cleanup")
                or attempt.get("status") not in ("dispatching", "received", "exception")):
            raise ValueError("attempt_identity_or_state_invalid")
        item = {key: attempt[key] for key in ("id", "step_id", "phase", "status")}
        receipt = attempt.get("receipt")
        code = receipt.get("status_code", receipt.get("http_status")) if isinstance(receipt, dict) else None
        if type(code) is int and 100 <= code <= 599:
            item["http_status"] = code
        public_attempts.append(item)
        phase_counts[item["phase"]] += 1
        attempt_counts[item["status"]] += 1
    recovery = raw.get("recovery_count", 0) if ledger else raw.get("previous_attempt_count", 0)
    if type(recovery) is not int or recovery < 0:
        raise ValueError("recovery_count_invalid")
    if status == "passed" and attempt_counts["dispatching"] and not recovery:
        raise ValueError("passed_run_has_unresolved_attempt")
    if (phase_counts["business"] > plan["limits"]["max_requests"]
            or not recovery and phase_counts["cleanup"] > plan["limits"]["cleanup_max_requests"]):
        raise ValueError("request_budget_exceeded")
    if not ledger:
        business, cleanup = raw.get("business_request_count"), raw.get("cleanup_request_count")
        if (type(business) is not int or type(cleanup) is not int or min(business, cleanup) < 0
                or business > plan["limits"]["max_requests"] or cleanup > plan["limits"]["cleanup_max_requests"]
                or type(raw.get("request_count")) is not int or raw["request_count"] != business + cleanup
                or len(attempts) != business + cleanup + recovery
                or not recovery and (business != phase_counts["business"] or cleanup != phase_counts["cleanup"])):
            raise ValueError("request_counts_invalid")

    cleanup_result = raw.get("cleanup") if not ledger else None
    if not ledger and (not isinstance(cleanup_result, dict)
                       or cleanup_result.get("status") not in ("passed", "incomplete")):
        raise ValueError("cleanup_state_invalid")
    resources = raw.get("resources") if ledger else cleanup_result.get("resources")
    creations = raw.get("creations") if ledger else cleanup_result.get("unknown_creations")
    if not isinstance(resources, list) or not isinstance(creations, list):
        raise ValueError("cleanup_evidence_invalid")
    resource_counts: Counter[str] = Counter()
    for resource in resources:
        if (not isinstance(resource, dict) or resource.get("run_id") != run_id
                or resource.get("step_id") not in steps_by_id
                or resource.get("status") not in ("registered", "cleaning", "deleted", "cleanup_failed", "cleanup_unknown")):
            raise ValueError("resource_ownership_or_state_invalid")
        resource_counts[resource["status"]] += 1
    unknown = 0
    attempt_ids = {attempt["id"] for attempt in public_attempts}
    for creation in creations:
        if (not isinstance(creation, dict) or creation.get("run_id") != run_id
                or creation.get("step_id") not in steps_by_id
                or creation.get("status") not in (("pending", "known", "rejected", "unknown") if ledger else ("pending", "unknown"))
                or creation.get("attempt_id") not in attempt_ids):
            raise ValueError("creation_ownership_or_state_invalid")
        unknown += creation["status"] in ("pending", "unknown")
    cleanup_failed = unknown or resource_counts["deleted"] != len(resources)
    if phase == "complete":
        cleanup_status = "incomplete" if cleanup_failed or cleanup_result and cleanup_result["status"] != "passed" else "passed"
    else:
        cleanup_status = "running" if phase == "cleanup" else "pending"

    current = None
    active = raw.get("active_request") if ledger else None
    if active is not None:
        if (not isinstance(active, dict) or active.get("step_id") not in steps_by_id
                or active.get("phase") not in ("business", "cleanup") or phase == "complete"
                or not public_attempts or public_attempts[-1]["status"] != "dispatching"
                or any(active[key] != public_attempts[-1][key] for key in ("step_id", "phase"))):
            raise ValueError("active_request_invalid")
        current = active["step_id"]
    if current is None and phase == "business":
        events = raw.get("events", [])
        if not isinstance(events, list):
            raise ValueError("events_invalid")
        for event in reversed(events):
            if isinstance(event, dict) and event.get("name") == "step_inputs":
                if event.get("step_id") in steps_by_id and event["step_id"] not in steps:
                    current = event["step_id"]
                break

    public_steps = []
    step_counts: Counter[str] = Counter()
    for step_id, definition in steps_by_id.items():
        result = steps.get(step_id, {})
        state = result.get("status", "running" if step_id == current and phase == "business" else "pending")
        item = {"id": step_id, "case_id": definition["case_id"], "status": state}
        if "accepted" in result and type(result["accepted"]) is bool:
            item["accepted"] = result["accepted"]
        if result.get("blocked_dependencies"):
            item["blocked_dependencies"] = list(result["blocked_dependencies"])
        public_steps.append(item)
        step_counts[state] += 1
    public_cases = []
    case_counts: Counter[str] = Counter()
    for case_id in plan["selected_cases"]:
        states = [step["status"] for step in public_steps if step["case_id"] == case_id]
        if "pending" in states or "running" in states:
            state = "pending" if all(value == "pending" for value in states) else "running"
        else:
            state = next(value for value in ("failed", "blocked", "cancelled", "inconclusive", "skipped", "passed") if value in states)
        public_cases.append({"id": case_id, "status": state})
        case_counts[state] += 1
    return {"run_id": run_id, "run_index": index, "status": status, "phase": phase,
            "steps": public_steps, "cases": public_cases, "step_counts": dict(step_counts),
            "case_counts": dict(case_counts), "attempts": public_attempts,
            "attempt_counts": dict(attempt_counts), "attempt_count": len(attempts),
            "business_request_count": phase_counts["business"], "cleanup_request_count": phase_counts["cleanup"],
            "cleanup_status": cleanup_status, "resource_count": len(resources),
            "deleted_resource_count": resource_counts["deleted"], "unknown_creation_count": unknown,
            "cleanup_failed_resource_count": resource_counts["cleanup_failed"] + resource_counts["cleanup_unknown"],
            "current_step": current, "current_case": steps_by_id[current]["case_id"] if current else None,
            "cleanup_only": recovery > 0 or raw.get("cleanup_only") is True}


def _workflow_progress_policy_run(raw: dict[str, Any], public: dict[str, Any], *, ledger: bool) -> dict[str, Any]:
    """Keep only the evidence needed to recompute frozen all/any acceptance."""
    steps = {}
    for step_id, result in raw["steps"].items():
        steps[step_id] = {key: result[key] for key in ("status", "accepted", "aggregate_eligible", "aggregation_key") if key in result}
        if result.get("error"):
            steps[step_id]["error"] = True
    resources = raw["resources"] if ledger else raw["cleanup"]["resources"]
    return {"run_id": public["run_id"], "run_index": public["run_index"],
            "status": public["status"], "steps": steps,
            "fatal_reason": bool(raw.get("fatal_reason")), "cleanup_only": public["cleanup_only"],
            "cleanup": {"status": public["cleanup_status"],
                        "resources": [{"run_id": row["run_id"], "status": row["status"]} for row in resources],
                        "unknown_creations": [{}] * public["unknown_creation_count"]}}


def _workflow_job_progress(job: Job, verdict: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Use frozen workflow units and owned durable evidence, never reference cells."""
    from lib.parameter_job_controls import workflow_execution_from_job
    from lib.test_runner import digest_json, summarize_plan_runs

    spec = job.job_spec if isinstance(job.job_spec, dict) else {}
    errors: list[str] = []
    try:
        saved = _workflow_progress_json(job.report_dir / "job_spec.json")
    except (OSError, ValueError, TypeError):
        saved = None
        if spec.get("schema_version") == WORKFLOW_JOB_SPEC_VERSION:
            errors.append("frozen_job_spec_unreadable")
    if spec.get("schema_version") != WORKFLOW_JOB_SPEC_VERSION and (saved or {}).get("schema_version") != WORKFLOW_JOB_SPEC_VERSION:
        return None
    spec = saved if saved is not None else spec
    try:
        if errors or workflow_execution_from_job(spec) is None:
            raise ValueError("invalid_frozen_workflow")
        plan = spec["execution_plan"]
    except (KeyError, TypeError, ValueError):
        return {"workflow": True, "percent": 0, "label": f"{job.status} · invalid workflow evidence",
                "detail": "Frozen workflow plan is invalid or unreadable.", "status": "invalid",
                "pass": False, "evidence_status": "invalid", "evidence_errors": ["frozen_job_spec_invalid"],
                "cleanup_status": "unknown", "runs": []}

    runs: dict[int, dict[str, Any]] = {}
    policy_runs: dict[int, dict[str, Any]] = {}
    invalid_indices: set[int] = set()
    base = job.report_dir / "workflow_runs"
    try:
        if base.is_symlink():
            raise ValueError("unsafe_ledger_directory")
        paths = sorted(base.glob("*/ledger.json")) if base.is_dir() else []
        for path in paths:
            try:
                raw = _workflow_progress_json(path)
                if raw is None:
                    raise ValueError("missing_ledger")
                claimed = raw.pop("ledger_digest", None)
                if claimed != digest_json(raw):
                    raise ValueError("ledger_digest_mismatch")
                if (type(raw.get("ledger_schema_version")) is not int or raw["ledger_schema_version"] != 1
                        or raw.get("plan_digest") != plan["plan_digest"]
                        or digest_json(raw.get("target")) != digest_json(plan["target"])
                        or raw.get("run_id") != path.parent.name):
                    raise ValueError("ledger_ownership_mismatch")
                run = _workflow_progress_run(raw, plan, ledger=True)
                index = run["run_index"]
                if index in runs or index in invalid_indices:
                    runs.pop(index, None)
                    policy_runs.pop(index, None)
                    invalid_indices.add(index)
                    raise ValueError("duplicate_run_index")
                runs[index] = run
                policy_runs[index] = _workflow_progress_policy_run(raw, run, ledger=True)
            except (KeyError, OSError, ValueError, TypeError):
                errors.append("ledger_invalid_or_unowned")
    except (OSError, ValueError):
        errors.append("ledger_directory_unreadable")

    report_status = None
    report = verdict.get("workflow_result") if isinstance(verdict, dict) else None
    try:
        if report is None:
            report = _workflow_progress_json(job.report_dir / "workflow_result.json")
        if report is None:
            output = _workflow_progress_json(job.report_dir / ("summary.json" if job.type == "image_param_test" else "verdict.json"))
            report = output.get("workflow_result") if output else None
        if report is not None:
            if (not isinstance(report, dict) or type(report.get("report_schema_version")) is not int
                    or report["report_schema_version"] != 1
                    or report.get("plan_digest") != plan["plan_digest"]
                    or report.get("workflow_id") != plan["workflow_id"]
                    or digest_json(report.get("target")) != digest_json(plan["target"])
                    or report.get("status") not in ("passed", "failed", "cancelled", "incomplete")
                    or report.get("cleanup_only") is True or not isinstance(report.get("runs"), list)):
                raise ValueError("final_result_identity_invalid")
            report_runs = [_workflow_progress_run(raw, plan, ledger=False) for raw in report["runs"]]
            if (not report_runs or len({run["run_id"] for run in report_runs}) != len(report_runs)
                    or [run["run_index"] for run in report_runs] != list(range(1, len(report_runs) + 1))
                    or type(report.get("requested_run_count", plan["run_count"])) is not int
                    or report.get("requested_run_count", plan["run_count"]) != plan["run_count"]
                    or type(report.get("completed_run_count", len(report_runs))) is not int
                    or report.get("completed_run_count", len(report_runs)) != len(report_runs)
                    or report["status"] == "passed" and len(report_runs) != plan["run_count"]):
                raise ValueError("final_result_runs_invalid")
            policy_reports = [_workflow_progress_policy_run(raw, run, ledger=False)
                              for raw, run in zip(report["runs"], report_runs)]
            report_outcome = summarize_plan_runs(plan, policy_reports)
            if ("case_outcomes" in report and report["case_outcomes"] != report_outcome["case_outcomes"]
                    or "any" in plan["case_policies"].values() and "case_outcomes" not in report):
                raise ValueError("final_result_case_outcomes_invalid")
            # Preserve valid individual evidence when an overall PASS overlooks
            # cleanup or case-policy failure. The result classifier separately
            # flags the inconsistent envelope; progress keeps the real counts.
            report_status = report_outcome["status"] if report["status"] == "passed" else report["status"]
            for run, policy_run in zip(report_runs, policy_reports):
                index = run["run_index"]
                if index in invalid_indices:
                    continue
                if index in runs and runs[index]["run_id"] != run["run_id"]:
                    runs.pop(index)
                    policy_runs.pop(index, None)
                    invalid_indices.add(index)
                    errors.append("final_result_run_identity_conflict")
                else:
                    runs.setdefault(index, run)
                    policy_runs.setdefault(index, policy_run)
    except (KeyError, OSError, ValueError, TypeError):
        errors.append("final_workflow_result_invalid")

    public_runs = [runs[index] for index in sorted(runs)]
    state_names = ("pending", "running", "passed", "failed", "blocked", "skipped", "inconclusive", "cancelled")
    step_counts = Counter({state: 0 for state in state_names})
    case_counts = Counter({state: 0 for state in state_names})
    attempt_counts = Counter({state: 0 for state in ("dispatching", "received", "exception")})
    for run in public_runs:
        step_counts.update(run["step_counts"])
        case_counts.update(run["case_counts"])
        attempt_counts.update(run["attempt_counts"])
    total_steps = len(plan["ordered_steps"]) * plan["run_count"]
    total_cases = len(plan["selected_cases"]) * plan["run_count"]
    step_counts["pending"] += total_steps - sum(step_counts.values())
    case_counts["pending"] += total_cases - sum(case_counts.values())
    completed_steps = total_steps - step_counts["pending"] - step_counts["running"]
    completed_cases = total_cases - case_counts["pending"] - case_counts["running"]
    completed_runs = sum(run["phase"] == "complete" for run in public_runs)
    cleanups = {run["cleanup_status"] for run in public_runs}
    cleanup_status = next((state for state in ("incomplete", "running", "pending", "passed") if state in cleanups),
                          "unknown" if job.status in {"completed", "failed", "stopped"} else "pending")
    if errors and cleanup_status == "passed":
        cleanup_status = "unknown"
    aggregate = summarize_plan_runs(plan, [policy_runs.get(index, {}) for index in range(1, plan["run_count"] + 1)])
    successful = (not errors and completed_runs == plan["run_count"] and cleanup_status == "passed"
                  and aggregate["status"] == "passed"
                  and report_status in (None, "passed") and job.status not in {"stopping", "stopped", "failed"})
    if job.status in {"stopping", "stopped"}:
        status = job.status
    elif errors:
        status = "invalid"
    elif cleanup_status == "incomplete":
        status = "incomplete"
    elif successful:
        status = "passed"
    elif report_status:
        status = report_status if report_status != "passed" else "incomplete"
    elif any(run["cleanup_only"] for run in public_runs):
        status = "incomplete"
    elif completed_runs == plan["run_count"] and any(run["status"] == "failed" for run in public_runs):
        status = "failed"
    elif job.status == "completed":
        status = "incomplete"
    else:
        status = job.status
    current_run = next((run for run in reversed(public_runs) if run["current_step"]), {})
    counts = {key: sum(run[key] for run in public_runs) for key in (
        "attempt_count", "business_request_count", "cleanup_request_count", "resource_count",
        "deleted_resource_count", "unknown_creation_count", "cleanup_failed_resource_count")}
    detail = (f"{completed_cases}/{total_cases} cases · {completed_steps}/{total_steps} steps · "
              f"{counts['attempt_count']} attempts · blocked {step_counts['blocked']} · cleanup {cleanup_status}")
    if errors:
        detail += " · invalid or unowned evidence ignored"
    return {"workflow": True, "percent": int(completed_steps * 100 / total_steps) if total_steps else 0,
            "label": f"{completed_cases}/{total_cases} cases · {status}", "detail": detail,
            "status": status, "pass": successful, "evidence_status": "invalid" if errors else "verified" if public_runs else "pending",
            "evidence_errors": sorted(set(errors)), "plan_digest": plan["plan_digest"],
            "requested_cases": list(plan["requested_cases"]), "selected_cases": list(plan["selected_cases"]),
            "case_policies": copy.deepcopy(plan["case_policies"]), "case_outcomes": aggregate["case_outcomes"],
            "total_runs": plan["run_count"], "completed_runs": completed_runs,
            "total_cases": total_cases, "completed_cases": completed_cases,
            "passed_cases": case_counts["passed"], "failed_cases": case_counts["failed"], "blocked_cases": case_counts["blocked"],
            "total_steps": total_steps, "completed_steps": completed_steps,
            "passed_steps": step_counts["passed"], "failed_steps": step_counts["failed"], "blocked_steps": step_counts["blocked"],
            "pass_count": case_counts["passed"], "failure_count": case_counts["failed"],
            "step_counts": dict(step_counts), "case_counts": dict(case_counts), "attempt_counts": dict(attempt_counts),
            "cleanup_status": cleanup_status, "current_step": current_run.get("current_step"),
            "current_case": current_run.get("current_case"), "runs": public_runs, **counts}


def _job_progress(
    job: Job,
    summary: dict[str, Any] | None,
    verdict: dict[str, Any] | None,
    param_results: Any,
    cache_progress: dict[str, Any] | None,
) -> dict[str, Any]:
    workflow_progress = _workflow_job_progress(job, verdict)
    if workflow_progress is not None:
        return workflow_progress

    if job.status in {"completed", "failed", "stopped"}:
        progress = {
            "percent": 100,
            "label": job.status,
            "detail": _progress_detail(job, summary, verdict, param_results),
        }
        if job.type == "param_test":
            progress.update(_param_progress_counts(job, param_results))
        return progress

    if job.type == "param_test":
        counts = _param_progress_counts(job, param_results)
        total = int(counts["supported_total"])
        completed = int(counts["supported_completed"])
        percent = int(completed * 100 / total) if total else 0
        return {
            "percent": percent,
            "label": f"{completed}/{total} reference cells",
            "detail": "参数兼容性测试",
            **counts,
        }

    if job.type == "cache_suite":
        if cache_progress:
            return {
                "percent": int(cache_progress.get("percent") or 0),
                "label": str(cache_progress.get("label") or "cache suite"),
                "detail": str(cache_progress.get("phase") or "cache suite"),
            }
        total_steps = int(
            (job.cache_plan or {}).get("estimated_request_count")
            or _cache_total_steps(job.cache_measured_requests)
        )
        record_count = _record_count(job.report_dir)
        percent = (
            int(min(record_count, total_steps) * 100 / total_steps)
            if total_steps
            else 0
        )
        return {
            "percent": percent,
            "label": f"{min(record_count, total_steps)}/{total_steps} steps",
            "detail": "Cache suite",
        }

    duration_sec = _job_duration_seconds(job)
    if duration_sec and job.started_at:
        elapsed = max(time.time() - job.started_at, 0)
        percent = int(min(elapsed / duration_sec, 0.99) * 100)
        return {
            "percent": percent,
            "label": f"{int(elapsed)}/{int(duration_sec)} sec",
            "detail": "压测进行中",
        }

    return {"percent": 0, "label": job.status, "detail": "等待进度数据"}


def _progress_detail(
    job: Job,
    summary: dict[str, Any] | None,
    verdict: dict[str, Any] | None,
    param_results: Any,
) -> str:
    if job.type == "param_test" and isinstance(param_results, list):
        passed = sum(
            1
            for item in param_results
            if item.get("status") == "pass"
            and item.get("overall_pass", item.get("pass")) is True
        )
        failed = sum(
            1
            for item in param_results
            if item.get("overall_pass", item.get("pass")) is False
        )
        incompatible = sum(
            1 for item in param_results if item.get("status") == "incompatible"
        )
        unexpected = sum(
            1 for item in param_results if item.get("status") == "unexpected_acceptance"
        )
        expected_rejection = sum(
            1 for item in param_results if item.get("status") == "expected_rejection"
        )
        return (
            f"passed {passed}, expected_rejection {expected_rejection}, "
            f"incompatible {incompatible}, unexpected_acceptance {unexpected}, failed {failed}"
        )
    if verdict:
        return "pass" if verdict.get("pass") else "failed"
    if summary:
        return f"records {summary.get('record_count', 0)}"
    return job.status


def _param_progress_counts(job: Job, param_results: Any) -> dict[str, int]:
    results = param_results if isinstance(param_results, list) else []
    if job.job_spec.get("schema_version") == WORKFLOW_JOB_SPEC_VERSION:
        plan = job.job_spec.get("execution_plan") or {}
        total = len(plan.get("selected_cases") or []) * int(plan.get("run_count") or 1)
        return {"supported_completed": len(results), "supported_total": total, "total_cells": total}
    total = _reference_profile_count(job.reference_source) * max(
        int(job.param_test_runs or 1), 1
    )
    if job.job_spec.get("parameter_suite"):
        from lib.fixed_parameter_specs import build_fixed_plan
        plan = build_fixed_plan(job.job_spec.get("model_profile_database"),
            suite_id=job.job_spec["parameter_suite"], job_type=job.type, runs=job.param_test_runs,
            frozen_plan=job.job_spec.get("fixed_parameter_plan"))
        total = plan.request_cap
    return {
        "supported_completed": len(results),
        "supported_total": total,
        "total_cells": total,
    }


def _reference_profile_count(reference_source: str | None) -> int:
    try:
        if not reference_source:
            return 0
        return len(test_profiles_for_reference(reference_source))
    except Exception:
        return 0


def _job_param_profiles(job: Job) -> set[str]:
    verdict = _read_json(job.report_dir / "verdict.json")
    param_specs = verdict.get("param_specs") if isinstance(verdict, dict) else {}
    profiles = param_specs.get("test_profiles") if isinstance(param_specs, dict) else []
    return {str(profile) for profile in profiles or []}


def _cache_total_steps(measured_requests: int | None = None) -> int:
    cache_cfg = load_config().get("cache_test") or {}
    warmup = int(cache_cfg.get("warmup_requests", 2))
    measured = int(
        measured_requests
        if measured_requests is not None
        else cache_cfg.get("measured_requests", cache_cfg.get("repeat_count", 50))
    )
    wait = 1 if float(cache_cfg.get("wait_after_warmup_sec", 5)) > 0 else 0
    return warmup + wait + measured


def _historical_cache_measured_requests(
    report_dir: Path,
    config: dict[str, Any],
    *,
    verdict: dict[str, Any] | None = None,
    job_spec: dict[str, Any] | None = None,
) -> int:
    if _job_type_from_report_dir(report_dir) == "cache_suite":
        estimated = int(
            (((job_spec or {}).get("cache_plan") or {}).get("estimated_request_count"))
            or 0
        )
        if estimated > 0:
            return estimated
        summary = (verdict or {}).get("summary") or {}
        recorded = int(
            summary.get("business_record_count") or summary.get("record_count") or 0
        )
        if recorded > 0:
            return recorded
        measured = sum(
            1
            for record in _load_result_records(report_dir)
            if record.group == "cache_profiles" and not record.is_warmup
        )
        if measured > 0:
            return measured
    return _resolve_cache_measured_requests(config, None)


def _record_count(report_dir: Path) -> int:
    count = 0
    for path in sorted(report_dir.rglob("request_records.jsonl")):
        count += len(load_records(path))
    return count


def _job_duration_seconds(job: Job) -> float | None:
    try:
        if job.type == "quick_load" and job.duration:
            return parse_duration_seconds(job.duration)
        if job.type == "staircase":
            config = load_config()
            staircase_cfg = job.staircase_plan or config.get("staircase") or {}
            warmup_cfg = staircase_cfg.get("warmup") or config.get("warmup") or {}
            step_duration = parse_duration_seconds(
                str(staircase_cfg.get("step_duration", "5m"))
            )
            step_count = len(staircase_cfg.get("steps", []))
            warmup_duration = (
                parse_duration_seconds(str(warmup_cfg.get("duration", "1m")))
                if warmup_cfg.get("enabled")
                else 0
            )
            return step_count * step_duration + (
                step_count * warmup_duration
                if warmup_cfg.get("per_step")
                else warmup_duration
            )
        if job.type == "soak" and job.soak_plan:
            return parse_duration_seconds(str(job.soak_plan.get("duration") or "1h"))
    except Exception:
        return None
    return None


def _job_type_from_report_dir(report_dir: Path) -> str | None:
    job_spec = _read_json(report_dir / "job_spec.json")
    if isinstance(job_spec, dict):
        spec_type = str(job_spec.get("type") or "")
        if spec_type in SUPPORTED_JOB_TYPES:
            return spec_type
    name = report_dir.name
    if "_image_param_test_" in name:
        return "image_param_test"
    if "_trace_test_" in name:
        return "trace_test"
    if "_param_test_" in name:
        return "param_test"
    if "_cache_suite_" in name:
        return "cache_suite"
    if "_staircase_" in name:
        return "staircase"
    if "_soak_" in name:
        return "soak"
    if "_quick_load_" in name:
        return "quick_load"
    return None


def _report_files(report_dir: Path) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for path in sorted(report_dir.rglob("*")):
        if not path.is_file() or path.suffix not in {
            ".html",
            ".json",
            ".jsonl",
            ".log",
        }:
            continue
        rel = path.relative_to(REPORTS_ROOT)
        files.append(
            {
                "name": str(path.relative_to(report_dir)),
                "url": f"/reports/{rel.as_posix()}",
            }
        )
        if len(files) >= 20:
            break
    return files


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _first_request_record(report_dir: Path) -> RequestRecord | None:
    for path in sorted(report_dir.rglob("request_records.jsonl")):
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    return RequestRecord.from_dict(json.loads(line))
    return None


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _tail(path: Path, max_chars: int = 6000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-max_chars:]


def _validate_model(provider_cfg: dict[str, Any], model: str) -> None:
    candidates = (provider_cfg.get("models") or {}).get("candidates") or []
    if candidates and model not in candidates:
        raise ValueError(
            f"Model {model!r} is not configured for provider {provider_cfg.get('name')!r}."
        )


def _preflight_job(
    config: dict[str, Any],
    provider: str,
    model: str,
    job_type: str,
    workload: str,
    reference_source: str,
    api_form: str,
    route_profile: str,
    model_profile_database: dict[str, Any] | None = None,
) -> None:
    transport = get_model_transport(
        config,
        model,
        provider,
        route_profile=route_profile,
        api_form=api_form,
    )
    get_provider_interface(config, transport, provider)
    if job_type not in {"quick_load", "staircase", "soak"}:
        return
    preflight_config = copy.deepcopy(config)
    if model_profile_database is not None:
        preflight_config["_model_profile_database"] = copy.deepcopy(model_profile_database)
    preflight_config["active_provider"] = provider
    preflight_config["providers"][provider]["models"]["default"] = model
    preflight_models = preflight_config["providers"][provider]["models"]
    preflight_models.setdefault("default_routes", {})[model] = route_profile
    preflight_models.setdefault("default_api_forms", {}).setdefault(model, {})[
        route_profile
    ] = api_form
    for group, profile, _weight in weighted_workload_profiles(
        preflight_config,
        workload,
        api_form=api_form,
        route_profile=route_profile,
        reference_source=reference_source,
    ):
        if group == "control":
            continue
        built = build_request(
            preflight_config,
            group,
            profile,
            api_form_override=api_form,
            route_profile_override=route_profile,
            reference_source=reference_source,
        )
        get_provider_interface(
            config,
            str(built.metadata.get("transport") or transport),
            provider,
        )


def _new_job_id(job_type: str, provider: str, model: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{job_type}_{provider}_{model}")
    return f"{stamp}_{safe}_{uuid.uuid4().hex[:8]}"


def _resolve_timeout_sec(config: dict[str, Any], value: Any) -> int:
    requested = _optional_int(value)
    if requested is not None:
        return max(requested, 1)
    return get_timeout_sec(config)


def _resolve_image_timeout_sec(config: dict[str, Any], value: Any) -> int:
    if value in (None, ""):
        return get_timeout_sec(config)
    try:
        requested = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_sec must be an integer.") from exc
    if requested <= 0:
        raise ValueError("timeout_sec must be positive.")
    return requested


def _resolve_cache_measured_requests(config: dict[str, Any], value: Any) -> int:
    cache_cfg = config.get("cache_test") or {}
    default = int(cache_cfg.get("measured_requests", cache_cfg.get("repeat_count", 50)))
    requested = default if value in (None, "") else int(value)
    if requested < 1 or requested > MAX_CACHE_MEASURED_REQUESTS:
        raise ValueError(
            "cache_measured_requests must be between "
            f"1 and {MAX_CACHE_MEASURED_REQUESTS}"
        )
    return requested


def _resolve_target_rpm(config: dict[str, Any], value: Any) -> float:
    if value in (None, ""):
        return _default_target_rpm(config)
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return _default_target_rpm(config)


def _default_target_rpm(config: dict[str, Any]) -> float:
    thresholds = config.get("thresholds", {}).get("staircase", {})
    try:
        return max(
            float(thresholds.get("target_business_rpm_min", DEFAULT_TARGET_RPM)), 0.0
        )
    except (TypeError, ValueError):
        return DEFAULT_TARGET_RPM


def _resolve_target_tpm(config: dict[str, Any], value: Any) -> float:
    if value in (None, ""):
        return _default_target_tpm(config)
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return _default_target_tpm(config)


def _default_target_tpm(config: dict[str, Any]) -> float:
    thresholds = config.get("thresholds", {}).get("staircase", {})
    try:
        return max(
            float(thresholds.get("target_total_tpm_min", DEFAULT_TARGET_TPM)), 0.0
        )
    except (TypeError, ValueError):
        return DEFAULT_TARGET_TPM


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _remaining_workflow_request_timeout(job: Job, spec: dict[str, Any]) -> float | None:
    """Read the newest owned durable deadline; unknown telemetry keeps the full bound."""
    from lib.test_runner import digest_json
    base = job.report_dir / "workflow_runs"
    if not base.is_dir() or base.is_symlink():
        return None
    try:
        candidates = [path for path in base.glob("*/ledger.json")
                      if not path.is_symlink() and not path.parent.is_symlink()]
        if not candidates:
            return None
        path = max(candidates, key=lambda item: item.stat().st_mtime_ns)
        # Large evidence files are not needed to request cooperative cancellation.
        if path.stat().st_size > 64 * 1024 * 1024:
            return None
        ledger = _read_json(path)
        if not isinstance(ledger, dict):
            return None
        claimed = ledger.pop("ledger_digest", None)
        if claimed != digest_json(ledger):
            return None
        plan = spec["execution_plan"]
        index = ledger.get("run_index")
        if (type(ledger.get("ledger_schema_version")) is not int or ledger["ledger_schema_version"] != 1
                or ledger.get("plan_digest") != plan["plan_digest"] or ledger.get("target") != plan["target"]
                or ledger.get("run_id") != path.parent.name or ledger.get("status") != "running"
                or type(index) is not int or not 1 <= index <= plan["run_count"]):
            return None
        active = ledger.get("active_request")
        if active is None:
            return None
        if not isinstance(active, dict) or active.get("phase") not in {"business", "cleanup"}:
            return None
        started, deadline, timeout = (active.get(key) for key in ("started_at", "deadline_at", "timeout_seconds"))
        if (any(type(value) not in (int, float) or not math.isfinite(value) for value in (started, deadline, timeout))
                or not 0 < timeout <= plan["limits"]["request_timeout_seconds"]
                or not math.isclose(deadline - started, timeout, abs_tol=0.0001)):
            return None
        return max(0.0, deadline - time.time())
    except (KeyError, OSError, TypeError, ValueError):
        return None


def _termination_grace_for_job(job: Job) -> float:
    """Read immutable disk controls; stopping never needs provider credentials."""
    path = job.report_dir / "job_spec.json"
    raw = _read_json(path)
    raw = raw if isinstance(raw, dict) else {}
    if (raw.get("schema_version") == WORKFLOW_JOB_SPEC_VERSION
            or job.job_spec.get("schema_version") == WORKFLOW_JOB_SPEC_VERSION):
        spec = load_job_spec(path)
        if not spec or spec.get("schema_version") != WORKFLOW_JOB_SPEC_VERSION:
            raise RuntimeError("Workflow stop requires the persisted schema-v6 JobSpec")
        return workflow_termination_grace_seconds(spec, _remaining_workflow_request_timeout(job, spec))
    return 5.0


def _signal_process_group(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        process.terminate()


def _terminate_process_group(process: subprocess.Popen[Any], *, grace_seconds: float = 5.0,
                             signal_sent: bool = False) -> None:
    if not signal_sent:
        _signal_process_group(process)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            process.kill()


JOB_MANAGER = JobManager()


INDEX_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LLM Loadtest Console</title>
  <style>
    :root { color-scheme: light; --border: #d6dbe3; --text: #172033; --muted: #657188; --bg: #f7f8fb; --panel: #fff; --accent: #2563eb; --danger: #b42318; --ok: #067647; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--text); background: var(--bg); }
    header { padding: 22px 28px; border-bottom: 1px solid var(--border); background: var(--panel); }
    h1 { margin: 0; font-size: 24px; }
    main { padding: 22px 28px 40px; display: grid; gap: 18px; }
    section { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 18px; }
    h2 { margin: 0 0 14px; font-size: 17px; }
    .controls { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; align-items: end; }
    label { display: grid; gap: 5px; color: var(--muted); font-size: 13px; }
    label.small-field { min-width: 160px; }
    select, input, button { height: 38px; border-radius: 6px; border: 1px solid var(--border); background: #fff; color: var(--text); padding: 0 10px; font-size: 14px; }
    button { cursor: pointer; border-color: #b8c2d1; font-weight: 600; }
    button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
    button.danger { background: #fff; border-color: #f2afa8; color: var(--danger); }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
    .tabs { display: flex; flex-wrap: wrap; gap: 8px; border-bottom: 1px solid var(--border); margin-bottom: 14px; }
    .tab { height: 36px; border: 0; border-bottom: 3px solid transparent; border-radius: 0; background: transparent; color: var(--muted); }
    .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }
    .pill { display: inline-flex; align-items: center; min-height: 28px; padding: 4px 9px; border: 1px solid var(--border); border-radius: 999px; color: var(--muted); font-size: 13px; }
    .pill.ok { color: var(--ok); border-color: #9ad7bc; }
    .pill.bad { color: var(--danger); border-color: #f2afa8; }
    .progress-wrap { display: grid; gap: 8px; margin-top: 12px; }
    .progress-head { display: flex; justify-content: space-between; gap: 12px; color: var(--muted); font-size: 13px; }
    .progress { height: 12px; overflow: hidden; border-radius: 999px; background: #e8edf5; border: 1px solid var(--border); }
    .progress > div { height: 100%; width: 0%; background: var(--accent); transition: width .25s ease; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }
    .metric { border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
    .metric div:first-child { color: var(--muted); font-size: 12px; text-transform: uppercase; }
    .metric div:last-child { margin-top: 4px; font-size: 22px; font-weight: 700; overflow-wrap: anywhere; }
    table { border-collapse: collapse; width: 100%; font-size: 13px; }
    th, td { border: 1px solid var(--border); padding: 7px 9px; text-align: left; vertical-align: top; }
    th { background: #f3f5f8; color: #344054; }
    .table-scroll { overflow-x: auto; max-width: 100%; border: 1px solid var(--border); border-radius: 6px; }
    .table-scroll table { border: 0; }
    .matrix-table { table-layout: fixed; width: 100%; }
    .matrix-table th, .matrix-table td { padding: 6px 8px; }
    .matrix-table th:first-child, .matrix-table td:first-child { width: 260px; }
    .run-list { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
    .run-chip { display: inline-flex; align-items: center; justify-content: center; min-width: 32px; height: 24px; padding: 0 6px; border: 1px solid var(--border); border-radius: 5px; font-size: 12px; font-weight: 700; background: #fff; }
    .run-chip.status-pass { border-color: #9ad7bc; background: #ecfdf3; }
    .run-chip.status-expected_rejection { border-color: #9ad7bc; background: #f0fdf4; }
    .run-chip.status-incompatible { border-color: #fdb022; background: #fffaeb; }
    .run-chip.status-unexpected_acceptance { border-color: #f2afa8; background: #fff1f0; }
    .run-chip.status-fail { border-color: #f2afa8; background: #fef3f2; }
    .run-chip.status-waiting { background: #f3f5f8; }
    .status-pass { color: var(--ok); font-weight: 700; }
    .status-expected_rejection { color: #067647; font-weight: 700; }
    .status-incompatible { color: #b54708; font-weight: 700; }
    .status-unexpected_acceptance { color: #b42318; font-weight: 700; }
    .status-fail { color: var(--danger); font-weight: 700; }
    .status-waiting { color: var(--muted); }
    pre { max-height: 280px; overflow: auto; background: #101828; color: #eef4ff; padding: 12px; border-radius: 8px; font-size: 12px; }
    a { color: var(--accent); text-decoration: none; }
    .muted { color: var(--muted); }
  </style>
</head>
<body>
  <header>
    <h1>LLM Loadtest Console</h1>
    <div class="muted">自研控制台，Locust 作为 headless 执行后端。</div>
  </header>
  <main>
    <section>
      <h2>Provider / Model</h2>
      <div class="controls">
        <label>Provider<select id="provider"></select></label>
        <label>Model<select id="model"></select></label>
        <label>Reference Source<select id="referenceSource"></select></label>
        <label>Workload<select id="workload"><option value="throughput">throughput</option><option value="mixed_compat">mixed_compat</option><option value="cache_suite">cache_suite</option></select></label>
        <label>Users<input id="users" type="number" min="1"></label>
        <label>Spawn rate<input id="spawnRate" type="number" min="1"></label>
        <label>Duration<input id="duration" placeholder="2m"></label>
      </div>
      <div class="row" style="margin-top:12px">
        <span id="keyStatus" class="pill">key: unknown</span>
        <span id="familyStatus" class="pill">family: unknown</span>
      </div>
    </section>

    <section>
      <h2>Actions</h2>
      <div class="tabs">
        <button id="tabParam" class="tab active" onclick="setTab('param')">参数测试</button>
        <button id="tabLoad" class="tab" onclick="setTab('load')">压测</button>
        <button id="tabCache" class="tab" onclick="setTab('cache')">Cache 测试</button>
      </div>
      <div id="panelParam" class="tab-panel active">
        <div class="row">
          <label>参数套件<select id="parameterSuite" disabled><option value="">通用参数矩阵</option></select></label>
          <label class="small-field">每个 profile 测试次数<input id="paramTestRuns" type="number" min="1" max="1000" step="1" inputmode="numeric" value="3"></label>
          <span id="paramRunHint" class="pill">0 cells</span>
          <button class="primary" onclick="createJob('param_test')">运行参数测试</button>
          <button class="danger" onclick="stopActiveJob()">停止当前 Job</button>
        </div>
      </div>
      <div id="panelLoad" class="tab-panel">
        <div class="row">
          <button class="primary" onclick="createJob('quick_load')">开始快速压测</button>
          <button onclick="createJob('staircase')">运行 Staircase</button>
          <button class="danger" onclick="stopActiveJob()">停止当前 Job</button>
        </div>
      </div>
      <div id="panelCache" class="tab-panel">
        <div class="row">
          <button class="primary" onclick="createJob('cache_suite')">运行 Cache 测试</button>
          <button class="danger" onclick="stopActiveJob()">停止当前 Job</button>
        </div>
      </div>
      <div id="actionError" class="muted" style="margin-top:10px"></div>
    </section>

    <section>
      <h2>Current Job</h2>
      <div id="jobMeta" class="muted">No job yet.</div>
      <div id="progressWrap" class="progress-wrap">
        <div class="progress-head">
          <span id="progressLabel">No progress yet.</span>
          <span id="progressDetail"></span>
        </div>
        <div class="progress"><div id="progressBar"></div></div>
      </div>
      <div class="grid" id="metrics" style="margin-top:12px"></div>
      <h2 style="margin-top:18px">Report Files</h2>
      <div id="files" class="row"></div>
    </section>

    <section id="paramSpecsSection">
      <h2>Reference Parameter Specs</h2>
      <table>
        <thead><tr><th>Parameter</th><th>Official / Contract</th><th>Local support</th><th>Coverage</th></tr></thead>
        <tbody id="paramSpecs"></tbody>
      </table>
    </section>

    <section id="paramResultsSection">
      <h2>Reference Param Test Matrix</h2>
      <div class="table-scroll">
        <table class="matrix-table">
          <thead id="paramResultsHead"><tr><th>Parameter / Profile</th><th>Status</th></tr></thead>
          <tbody id="paramResults"></tbody>
        </table>
      </div>
    </section>

    <section id="paramIncompatibleSection">
      <h2>Failed / Incompatible Case Log</h2>
      <pre id="paramFailedCaseLog">No failed, incompatible, or unexpected-acceptance parameter test cases.</pre>
    </section>

    <section>
      <h2>Log Tail</h2>
      <pre id="logTail"></pre>
    </section>
  </main>
  <script>
    let config = null;
    let currentJobId = null;
    let activeTab = "param";
    let currentParamSpecs = null;

    const $ = id => document.getElementById(id);
    const fmtPct = v => v === null || v === undefined ? "waiting" : (Number(v) * 100).toFixed(2) + "%";
    const fmtNum = v => v === null || v === undefined ? "waiting" : Number(v).toFixed(2);
    const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

    function setTab(tab, clearError = true) {
      activeTab = tab;
      for (const name of ["Param", "Load", "Cache"]) {
        const key = name.toLowerCase();
        $("tab" + name).classList.toggle("active", key === tab);
        $("panel" + name).classList.toggle("active", key === tab);
      }
      $("paramSpecsSection").style.display = tab === "param" ? "" : "none";
      $("paramResultsSection").style.display = tab === "param" ? "" : "none";
      $("paramIncompatibleSection").style.display = tab === "param" ? "" : "none";
      if (clearError) $("actionError").textContent = "";
    }

    function tabForJobType(type) {
      if (type === "param_test") return "param";
      if (type === "cache_suite") return "cache";
      return "load";
    }

    async function loadConfig() {
      config = await fetch("/api/config", { cache: "no-store" }).then(r => r.json());
      $("users").value = config.defaults.users;
      $("spawnRate").value = config.defaults.spawn_rate;
      $("duration").value = config.defaults.duration;
      $("paramTestRuns").max = config.defaults.param_test_runs_max || 1000;
      $("paramTestRuns").value = config.defaults.param_test_runs || 1;
      $("workload").value = config.defaults.workload;
      $("provider").innerHTML = config.providers.map(p => `<option value="${esc(p.name)}">${esc(p.label || p.name)}</option>`).join("");
      $("provider").value = config.active_provider;
      renderModels();
      await loadParamSpecs();
      renderParamRunHint();
    }

    function selectedProvider() {
      return config.providers.find(p => p.name === $("provider").value);
    }

    function renderModels() {
      const p = selectedProvider();
      const models = (p.models && p.models.candidates) || [];
      $("model").innerHTML = models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
      $("model").value = models.includes(config.active_model) ? config.active_model : ((p.models && p.models.default) || models[0] || "");
      $("keyStatus").className = "pill " + (p.has_key ? "ok" : "bad");
      $("keyStatus").textContent = p.has_key ? "key: configured" : "key: missing";
      renderFamily();
      renderReferenceSources(true);
    }

    function renderFamily() {
      const p = selectedProvider();
      const families = (p.models && p.models.families) || {};
      const model = $("model").value;
      const family = families[model] || (
        model.toLowerCase().includes("fable") ? "claude_fable"
        : model.startsWith("glm") ? "glm"
        : model.startsWith("deepseek") ? "deepseek"
        : model.startsWith("qwen") ? "qwen"
        : model.startsWith("gemini") ? "gemini"
        : model.startsWith("claude") ? "claude"
        : model.startsWith("grok") ? "grok"
        : model.startsWith("kimi") || model.startsWith("moonshotai/") ? "kimi"
        : model.toLowerCase().startsWith("minimax") ? "minimax"
        : model.startsWith("gpt") || model.startsWith("openai/") ? "gpt"
        : "unknown"
      );
      $("familyStatus").textContent = "family: " + family;
      $("familyStatus").dataset.family = family;
      return family;
    }

    function renderReferenceSources(useDefault) {
      $("parameterSuite").value = "";
      const sources = config.reference_sources || [];
      const family = $("familyStatus").dataset.family || config.model_family || "";
      const previous = $("referenceSource").value;
      $("referenceSource").innerHTML = sources.map(source => `<option value="${esc(source.id)}">${esc(source.label || source.id)}</option>`).join("");
      const preferred = referenceSourceForFamily(family);
      if (useDefault || !sources.some(source => source.id === previous)) {
        $("referenceSource").value = preferred;
      } else {
        $("referenceSource").value = previous;
      }
      renderParamRunHint();
    }

    function referenceSourceForFamily(family) {
      const sources = config.reference_sources || [];
      const match = sources.find(source => (source.default_for_families || []).includes(family));
      return (match && match.id) || config.default_reference_source || (sources[0] && sources[0].id) || "";
    }

    async function loadParamSpecs() {
      renderFamily();
      const referenceSource = $("referenceSource").value || config.default_reference_source;
      const source = (config.reference_sources || []).find(item => item.id === referenceSource);
      const chosenSuite = $("parameterSuite").value;
      const query = new URLSearchParams({reference_source: referenceSource, provider: $("provider").value, model: $("model").value});
      if (source && source.api_form) query.set("api_form", source.api_form);
      if (source && source.route_profile) query.set("route_profile", source.route_profile);
      if (chosenSuite) query.set("parameter_suite", chosenSuite);
      const payload = await fetch(`/api/param-specs?${query}`, { cache: "no-store" }).then(r => r.json());
      if (payload.error) { $("actionError").textContent = payload.error; return; }
      currentParamSpecs = payload;
      const suites = payload.available_parameter_suites || [];
      $("parameterSuite").innerHTML = `<option value="">${source && source.fixed_case_suite ? "固定前缀验证" : "通用参数矩阵"}</option>` +
        suites.map(suite => `<option value="${esc(suite.id)}">${esc(suite.label)}</option>`).join("");
      $("parameterSuite").disabled = suites.length === 0;
      $("parameterSuite").value = suites.some(suite => suite.id === chosenSuite) ? chosenSuite : "";
      $("paramSpecs").innerHTML = payload.comparison.map(row =>
        `<tr><td>${esc(row.parameter)}</td><td>${esc(row.official)}</td><td>${esc(row.local)}</td><td>${esc(row.coverage)}</td></tr>`
      ).join("");
      renderParamRunHint();
    }

    function paramTestRunsValue() {
      if ($("parameterSuite").value) return 1;
      const source = ((config && config.reference_sources) || []).find(item => item.id === $("referenceSource").value);
      if (source && source.fixed_case_suite) return 1;
      const parsed = Math.trunc(Number($("paramTestRuns").value || 1));
      if (!Number.isFinite(parsed)) return 1;
      const maxRuns = Number((config && config.defaults && config.defaults.param_test_runs_max) || 1000);
      return Math.max(1, Math.min(parsed, maxRuns));
    }

    function renderParamRunHint() {
      if (!config || !$("paramRunHint")) return;
      const sources = config.reference_sources || [];
      const source = sources.find(item => item.id === $("referenceSource").value);
      const profiles = Number((source && source.test_profile_count) || 0);
      const runs = paramTestRunsValue();
      const cells = profiles * runs;
      $("paramTestRuns").disabled = Boolean($("parameterSuite").value || (source && source.fixed_case_suite));
      if ($("parameterSuite").value) {
        $("paramTestRuns").value = 1;
        $("paramRunHint").textContent = `${Number((currentParamSpecs && currentParamSpecs.fixed_request_count) || 8)} 项固定对照 · 顺序执行一次`;
        return;
      }
      if (source && source.fixed_case_suite) {
        $("paramTestRuns").value = 1;
        $("paramRunHint").textContent = "5 项固定验证 · 顺序执行一次 · 前缀格式与严格尾串分别报告";
        return;
      }
      $("paramRunHint").textContent = `${cells} cells = ${profiles} profiles × ${runs} runs`;
    }

    async function createJob(type) {
      $("actionError").textContent = "";
      const paramTestRuns = paramTestRunsValue();
      $("paramTestRuns").value = paramTestRuns;
      const source = (config.reference_sources || []).find(item => item.id === $("referenceSource").value);
      const payload = {
        type,
        provider: $("provider").value,
        model: $("model").value,
        reference_source: $("referenceSource").value,
        api_form: source && source.api_form,
        route_profile: source && source.route_profile,
        parameter_suite: type === "param_test" ? ($("parameterSuite").value || null) : null,
        param_test_runs: paramTestRuns,
        workload: $("workload").value,
        users: Number($("users").value || 10),
        spawn_rate: Number($("spawnRate").value || 2),
        duration: $("duration").value || "2m"
      };
      const resp = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await resp.json();
      if (!resp.ok) {
        $("actionError").textContent = data.error || "failed to create job";
        return;
      }
      currentJobId = data.id;
      setTab(tabForJobType(data.type), false);
      renderJob(data);
    }

    async function stopActiveJob() {
      if (!currentJobId) return;
      const data = await fetch(`/api/jobs/${currentJobId}/stop`, { method: "POST" }).then(r => r.json());
      renderJob(data);
    }

    async function pollJob() {
      if (!currentJobId) {
        const jobs = await fetch("/api/jobs", { cache: "no-store" }).then(r => r.json());
        if (jobs.jobs && jobs.jobs.length) currentJobId = jobs.jobs[0].id;
      }
      if (!currentJobId) return;
      const data = await fetch(`/api/jobs/${currentJobId}`, { cache: "no-store" }).then(r => r.json());
      if (!data.error) renderJob(data);
    }

    function renderJob(job) {
      currentJobId = job.id;
      setTab(tabForJobType(job.type), false);
      $("jobMeta").innerHTML = `<b>${esc(job.id)}</b> · ${esc(job.type)} · ${esc(job.status)} · ${esc(job.provider_label)} / ${esc(job.model)} · ${esc(job.report_dir)}`;
      renderProgress(job);
      const metrics = job.type === "param_test" ? paramTestMetrics(job) : (job.type === "cache_suite" ? cacheMetrics(job) : loadTestMetrics(job));
      $("metrics").innerHTML = metrics.map(([k, v]) => `<div class="metric"><div>${esc(k)}</div><div>${esc(v)}</div></div>`).join("");
      $("files").innerHTML = (job.report_files || []).map(f => `<a class="pill" href="${esc(f.url)}" target="_blank">${esc(f.name)}</a>`).join("") || '<span class="muted">No report files yet.</span>';
      $("logTail").textContent = job.log_tail || "";
      renderParamResults(job);
    }

    function renderProgress(job) {
      const progress = job.progress || {};
      const percent = Math.max(0, Math.min(100, Number(progress.percent || 0)));
      $("progressBar").style.width = percent + "%";
      $("progressLabel").textContent = `${percent}% · ${progress.label || job.status || "waiting"}`;
      $("progressDetail").textContent = progress.detail || "";
    }

    function loadTestMetrics(job) {
      const s = job.summary || {};
      return [
        ["Success RPM", fmtNum(s.business_rpm)],
        ["Success rate", fmtPct(s.success_rate)],
        ["P95 latency", fmtNum(s.p95_latency_ms)],
        ["Cache hit rate", fmtPct(s.cache_hit_rate)],
        ["Cache eligible", s.cache_eligible_record_count ?? "n/a"],
        ["Records", s.record_count ?? "n/a"],
        ["Return code", job.returncode ?? "running"],
        ["Family", job.model_family]
      ];
    }

    function paramTestMetrics(job) {
      const allResults = Array.isArray(job.param_results) ? job.param_results : [];
      const results = referenceParamResults(allResults);
      const passed = results.filter(row => row.status === "pass" || row.status === "expected_rejection").length;
      const expectedRejection = results.filter(row => row.status === "expected_rejection").length;
      const failed = results.filter(row => row.status === "fail").length;
      const incompatible = results.filter(row => row.status === "incompatible").length;
      const unexpected = results.filter(row => row.status === "unexpected_acceptance").length;
      const total = (job.verdict && job.verdict.total) || (job.progress && job.progress.total_cells) || results.length;
      const successRate = total ? passed / Number(total) : null;
      return [
        ["Cells complete", results.length],
        ["Overall success rate", fmtPct(successRate)],
        ["Pass cells", passed],
        ["Expected rejection", expectedRejection],
        ["Incompatible cells", incompatible],
        ["Unexpected acceptance", unexpected],
        ["Fail cells", failed],
        ["Total", total || "waiting"],
        ["Return code", job.returncode ?? "running"],
        ["Reference", job.reference_label || job.reference_source || "waiting"]
      ];
    }

    function cacheMetrics(job) {
      const s = job.summary || {};
      const cp = job.cache_progress || {};
      return [
        ["Cache progress", cp.label || (job.progress && job.progress.label) || job.status],
        ["Phase", cp.phase || (job.progress && job.progress.detail) || "waiting"],
        ["Cache hit rate", fmtPct(s.cache_hit_rate)],
        ["Success rate", fmtPct(s.success_rate)],
        ["Records", s.record_count ?? "waiting"],
        ["Return code", job.returncode ?? "running"],
        ["Family", job.model_family]
      ];
    }

    function renderParamResults(job) {
      const allResults = Array.isArray(job.param_results) ? job.param_results : [];
      const results = referenceParamResults(allResults);
      if (!Array.isArray(results) || !results.length) {
        const message = job.type === "param_test" && job.status === "running"
          ? "Parameter test is running; waiting for the first reference profile result."
          : "No parameter test results yet.";
        $("paramResultsHead").innerHTML = "<tr><th>Parameter / Profile</th><th>Expectation</th><th>Runs</th></tr>";
        $("paramResults").innerHTML = `<tr><td colspan="3" class="muted">${esc(message)}</td></tr>`;
        renderFailedCaseLog(job, []);
        return;
      }
      const runs = paramRunCount(job, results);
      $("paramResultsHead").innerHTML = "<tr><th>Parameter / Profile</th><th>Expectation</th><th>Runs</th></tr>";
      const rows = matrixRows(results);
      $("paramResults").innerHTML = rows.map(row => {
        const chips = Array.from({ length: runs }, (_, index) => {
          const runIndex = index + 1;
          const item = row.runs[runIndex];
          const status = item ? item.status : "waiting";
          const expectation = item && item.expectation ? item.expectation : (row.expectation || "");
          const title = item
            ? `${expectation || ""} ${status} ${item.failure_classification || ""} ${item.message || ""}`.trim()
            : "waiting";
          return `<span class="run-chip status-${esc(status)}" title="${esc(title)}">R${runIndex}:${esc(statusLabel(status))}</span>`;
        }).join("");
        const expectation = row.expectation || "";
        return `<tr><td><b>${esc(row.parameter)}</b><div class="muted">${esc(row.profile)}</div></td><td>${esc(expectation || "-")}</td><td><div class="run-list">${chips}</div></td></tr>`;
      }).join("");
      renderFailedCaseLog(job, results);
    }

    function statusLabel(status) {
      if (status === "pass") return "ok";
      if (status === "expected_rejection") return "rej";
      if (status === "incompatible") return "inc";
      if (status === "unexpected_acceptance") return "acc";
      if (status === "fail") return "fail";
      return "-";
    }

    function renderFailedCaseLog(job, results) {
      if (job && job.param_failed_cases_log) {
        $("paramFailedCaseLog").textContent = job.param_failed_cases_log;
        return;
      }
      const failed = (Array.isArray(results) ? results : []).filter(
        row => row.status === "incompatible" || row.status === "fail" || row.status === "unexpected_acceptance"
      );
      if (!failed.length) {
        $("paramFailedCaseLog").textContent = "No failed, incompatible, or unexpected-acceptance parameter test cases.";
        return;
      }
      $("paramFailedCaseLog").textContent = failed.map((item, index) => [
        `===== Case ${index + 1}: ${item.status} =====`,
        `profile: ${item.profile || ""}`,
        `parameter: ${item.parameter || ""}`,
        `expectation: ${item.expectation || ""}`,
        `run_index: ${item.run_index || ""}`,
        `provider/model: ${item.provider || ""} / ${item.model || ""}`,
        `reference: ${item.reference_source || ""} (${item.reference_family || ""})`,
        `input_sample: ${item.input_sample || ""}`,
        `status_code: ${item.status_code ?? ""}`,
        `latency_ms: ${item.latency_ms ?? ""}`,
        `failure_classification: ${item.failure_classification || ""}`,
        `warnings: ${JSON.stringify(item.warnings || [])}`,
        "message:",
        item.message || "",
      ].join("\n")).join("\n\n");
    }

    function referenceParamResults(results) {
      if (!Array.isArray(results)) return [];
      return results.filter(row => row.status !== "expected_unsupported");
    }

    function paramRunCount(job, results) {
      const fromVerdict = job.verdict && job.verdict.param_test_runs;
      const fromJob = job.param_test_runs;
      const fromRows = Math.max(...results.map(row => Number(row.run_index || 1)));
      return Math.max(Number(fromVerdict || fromJob || fromRows || 1), 1);
    }

    function matrixRows(results) {
      const rows = [];
      const byProfile = new Map();
      for (const item of results) {
        const profile = item.profile || item.parameter || "unknown";
        if (!byProfile.has(profile)) {
          const row = {
            profile,
            parameter: item.parameter || profile,
            expectation: item.expectation || "",
            runs: {},
          };
          byProfile.set(profile, row);
          rows.push(row);
        }
        const row = byProfile.get(profile);
        if (!row.expectation && item.expectation) row.expectation = item.expectation;
        row.runs[Number(item.run_index || 1)] = item;
      }
      return rows;
    }

    $("provider").addEventListener("change", () => { renderModels(); loadParamSpecs(); });
    $("model").addEventListener("change", () => { renderFamily(); renderReferenceSources(true); loadParamSpecs(); });
    $("referenceSource").addEventListener("change", () => { $("parameterSuite").value = ""; loadParamSpecs(); });
    $("parameterSuite").addEventListener("change", () => { loadParamSpecs(); });
    $("paramTestRuns").addEventListener("input", renderParamRunHint);
    $("paramTestRuns").addEventListener("change", () => { $("paramTestRuns").value = paramTestRunsValue(); renderParamRunHint(); });
    loadConfig().then(pollJob);
    setInterval(pollJob, 3000);
  </script>
</body>
</html>
"""


def main() -> None:
    port = int(os.getenv("WEB_CONSOLE_PORT", "8090"))
    host = os.getenv("WEB_CONSOLE_HOST", "0.0.0.0")
    _ensure_secret_key()
    generated = _ensure_auth_configured()
    if generated:
        user, _password = generated
        print("=" * 60)
        print(f"Web console credentials generated (stored in {CONSOLE_AUTH_PATH}):")
        print(f"  user:     {user}")
        print(f"  retrieve password with the console helper ({CONSOLE_PASSWORD_PATH})")
        print("  override via WEB_CONSOLE_USER / WEB_CONSOLE_PASSWORD")
        print("=" * 60, flush=True)
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
