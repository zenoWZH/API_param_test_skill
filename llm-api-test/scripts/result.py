from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_env

skill_env.ensure_skill_env()
sys.path.insert(0, str(skill_env.APP_ROOT))

from lib.config import default_reports_root  # noqa: E402

PARAM_KEYS = [
    "pass",
    "provider",
    "provider_label",
    "model",
    "model_family",
    "reference_source",
    "reference_label",
    "stage",
    "compatibility_pass",
    "token_accuracy_pass",
    "model_identity_pass",
    "model_identity_summary",
    "overall_success_rate",
    "passed",
    "failed",
    "passed_supported",
    "incompatible",
    "tested_params",
    "failures",
    "incompatibilities",
    "token_audit_summary",
    "performance_summary",
]
CACHE_KEYS = [
    "pass",
    "threshold_pass",
    "stage",
    "mode",
    "latency_speedup_ratio",
    "latency_evidence_only",
    "official_usage_required_for_hit_rate",
    "summary",
    "failures",
]
STAIRCASE_KEYS = [
    "pass",
    "stage",
    "target_business_rpm_min",
    "target_total_tpm_min",
    "max_qualified_business_rpm",
    "max_qualified_total_tpm",
    "max_quality_passing_business_rpm",
    "peak_business_rpm",
    "peak_total_tpm",
    "highest_passing_step",
    "first_failing_step",
    "failures",
]
TRACE_KEYS = [
    "pass",
    "provider",
    "model",
    "best_match",
    "best_match_label",
    "best_score",
    "expected",
    "match_expected",
    "threshold",
    "per_candidate_scores",
    "corpus_entries",
]


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _pick(payload: dict, keys: list[str]) -> dict:
    return {key: payload.get(key) for key in keys if payload.get(key) is not None}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print a condensed result JSON for a job, ready for summarization."
    )
    parser.add_argument("--id", dest="job_id", required=True)
    parser.add_argument("--full", action="store_true", help="dump raw verdict/summary files")
    args = parser.parse_args()
    report_dir = default_reports_root() / "jobs" / args.job_id
    if not report_dir.is_dir():
        print(json.dumps({"error": f"job not found: {args.job_id}"}), file=sys.stderr)
        return 2
    job_spec = _read_json(report_dir / "job_spec.json") or {}
    run = _read_json(report_dir / "run.json") or {}
    verdict = _read_json(report_dir / "verdict.json") or {}
    output: dict = {
        "job_id": args.job_id,
        "type": job_spec.get("type") or run.get("type"),
        "provider": job_spec.get("provider") or run.get("provider"),
        "model": job_spec.get("model") or run.get("model"),
        "returncode": run.get("returncode"),
        "report_dir": str(report_dir),
    }
    job_type = output["type"]
    if args.full:
        output["verdict"] = verdict or None
        output["summary"] = _read_json(report_dir / "summary.json")
        output["load_result"] = _read_json(report_dir / "load_result.json")
        output["param_results"] = _read_json(report_dir / "param_results.json")
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    if job_type == "param_test":
        output["result"] = _pick(verdict, PARAM_KEYS)
    elif job_type == "cache_suite":
        output["result"] = _pick(verdict, CACHE_KEYS)
    elif job_type in {"staircase", "quick_load", "soak"}:
        load_result = _read_json(report_dir / "load_result.json") or {}
        output["result"] = _pick(verdict, STAIRCASE_KEYS)
        if load_result:
            output["result"]["load_result"] = load_result
    elif job_type == "trace_test":
        output["result"] = _pick(verdict, TRACE_KEYS)
    elif job_type == "image_param_test":
        output["result"] = _read_json(report_dir / "summary.json") or _pick(
            verdict, ["pass", "failures"]
        )
    else:
        output["result"] = verdict or _read_json(report_dir / "summary.json")
    if not output.get("result"):
        output["hint"] = "no verdict yet; job may still be running (check jobs.py --id)"
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
