"""Exact Google resource lifecycles compiled into the common execution engine.

No credential discovery occurs here. Vertex OAuth and the designated AI Studio
credential are supplied by the caller after the complete plan has been validated.
Ordinary Interactions remains unavailable; its dedicated approval is a separate gate.
"""
from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

import requests

from lib import ai_studio_interactions_scope as scope
from lib import live_stateful_runners as domain
from lib.credential_security import ProviderCredential
from lib.parameter_output_limit import enforce_parameter_test_output_limit
from lib.test_runner import CancellationContext, HandlerRegistry, IntegrityError, PlanValidationError, compile_plan, digest_json, execute_plan, validate_plan
from lib.test_runner.cancellation import response_deadline
from lib.test_runner.common import canonical_json, json_copy

FACTORY_ID = "gemini_resources"
FACTORY_VERSION = "1"
_IDS = {name: f"gemini.resources.{name}.v1" for name in ("vertex_create", "vertex_use", "interaction_parent", "interaction_child", "interaction_complete")}


def _source_digest():
    root = Path(__file__).resolve().parents[3]
    names = ("lib/live_stateful_runners.py", "lib/offline_stateful_runners.py", "lib/ai_studio_interactions_scope.py",
             "lib/credential_security.py", "lib/parameter_output_limit.py", "lib/gemini_api_version.py")
    return digest_json({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names})


def source_digest():
    """Portable factory identity; execution additionally binds transport helpers."""
    root = Path(__file__).resolve().parents[3]
    names = ("lib/test_runner/adapters/gemini_resources.py", "lib/live_stateful_runners.py",
             "lib/offline_stateful_runners.py", "lib/ai_studio_interactions_scope.py")
    return digest_json({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names})


def _factory():
    return {"factory_id": FACTORY_ID, "version": FACTORY_VERSION, "source_sha256": source_digest()}


def _ref(step, path, kind="string"):
    return {"$ref": {"step": step, "path": path, "type": kind}}


def _limits(limits, resource_count):
    return {"max_requests": limits.max_requests - resource_count,
            "request_timeout_seconds": max(limits.timeout_sec, limits.cleanup_timeout_sec),
            "deadline_seconds": limits.max_run_seconds,
            "cleanup_max_requests": resource_count,
            "cleanup_deadline_seconds": resource_count * limits.cleanup_timeout_sec}


def _common_target(interface_id, model, source, api_form, *, credential_scope_id):
    if not isinstance(credential_scope_id, str) or not credential_scope_id.strip():
        raise ValueError("An exact nonempty credential account scope is required")
    return {"provider": source, "source_id": source, "model": model, "interface_id": interface_id,
            "route_profile": source, "api_form": api_form, "credential_scope_id": credential_scope_id}


def prepare_vertex_cached_content_plan(*, catalog, interface_id, model, project, project_number, location,
                                      cache_text, prompt, ttl_seconds, limits, runs=1, credential_scope_id=None):
    limits.validate(3)
    domain._target(catalog, interface_id, model, "google_vertex", "gemini_generate_content", ("cachedContent",), "v1",
                   "/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent")
    if (not isinstance(project, str) or not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project)
            or not isinstance(project_number, str) or not re.fullmatch(r"[0-9]{6,20}", project_number)):
        raise ValueError("an exact project ID and project number are required")
    if not isinstance(location, str) or not re.fullmatch(r"[a-z]+-[a-z]+[0-9]", location):
        raise ValueError("an explicit regional Vertex location is required")
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
        raise ValueError("ttl_seconds must be in [1, 300]")
    domain._text(cache_text, limits.max_input_chars)
    domain._text(prompt, limits.max_input_chars - len(cache_text))
    parent = f"projects/{project}/locations/{location}"
    model_resource = f"{parent}/publishers/google/models/{model}"
    resource_scope = {"kind": "vertex_cache", "base_url": f"https://{location}-aiplatform.googleapis.com/v1/",
                      "project": project, "project_number": project_number, "location": location,
                      "parent": parent, "model_resource": model_resource, "ttl_seconds": ttl_seconds}
    target = _common_target(interface_id, model, "google_vertex", "gemini_generate_content", credential_scope_id=credential_scope_id if credential_scope_id is not None else f"project:{project_number}")
    target.update(project=project, project_number=project_number, location=location)
    create_body = {"model": model_resource, "ttl": f"{ttl_seconds}s", "contents": [{"role": "user", "parts": [{"text": cache_text}]}]}
    use_body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "cachedContent": _ref("create", ["resource_id"]),
                "generationConfig": {"maxOutputTokens": limits.max_output_tokens}}
    definition = {"workflow_schema_version": 1, "id": f"gemini-resources:vertex:{interface_id}", "factory_id": FACTORY_ID,
                  "factory": _factory(), "target": target, "resource_scope": resource_scope, "lifecycle_limits": dataclasses.asdict(limits),
                  "cases": [{"id": "cache_create"}, {"id": "cache_use"}], "limits": _limits(limits, 1),
                  "steps": [{"id": "create", "case_id": "cache_create", "handler": _IDS["vertex_create"], "inputs": {"body": create_body}},
                            {"id": "use", "case_id": "cache_use", "handler": _IDS["vertex_use"], "inputs": {"body": use_body}}]}
    registry = gemini_resource_registry()
    return compile_plan(definition, registry, run_count=runs), registry


