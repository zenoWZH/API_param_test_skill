"""Frozen, injected, serial functional-test workflow engine.

See ``docs/unified_test_runner_implementation_plan.md`` for the acceptance scope.
This package does not schedule load/pressure tests or import handlers from config.
"""
from .cancellation import CancellationContext
from .common import BudgetExceeded, DispatchStopped, IntegrityError, PlanValidationError, digest_json
from .compiler import compile_plan, validate_plan
from .executor import RunContext, execute_plan, summarize_plan_runs
from .registry import HandlerRegistry

__all__ = [
    "compile_plan", "validate_plan", "execute_plan", "HandlerRegistry", "RunContext",
    "CancellationContext", "PlanValidationError", "IntegrityError", "DispatchStopped",
    "BudgetExceeded", "digest_json",
    "summarize_plan_runs",
]
