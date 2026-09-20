from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.cache_suite import run_cache_suite
from lib.client import DeepSeekClient
from lib.config import default_reports_root, get_active_provider_name, get_selected_model, load_config, resolve_threshold_config
from lib.job_spec import (
    CURRENT_MPDB_SNAPSHOT_SCHEMA_VERSION,
    JOB_SPEC_VERSION,
    load_job_spec,
    resolve_job_model_profile_snapshot,
)
from lib.threshold import check_cache


def _require_current_cache_job_spec(job_spec: dict) -> dict:
    """Reject legacy cache replays instead of resolving them from live config."""

    if job_spec.get("schema_version") != JOB_SPEC_VERSION:
        raise RuntimeError(
            f"Cache job replay requires schema {JOB_SPEC_VERSION}; legacy jobs "
            "remain display-only."
        )
    if job_spec.get("type") != "cache_suite":
        raise RuntimeError("Cache runner requires a cache_suite Job spec.")
    if not isinstance(job_spec.get("cache_plan"), dict) or not job_spec["cache_plan"]:
        raise RuntimeError("Cache job replay requires a frozen cache_plan.")
    snapshot = job_spec.get("model_profile_database")
    if not isinstance(snapshot, dict) or not snapshot:
        raise RuntimeError(
            "Cache job replay requires an immutable MPDB snapshot; refusing "
            "to reinterpret the Job through current runtime configuration."
        )
    snapshot_version = snapshot.get("snapshot_schema_version")
    if (
        isinstance(snapshot_version, bool)
        or not isinstance(snapshot_version, int)
        or snapshot_version != CURRENT_MPDB_SNAPSHOT_SCHEMA_VERSION
    ):
        raise RuntimeError(
            "Cache job replay requires MPDB snapshot schema "
            f"{CURRENT_MPDB_SNAPSHOT_SCHEMA_VERSION}."
        )
    resolved = resolve_job_model_profile_snapshot(job_spec)
    if resolved.get("resolution_status") != "snapshot":
        raise RuntimeError(
            "Cache job replay requires a valid immutable MPDB snapshot: "
            f"{resolved.get('error') or resolved.get('resolution_status')}"
        )
    return snapshot


def main() -> int:
    config = load_config()
    job_spec = load_job_spec(os.getenv("LOADTEST_JOB_SPEC"))
    if job_spec:
        snapshot = _require_current_cache_job_spec(job_spec)
        config["cache_test"] = dict(job_spec["cache_plan"])
        config["_model_profile_database"] = dict(snapshot)
    cache_plan = config.get("cache_test") or {}
    config.setdefault("thresholds", {})["cache"] = resolve_threshold_config(
        config,
        "cache",
        get_active_provider_name(config),
        get_selected_model(config),
        cache_plan.get("thresholds") if isinstance(cache_plan, dict) else None,
    )
    client = DeepSeekClient.from_config(config)
    output_dir = Path(os.getenv("LOADTEST_REPORT_DIR") or default_reports_root() / "cache")
    measured_requests_raw = os.getenv("LOADTEST_CACHE_MEASURED_REQUESTS")
    measured_requests = (
        int(measured_requests_raw) if measured_requests_raw else None
    )
    result = run_cache_suite(
        config,
        client,
        output_dir,
        measured_requests=measured_requests,
    )
    verdict = check_cache(result, config, output_dir)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