def prepare_ai_studio_interactions_plan(*, catalog, interface_id, model, first_input, next_input, state_nonce,
                                       background, limits, runs=1, dedicated_only=True, credential_scope_id="designated:GEMINI_API_KEY"):
    limits.validate(4)
    if type(background) is not bool:
        raise ValueError("background must be boolean")
    scope.validate_nonce(state_nonce)
    if first_input != scope.first_input(state_nonce) or next_input != scope.NEXT_INPUT:
        raise ValueError("state proof requires the fixed parent-only nonce and independent child question")
    if interface_id == scope.PROFILE_ID + "#gemini-interactions-default":
        raise ValueError("Ordinary Gemini Interactions remains disabled")
    if interface_id == scope.INTERFACE_ID:
        if model != scope.MODEL or background is not False or digest_json(dataclasses.asdict(limits)) != digest_json(scope.LIMITS) or runs != 1:
            raise ValueError("dedicated lifecycle must retain the exact four-request limits and model")
        domain.resolve_ai_studio_lifecycle_target(catalog)
    elif dedicated_only:
        raise ValueError("Interactions workflow requires its separately approved dedicated lifecycle interface")
    else:
        # Compatibility for the existing explicitly supplied lifecycle library
        # target; this does not open the ordinary parameter workflow entry.
        required = ("store", "previous_interaction_id", "response.steps") + (("background",) if background else ())
        domain._target(catalog, interface_id, model, "google_ai_studio", "gemini_interactions", required, "v1beta", "/v1beta/interactions")
    if type(runs) is not int or runs != 1:
        raise ValueError("The approved nonce-chain resource lifecycle permits one frozen run")
    domain._text(first_input, limits.max_input_chars)
    domain._text(next_input, limits.max_input_chars - len(first_input))
    first = {"model": model, "input": first_input, "store": True, "stream": False,
             "generation_config": {"max_output_tokens": limits.max_output_tokens}}
    second = {"model": model, "input": next_input, "store": True, "stream": False,
              "generation_config": {"max_output_tokens": limits.max_output_tokens},
              "previous_interaction_id": _ref("parent", ["resource_id"]), "background": background}
    definition = {"workflow_schema_version": 1, "id": f"gemini-resources:interactions:{interface_id}", "factory_id": FACTORY_ID,
                  "factory": _factory(), "target": _common_target(interface_id, model, "google_ai_studio", "gemini_interactions", credential_scope_id=credential_scope_id),
                  "resource_scope": {"kind": "interactions", "base_url": scope.URL, "state_nonce": state_nonce, "background": background},
                  "lifecycle_limits": dataclasses.asdict(limits), "limits": _limits(limits, 2),
                  "cases": [{"id": "stateful_nonce_chain"}], "steps": [
                      {"id": "parent", "case_id": "stateful_nonce_chain", "handler": _IDS["interaction_parent"], "inputs": {"body": first}},
                      {"id": "child", "case_id": "stateful_nonce_chain", "handler": _IDS["interaction_child"], "inputs": {"body": second}},
                      {"id": "complete", "case_id": "stateful_nonce_chain", "handler": _IDS["interaction_complete"],
                       "inputs": {"response": _ref("child", ["response"], "object"), "resource_id": _ref("child", ["resource_id"]),
                                  "previous": _ref("parent", ["resource_id"])}}]}
    registry = gemini_resource_registry()
    return compile_plan(definition, registry), registry


