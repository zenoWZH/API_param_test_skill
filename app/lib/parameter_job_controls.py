"""Immutable functional execution controls; validation performs no live I/O."""
from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping
from typing import Any

PARAMETER_JOB_SPEC_VERSION = 5
PARAMETER_EXECUTION_SCHEMA_VERSION = 1
WORKFLOW_JOB_SPEC_VERSION = 6
WORKFLOW_EXECUTION_SCHEMA_VERSION = 2
WORKFLOW_JOB_TYPES = frozenset({"param_test", "image_param_test"})
DEFAULT_PARAM_TEST_RUNS = 1
MAX_PARAM_TEST_RUNS = 1000
TOOL_VALIDATION_MODES = frozenset({
    "auto", "openai_compat", "gemini_native", "gemini_interactions",
    "claude_native", "openai_responses",
})
_CONFIG_KEY = "_parameter_execution_controls"


def make_parameter_execution(
    runs: int = DEFAULT_PARAM_TEST_RUNS, tool_validation_mode: str = "auto",
) -> dict[str, Any]:
    if type(runs) is not int or not 1 <= runs <= MAX_PARAM_TEST_RUNS:
        raise ValueError("parameter_execution.runs must be an integer from 1 to 1000")
    if not isinstance(tool_validation_mode, str) or tool_validation_mode not in TOOL_VALIDATION_MODES:
        raise ValueError("parameter_execution.tool_validation_mode is not supported")
    return {"schema_version": PARAMETER_EXECUTION_SCHEMA_VERSION,
            "runs": runs, "tool_validation_mode": tool_validation_mode}


