from __future__ import annotations

import json
import copy
import hashlib
import hmac
import os
import re
import secrets
import signal
import subprocess
import sys
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
from lib.metrics import (
    RequestRecord,
    build_time_series,
    load_history,
    load_records,
    percentile,
    summarize_records,
    write_json,
)
from lib.deepseek_params import build_request, weighted_workload_profiles
from lib.job_spec import (
    SUPPORTED_JOB_TYPES,
    make_job_spec,
    resolve_cache_plan,
    resolve_image_plan,
    resolve_request_mode,
    resolve_soak_plan,
    resolve_staircase_plan,
    validate_workload,
)
from lib.reference_specs import (
    capability_profile_snapshot,
    default_reference_source_for_family,
    default_reference_source_for_model,
    get_reference_source,
    list_reference_sources,
    load_model_capability_profile,
    model_reference_spec_payload,
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
DEFAULT_PARAM_TEST_RUNS = 3
MAX_PARAM_TEST_RUNS = 1000
TOOL_VALIDATION_MODES = {"auto", "openai_compat", "gemini_native", "claude_native"}
MAX_CACHE_MEASURED_REQUESTS = 1000
DEFAULT_TARGET_RPM = 0.0
DEFAULT_TARGET_TPM = 0.0
LOAD_RESULT_SCHEMA_VERSION = 8

app = Flask(__name__)


def _load_auth_credentials() -> tuple[str, str, str] | None:
    if os.getenv("LLM_API_TEST_DISABLE_AUTH") == "1":
        return None
    user = os.getenv("WEB_CONSOLE_USER")
    password = os.getenv("WEB_CONSOLE_PASSWORD")
    if user and password:
        return user, "plain", password
    if not CONSOLE_AUTH_PATH.exists():
        return None
    try:
        data = json.loads(CONSOLE_AUTH_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not all(data.get(key) for key in ("user", "salt", "password_pbkdf2")):
        return None
    return str(data["user"]), str(data["salt"]), str(data["password_pbkdf2"])


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 100_000
    ).hex()


def _verify_password(password: str, creds: tuple[str, str, str]) -> bool:
    _user, salt, stored = creds
    if salt == "plain":
        return hmac.compare_digest(password, stored)
    return hmac.compare_digest(_hash_password(password, salt), stored)


def _write_auth_file(user: str, password: str) -> None:
    salt = secrets.token_hex(16)
    payload = {
        "user": user,
        "salt": salt,
        "password_pbkdf2": _hash_password(password, salt),
    }
    CONSOLE_AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONSOLE_AUTH_PATH.write_text(json.dumps(payload), encoding="utf-8")
    CONSOLE_AUTH_PATH.chmod(0o600)
    CONSOLE_PASSWORD_PATH.write_text(password, encoding="utf-8")
    CONSOLE_PASSWORD_PATH.chmod(0o600)


def _ensure_auth_configured() -> tuple[str, str] | None:
    if _load_auth_credentials() is not None:
        return None
    user = "admin"
    password = secrets.token_urlsafe(12)
    _write_auth_file(user, password)
    return user, password


def _ensure_secret_key() -> None:
    if CONSOLE_SECRET_PATH.exists():
        app.secret_key = CONSOLE_SECRET_PATH.read_text(encoding="utf-8").strip()
        return
    CONSOLE_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32)
    CONSOLE_SECRET_PATH.write_text(secret, encoding="utf-8")
    CONSOLE_SECRET_PATH.chmod(0o600)
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
    creds = _load_auth_credentials()
    if creds is None:
        return None
    path = request.path
    if path in {"/login", "/favicon.ico"} or path.startswith("/static/"):
        return None
    if session.get("auth_user"):
        return None
    if path.startswith("/api/") or path.startswith("/reports/"):
        return jsonify({"error": "authentication required"}), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login() -> Any:
    creds = _load_auth_credentials()
    if creds is None:
        return redirect("/")
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
            job.returncode = int(returncode)
            job.finished_at = float(
                (run.get("finished_at") if isinstance(run, dict) else None)
                or time.time()
            )
            job.status = (
                "stopped"
                if job.stop_requested
                else ("completed" if job.returncode == 0 else "failed")
            )
            _ensure_load_result(job)

    def create(self, payload: dict[str, Any]) -> Job:
        config = load_config()
        job_type = str(payload.get("type") or "quick_load")
        if job_type not in SUPPORTED_JOB_TYPES:
            raise ValueError(f"Unsupported job type: {job_type}")
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
        users = _optional_int(payload.get("users"))
        spawn_rate = _optional_int(payload.get("spawn_rate"))
        duration = str(payload.get("duration") or DEFAULT_QUICK_DURATION)
        reference_source = str(
            payload.get("reference_source")
            or default_reference_source_for_model(
                config,
                family,
                model,
                provider,
                api_form=api_form,
                route_profile=route_profile,
            )
        )
        allowed_reference_sources = reference_sources_for_model(
            config,
            family,
            model,
            provider,
            api_form=api_form,
            route_profile=route_profile,
        )
        if reference_source not in allowed_reference_sources:
            raise ValueError(
                f"Reference source {reference_source!r} is not part of the "
                f"{family}/{model} family suite."
            )
        capability = load_model_capability_profile(
            "text",
            family,
            model,
            api_form=api_form,
            route_profile=route_profile,
            reference_source=reference_source,
            provider_override=get_model_api_forms(
                config,
                model,
                provider,
                route_profile=route_profile,
            )[api_form],
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
            and capability.get("pressure_test_enabled") is not True
        ):
            raise ValueError(
                f"Pressure testing is disabled for {family}/{model}: "
                f"{capability.get('disabled_reason') or 'model profile policy'}."
            )
        reference = get_reference_source(reference_source)
        param_test_runs = min(
            max(
                _optional_int(payload.get("param_test_runs"))
                or DEFAULT_PARAM_TEST_RUNS,
                1,
            ),
            MAX_PARAM_TEST_RUNS,
        )
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
                job_type, report_dir, users, spawn_rate, duration
            )
            job_spec = make_job_spec(
                job_type=job_type,
                provider=provider,
                model=model,
                model_family=family,
                api_form=api_form,
                route_profile=route_profile,
                model_profile_id=str(capability.get("model_api_profile_id") or ""),
                transport=str(capability.get("transport") or ""),
                workload=workload,
                request_mode=request_mode,
                target_rpm=target_rpm,
                target_tpm=target_tpm,
                staircase_plan=staircase_plan,
                cache_plan=cache_plan,
                soak_plan=soak_plan,
                reference_source=reference_source,
                reference_route_profile=str(reference.get("route_profile") or ""),
                model_capability_profile=capability_profile_snapshot(
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
                ),
            )
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
            job_spec = make_job_spec(
                job_type="image_param_test",
                provider=provider,
                model=model,
                model_family=str(model_cfg.get("family") or ""),
                api_form=str(image_plan.get("api_form") or ""),
                route_profile=str(image_plan.get("route_profile") or ""),
                model_profile_id=str(
                    (image_plan.get("model_capability_profile") or {}).get(
                        "model_api_profile_id"
                    )
                    or ""
                ),
                transport=str(image_plan.get("transport") or ""),
                workload="image_param",
                request_mode="fixed",
                target_rpm=0.0,
                target_tpm=0.0,
                image_plan=image_plan,
                model_capability_profile=image_plan.get("model_capability_profile"),
            )
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
    ) -> dict[str, Any] | None:
        with self._lock:
            try:
                current_profiles = set(test_profiles_for_reference(reference_source))
            except KeyError:
                current_profiles = set()
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
                and (
                    not current_profiles
                    or not _job_param_profiles(job)
                    or _job_param_profiles(job) == current_profiles
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
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            job = self._refresh_locked(self._jobs[job_id])
            if job.process is not None and job.process.poll() is None:
                job.stop_requested = True
                job.status = "stopping"
                _terminate_process_group(job.process)
            elif job.external and job.status == "running" and job.pid:
                job.stop_requested = True
                job.status = "stopping"
                try:
                    os.killpg(job.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        return self.get(job_id)

    def public(self, job: Job, include_detail: bool = True) -> dict[str, Any]:
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
            "reference_source": job.reference_source,
            "reference_label": job.reference_label,
            "param_test_runs": job.param_test_runs,
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
        if include_detail:
            records = _load_result_records(job.report_dir)
            summary = _job_summary(job, records)
            time_series = _job_time_series(job, records)
            load_result = _ensure_load_result(job)
            verdict = _read_json(job.report_dir / "verdict.json")
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
                image_summary = _read_json(job.report_dir / "summary.json")
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
        verdict = _read_json(report_dir / "verdict.json") or {}
        load_result = _read_json(report_dir / "load_result.json") or {}
        first_record = (
            _first_request_record(report_dir)
            if not verdict and not load_result
            else None
        )
        record_extra = first_record.extra if first_record else {}
        provider = str(
            verdict.get("provider")
            or load_result.get("provider")
            or record_extra.get("provider")
            or get_active_provider_name(config)
        )
        try:
            provider_cfg = get_provider_config(config, provider)
        except KeyError:
            provider_cfg = {"label": provider}
        model = str(
            verdict.get("model")
            or load_result.get("model")
            or record_extra.get("requested_model")
            or get_selected_model(
                config,
                provider if provider in (config.get("providers") or {}) else None,
            )
        )
        family = str(
            verdict.get("model_family")
            or load_result.get("model_family")
            or record_extra.get("model_family")
            or get_model_family(
                config,
                model,
                provider if provider in (config.get("providers") or {}) else None,
            )
        )
        route_profile = str(
            verdict.get("route_profile")
            or load_result.get("route_profile")
            or job_spec.get("route_profile")
            or record_extra.get("route_profile")
            or (
                get_model_route_profile(config, model, provider)
                if provider in (config.get("providers") or {})
                else ""
            )
        )
        api_form = str(
            verdict.get("api_form")
            or load_result.get("api_form")
            or job_spec.get("api_form")
            or record_extra.get("api_form")
            or (
                get_model_api_form(
                    config,
                    model,
                    provider,
                    route_profile=route_profile,
                )
                if provider in (config.get("providers") or {}) and route_profile
                else ""
            )
        )
        reference_source = str(
            verdict.get("reference_source")
            or (
                default_reference_source_for_model(
                    config,
                    family,
                    model,
                    provider,
                    api_form=api_form or None,
                    route_profile=route_profile or None,
                )
                if provider in (config.get("providers") or {})
                else default_reference_source_for_family(family)
            )
        )
        try:
            reference = get_reference_source(reference_source)
        except KeyError:
            reference = {"label": reference_source}
        if verdict:
            returncode = 0 if verdict.get("pass", True) else 1
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
                verdict.get("param_test_runs") or DEFAULT_PARAM_TEST_RUNS
            ),
            tool_validation_mode=str(verdict.get("tool_validation_mode") or "auto"),
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
            status=str(
                load_result.get("status")
                or ("completed" if returncode == 0 else "failed")
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
        configured = list_image_providers(config)
        default_provider = (
            configured[0]["name"] if configured else get_active_provider_name(config)
        )
        provider = str(job_spec.get("provider") or default_provider)
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
        model = str(
            job_spec.get("model") or image_plan.get("model") or plan.get("model") or ""
        )
        family = str(image_plan.get("family") or plan.get("family") or "image")
        if not model and configured:
            model = str(configured[0].get("default_model") or "")
        if family == "image":
            try:
                family = str(
                    get_image_model_config(config, provider, model).get("family")
                    or family
                )
            except (KeyError, ValueError):
                pass
        if isinstance(summary, dict):
            returncode = 0 if summary.get("pass") is True else 1
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
                job_spec.get("api_form")
                or (image_plan.get("model_capability_profile") or {}).get("api_form")
                or ""
            ),
            route_profile=str(job_spec.get("route_profile") or "dynamic_aggregator"),
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
                "LOADTEST_JOB_SPEC": str(job.report_dir / "job_spec.json"),
                "PYTHONUNBUFFERED": "1",
            },
        )
        if job.reference_source:
            env["LOADTEST_REFERENCE_SOURCE"] = job.reference_source
        if job.type == "param_test":
            env["LOADTEST_PARAM_TEST_RUNS"] = str(job.param_test_runs)
            env["LOADTEST_TOOL_VALIDATION_MODE"] = job.tool_validation_mode
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
            job.returncode = returncode
            job.finished_at = time.time()
            if job.stop_requested:
                job.status = "stopped"
            else:
                job.status = "completed" if returncode == 0 else "failed"
            _ensure_load_result(job)

    def _refresh_locked(self, job: Job) -> Job:
        if job.process is None:
            return job
        returncode = job.process.poll()
        if returncode is not None and job.status in {"queued", "running", "stopping"}:
            job.returncode = returncode
            job.finished_at = job.finished_at or time.time()
            job.status = (
                "stopped"
                if job.stop_requested
                else ("completed" if returncode == 0 else "failed")
            )
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
    default_reference_source = default_reference_source_for_model(
        config,
        family,
        model,
        provider,
        api_form=api_form,
        route_profile=route_profile,
    )
    capability_registry = _capability_registry_payload(config)
    return jsonify(
        {
            "active_provider": provider,
            "active_model": model,
            "model_family": family,
            "default_api_form": api_form,
            "route_profile": route_profile,
            "default_reference_source": default_reference_source,
            "reference_sources": list_reference_sources(),
            "model_capabilities": capability_registry["text"],
            "providers": list_public_providers(config),
            "image_providers": list_image_providers(config),
            "image_model_capabilities": capability_registry["image"],
            "capability_summary": _capability_registry_summary(capability_registry),
            "image_defaults": {
                "suite": "smoke",
                "quality": "low",
                "output_format": "png",
                "include_2k": False,
                "include_4k": False,
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
                        source = default_reference_source_for_model(
                            config,
                            family,
                            model_id,
                            provider,
                            api_form=form_id,
                            route_profile=route,
                        )
                        capability = load_model_capability_profile(
                            "text",
                            family,
                            model_id,
                            api_form=form_id,
                            route_profile=route,
                            reference_source=source,
                            provider_override=get_model_api_forms(
                                config,
                                model_id,
                                provider,
                                route_profile=route,
                            )[form_id],
                        )
                        allowed_sources = reference_sources_for_model(
                            config,
                            family,
                            model_id,
                            provider,
                            api_form=form_id,
                            route_profile=route,
                        )
                        api_form_rows[form_id] = {
                            "api_form": form_id,
                            "transport": capability.get("transport"),
                            "route_profile": route,
                            "reference_source": source,
                            "reference_sources": allowed_sources,
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
                            "disabled_reason": capability.get("disabled_reason"),
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
                            "suite": capability.get("suite"),
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
    return {"text": text, "image": image}


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
            reference_source = str(
                request.args.get("reference_source")
                or default_reference_source_for_family(
                    family,
                    route_profile=route_profile,
                    api_form=api_form,
                )
            )
            reference = get_reference_source(reference_source)
            if str(reference.get("model_family") or "") != family:
                raise ValueError(
                    f"Reference source {reference_source!r} does not belong to "
                    f"family {family!r}."
                )
            if api_form and str(reference.get("api_form") or "") != api_form:
                raise ValueError(
                    f"Reference source {reference_source!r} does not belong to "
                    f"API form {api_form!r}."
                )
            if (
                route_profile
                and str(reference.get("route_profile") or "") != route_profile
            ):
                raise ValueError(
                    f"Reference source {reference_source!r} does not belong to "
                    f"route profile {route_profile!r}."
                )
        except (KeyError, RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(reference_spec_payload(reference_source))

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
    default_source = default_reference_source_for_model(
        config,
        family,
        model,
        provider,
        api_form=api_form,
        route_profile=route_profile,
    )
    reference_source = str(request.args.get("reference_source") or default_source)
    allowed_reference_sources = reference_sources_for_model(
        config,
        family,
        model,
        provider,
        api_form=api_form,
        route_profile=route_profile,
    )
    if reference_source not in allowed_reference_sources:
        return jsonify(
            {
                "error": (
                    f"Reference source {reference_source!r} is not part of the "
                    f"{family}/{model} family suite."
                ),
                "allowed_reference_sources": allowed_reference_sources,
            }
        ), 400
    return jsonify(
        model_reference_spec_payload(
            "text",
            family,
            model,
            reference_source,
            api_form=api_form,
            route_profile=route_profile,
            provider_override=get_model_api_forms(
                config,
                model,
                provider,
                route_profile=route_profile,
            )[api_form],
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
    reference_source = str(
        request.args.get("reference_source")
        or default_reference_source_for_model(
            config,
            family,
            model,
            provider,
            api_form=api_form,
            route_profile=route_profile,
        )
    )
    tool_validation_mode = str(request.args.get("tool_validation_mode") or "auto")
    if tool_validation_mode not in TOOL_VALIDATION_MODES:
        return jsonify({"error": "invalid tool_validation_mode"}), 400
    capability = load_model_capability_profile(
        "text",
        family,
        model,
        route_profile=route_profile,
        api_form=api_form,
        reference_source=reference_source,
        provider_override=get_model_api_forms(
            config,
            model,
            provider,
            route_profile=route_profile,
        )[api_form],
    )
    result = JOB_MANAGER.latest_param_result(
        provider,
        model,
        route_profile,
        api_form,
        str(capability.get("model_api_profile_id") or ""),
        reference_source,
        tool_validation_mode,
    )
    return jsonify({"result": result})


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


@app.get("/reports/<path:relpath>")
def reports_file(relpath: str) -> Any:
    return send_from_directory(REPORTS_ROOT, relpath)


def _command_for_job(
    job_type: str,
    report_dir: Path,
    users: int | None,
    spawn_rate: int | None,
    duration: str,
) -> list[str]:
    if job_type == "param_test":
        return [sys.executable, "scripts/param_test.py"]
    if job_type == "cache_suite":
        return [sys.executable, "scripts/run_cache.py"]
    if job_type == "staircase":
        return [sys.executable, "scripts/run_staircase.py"]
    if job_type == "soak":
        return [sys.executable, "scripts/run_soak.py"]
    return [
        sys.executable,
        "-m",
        "locust",
        "-f",
        "locustfile.py",
        "--headless",
        "-u",
        str(users or DEFAULT_QUICK_USERS),
        "-r",
        str(spawn_rate or DEFAULT_QUICK_SPAWN_RATE),
        "-t",
        duration,
        "--csv",
        str(report_dir / "locust"),
        "--html",
        str(report_dir / "report.html"),
    ]


def _image_command_for_job(
    report_dir: Path,
    image_plan: dict[str, Any],
) -> list[str]:
    command = [
        sys.executable,
        "scripts/image_param_test.py",
        "--base-url",
        str(image_plan["endpoint"]),
        "--provider",
        str(image_plan["provider"]),
        "--api-key-env",
        SELECTED_API_KEY_ENV,
        "--model",
        str(image_plan["model"]),
        "--family",
        str(image_plan["family"]),
        "--route-profile",
        str(image_plan["route_profile"]),
        "--api-form",
        str(image_plan["api_form"]),
        "--transport",
        str(image_plan["transport"]),
        "--auth-mode",
        str(image_plan["auth_mode"]),
        "--suite",
        str(image_plan["suite"]),
        "--timeout",
        str(image_plan["timeout_sec"]),
        "--output-dir",
        str(report_dir),
    ]
    if image_plan.get("quality") is not None:
        command.extend(["--quality", str(image_plan["quality"])])
    if image_plan.get("output_format") is not None:
        command.extend(["--output-format", str(image_plan["output_format"])])
    if image_plan.get("include_2k"):
        command.append("--include-2k")
    if image_plan.get("include_4k"):
        command.append("--include-4k")
    if image_plan.get("no_negative"):
        command.append("--no-negative")
    if image_plan.get("no_cross_control"):
        command.append("--no-cross-control")
    if image_plan.get("visual_forensics") is False:
        command.append("--no-visual-forensics")
    for case_name in image_plan.get("cases") or []:
        command.extend(["--case", str(case_name)])
    return command


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
        for records_path in sorted(JOBS_ROOT.rglob("request_records.jsonl")):
            report_dir = records_path.parent.resolve()
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
        and cached.get("profile_stats")
        and isinstance(cached.get("time_series"), list)
    ):
        return cached

    records = _load_result_records(report_dir)
    measured = _measured_load_records(records)
    if not measured:
        return None

    metadata = _infer_load_result_metadata(
        report_dir,
        measured,
        result_type=result_type,
        provider=provider,
        provider_label=provider_label,
        model=model,
        model_family=model_family,
        workload=workload,
        users=users,
        spawn_rate=spawn_rate,
        duration=duration,
        status=status,
        returncode=returncode,
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        target_rpm=target_rpm,
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
    )
    result = {
        **metadata,
        "schema_version": LOAD_RESULT_SCHEMA_VERSION,
        "id": report_dir.relative_to(REPORTS_ROOT.resolve()).as_posix(),
        "report_dir": str(report_dir),
        "summary": summary,
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


def _measured_load_records(records: list[RequestRecord]) -> list[RequestRecord]:
    return [
        item
        for item in records
        if item.task_name.startswith("chat:")
        and item.group in {"throughput_profiles", "compatibility_profiles"}
        and not item.is_warmup
        and not item.is_retry
    ]


def _infer_load_result_metadata(
    report_dir: Path,
    measured: list[RequestRecord],
    **overrides: Any,
) -> dict[str, Any]:
    first = measured[0]
    extra = first.extra or {}
    timestamps = [item.timestamp for item in measured if item.timestamp]
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
    passed = sum(1 for item in results if item.get("pass") is True)
    failed = sum(1 for item in results if item.get("pass") is False)
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


def _job_progress(
    job: Job,
    summary: dict[str, Any] | None,
    verdict: dict[str, Any] | None,
    param_results: Any,
    cache_progress: dict[str, Any] | None,
) -> dict[str, Any]:
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
        passed = sum(1 for item in param_results if item.get("status") == "pass")
        failed = sum(1 for item in param_results if item.get("status") == "fail")
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
    total = _reference_profile_count(job.reference_source) * max(
        int(job.param_test_runs or 1), 1
    )
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


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        process.terminate()
    try:
        process.wait(timeout=5)
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
      $("paramTestRuns").value = config.defaults.param_test_runs || 3;
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
      const payload = await fetch(`/api/param-specs?reference_source=${encodeURIComponent(referenceSource)}`, { cache: "no-store" }).then(r => r.json());
      $("paramSpecs").innerHTML = payload.comparison.map(row =>
        `<tr><td>${esc(row.parameter)}</td><td>${esc(row.official)}</td><td>${esc(row.local)}</td><td>${esc(row.coverage)}</td></tr>`
      ).join("");
      renderParamRunHint();
    }

    function paramTestRunsValue() {
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
      $("paramRunHint").textContent = `${cells} cells = ${profiles} profiles × ${runs} runs`;
    }

    async function createJob(type) {
      $("actionError").textContent = "";
      const paramTestRuns = paramTestRunsValue();
      $("paramTestRuns").value = paramTestRuns;
      const payload = {
        type,
        provider: $("provider").value,
        model: $("model").value,
        reference_source: $("referenceSource").value,
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
    $("referenceSource").addEventListener("change", () => { loadParamSpecs(); });
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
        user, password = generated
        print("=" * 60)
        print(f"Web console credentials generated (stored in {CONSOLE_AUTH_PATH}):")
        print(f"  user:     {user}")
        print(f"  password: {password}")
        print("  override anytime via WEB_CONSOLE_USER / WEB_CONSOLE_PASSWORD")
        print("=" * 60, flush=True)
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