def _owned_name(plan, value):
    resource = plan["definition"]["resource_scope"]
    if not isinstance(value, str):
        return False
    if resource["kind"] == "vertex_cache":
        pattern = rf"projects/(?:{re.escape(resource['project'])}|{resource['project_number']})/locations/{resource['location']}/cachedContents/[A-Za-z0-9_-]+"
    else:
        pattern = r"[A-Za-z0-9_-]+"
    return re.fullmatch(pattern, value) is not None


def _register_owned(context, request, response):
    if request["operation"] not in {"vertex_create", "interaction_parent", "interaction_child"}:
        return None
    resource_scope = context._plan["definition"]["resource_scope"]
    value = response.get("name" if resource_scope["kind"] == "vertex_cache" else "id")
    if not _owned_name(context._plan, value):
        raise ValueError("Resource response did not identify an exact owned resource")
    existing = next((resource for resource in context.resources if resource["resource_id"] == value), None)
    if existing is not None:
        if existing["creation_attempt_id"] == context.last_creation_attempt_id:
            return value
        raise ValueError("Create response reused an existing resource ID")
    previous = request.get("body", {}).get("previous_interaction_id")
    delete_url = resource_scope["base_url"] + (value if resource_scope["kind"] == "vertex_cache" else "/" + value)
    context.register_resource(value, cleanup_request={"operation": "delete", "method": "DELETE", "url": delete_url, "resource_id": value},
                              depends_on=[previous] if previous else [],
                              identity={"kind": resource_scope["kind"], "target": context.target})
    return value


def _response(receipt):
    if (type(receipt.get("status_code")) is not int or not 200 <= receipt["status_code"] < 300
            or receipt.get("response_complete") is not True or not isinstance(receipt.get("response"), dict)
            or "error" in receipt["response"]):
        raise ValueError("Official resource lifecycle did not return a complete successful JSON response")
    return receipt["response"]


def _vertex_create(context, inputs):
    resource = context._plan["definition"]["resource_scope"]
    request = {"operation": "vertex_create", "method": "POST", "url": resource["base_url"] + resource["parent"] + "/cachedContents", "body": inputs["body"]}
    response = _response(context.dispatch(request, creates_resource=True))
    name = _register_owned(context, request, response)
    numeric_model = resource["model_resource"].replace(f"projects/{resource['project']}/", f"projects/{resource['project_number']}/", 1)
    if response.get("model") not in {resource["model_resource"], numeric_model}:
        raise ValueError("Vertex cache returned model mismatch")
    domain._cache_expiry(response, resource["ttl_seconds"])
    return {"status": "passed", "accepted": True, "resource_id": name, "response": response,
            "ttl_verified": True, "identity_audit": {"status": "match"}, "semantic_effect": {"cache_created": True}}


def _vertex_use(context, inputs):
    resource = context._plan["definition"]["resource_scope"]
    response = _response(context.dispatch({"operation": "vertex_use", "method": "POST", "url": resource["base_url"] + resource["model_resource"] + ":generateContent", "body": inputs["body"]}))
    usage = domain.validate_vertex_cached_response(response, model=context.target["model"], output_limit=context._plan["definition"]["lifecycle_limits"]["max_output_tokens"])
    return {"status": "passed", "accepted": True, "response": response, "usage": usage, "returned_model": response["modelVersion"],
            "identity_audit": {"status": "match"}, "envelope_completeness": {"status": "complete"},
            "semantic_effect": {"cached_content_used": True}, "token_audit": {"status": "passed", "usage": usage}}


def _validate_interaction(context, response, previous, expected_text):
    if response.get("model") != context.target["model"]:
        raise ValueError("Interactions returned model mismatch")
    if response.get("status") != "completed":
        raise ValueError("Interaction did not reach completed")
    if domain._interaction_text(response, previous) != expected_text:
        raise ValueError("Interaction did not satisfy independent ACK or parent-only nonce recall")
    return domain._usage(response.get("usage"), interactions=True, output_limit=context._plan["definition"]["lifecycle_limits"]["max_output_tokens"])


