"""Deterministic full-suite selection, prerequisite closure and frozen-plan validation."""
from __future__ import annotations

import heapq
import math

from .common import PlanValidationError, digest_json, json_copy, require_id
from .references import references, validate_path
from .registry import HandlerRegistry
from . import common

DEFAULT_LIMITS = {
    "max_requests": 1000,
    "request_timeout_seconds": 60,
    "deadline_seconds": 3600,
    "cleanup_max_requests": 100,
    "cleanup_deadline_seconds": 120,
}
DEFAULT_POLICY = {"fatal_status_codes": [401, 403, 429]}


def _limits(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULT_LIMITS):
        raise PlanValidationError("Unknown or invalid limits")
    result = {**DEFAULT_LIMITS, **value}
    for name, amount in result.items():
        if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0 or (amount == 0 and name != "cleanup_max_requests"):
            raise PlanValidationError(f"{name} must be positive and finite")
        if name.endswith("max_requests") and type(amount) is not int:
            raise PlanValidationError(f"{name} must be an integer")
    return result


def _policy(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise PlanValidationError("Unknown or invalid execution policy")
    result = {**DEFAULT_POLICY, **value}
    codes = result["fatal_status_codes"]
    if not isinstance(codes, list) or any(type(code) is not int or not 100 <= code <= 599 for code in codes):
        raise PlanValidationError("fatal_status_codes must be HTTP status integers")
    result["fatal_status_codes"] = sorted(set(codes))
    return result


def _compile(workflow: dict, bindings: dict, *, selected_cases=None, run_count=1, target=None, engine_binding=None) -> dict:
    definition = json_copy(workflow)
    if not isinstance(definition, dict) or type(definition.get("workflow_schema_version")) is not int or definition["workflow_schema_version"] != 1:
        raise PlanValidationError("workflow_schema_version must be 1")
    workflow_id = require_id(definition.get("id"), "Workflow ID")
    actual_target = definition.get("target")
    if not isinstance(actual_target, dict) or not actual_target:
        raise PlanValidationError("Workflow requires an exact nonempty target object")
    if target is not None and digest_json(actual_target) != digest_json(target):
        raise PlanValidationError("Workflow target mismatch")
    if type(run_count) is not int or run_count < 1:
        raise PlanValidationError("run_count must be a positive integer")
    cases = definition.get("cases")
    steps = definition.get("steps")
    if not isinstance(cases, list) or not cases or not isinstance(steps, list) or not steps:
        raise PlanValidationError("Workflow requires nonempty cases and steps")
    case_map = {}
    for case in cases:
        if not isinstance(case, dict):
            raise PlanValidationError("Case must be an object")
        case_id = require_id(case.get("id"), "Case ID")
        if case_id in case_map:
            raise PlanValidationError(f"Duplicate case ID: {case_id}")
        if type(case.get("enabled", True)) is not bool:
            raise PlanValidationError("Case enabled flag must be boolean")
        if not isinstance(case.get("success_policy", "all"), str) or case.get("success_policy", "all") not in {"all", "any"}:
            raise PlanValidationError("Case success_policy must be all or any")
        case_map[case_id] = case
    step_map = {}
    dependencies = {}
    for step in steps:
        if not isinstance(step, dict):
            raise PlanValidationError("Step must be an object")
        step_id = require_id(step.get("id"), "Step ID")
        if step_id in step_map:
            raise PlanValidationError(f"Duplicate step ID: {step_id}")
        if step.get("case_id") not in case_map:
            raise PlanValidationError(f"Unknown case for step: {step_id}")
        handler = require_id(step.get("handler"), "Step handler")
        if handler not in bindings:
            raise PlanValidationError(f"Unregistered handler: {handler}")
        if not isinstance(step.get("inputs", {}), dict):
            raise PlanValidationError("Step inputs must be an object")
        depends = step.get("depends_on", [])
        if not isinstance(depends, list) or any(not isinstance(dep, str) for dep in depends):
            raise PlanValidationError("depends_on must contain step IDs")
        requirements = step.get("requires", [])
        if not isinstance(requirements, list):
            raise PlanValidationError("requires must be a list")
        deps = set(depends)
        for requirement in requirements:
            if not isinstance(requirement, dict) or set(requirement) - {"step", "path", "equals"}:
                raise PlanValidationError("Invalid prerequisite condition")
            deps.add(require_id(requirement.get("step"), "Prerequisite producer"))
            validate_path(requirement.get("path", []))
            if "equals" not in requirement:
                raise PlanValidationError("Prerequisite condition requires equals")
        deps.update(ref["step"] for ref in references(step.get("inputs", {})))
        step_map[step_id] = step
        dependencies[step_id] = deps
    for step_id, deps in dependencies.items():
        if not deps <= step_map.keys():
            raise PlanValidationError(f"Unknown prerequisite for {step_id}: {sorted(deps - step_map.keys())}")
    # Validate the whole declared graph, including branches outside this selection.
    positions = {step["id"]: index for index, step in enumerate(steps)}
    remaining = {name: len(deps) for name, deps in dependencies.items()}
    children = {name: [] for name in step_map}
    for name, deps in dependencies.items():
        for dep in deps:
            children[dep].append(name)
    ready = [(positions[name], name) for name, count in remaining.items() if count == 0]
    heapq.heapify(ready)
    ordered = []
    while ready:
        _, name = heapq.heappop(ready)
        ordered.append(name)
        for child in children[name]:
            remaining[child] -= 1
            if remaining[child] == 0:
                heapq.heappush(ready, (positions[child], child))
    if len(ordered) != len(steps):
        raise PlanValidationError("Workflow dependency cycle")
    enabled = [name for name, case in case_map.items() if case.get("enabled", True)]
    if selected_cases is None:
        requested = enabled
    else:
        if not isinstance(selected_cases, list) or any(not isinstance(name, str) for name in selected_cases):
            raise PlanValidationError("selected_cases must be a list of case IDs")
        if len(set(selected_cases)) != len(selected_cases):
            raise PlanValidationError("Duplicate selected case")
        if any(name not in enabled for name in selected_cases):
            raise PlanValidationError("Unknown or disabled selected case")
        requested = [name for name in enabled if name in selected_cases]
    if not requested:
        raise PlanValidationError("Selected suite is empty")
    for name in requested:
        if not any(step["case_id"] == name for step in steps):
            raise PlanValidationError(f"Selected case has no steps: {name}")
    selected = {name for name, step in step_map.items() if step["case_id"] in requested}
    pending = list(selected)
    while pending:
        for dependency in dependencies[pending.pop()]:
            if step_map[dependency]["case_id"] not in enabled:
                raise PlanValidationError("Prerequisite is outside the enabled suite")
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    expanded_cases = [name for name in case_map if any(step_map[s]["case_id"] == name for s in selected)]
    ordered_steps = []
    for name in ordered:
        if name in selected:
            normalized = json_copy(step_map[name])
            normalized["depends_on"] = sorted(dependencies[name], key=positions.__getitem__)
            normalized.setdefault("inputs", {})
            normalized.setdefault("requires", [])
            ordered_steps.append(normalized)
    plan = {
        "plan_schema_version": 1,
        "workflow_schema_version": 1,
        "workflow_id": workflow_id,
        "target": actual_target,
        "definition": definition,
        "definition_digest": digest_json(definition),
        "engine_digest": engine_binding or common.engine_digest(),
        "handler_bindings": json_copy(bindings),
        "requested_cases": requested,
        "selected_cases": expanded_cases,
        "case_policies": {name: case_map[name].get("success_policy", "all") for name in expanded_cases},
        "ordered_steps": ordered_steps,
        "run_count": run_count,
        "limits": _limits(definition.get("limits", {})),
        "policy": _policy(definition.get("policy", {})),
    }
    plan["plan_digest"] = digest_json(plan)
    return plan


def compile_plan(workflow: dict, registry: HandlerRegistry, *, selected_cases: list[str] | None = None, run_count: int = 1, target: dict | None = None) -> dict:
    """Compile a deterministic JSON plan; default is all enabled cases, one run."""
    return _compile(workflow, registry.bindings(), selected_cases=selected_cases, run_count=run_count, target=target)


def validate_plan(plan: dict, registry: HandlerRegistry | None = None, *, target: dict | None = None) -> dict:
    """Validate integrity and rebuild graph/selection before any credentials or IO.

    Without a registry this validates only the frozen structure. Execution always
    supplies the current trusted registry to additionally verify code bindings.
    """
    frozen = json_copy(plan)
    if not isinstance(frozen, dict) or type(frozen.get("plan_schema_version")) is not int or frozen["plan_schema_version"] != 1:
        raise PlanValidationError("Unsupported plan schema")
    claimed_digest = frozen.pop("plan_digest", None)
    if claimed_digest != digest_json(frozen):
        raise PlanValidationError("Plan digest mismatch")
    bindings = frozen.get("handler_bindings")
    if not isinstance(bindings, dict) or not bindings:
        raise PlanValidationError("Missing handler bindings")
    for name, binding in bindings.items():
        require_id(name, "Handler ID")
        if not isinstance(binding, dict) or set(binding) != {"version", "digest"}:
            raise PlanValidationError("Invalid handler binding")
        require_id(binding["version"], "Handler version")
        digest = binding["digest"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise PlanValidationError("Invalid handler digest")
    engine_binding = frozen.get("engine_digest")
    if not isinstance(engine_binding, str) or len(engine_binding) != 64 or any(c not in "0123456789abcdef" for c in engine_binding):
        raise PlanValidationError("Invalid engine digest")
    rebuilt = _compile(frozen.get("definition"), bindings, selected_cases=frozen.get("requested_cases"), run_count=frozen.get("run_count"), target=target, engine_binding=engine_binding)
    frozen["plan_digest"] = claimed_digest
    if digest_json(rebuilt) != digest_json(frozen):
        raise PlanValidationError("Plan does not match its frozen definition")
    if registry is not None:
        if engine_binding != common.engine_digest():
            raise common.IntegrityError("Common execution engine digest drift")
        registry.verify(bindings)
    return frozen
