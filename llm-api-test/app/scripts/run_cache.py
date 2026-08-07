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
from lib.job_spec import load_job_spec
from lib.threshold import check_cache


def main() -> int:
    config = load_config()
    job_spec = load_job_spec(os.getenv("LOADTEST_JOB_SPEC"))
    if job_spec and isinstance(job_spec.get("cache_plan"), dict):
        config["cache_test"] = dict(job_spec["cache_plan"])
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