def _interaction_create(context, inputs, *, parent):
    resource = context._plan["definition"]["resource_scope"]
    operation = "interaction_parent" if parent else "interaction_child"
    request = {"operation": operation, "method": "POST", "url": resource["base_url"], "body": inputs["body"]}
    response = _response(context.dispatch(request, creates_resource=True))
    name = _register_owned(context, request, response)
    if response.get("model") != context.target["model"]:
        raise ValueError("Interactions returned model mismatch")
    result = {"status": "passed", "accepted": True, "resource_id": name, "response": response}
    if parent:
        result.update(usage=_validate_interaction(context, response, None, scope.ACK), parent_ack_verified=True,
                      previous_echo_observed="previous_interaction_id" in response)
    return result


def _interaction_parent(context, inputs):
    return _interaction_create(context, inputs, parent=True)


def _interaction_child(context, inputs):
    return _interaction_create(context, inputs, parent=False)


def _interaction_complete(context, inputs):
    resource = context._plan["definition"]["resource_scope"]
    limits = context._plan["definition"]["lifecycle_limits"]
    response = inputs["response"]
    polls = 0
    while response.get("status") in {"queued", "in_progress"}:
        if not resource["background"] or polls >= limits["max_polls"]:
            raise TimeoutError("Interaction did not finish within the poll limit")
        # Cooperative cancellation remains responsive throughout the bounded poll gap.
        end = time.monotonic() + limits["poll_interval_sec"]
        while time.monotonic() < end:
            context.cancel.raise_if_cancelled()
            time.sleep(min(0.05, max(0, end - time.monotonic())))
        response = _response(context.dispatch({"operation": "poll", "method": "GET", "url": resource["base_url"] + "/" + inputs["resource_id"], "resource_id": inputs["resource_id"]}, kind="poll"))
        if response.get("id") != inputs["resource_id"] or response.get("model") != context.target["model"]:
            raise ValueError("Interaction poll identity mismatch")
        polls += 1
    usage = _validate_interaction(context, response, inputs["previous"], resource["state_nonce"])
    return {"status": "passed", "accepted": True, "response": response, "usage": usage, "state_recall_verified": True,
            "processing_complete": True, "previous_echo_observed": "previous_interaction_id" in response,
            "identity_audit": {"status": "match"}, "envelope_completeness": {"status": "complete"},
            "semantic_effect": {"parent_only_nonce_recalled": True}, "token_audit": {"status": "passed", "usage": usage}}


def register_handlers(registered):
    for name, handler in (("vertex_create", _vertex_create), ("vertex_use", _vertex_use), ("interaction_parent", _interaction_parent),
                          ("interaction_child", _interaction_child), ("interaction_complete", _interaction_complete)):
        registered.register(_IDS[name], handler, version="1", digest=_source_digest)
    return registered


def gemini_resource_registry():
    return register_handlers(HandlerRegistry())


