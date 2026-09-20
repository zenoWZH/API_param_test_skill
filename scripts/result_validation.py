"""Use the engine's current evidence rules in every standalone result view."""
from __future__ import annotations


def classify_result(job_spec: dict, result: dict, job_type: str | None = None):
    from lib.job_spec import classify_cache_result, classify_parameter_result, classify_workflow_result

    if not result:
        return None
    kind = job_type or job_spec.get("type")
    if kind == "cache_suite":
        return classify_cache_result(job_spec, result)
    if kind in {"param_test", "image_param_test"}:
        if job_spec.get("schema_version") == 6 and "test_workflow_snapshot" in job_spec:
            return classify_workflow_result(job_spec, result)
        return classify_parameter_result(job_spec, result, job_type=kind)
    return None
