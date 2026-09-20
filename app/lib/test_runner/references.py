"""Structured, typed JSON result references with explicit per-run ownership."""
from __future__ import annotations

from typing import Any, Iterator

from .common import PlanValidationError, json_copy, require_id

_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool, "null": type(None)}


def validate_path(path: Any) -> list:
    if not isinstance(path, list) or any(type(part) not in (str, int) or (type(part) is int and part < 0) for part in path):
        raise PlanValidationError("Reference path must contain string keys and nonnegative integer indexes")
    return path


def _reference(value: dict) -> dict | None:
    marker = "$result_ref" if "$result_ref" in value else None
    # JSON Schema owns $ref. Retain the old runner spelling only when its
    # complete explicit shape makes it unambiguously a result reference.
    legacy = value.get("$ref")
    if marker is None and set(value) == {"$ref"} and isinstance(legacy, dict) and {"step", "path", "type"} <= legacy.keys():
        marker = "$ref"
    if marker is None:
        return None
    if set(value) != {marker} or not isinstance(value[marker], dict):
        raise PlanValidationError("A result reference must be a single marker with an object value")
    ref = value[marker]
    if set(ref) - {"step", "path", "type", "run_id"}:
        raise PlanValidationError("Unknown reference field")
    require_id(ref.get("step"), "Reference producer")
    validate_path(ref.get("path", []))
    if not isinstance(ref.get("type"), str) or ref["type"] not in _TYPES:
        raise PlanValidationError("Reference must declare a supported JSON type")
    if "run_id" in ref:
        require_id(ref["run_id"], "Reference run ID")
    return ref


def references(value: Any) -> Iterator[dict]:
    if isinstance(value, dict):
        if set(value) == {"$result_literal"}:
            return
        ref = _reference(value)
        if ref is not None:
            yield ref
        else:
            for child in value.values():
                yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


def value_at_path(value: Any, path: list) -> Any:
    for part in validate_path(path):
        if type(part) is str and isinstance(value, dict) and part in value:
            value = value[part]
        elif type(part) is int and isinstance(value, list) and part < len(value):
            value = value[part]
        else:
            raise PlanValidationError(f"Unresolved result path component: {part!r}")
    return value


def materialize(value: Any, results: dict, *, run_id: str) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$result_literal"}:
            return json_copy(value["$result_literal"])
        ref = _reference(value)
        if ref is not None:
            if ref.get("run_id", run_id) != run_id:
                raise PlanValidationError("Cross-run result reference is forbidden")
            if ref["step"] not in results:
                raise PlanValidationError(f"Missing reference producer: {ref['step']}")
            result = value_at_path(results[ref["step"]], ref.get("path", []))
            expected = _TYPES[ref["type"]]
            types = expected if isinstance(expected, tuple) else (expected,)
            if type(result) not in types:
                raise PlanValidationError(f"Reference type mismatch: expected {ref['type']}")
            return json_copy(result)
        return {key: materialize(child, results, run_id=run_id) for key, child in value.items()}
    if isinstance(value, list):
        return [materialize(child, results, run_id=run_id) for child in value]
    return json_copy(value)