class GeminiResourceDispatcher:
    """One bounded HTTP attempt with exact endpoint and owned-resource checks."""
    def __init__(self, plan, *, credential, request=None, credential_scope_id=None):
        self.plan = validate_plan(plan)
        if credential_scope_id is not None and credential_scope_id != self.plan["target"]["credential_scope_id"]:
            raise IntegrityError("Credential account scope differs from the frozen resource target")
        resource = self.plan["definition"]["resource_scope"]
        self.credential = credential if isinstance(credential, ProviderCredential) else ProviderCredential.create(
            provider=self.plan["target"]["source_id"], secret=domain._credential(credential), base_urls=[resource["base_url"]])
        if self.credential.provider != self.plan["target"]["source_id"]:
            raise IntegrityError("Credential provider differs from the frozen official resource source")
        self._session = None
        self._request = request
        self.interruption = None

    def close(self):
        if self._session is not None:
            self._session.close()
            self._session = None

    def _validate(self, request, context):
        if context.target != self.plan["target"] or context._plan["plan_digest"] != self.plan["plan_digest"]:
            raise IntegrityError("Resource dispatcher target or plan mismatch")
        resource = self.plan["definition"]["resource_scope"]
        operation, method, url = request.get("operation"), request.get("method"), request.get("url")
        base = resource["base_url"]
        if operation in {"delete", "poll"}:
            identity = request.get("resource_id")
            owned = {row["resource_id"] for row in context.resources}
            expected = base + (identity if resource["kind"] == "vertex_cache" else "/" + str(identity)) if isinstance(identity, str) else None
            if identity not in owned or not _owned_name(self.plan, identity) or url != expected or method != ("DELETE" if operation == "delete" else "GET"):
                raise ValueError("Resource operation is not scoped to an exact owned ID")
            if operation == "poll" and (resource["kind"] != "interactions" or not resource["background"]):
                raise ValueError("Polling is outside this resource lifecycle scope")
        elif resource["kind"] == "vertex_cache":
            expected = base + resource["parent"] + "/cachedContents" if operation == "vertex_create" else base + resource["model_resource"] + ":generateContent"
            if operation not in {"vertex_create", "vertex_use"} or method != "POST" or url != expected:
                raise ValueError("Vertex resource operation changed its project, location or model endpoint")
            if operation == "vertex_use" and request["body"].get("cachedContent") not in {row["resource_id"] for row in context.resources}:
                raise ValueError("Vertex generation references a foreign cache")
        elif operation not in {"interaction_parent", "interaction_child"} or method != "POST" or url != base:
            raise ValueError("Interaction operation changed its source/version endpoint")
        if method == "POST" and operation != "vertex_create":
            body = copy.deepcopy(request["body"])
            enforce_parameter_test_output_limit(body, self.plan["target"]["api_form"])
            if digest_json(body) != digest_json(request["body"]):
                raise ValueError("Frozen lifecycle body would change at the output-floor boundary")
        return resource

    def __call__(self, request, *, timeout, context):
        resource = self._validate(request, context)
        limits = self.plan["definition"]["lifecycle_limits"]
        timeout = min(timeout, limits["cleanup_timeout_sec"] if context.phase == "cleanup" else limits["timeout_sec"])
        if self._request is None:
            self._session = requests.Session()
            self._session.trust_env = False
            self._session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
            self._request = self._session.request
        observer_method = getattr(type(self._request), "set_workflow_resource_observer", None)
        if callable(observer_method):
            self._request.set_workflow_resource_observer(lambda payload: _register_owned(context, request, payload))
        owns_deadline = getattr(type(self._request), "workflow_response_deadline", False) is True
        deadline = contextlib.nullcontext() if owns_deadline else response_deadline(timeout)
        headers = self.credential.auth_headers(url=request["url"], auth_mode="bearer" if resource["kind"] == "vertex_cache" else "google_api_key")
        headers["Content-Type"] = "application/json"
        response = None
        started = time.monotonic()
        try:
            with deadline:
                try:
                    response = self._request(request["method"], request["url"], headers=headers, json=request.get("body"),
                                             timeout=timeout, allow_redirects=False, stream=True)
                except Exception:
                    raise RuntimeError("official lifecycle transport failed") from None
                raw = bytearray()
                for chunk in response.iter_content(chunk_size=1):
                    if time.monotonic() - started >= timeout:
                        raise TimeoutError("official lifecycle response deadline exceeded")
                    if not isinstance(chunk, bytes) or len(raw) + len(chunk) > limits["max_response_bytes"]:
                        raise ValueError("official lifecycle response exceeds its byte allowance")
                    raw.extend(chunk)
                if time.monotonic() - started >= timeout:
                    raise TimeoutError("official lifecycle response deadline exceeded")
                payload = {} if request["method"] == "DELETE" and not raw.strip() else domain._response_json(bytes(raw))
                status = response.status_code
                if type(status) is not int:
                    raise ValueError("Lifecycle response status must be an integer")
                if 200 <= status < 300 and request["operation"] in {"vertex_create", "interaction_parent", "interaction_child"}:
                    _register_owned(context, request, payload)
                return {"status_code": status, "response": payload, "response_complete": True,
                        "response_sha256": hashlib.sha256(raw).hexdigest(), "response_bytes": len(raw),
                        "deleted": request["method"] == "DELETE" and status in {200, 204} and payload == {}}
        except BaseException as exc:
            if not isinstance(exc, Exception) and self.interruption is None:
                self.interruption = exc
            raise
        finally:
            try:
                if response is not None:
                    response.close()
            except BaseException as exc:
                if not isinstance(exc, Exception) and self.interruption is None:
                    self.interruption = exc
                raise
            finally:
                if callable(observer_method):
                    self._request.set_workflow_resource_observer(None)


