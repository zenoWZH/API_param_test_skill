"""Serial independent functional runs with one counted injected dispatch boundary.

Dispatchers are trusted protocol adapters called as
``dispatcher(request, timeout=seconds, context=context)``. They must bound the
entire send/read operation by timeout, disable implicit retry/redirect sends,
and route every upload, poll, public fetch and deletion through this boundary.
The core cannot safely interrupt arbitrary Python IO; a supervisor enforces
the documented request + cleanup + five-second hard-kill grace.
"""
from __future__ import annotations

import time
import uuid

from .cancellation import CancellationContext
from .common import BudgetExceeded, DispatchStopped, IntegrityError, PlanValidationError, digest_json, json_copy, require_id
from .compiler import validate_plan
from .ledger import ResourceLedger, evidence_copy
from .references import materialize, value_at_path
from .registry import HandlerRegistry
from . import common

_STATUSES = {"passed", "failed", "blocked", "skipped", "inconclusive", "cancelled"}


class RunContext:
    """Handler context. Mutable execution internals are never shared between runs."""
    def __init__(self, plan, registry, dispatcher, ledger, cancellation, *, clock=time.monotonic):
        self._plan = plan
        self._registry = registry
        self._dispatcher = dispatcher
        self._ledger = ledger
        self._clock = clock
        self.cancel = cancellation
        self.cancellation = cancellation
        self.run_id = ledger.state["run_id"]
        self.run_index = ledger.state["run_index"]
        self.step_id = None
        self.phase = "business"
        self._results = {}
        self._deadline = clock() + self.limits["deadline_seconds"]
        self._cleanup_elapsed = 0.0
        self._cleanup_started = None
        self._business_count = 0
        self._cleanup_count = 0
        self._fatal_reason = None
        self.last_creation_attempt_id = None
        self._active_request_deadline = None
        # Preserve the already-reviewed in-memory cleanup code for this run.
        # Files can drift after a create; that stops business, not deletion by
        # the original callable. A fresh recovery process must reverify bindings.
        self._original_handlers = {}
        if ledger.state["recovery_count"] == 0:
            for name, binding in plan["handler_bindings"].items():
                function = registry.resolve(name, binding)
                self._original_handlers[name] = (function, getattr(function, "__code__", None))

    @property
    def target(self):
        return json_copy(self._plan["target"])

    @property
    def limits(self):
        return json_copy(self._plan["limits"])

    @property
    def results(self):
        return json_copy(self._results)

    @property
    def resources(self):
        return evidence_copy(self._ledger.state["resources"])

    @property
    def request_count(self):
        return self._business_count + self._cleanup_count

    @property
    def remaining_request_seconds(self):
        return max(0.0, (self._active_request_deadline or self._clock()) - self._clock())

    @property
    def cleanup_remaining_seconds(self):
        active = self._clock() - self._cleanup_started if self._cleanup_started is not None else 0.0
        return max(0.0, self.limits["cleanup_deadline_seconds"] - self._cleanup_elapsed - active)

    def record_event(self, name: str, data=None):
        require_id(name, "Event name")
        self._ledger.state["events"].append({"name": name, "step_id": self.step_id, "phase": self.phase, "data": evidence_copy(data)})
        self._ledger.save()

    def _business_allowed(self):
        self.cancel.raise_if_cancelled()
        if self._fatal_reason:
            raise DispatchStopped(self._fatal_reason)
        try:
            if common.engine_digest() != self._plan["engine_digest"]:
                raise IntegrityError("Common execution engine digest drift")
            self._registry.verify(self._plan["handler_bindings"])
        except (IntegrityError, PlanValidationError) as exc:
            self._fatal_reason = str(exc)
            raise IntegrityError(str(exc)) from exc

    def dispatch(self, request: dict, *, kind: str = "api", creates_resource: bool = False):
        """Count and journal exactly one transport attempt, with zero retries."""
        outgoing = json_copy(request)
        if not isinstance(outgoing, dict):
            raise PlanValidationError("Dispatch request must be a JSON object")
        require_id(kind, "Dispatch kind")
        if type(creates_resource) is not bool:
            raise PlanValidationError("creates_resource must be boolean")
        if self.phase == "cleanup":
            if creates_resource:
                raise PlanValidationError("Cleanup cannot create new resources")
            if self._cleanup_count >= self.limits["cleanup_max_requests"]:
                raise BudgetExceeded("Cleanup request budget exhausted")
            remaining = self.cleanup_remaining_seconds
        else:
            self._business_allowed()
            if self._business_count >= self.limits["max_requests"]:
                raise BudgetExceeded("Business request budget exhausted")
            remaining = self._deadline - self._clock()
        timeout = min(self.limits["request_timeout_seconds"], remaining)
        if timeout <= 0:
            raise BudgetExceeded(f"{self.phase} deadline exhausted")
        attempt_id = f"{self.run_id}:attempt:{len(self._ledger.state['attempts']) + 1}"
        attempt = {"id": attempt_id, "step_id": self.step_id, "phase": self.phase, "kind": kind, "request": evidence_copy(outgoing), "timeout_seconds": timeout, "status": "dispatching"}
        self._ledger.state["attempts"].append(attempt)
        if creates_resource:
            self.last_creation_attempt_id = attempt_id
            self._ledger.state["creations"].append({"attempt_id": attempt_id, "step_id": self.step_id, "run_id": self.run_id, "status": "pending", "resource_ids": []})
        if self.phase == "cleanup":
            self._cleanup_count += 1
        else:
            self._business_count += 1
        started_at = time.time()
        self._ledger.state["active_request"] = {"started_at": started_at, "deadline_at": started_at + timeout,
                                                 "timeout_seconds": timeout, "step_id": self.step_id, "phase": self.phase}
        # Commit before sending: a crash may leave an unknown send, never a retry.
        self._ledger.save()
        start = self._clock()
        self._active_request_deadline = start + timeout
        try:
            receipt = json_copy(self._dispatcher(outgoing, timeout=timeout, context=self))
            if not isinstance(receipt, dict):
                raise PlanValidationError("Dispatcher receipt must be a JSON object")
            attempt["receipt"] = evidence_copy(receipt)
            attempt["status"] = "received"
            code = receipt.get("status_code", receipt.get("http_status"))
            if self.phase != "cleanup" and code in self._plan["policy"]["fatal_status_codes"]:
                self._fatal_reason = f"Fatal HTTP status: {code}"
            return receipt
        except BaseException as exc:
            attempt["status"] = "exception"
            attempt["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            attempt["elapsed_seconds"] = max(0.0, self._clock() - start)
            if attempt["elapsed_seconds"] > timeout:
                attempt["deadline_exceeded"] = True
                if self.phase != "cleanup":
                    self._fatal_reason = "Dispatcher exceeded its frozen request deadline"
            self._active_request_deadline = None
            self._ledger.state["active_request"] = None
            self._ledger.state["fatal_reason"] = self._fatal_reason
            self._ledger.save()

    def register_resource(self, resource_id: str, *, cleanup_request: dict | None = None, cleanup_handler: str | None = None, cleanup_inputs: dict | None = None, depends_on: list[str] | None = None, creation_attempt_id: str | None = None, identity: dict | None = None):
        """Persist known exact identity before performing any semantic validation."""
        require_id(resource_id, "Resource ID")
        if self.phase != "business":
            raise PlanValidationError("Resources may only be registered during business execution")
        if (cleanup_request is None) == (cleanup_handler is None):
            raise PlanValidationError("Resource requires exactly one cleanup request or handler")
        if cleanup_handler is not None:
            if cleanup_handler not in self._plan["handler_bindings"]:
                raise PlanValidationError("Cleanup handler was not frozen into this plan")
            self._registry.resolve(cleanup_handler, self._plan["handler_bindings"][cleanup_handler])
        if cleanup_request is not None and not isinstance(cleanup_request, dict):
            raise PlanValidationError("Cleanup request must be an object")
        if cleanup_inputs is not None and not isinstance(cleanup_inputs, dict):
            raise PlanValidationError("Cleanup inputs must be an object")
        if identity is not None and not isinstance(identity, dict):
            raise PlanValidationError("Resource identity must be an object")
        resources = self._ledger.state["resources"]
        owned = {resource["resource_id"] for resource in resources}
        if resource_id in owned:
            raise PlanValidationError("Resource is already registered in this run")
        dependencies = depends_on or []
        if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies) or not set(dependencies) <= owned:
            raise PlanValidationError("Resource dependencies must belong to the current run")
        creation_attempt_id = creation_attempt_id or self.last_creation_attempt_id
        creation = next((item for item in self._ledger.state["creations"] if item["attempt_id"] == creation_attempt_id), None)
        if creation is None or creation["step_id"] != self.step_id or creation["status"] == "rejected":
            raise PlanValidationError("Resource registration requires this step's recorded creation attempt")
        record = {
            "resource_id": resource_id, "run_id": self.run_id, "step_id": self.step_id,
            "creation_attempt_id": creation_attempt_id, "identity": json_copy(identity or {}),
            "depends_on": list(dict.fromkeys(dependencies)), "status": "registered",
            "cleanup_request": json_copy(cleanup_request), "cleanup_handler": cleanup_handler,
            "cleanup_inputs": json_copy(cleanup_inputs or {}), "cleanup_attempts": [],
        }
        resources.append(record)
        creation["status"] = "known"
        creation["resource_ids"].append(resource_id)
        self._ledger.save()
        return resource_id

    def creation_failed(self, attempt_id: str | None = None, *, definitely_not_created: bool):
        attempt_id = attempt_id or self.last_creation_attempt_id
        creation = next((item for item in self._ledger.state["creations"] if item["attempt_id"] == attempt_id), None)
        if creation is None or creation["step_id"] != self.step_id or creation["resource_ids"]:
            raise PlanValidationError("Unknown creation attempt or already-owned resource")
        if type(definitely_not_created) is not bool:
            raise PlanValidationError("definitely_not_created must be boolean")
        creation["status"] = "rejected" if definitely_not_created else "unknown"
        self._ledger.save()

    def cleanup_resource(self, resource_id: str, *, _recovery: bool = False):
        """Perform one bounded cleanup; confirmed deletions and failures aren't retried."""
        resource = next((item for item in self._ledger.state["resources"] if item["resource_id"] == resource_id), None)
        if resource is None or resource["run_id"] != self.run_id:
            raise PlanValidationError("Cleanup may only act on resources owned by this run")
        if resource["status"] == "deleted" or (resource["cleanup_attempts"] and not _recovery):
            return json_copy(resource)
        dependents = [item["resource_id"] for item in self._ledger.state["resources"] if resource_id in item["depends_on"] and item["status"] in {"registered", "cleaning"}]
        if dependents:
            raise PlanValidationError(f"Cleanup must remove resource dependents first: {dependents}")
        previous_phase, previous_step = self.phase, self.step_id
        self.phase, self.step_id = "cleanup", resource["step_id"]
        outer_cleanup = self._cleanup_started is None
        if outer_cleanup:
            self._cleanup_started = self._clock()
        cleanup_attempt = {"index": len(resource["cleanup_attempts"]) + 1, "status": "started"}
        resource["cleanup_attempts"].append(cleanup_attempt)
        resource["status"] = "cleaning"
        self._ledger.save()
        try:
            if resource["cleanup_handler"]:
                name = resource["cleanup_handler"]
                if name in self._original_handlers:
                    handler, original_code = self._original_handlers[name]
                    if getattr(handler, "__code__", None) is not original_code:
                        raise IntegrityError("Original cleanup callable was modified in memory")
                else:
                    handler = self._registry.resolve(name, self._plan["handler_bindings"][name])
                receipt = json_copy(handler(self, json_copy(resource["cleanup_inputs"])))
            else:
                receipt = self.dispatch(resource["cleanup_request"], kind="delete")
            confirmed = isinstance(receipt, dict) and (receipt.get("deleted") is True or receipt.get("cleanup_confirmed") is True)
            resource["receipt"] = evidence_copy(receipt)
            resource["status"] = "deleted" if confirmed else "cleanup_failed"
            cleanup_attempt.update({"status": resource["status"], "receipt": evidence_copy(receipt)})
        except (KeyboardInterrupt, SystemExit) as exc:
            self.cancel.request(type(exc).__name__)
            resource["status"] = "cleanup_unknown"
            cleanup_attempt.update({"status": "cleanup_unknown", "error": {"type": type(exc).__name__, "message": str(exc)}})
        except Exception as exc:
            resource["status"] = "cleanup_failed"
            cleanup_attempt.update({"status": "cleanup_failed", "error": {"type": type(exc).__name__, "message": str(exc)}})
        finally:
            if outer_cleanup:
                self._cleanup_elapsed += max(0.0, self._clock() - self._cleanup_started)
                self._cleanup_started = None
            self.phase, self.step_id = previous_phase, previous_step
            self._ledger.save()
        return json_copy(resource)

    def _cleanup_all(self, *, recovery=False):
        self.phase = "cleanup"
        self._ledger.state["phase"] = "cleanup"
        for creation in self._ledger.state["creations"]:
            if creation["status"] == "pending":
                creation["status"] = "unknown"
        self._ledger.save()
        for resource in reversed(self._ledger.state["resources"]):
            try:
                self.cleanup_resource(resource["resource_id"], _recovery=recovery)
            except Exception as exc:
                # A dependent cleanup failure cannot suppress independent cleanups.
                resource["status"] = "cleanup_failed"
                resource["cleanup_error"] = {"type": type(exc).__name__, "message": str(exc)}
                self._ledger.save()


