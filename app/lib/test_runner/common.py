"""Strict JSON and integrity primitives for frozen functional-test plans."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any


class PlanValidationError(ValueError):
    """A definition, frozen plan, reference or registered handler is invalid."""


class IntegrityError(RuntimeError):
    """Execution no longer matches the frozen definition."""


class DispatchStopped(RuntimeError):
    """No more business requests are permitted in this run."""


class BudgetExceeded(DispatchStopped):
    """A request count or deadline was exhausted."""


def json_copy(value: Any) -> Any:
    """Validate actual JSON types without coercing tuples, keys or nonfinite floats."""
    def check(item: Any) -> None:
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise PlanValidationError(f"Not a JSON value: {type(item).__name__}")
    check(value)
    return copy.deepcopy(value)


def canonical_json(value: Any) -> str:
    json_copy(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def engine_digest() -> str:
    """Bind the explicitly maintained common execution closure, identically in App."""
    root = Path(__file__).resolve().parent
    files = ("__init__.py", "common.py", "registry.py", "references.py", "compiler.py", "cancellation.py", "ledger.py", "executor.py")
    return digest_json({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files})


def require_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanValidationError(f"{name} must be a nonempty string")
    return value