def resource_summary(plan, report):
    if len(report["runs"]) > 1:
        summaries = [resource_summary(plan, {**report, "runs": [run], "status": run["status"]}) for run in report["runs"]]
        result = copy.deepcopy(summaries[0])
        result.update(success=report["status"] == "passed", cleanup_complete=all(row["cleanup_complete"] for row in summaries),
                      requests_sent=sum(row["requests_sent"] for row in summaries), run_results=summaries,
                      events=[event for row in summaries for event in row["events"]])
        return result
    run = report["runs"][0]
    steps = run["steps"]
    kind = plan["definition"]["resource_scope"]["kind"]
    result = {"approval_code": "R7-CACHED-CONTENT-LIFECYCLE-LIVE" if kind == "vertex_cache" else scope.APPROVAL,
              "interface_id": plan["target"]["interface_id"], "api_version": "v1" if kind == "vertex_cache" else "v1beta",
              "success": report["status"] == "passed", "cleanup_complete": run["cleanup"]["status"] == "passed",
              "requests_sent": run["request_count"], "workflow_plan_digest": plan["plan_digest"],
              "workflow_ledger_path": run["ledger_path"], "events": []}
    for index, attempt in enumerate(run["attempts"], 1):
        operation = attempt["request"].get("operation")
        operation = {"vertex_create": "create", "vertex_use": "use", "interaction_parent": "store", "interaction_child": "previous"}.get(operation, operation)
        result["events"].append({"sequence": index, "operation": operation, "run_index": run["run_index"],
                                 "http_status": attempt.get("receipt", {}).get("status_code"), "status": attempt["status"]})
    failures = [row.get("error", {}).get("type") for row in steps.values() if row.get("error")]
    if failures:
        result["failure_type"] = failures[0]
    cleanup_errors = [attempt.get("error", {}).get("type") for resource in run["cleanup"]["resources"] for attempt in resource["cleanup_attempts"] if attempt.get("error")]
    if cleanup_errors:
        result["cleanup_failure_type"] = cleanup_errors[0]
    if run["cleanup"]["unknown_creations"]:
        result["cleanup_note"] = "a create attempt did not return a usable owned resource ID; account-side reconciliation required"
    if kind == "vertex_cache":
        resources = run["cleanup"]["resources"]
        if resources:
            result["resource_name"] = resources[0]["resource_id"]
        result.update(usage=steps.get("use", {}).get("usage"), ttl_verified=steps.get("create", {}).get("ttl_verified", False))
        if steps.get("use", {}).get("returned_model"):
            result["returned_model"] = steps["use"]["returned_model"]
    else:
        parent, complete = steps.get("parent", {}), steps.get("complete", {})
        result.update(resource_ids=[row["resource_id"] for row in run["cleanup"]["resources"]],
                      parent_ack_verified=parent.get("parent_ack_verified", False), state_recall_verified=complete.get("state_recall_verified", False),
                      processing_complete=complete.get("processing_complete", False),
                      previous_echo_observed=[row["previous_echo_observed"] for row in (parent, complete) if "previous_echo_observed" in row],
                      usage=[row["usage"] for row in (parent, complete) if "usage" in row])
        if result["processing_complete"]:
            result["returned_model"] = plan["target"]["model"]
        elif result["resource_ids"] and not run["cleanup"]["unknown_creations"]:
            result["processing_note"] = "resource deletion does not prove background completion or cancellation"
    return result


def execute_gemini_resource_plan(plan, registry, *, credential, request=None, evidence_dir=None, cancellation=None):
    frozen = validate_plan(plan, registry)
    directory = Path(evidence_dir) if evidence_dir is not None else Path(tempfile.mkdtemp(prefix="gemini-resources-"))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    plan_path = directory / "plan.json"
    try:
        descriptor = os.open(plan_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if plan_path.is_symlink() or digest_json(json.loads(plan_path.read_text())) != digest_json(frozen):
            raise IntegrityError("Resource evidence directory contains a different frozen plan")
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical_json(frozen) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    dispatcher = GeminiResourceDispatcher(frozen, credential=credential, request=request)
    cancel = cancellation or CancellationContext()
    try:
        with cancel.install_signal_handlers():
            report = execute_plan(frozen, registry, dispatcher, cancellation=cancel, evidence_dir=directory)
        result = resource_summary(frozen, report)
        result["workflow_plan_path"] = str(plan_path.absolute())
        if dispatcher.interruption is not None:
            dispatcher.interruption.workflow_result = result
            raise dispatcher.interruption
        if report["status"] == "cancelled":
            interruption = KeyboardInterrupt(cancel.reason)
            interruption.workflow_result = result
            raise interruption
        return result, report
    finally:
        dispatcher.close()