def _handler_result(value):
    result = json_copy(value)
    if not isinstance(result, dict) or result.get("status") not in _STATUSES:
        raise PlanValidationError("Handler must return an explicit supported status")
    if result["status"] == "passed" and type(result.get("accepted")) is not bool:
        raise PlanValidationError("Passing handler must distinguish accepted responses from negative cases")
    return result


def _blocked_by(step, results):
    blocked = []
    for dependency in step["depends_on"]:
        result = results.get(dependency, {})
        if result.get("status") != "passed" or result.get("accepted") is not True:
            blocked.append(dependency)
    for requirement in step["requires"]:
        try:
            value = value_at_path(results[requirement["step"]], requirement.get("path", []))
            if digest_json(value) != digest_json(requirement["equals"]):
                blocked.append(requirement["step"])
        except (KeyError, PlanValidationError):
            blocked.append(requirement["step"])
    return sorted(set(blocked))


def _run(plan, registry, dispatcher, cancellation, evidence_dir, run_id, run_index, *, cleanup_only=False):
    ledger = ResourceLedger(evidence_dir, run_id=run_id, plan=plan, run_index=run_index, recover=cleanup_only)
    try:
        context = RunContext(plan, registry, dispatcher, ledger, cancellation)
    except BaseException:
        ledger.close()
        raise
    previous_attempt_count = len(ledger.state["attempts"])
    context._results = json_copy(ledger.state["steps"]) if cleanup_only else {}
    try:
        if not cleanup_only:
            for step in plan["ordered_steps"]:
                context.step_id = step["id"]
                context.last_creation_attempt_id = None
                blocked = _blocked_by(step, context._results)
                if blocked:
                    result = {"status": "blocked", "accepted": False, "blocked_dependencies": blocked}
                elif cancellation.cancelled or context._fatal_reason:
                    result = {"status": "cancelled" if cancellation.cancelled else "blocked", "accepted": False, "reason": cancellation.reason or context._fatal_reason}
                else:
                    try:
                        context._business_allowed()
                        inputs = materialize(step["inputs"], context._results, run_id=context.run_id)
                        context.record_event("step_inputs", {"template": step["inputs"], "materialized": inputs})
                        handler = registry.resolve(step["handler"], plan["handler_bindings"][step["handler"]])
                        result = _handler_result(handler(context, inputs))
                        if context._fatal_reason and result["status"] == "passed":
                            result = {**result, "status": "failed", "reason": context._fatal_reason}
                    except (KeyboardInterrupt, SystemExit) as exc:
                        cancellation.request(type(exc).__name__)
                        result = {"status": "cancelled", "accepted": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
                    except Exception as exc:
                        if isinstance(exc, (IntegrityError, PlanValidationError)):
                            context._fatal_reason = str(exc)
                        result = {"status": "cancelled" if cancellation.cancelled else "failed", "accepted": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
                context._results[step["id"]] = result
                ledger.state["steps"][step["id"]] = evidence_copy(result)
                ledger.state["fatal_reason"] = context._fatal_reason
                ledger.save()
        context._cleanup_all(recovery=cleanup_only)
        unknown = [item for item in ledger.state["creations"] if item["status"] in {"pending", "unknown"}]
        cleanup_passed = not unknown and all(item["status"] == "deleted" for item in ledger.state["resources"])
        all_passed = bool(context._results) and all(result["status"] == "passed" for result in context._results.values())
        if cancellation.cancelled:
            status = "cancelled"
        elif not cleanup_passed:
            status = "incomplete"
        elif cleanup_only:
            status = "passed"  # Recovery success is explicitly scoped to cleanup_only.
        else:
            status = "passed" if all_passed else "failed"
        ledger.state.update({"status": status, "phase": "complete", "fatal_reason": context._fatal_reason})
        ledger.save()
        return {
            "run_id": context.run_id, "run_index": context.run_index, "status": status,
            "cleanup_only": cleanup_only, "steps": evidence_copy(context._results),
            "attempts": evidence_copy(ledger.state["attempts"]),
            "request_count": context.request_count, "business_request_count": context._business_count,
            "cleanup_request_count": context._cleanup_count,
            "attempt_count": len(ledger.state["attempts"]), "previous_attempt_count": previous_attempt_count,
            "cleanup": {"status": "passed" if cleanup_passed else "incomplete", "resources": context.resources, "unknown_creations": json_copy(unknown)},
            "events": evidence_copy(ledger.state["events"]), "fatal_reason": context._fatal_reason,
            "evidence_dir": str(ledger.base_dir), "ledger_path": str(ledger.path),
        }
    finally:
        # Even a failure in result journaling or materialization must enter cleanup.
        if ledger.state["phase"] not in {"complete", "cleanup"}:
            try:
                context._cleanup_all(recovery=cleanup_only)
            finally:
                ledger.close()
        else:
            ledger.close()


def summarize_plan_runs(plan: dict, runs: list[dict]) -> dict:
    """Evaluate frozen case policies without rewriting any individual run evidence.

    ``any`` can cover only a reviewed soft failure: the handler must explicitly
    set aggregate_eligible and an aggregation_key matching a successful exchange.
    Incomplete, blocked, exception, cleanup and fatal outcomes remain failures.
    """
    expected_count = plan["run_count"]
    expected_steps = {step["id"] for step in plan["ordered_steps"]}
    valid_runs = isinstance(runs, list) and len(runs) == expected_count
    valid_runs = valid_runs and all(isinstance(run, dict) and type(run.get("run_index")) is int
                                  and run["run_index"] == index for index, run in enumerate(runs, 1))
    valid_runs = valid_runs and all(isinstance(run.get("run_id"), str) and run["run_id"] for run in runs) and len({run["run_id"] for run in runs}) == expected_count
    outcomes = {}
    for case_id in plan["selected_cases"]:
        step_ids = [step["id"] for step in plan["ordered_steps"] if step["case_id"] == case_id]
        policy = plan["case_policies"][case_id]
        passing, failing, missing = [], [], []
        for index in range(1, expected_count + 1):
            run = runs[index - 1] if isinstance(runs, list) and len(runs) >= index else {}
            steps = run.get("steps") if isinstance(run, dict) else None
            if not isinstance(steps, dict) or any(name not in steps or not isinstance(steps[name], dict) for name in step_ids):
                missing.append(index)
            elif all(steps[name].get("status") == "passed" and type(steps[name].get("accepted")) is bool for name in step_ids):
                passing.append(index)
            else:
                failing.append(index)
        covered = []
        if policy == "any" and passing and not missing:
            for index in failing:
                row = runs[index - 1]["steps"]
                eligible = True
                for name in step_ids:
                    step = row[name]
                    if step.get("status") == "passed" and type(step.get("accepted")) is bool:
                        continue
                    key = step.get("aggregation_key")
                    if (step.get("status") != "failed" or step.get("aggregate_eligible") is not True
                            or step.get("error") or not isinstance(key, str) or not key
                            or not any(runs[other - 1]["steps"][name].get("aggregation_key") == key for other in passing)):
                        eligible = False
                        break
                if eligible:
                    covered.append(index)
        passed = valid_runs and not missing and bool(passing) and (not failing or policy == "any" and covered == failing)
        outcomes[case_id] = {"success_policy": policy, "status": "passed" if passed else "incomplete" if missing else "failed",
                             "passed_runs": passing, "failed_runs": failing, "covered_failed_runs": covered,
                             "missing_runs": missing, "expected_runs": expected_count}
    actual = runs if isinstance(runs, list) else []
    cancelled = any(isinstance(run, dict) and run.get("status") == "cancelled" for run in actual)
    cleanup_bad = any(not isinstance(run, dict) or not isinstance(run.get("cleanup"), dict)
                      or run["cleanup"].get("status") != "passed" or run["cleanup"].get("unknown_creations") != []
                      or not isinstance(run["cleanup"].get("resources"), list)
                      or any(not isinstance(item, dict) or item.get("status") != "deleted"
                             or item.get("run_id") != run.get("run_id") for item in run["cleanup"]["resources"])
                      for run in actual)
    incomplete = not valid_runs or cleanup_bad
    structural_failure = any(not isinstance(run, dict) or not isinstance(run.get("steps"), dict)
                             or set(run["steps"]) != expected_steps or run.get("fatal_reason")
                             or run.get("cleanup_only") is True or run.get("status") not in {"passed", "failed"}
                             or (run["status"] == "passed") != all(isinstance(step, dict) and step.get("status") == "passed"
                                                                  and type(step.get("accepted")) is bool for step in run["steps"].values())
                             for run in actual)
    status = "cancelled" if cancelled else "incomplete" if cleanup_bad else "failed" if structural_failure else "incomplete" if incomplete else "failed" if any(row["status"] != "passed" for row in outcomes.values()) else "passed"
    return {"status": status, "case_outcomes": outcomes}


def execute_plan(plan: dict, registry: HandlerRegistry, dispatcher, *, cancellation: CancellationContext | None = None, evidence_dir=None, cleanup_only: bool = False, run_id: str | None = None, target: dict | None = None) -> dict:
    """Execute independent serial runs, or recover cleanup for one exact original run.

    Evidence is always durable. Omitted evidence_dir creates a retained private
    temporary directory whose path is returned; it is never automatically removed.
    Cleanup recovery never invokes business handlers or replays unknown creates.
    """
    frozen = validate_plan(plan, None if cleanup_only else registry, target=target)
    if not callable(dispatcher):
        raise PlanValidationError("An injected dispatcher is required")
    if type(cleanup_only) is not bool:
        raise PlanValidationError("cleanup_only must be boolean")
    if cleanup_only and (not run_id or evidence_dir is None):
        raise PlanValidationError("Cleanup-only recovery requires the original run ID and evidence directory")
    if run_id is not None and frozen["run_count"] != 1 and not cleanup_only:
        raise PlanValidationError("Explicit run ID is only valid for one run")
    cancellation = cancellation or CancellationContext()
    runs = []
    for index in range(1, (1 if cleanup_only else frozen["run_count"]) + 1):
        result = _run(frozen, registry, dispatcher, cancellation, evidence_dir, run_id or uuid.uuid4().hex, index, cleanup_only=cleanup_only)
        runs.append(result)
        if cancellation.cancelled or result["fatal_reason"]:
            break
    statuses = {run["status"] for run in runs}
    status = "cancelled" if "cancelled" in statuses else "incomplete" if "incomplete" in statuses else "failed" if "failed" in statuses else "passed"
    summary = {"status": status, "case_outcomes": {}} if cleanup_only else summarize_plan_runs(frozen, runs)
    return {"report_schema_version": 1, "plan_digest": frozen["plan_digest"], "workflow_id": frozen["workflow_id"], "target": frozen["target"], **summary, "cleanup_only": cleanup_only, "requested_run_count": frozen["run_count"], "completed_run_count": len(runs), "runs": runs}