def parameter_execution_from_job(job_spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate new snapshots; do not fabricate controls for historical jobs."""
    if not isinstance(job_spec, dict):
        return None
    if job_spec.get("schema_version") == WORKFLOW_JOB_SPEC_VERSION:
        return workflow_execution_from_job(job_spec)
    if "execution_plan" in job_spec:
        raise ValueError("execution_plan requires a schema-v6 functional job")
    if job_spec.get("schema_version") != PARAMETER_JOB_SPEC_VERSION:
        if "parameter_execution" in job_spec:
            raise ValueError("parameter_execution requires a schema-v5 parameter job")
        return None
    if type(job_spec.get("schema_version")) is not int or job_spec.get("type") != "param_test":
        raise ValueError("Schema-v5 is reserved for text parameter jobs")
    controls = job_spec.get("parameter_execution")
    if not isinstance(controls, dict) or set(controls) != {"schema_version", "runs", "tool_validation_mode"}:
        raise ValueError("Schema-v5 parameter job requires complete parameter_execution controls")
    if type(controls["schema_version"]) is not int or controls["schema_version"] != PARAMETER_EXECUTION_SCHEMA_VERSION:
        raise ValueError("Unsupported parameter_execution schema_version")
    validated = make_parameter_execution(controls["runs"], controls["tool_validation_mode"])
    if (job_spec.get("parameter_suite") or job_spec.get("api_form") == "deepseek_beta_chat_prefix") and validated["runs"] != 1:
        raise ValueError("Fixed parameter suites require exactly one run")
    return validated


def historical_parameter_execution(job_spec: dict[str, Any] | None) -> dict[str, Any]:
    """Keep corrupt historical jobs readable; the result classifier flags them."""
    try:
        return parameter_execution_from_job(job_spec) or {}
    except ValueError:
        return {}


def bind_parameter_execution(
    config: dict[str, Any], job_spec: dict[str, Any] | None, environment: Mapping[str, str],
) -> None:
    # Never inherit an internal context from a previously used config object.
    config.pop(_CONFIG_KEY, None)
    controls = parameter_execution_from_job(job_spec)
    if controls is None:
        return
    raw_runs = environment.get("LOADTEST_PARAM_TEST_RUNS")
    if raw_runs not in (None, ""):
        try:
            matches = int(raw_runs) == controls["runs"]
        except (TypeError, ValueError):
            matches = False
        if not matches:
            raise ValueError("LOADTEST_PARAM_TEST_RUNS conflicts with immutable parameter_execution")
    raw_mode = environment.get("LOADTEST_TOOL_VALIDATION_MODE")
    if raw_mode not in (None, "", controls["tool_validation_mode"]):
        raise ValueError("LOADTEST_TOOL_VALIDATION_MODE conflicts with immutable parameter_execution")
    if controls["schema_version"] == WORKFLOW_EXECUTION_SCHEMA_VERSION:
        raw_digest = environment.get("LOADTEST_TEST_PLAN_DIGEST")
        if raw_digest not in (None, "", controls["plan_digest"]):
            raise ValueError("LOADTEST_TEST_PLAN_DIGEST conflicts with immutable parameter_execution")
        raw_cases = environment.get("LOADTEST_TEST_CASES")
        if raw_cases not in (None, ""):
            try:
                selected = json.loads(raw_cases)
            except (TypeError, ValueError) as exc:
                raise ValueError("LOADTEST_TEST_CASES conflicts with immutable parameter_execution") from exc
            if selected != controls["requested_cases"]:
                raise ValueError("LOADTEST_TEST_CASES conflicts with immutable parameter_execution")
    config[_CONFIG_KEY] = copy.deepcopy(controls)


def bound_parameter_execution(config: dict[str, Any] | None) -> dict[str, Any]:
    return (config or {}).get(_CONFIG_KEY) or {}


def parameter_execution_result_state(
    job_spec: dict[str, Any], result: dict[str, Any],
) -> tuple[str, list[str]]:
    if job_spec.get("schema_version") == WORKFLOW_JOB_SPEC_VERSION:
        return workflow_execution_result_state(job_spec, result)
    try:
        controls = parameter_execution_from_job(job_spec)
    except ValueError:
        return "invalid", ["parameter_execution_invalid"]
    if controls is None:
        return "legacy_unfrozen" if job_spec.get("type") == "param_test" else "not_applicable", []
    reasons = []
    if type(result.get("param_test_runs")) is not int or result["param_test_runs"] != controls["runs"]:
        reasons.append("parameter_execution_runs_mismatch")
    if result.get("tool_validation_mode") != controls["tool_validation_mode"]:
        reasons.append("parameter_execution_tool_validation_mode_mismatch")
    return ("mismatch" if reasons else "frozen"), reasons


_CONTROL_FIELDS = frozenset({
    "schema_version", "runs", "tool_validation_mode", "plan_digest",
    "requested_cases", "selected_cases", "limits",
})


def _validated_execution_plan(plan: Any) -> dict[str, Any]:
    """Validate the frozen definition before credentials or network access.

    Execution also checks the current handler registry before constructing a
    dispatcher. Historical reads rely on frozen signatures instead of current code.
    """
    from .test_runner import validate_plan
    return validate_plan(plan)


def _validate_workflow_target(job_spec: dict[str, Any], plan: dict[str, Any]) -> None:
    target = plan["target"]
    nested = target.get("execution_target")
    if nested is not None and not isinstance(nested, dict):
        raise ValueError("execution_plan.execution_target must be an object")
    views = (target, nested) if nested is not None else (target,)
    aliases = {"provider": "provider_id", "model": "request_model_id"}
    for field in ("provider", "model", "route_profile", "api_form"):
        names = (field, aliases[field]) if field in aliases else (field,)
        declared = [view[name] for view in views for name in names if name in view]
        expected = job_spec.get(field)
        # Registry targets can identify a route through their immutable interface;
        # when an explicit route is present, every redundant view must agree.
        required = field != "route_profile" or nested is None
        if (required and (expected in (None, "") or not declared)
                or any(value != expected for value in declared)):
            raise ValueError(f"execution_plan target conflicts with Job {field}")
    for field in ("source_id", "profile_id", "interface_id", "test_binding_id",
                  "reference_contract_id", "transport"):
        aliases = {"reference_contract_id": "contract_id", "transport": "transport_adapter_id"}
        names = (field, aliases[field]) if field in aliases else (field,)
        declared = [view[name] for view in views for name in names if name in view]
        if any(value != job_spec.get(field) for value in declared):
            raise ValueError(f"execution_plan target conflicts with Job {field}")


def make_workflow_execution(execution_plan: dict[str, Any],
                            tool_validation_mode: str = "auto") -> dict[str, Any]:
    plan = _validated_execution_plan(execution_plan)
    base = make_parameter_execution(plan["run_count"], tool_validation_mode)
    return {**base, "schema_version": WORKFLOW_EXECUTION_SCHEMA_VERSION,
            "plan_digest": plan["plan_digest"],
            "requested_cases": copy.deepcopy(plan["requested_cases"]),
            "selected_cases": copy.deepcopy(plan["selected_cases"]),
            "limits": copy.deepcopy(plan["limits"])}


def freeze_workflow_job(job_spec: dict[str, Any], execution_plan: dict[str, Any],
                        tool_validation_mode: str = "auto") -> dict[str, Any]:
    """Upgrade a functional snapshot by freezing a reviewed compiled plan."""
    if not isinstance(job_spec, dict) or job_spec.get("type") not in WORKFLOW_JOB_TYPES:
        raise ValueError("Schema-v6 is reserved for functional parameter/image jobs")
    frozen = copy.deepcopy(job_spec)
    frozen["schema_version"] = WORKFLOW_JOB_SPEC_VERSION
    frozen["execution_plan"] = copy.deepcopy(execution_plan)
    frozen["parameter_execution"] = make_workflow_execution(execution_plan, tool_validation_mode)
    workflow_execution_from_job(frozen)
    return frozen


def workflow_execution_from_job(job_spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return v2 controls only for a complete immutable v6 functional JobSpec."""
    if not isinstance(job_spec, dict):
        return None
    version = job_spec.get("schema_version")
    if version != WORKFLOW_JOB_SPEC_VERSION:
        if "execution_plan" in job_spec:
            raise ValueError("execution_plan requires a schema-v6 functional job")
        return None
    if type(version) is not int or job_spec.get("type") not in WORKFLOW_JOB_TYPES:
        raise ValueError("Schema-v6 is reserved for functional parameter/image jobs")
    controls = job_spec.get("parameter_execution")
    if not isinstance(controls, dict) or set(controls) != _CONTROL_FIELDS:
        raise ValueError("Schema-v6 requires complete parameter_execution controls")
    if type(controls["schema_version"]) is not int or controls["schema_version"] != WORKFLOW_EXECUTION_SCHEMA_VERSION:
        raise ValueError("Unsupported workflow parameter_execution schema_version")
    plan = _validated_execution_plan(job_spec.get("execution_plan"))
    _validate_workflow_target(job_spec, plan)
    expected = make_parameter_execution(plan["run_count"], controls["tool_validation_mode"])
    expected.update(schema_version=WORKFLOW_EXECUTION_SCHEMA_VERSION,
                    plan_digest=plan["plan_digest"], requested_cases=plan["requested_cases"],
                    selected_cases=plan["selected_cases"], limits=plan["limits"])
    if (type(controls["runs"]) is not int or controls != expected
            or not isinstance(controls["limits"], dict)
            or any(type(controls["limits"].get(key)) is not type(value)
                   for key, value in expected["limits"].items())):
        raise ValueError("parameter_execution conflicts with immutable execution_plan")
    return copy.deepcopy(expected)


def frozen_execution_plan_from_job(job_spec: dict[str, Any]) -> dict[str, Any] | None:
    controls = workflow_execution_from_job(job_spec)
    return copy.deepcopy(job_spec["execution_plan"]) if controls is not None else None


def bind_workflow_execution(config: dict[str, Any], job_spec: dict[str, Any],
                            environment: Mapping[str, str]) -> None:
    """Bind immutable controls through the same path as historical v5 jobs."""
    bind_parameter_execution(config, job_spec, environment)


def workflow_execution_result_state(job_spec: dict[str, Any],
                                    result: dict[str, Any]) -> tuple[str, list[str]]:
    try:
        controls = workflow_execution_from_job(job_spec)
    except (KeyError, TypeError, ValueError):
        return "invalid", ["workflow_execution_invalid"]
    if controls is None:
        return "not_applicable", []
    report = result.get("workflow_result", result)
    if not isinstance(report, dict):
        return "mismatch", ["workflow_result_missing"]
    plan = job_spec["execution_plan"]
    reasons: list[str] = []
    # The nested workflow report is authoritative, but any legacy summary
    # aliases that are present must describe the same frozen controls.
    if "param_test_runs" in result and (
        type(result["param_test_runs"]) is not int or result["param_test_runs"] != controls["runs"]
    ):
        reasons.append("parameter_execution_runs_mismatch")
    if "tool_validation_mode" in result and result["tool_validation_mode"] != controls["tool_validation_mode"]:
        reasons.append("parameter_execution_tool_validation_mode_mismatch")
    if report.get("cleanup_only") is True:
        reasons.append("workflow_cleanup_only_result")
    if type(report.get("report_schema_version")) is not int or report["report_schema_version"] != 1:
        reasons.append("workflow_result_schema_mismatch")
    for field in ("plan_digest", "workflow_id", "target"):
        if report.get(field) != plan[field]:
            reasons.append(f"workflow_result_{field}_mismatch")
    if report.get("status") not in ("passed", "failed", "cancelled", "incomplete"):
        reasons.append("workflow_result_status_invalid")
    runs = report.get("runs")
    if not isinstance(runs, list) or len(runs) != controls["runs"]:
        reasons.append("workflow_execution_runs_mismatch")
        runs = runs if isinstance(runs, list) else []
    run_ids: set[str] = set()
    expected_steps = {step["id"] for step in plan["ordered_steps"]}
    cleanup_incomplete = False
    for index, run in enumerate(runs, start=1):
        if not isinstance(run, dict):
            reasons.append("workflow_result_run_invalid")
            continue
        run_id = run.get("run_id")
        if not isinstance(run_id, str) or not run_id or run_id in run_ids:
            reasons.append("workflow_result_run_identity_invalid")
        else:
            run_ids.add(run_id)
        if type(run.get("run_index")) is not int or run["run_index"] != index:
            reasons.append("workflow_result_run_index_mismatch")
        if run.get("status") not in ("passed", "failed", "cancelled", "incomplete"):
            reasons.append("workflow_result_run_status_invalid")
        steps = run.get("steps")
        if (not isinstance(steps, dict) or not set(steps).issubset(expected_steps)
                or run.get("status") == "passed" and (
                    set(steps) != expected_steps
                    or any(not isinstance(step, dict) or step.get("status") != "passed"
                           or type(step.get("accepted")) is not bool for step in steps.values()))):
            reasons.append("workflow_result_steps_incomplete")
        counters = (run.get("business_request_count"), run.get("cleanup_request_count"))
        if any(type(value) is not int or value < 0 for value in counters):
            reasons.append("workflow_result_request_count_invalid")
        else:
            if (counters[0] > controls["limits"]["max_requests"]
                    or counters[1] > controls["limits"]["cleanup_max_requests"]):
                reasons.append("workflow_result_request_budget_exceeded")
            attempts = run.get("attempts")
            previous = run.get("previous_attempt_count", 0)
            if (type(run.get("request_count")) is not int or run["request_count"] != sum(counters)
                    or type(previous) is not int or previous < 0
                    or not isinstance(attempts, list) or len(attempts) != sum(counters) + previous):
                reasons.append("workflow_result_attempt_count_mismatch")
        cleanup = run.get("cleanup")
        if (not isinstance(cleanup, dict) or cleanup.get("status") != "passed"
                or cleanup.get("unknown_creations") != []):
            cleanup_incomplete = True
        elif (not isinstance(cleanup.get("resources"), list)
              or any(not isinstance(resource, dict) or resource.get("status") != "deleted"
                     or resource.get("run_id") != run_id for resource in cleanup["resources"])):
            cleanup_incomplete = True
    if cleanup_incomplete:
        reasons.append("workflow_cleanup_incomplete")
    if not reasons:
        from .test_runner import digest_json, summarize_plan_runs
        summary = summarize_plan_runs(plan, runs)
        if report.get("status") != summary["status"]:
            reasons.append("workflow_result_status_inconsistent")
        if "case_outcomes" in report:
            try:
                matches = digest_json(report["case_outcomes"]) == digest_json(summary["case_outcomes"])
            except (TypeError, ValueError):
                matches = False
            if not matches:
                reasons.append("workflow_case_outcomes_mismatch")
        elif "any" in plan["case_policies"].values():
            reasons.append("workflow_case_outcomes_missing")
    if reasons:
        return ("incomplete" if cleanup_incomplete else "mismatch"), sorted(set(reasons))
    return "frozen", []


def workflow_termination_grace_seconds(job_spec: dict[str, Any],
                                       remaining_request_timeout: float | None = None) -> float:
    """Allow the bounded in-flight request, reserved cleanup phase and five seconds."""
    controls = workflow_execution_from_job(job_spec)
    if controls is None:
        raise ValueError("Workflow termination grace requires a schema-v6 JobSpec")
    request_timeout = controls["limits"]["request_timeout_seconds"]
    remaining = request_timeout if remaining_request_timeout is None else remaining_request_timeout
    if type(remaining) not in (int, float) or not math.isfinite(remaining) or remaining < 0:
        raise ValueError("remaining_request_timeout must be finite and nonnegative")
    return min(float(remaining), float(request_timeout)) + controls["limits"]["cleanup_deadline_seconds"] + 5.0
