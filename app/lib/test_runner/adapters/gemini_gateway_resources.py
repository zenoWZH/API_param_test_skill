"""Bounded cachedContents sample for the designated inferenceai_gemini gateway.

Google Vertex supplies the reference identity. The authenticated execution target
is a Gemini-compatible gateway; this neither opens direct Vertex/Interactions
policies nor attests to the gateway's physical upstream. No discovery or network
operation is performed by plan preparation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote

import requests

from lib.config import get_provider_config, get_provider_interface
from lib.credential_security import ProviderCredential, credential_from_config
from lib.live_stateful_runners import _cache_expiry, _response_json, validate_vertex_cached_response
from lib.model_profile_catalog import Catalog, get_model_profile_catalog
from lib.test_runner import HandlerRegistry, IntegrityError, compile_plan, digest_json, validate_plan
from lib.test_runner.common import json_copy
from lib.test_runner.transport import HttpDispatcher

PROVIDER = "inferenceai_gemini"
ORIGIN = "https://model.service-inference.ai"
BASE = ORIGIN + "/v1beta"
TRANSPORT = "gemini_generate_content"
FACTORY_ID = "gemini_gateway_resources"
FACTORY_VERSION = "1"
CREATE = "gemini.gateway.cache.create.v1"
USE = "gemini.gateway.cache.use.v1"
DELETE = "gemini.gateway.cache.delete.v1"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_NAME = re.compile(r"cachedContents/[A-Za-z0-9_-]{1,256}\Z")
_SOURCE_FILES = ("lib/test_runner/adapters/gemini_gateway_resources.py", "lib/live_stateful_runners.py")


def source_digest():
    root = Path(__file__).resolve().parents[3]
    return digest_json({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in _SOURCE_FILES})


def execution_digest():
    root = Path(__file__).resolve().parents[3]
    files = ("lib/test_runner/transport.py", "lib/config.py", "lib/credential_security.py",
             "lib/parameter_output_limit.py", "lib/model_profile_catalog.py")
    return digest_json({"factory": source_digest(), **{
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}})


def credential_scope_digest(credential):
    """Return a non-secret account/key binding; never serialize the credential."""
    if not isinstance(credential, ProviderCredential) or credential.provider != PROVIDER:
        raise ValueError("Gateway cache requires the designated provider credential")
    header = credential.auth_headers(url=BASE + "/cachedContents", auth_mode="bearer")["Authorization"]
    return digest_json({"provider": PROVIDER, "origin": ORIGIN, "credential": header})


def _model(model):
    if not isinstance(model, str) or not re.fullmatch(r"gemini-[A-Za-z0-9._-]{1,100}", model):
        raise ValueError("An exact Gemini request model is required")
    return model


def _configuration(config, model, *, require_model=True):
    provider = get_provider_config(config, PROVIDER)
    interface = get_provider_interface(config, TRANSPORT, PROVIDER)
    if (str(interface.get("base_url", "")).rstrip("/") != BASE
            or interface.get("path") != "/models/{model}:generateContent" or interface.get("auth") != "bearer"
            or interface.get("disabled_reason")
            or any(key in interface and interface[key] is not True for key in
                   ("enabled", "executable", "runner_enabled", "parameter_test_enabled"))):
        raise ValueError("Gateway cache requires the exact configured native origin, path and bearer authentication")
    if require_model and model not in (provider.get("models") or {}).get("candidates", []):
        raise ValueError("Gateway model is absent from its configured provider")
    # Only hashes of configuration are frozen, not a private provider overlay or key.
    return {"provider_digest": digest_json(provider),
            "route": {"base_url": BASE, "path": interface["path"], "auth": "bearer"}}


def _config_digest(config):
    return digest_json(get_provider_config(config, PROVIDER))


def _reference(catalog, model):
    if not isinstance(catalog, Catalog):
        raise ValueError("Gateway reference identity requires the real MPDB Catalog")
    payload = catalog.payload
    matching = [(key, value) for key, value in payload["interfaces"].items()
                if value.get("source_id") == "google_vertex" and value.get("modality") == "text"
                and value.get("family_id") == "gemini" and value.get("api_form") == TRANSPORT
                and model in value.get("request_model_ids", [])]
    if len(matching) != 1:
        raise ValueError("An exact unambiguous Google Vertex reference interface is required")
    interface_id, interface = matching[0]
    profile = payload["profiles"][interface["profile_id"]]
    contract_id = interface["default_contract_id"]
    contract = payload["contracts"][contract_id]
    policies = {key: row for key, row in payload["test_bindings"].items()
                if row.get("extension_type") == "model_test_policy" and row.get("interface_id") == interface_id}
    if (profile.get("source_id") != "google_vertex" or model not in profile.get("request_model_ids", [])
            or contract.get("source_id") != "google_vertex" or contract.get("api_form") != TRANSPORT
            or interface.get("enabled") is not False or interface.get("executable") is not False
            or not policies or any(row.get("parameter_test_enabled") is not False for row in policies.values())):
        raise ValueError("The separate gateway sample must retain its existing disabled direct-Vertex reference gates")
    return {"source_id": "google_vertex", "profile_id": interface["profile_id"], "interface_id": interface_id,
            "contract_id": contract_id, "catalog_digest": catalog.digest,
            "test_extension_digest": payload.get("test_extension_digest"),
            "reference_snapshot_digest": digest_json({"profile": profile, "interface": interface,
                                                       "contract": contract, "policies": policies}),
            "native_execution_enabled": False, "reference_api_version": "v1"}


def _path(model, name=None):
    return "/v1beta/" + (name or "cachedContents") + "?model=" + quote(model, safe="")


def _create_request(target, body):
    return {"operation": "cache_create", "method": "POST", "path": _path(target["model"]),
            "body": json_copy(body), "capture_raw": True}


def _delete_request(target, name):
    return {"operation": "cache_delete", "method": "DELETE", "path": _path(target["model"], name), "resource_id": name}


def _identity(target):
    return {"kind": "gateway_cached_content", "provider": PROVIDER, "origin": ORIGIN,
            "routing_model": target["model"], "credential_scope_sha256": target["credential_scope_sha256"],
            "create_body_sha256": target["create_body_sha256"]}


def prepare_gateway_cached_content_plan(config, model, *, catalog=None, cache_text, prompt,
        credential_scope_sha256, expected_text=None, ttl_seconds=120, max_output_tokens=2048,
        request_timeout_seconds=120, cleanup_timeout_seconds=30):
    """Compile exactly one create/use experiment and one reserved owned deletion."""
    model = _model(model)
    config_binding = _configuration(config, model)
    if not isinstance(credential_scope_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", credential_scope_sha256):
        raise ValueError("A frozen credential account/key SHA256 is required")
    if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 300:
        raise ValueError("Gateway cache TTL must be an integer in [60, 300]")
    if type(max_output_tokens) is not int or not 256 <= max_output_tokens <= 4096:
        raise ValueError("Gateway generation output allowance must be in [256, 4096]")
    for value, maximum in ((request_timeout_seconds, 120), (cleanup_timeout_seconds, 30)):
        if type(value) not in (int, float) or not 1 <= value <= maximum:
            raise ValueError("Gateway request/cleanup timeout exceeds its bounded allowance")
    if (not isinstance(cache_text, str) or not cache_text.strip() or not isinstance(prompt, str) or not prompt.strip()
            or len(prompt) > 4096 or len(cache_text) + len(prompt) > 100000):
        raise ValueError("Bounded nonempty synthetic cache text and an independent prompt are required")
    if expected_text is not None and (not isinstance(expected_text, str) or not expected_text.strip()
            or len(expected_text) > 512 or expected_text not in cache_text or expected_text in prompt):
        raise ValueError("Expected text must occur in cache text and be absent from the independent use prompt")
    body = {"model": "models/" + model, "ttl": f"{ttl_seconds}s",
            "contents": [{"role": "user", "parts": [{"text": cache_text}]}]}
    if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > 128 * 1024:
        raise ValueError("Gateway cache body exceeds its byte allowance")
    catalog = catalog or get_model_profile_catalog()
    reference = _reference(catalog, model)
    target = {"provider": PROVIDER, "model": model, "source_id": "google_vertex", "modality": "text",
              "route_profile": "google_vertex", "api_form": TRANSPORT, "base_url": BASE,
              "profile_id": reference["profile_id"], "interface_id": reference["interface_id"],
              "contract_id": reference["contract_id"], "credential_scope_sha256": credential_scope_sha256,
              "create_body_sha256": digest_json(body), "supplier_wire_version": "v1beta",
              "execution_target": {"provider_id": PROVIDER, "request_model_id": model,
                                   "transport_adapter_id": TRANSPORT, "api_form": TRANSPORT}}
    arguments = {"model": model, "cache_text": cache_text, "prompt": prompt, "ttl_seconds": ttl_seconds,
                 "max_output_tokens": max_output_tokens, "credential_scope_sha256": credential_scope_sha256,
                 "expected_text": expected_text, "request_timeout_seconds": request_timeout_seconds,
                 "cleanup_timeout_seconds": cleanup_timeout_seconds}
    definition = {"workflow_schema_version": 1, "id": "gemini-gateway-cache/" + model, "target": target,
        "factory": {"factory_id": FACTORY_ID, "version": FACTORY_VERSION, "source_sha256": source_digest()},
        "factory_arguments": arguments, "configuration_binding": config_binding, "reference": reference,
        "proof_scope": "designated_gateway_cached_content_sample", "google_direct": False,
        "physical_upstream_verified": False, "native_vertex_certification": False,
        "cases": [{"id": "cache_create"}, {"id": "cache_use"}], "cleanup_handlers": [DELETE],
        "steps": [{"id": "create", "case_id": "cache_create", "handler": CREATE,
                   "inputs": {"request": _create_request(target, body)}, "request_cap": 1, "cleanup_request_cap": 1},
                  {"id": "use", "case_id": "cache_use", "handler": USE, "request_cap": 1, "cleanup_request_cap": 0,
                   "inputs": {"request": {"operation": "cache_use", "method": "POST",
                       "path": f"/v1beta/models/{model}:generateContent", "capture_raw": True,
                       "body": {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                                "cachedContent": {"$result_ref": {"step": "create", "path": ["resource_id"], "type": "string"}},
                                "generationConfig": {"maxOutputTokens": max_output_tokens}}}}}],
        "limits": {"max_requests": 2, "cleanup_max_requests": 1, "request_timeout_seconds": request_timeout_seconds,
                   "deadline_seconds": 2 * request_timeout_seconds, "cleanup_deadline_seconds": cleanup_timeout_seconds},
        "policy": {"fatal_status_codes": [*range(300, 400), 401, 402, 403, 404, 408, 429, *range(500, 600)]}}
    registry = _registry(config, catalog, model)
    return compile_plan(definition, registry, run_count=1), registry


def _business_digest(config, catalog, model):
    try:
        return digest_json({"source": execution_digest(), "config": _config_digest(config), "reference": _reference(catalog, model)})
    except Exception:
        raise IntegrityError("Gateway cache configuration or reference changed") from None


def _registry(config, catalog, model, *, cleanup_only=False):
    registry = HandlerRegistry()
    if not cleanup_only:
        declared = lambda: _business_digest(config, catalog, model)
        registry.register(CREATE, _create, version=FACTORY_VERSION, digest=declared)
        registry.register(USE, _use, version=FACTORY_VERSION, digest=declared)
    registry.register(DELETE, _delete, version=FACTORY_VERSION, digest=execution_digest)
    return registry


def registry_for_gateway_plan(config, plan, *, catalog=None, cleanup_only=False):
    frozen = validate_plan(plan)
    model = frozen["target"]["model"]
    if cleanup_only:
        _verify_configuration(config, frozen, cleanup_only=True)
        registry = _registry(config, None, model, cleanup_only=True)
        registry.verify({DELETE: frozen["handler_bindings"][DELETE]})
        return registry
    expected, registry = prepare_gateway_cached_content_plan(config, catalog=catalog, **frozen["definition"]["factory_arguments"])
    if digest_json(expected) != digest_json(frozen):
        raise IntegrityError("Current gateway sample differs from the frozen plan/source/configuration")
    return registry


def _verify_configuration(config, plan, *, cleanup_only=False):
    actual = _configuration(config, plan["target"]["model"], require_model=not cleanup_only)
    if actual["route"] != plan["definition"]["configuration_binding"]["route"]:
        raise IntegrityError("Gateway route or authentication changed")
    if not cleanup_only and actual != plan["definition"]["configuration_binding"]:
        raise IntegrityError("Gateway provider configuration changed")


def _valid_name(name):
    return isinstance(name, str) and _NAME.fullmatch(name) is not None


def _owned_resource(context, name):
    target = context._ledger.state["target"]
    matches = [item for item in context.resources if item["resource_id"] == name]
    expected_inputs = {"name": name, "request": _delete_request(target, name)}
    if (not _valid_name(name) or len(matches) != 1 or matches[0]["run_id"] != context.run_id
            or matches[0]["identity"] != _identity(target) or matches[0]["cleanup_handler"] != DELETE
            or digest_json(matches[0]["cleanup_inputs"]) != digest_json(expected_inputs)):
        raise IntegrityError("Gateway cleanup/use requires this exact run's owned cache and routing model")
    resource = matches[0]
    creation = next((row for row in context._ledger.state["creations"] if row["attempt_id"] == resource["creation_attempt_id"]), {})
    attempt = next((row for row in context._ledger.state["attempts"] if row["id"] == resource["creation_attempt_id"]), {})
    request = attempt.get("request", {})
    if (creation.get("run_id") != context.run_id or creation.get("step_id") != "create"
            or name not in creation.get("resource_ids", []) or attempt.get("phase") != "business"
            or request.get("operation") != "cache_create" or request.get("method") != "POST"
            or request.get("path") != _path(target["model"])
            or digest_json(request.get("body")) != target["create_body_sha256"]):
        raise IntegrityError("Gateway cache is missing its original owned creation attempt")
    payload = attempt.get("receipt", {}).get("response")
    if isinstance(payload, dict) and "name" in payload and payload["name"] != name:
        raise IntegrityError("Gateway cache name differs from its original creation receipt")
    return resource


def _observe_create(context, payload, status):
    if not isinstance(payload, dict):
        return
    name = payload.get("name")
    if type(status) is int and 200 <= status < 300 and _valid_name(name):
        existing = [row for row in context.resources if row["resource_id"] == name]
        if existing:
            if existing[0]["creation_attempt_id"] != context.last_creation_attempt_id:
                raise IntegrityError("Gateway create reused another creation's resource")
            return
        target = context._ledger.state["target"]
        context.register_resource(name, cleanup_handler=DELETE,
            cleanup_inputs={"name": name, "request": _delete_request(target, name)}, identity=_identity(target))
    elif (type(status) is int and 400 <= status < 500 and status != 408 and "name" not in payload
          and isinstance(payload.get("error"), dict) and bool(payload["error"])):
        context.creation_failed(context.last_creation_attempt_id, definitely_not_created=True)


def _successful(receipt):
    payload = receipt.get("response")
    if (receipt.get("http_status") not in (200, 201) or receipt.get("response_complete") is not True
            or receipt.get("error_type") or receipt.get("stream_observed")
            or not isinstance(payload, dict) or payload.get("error")):
        raise ValueError("Gateway lifecycle did not return a complete successful JSON resource response")
    return payload


def _create(context, inputs):
    receipt = context.dispatch(inputs["request"], creates_resource=True)
    payload, status = receipt.get("response"), receipt.get("http_status")
    if receipt.get("response_complete") and isinstance(payload, dict):
        _observe_create(context, payload, status)  # Also supports a fully injected offline dispatcher.
    if (type(status) is int and 400 <= status < 500 and status != 408 and isinstance(payload, dict)
            and "name" not in payload
            and receipt.get("response_complete") is True
            and isinstance(payload.get("error"), dict) and payload["error"]):
        return {"status": "failed", "accepted": False, "reason": "gateway_create_rejected", "receipt": receipt}
    payload = _successful(receipt)
    name = payload.get("name")
    _owned_resource(context, name)
    args = context._plan["definition"]["factory_arguments"]
    if payload.get("model") != "models/" + context.target["model"]:
        raise ValueError("Gateway cache returned a different model")
    _cache_expiry(payload, args["ttl_seconds"])
    return {"status": "passed", "accepted": True, "resource_id": name, "response": payload,
            "ttl_verified": True, "parameter_outcome": {"cache_created": True}}


def _use(context, inputs):
    request = inputs["request"]
    _owned_resource(context, request["body"]["cachedContent"])
    receipt = context.dispatch(request)
    payload = _successful(receipt)
    args = context._plan["definition"]["factory_arguments"]
    usage = validate_vertex_cached_response(payload, model=context.target["model"], output_limit=args["max_output_tokens"])
    visible = "".join(part["text"] for part in payload["candidates"][0]["content"]["parts"] if part.get("thought") is not True).strip()
    expected = args["expected_text"]
    if expected is not None and visible != expected:
        raise ValueError("Gateway cache did not recall the unique cache-only expected text")
    return {"status": "passed", "accepted": True, "response": payload, "usage": usage,
            "semantic_effect": {"positive_cached_usage": True, "unique_information_recalled": expected is not None},
            "identity_audit": {"status": "match"}, "token_audit": {"status": "passed", "usage": usage,
                "scope": "returned_usage_arithmetic_and_cache_telemetry", "independent_count_verified": False}}


def _delete(context, inputs):
    _owned_resource(context, inputs["name"])
    expected = _delete_request(context._ledger.state["target"], inputs["name"])
    if digest_json(inputs["request"]) != digest_json(expected):
        raise IntegrityError("Gateway cleanup request or model query changed")
    receipt = context.dispatch(expected, kind="delete")
    confirmed = (receipt.get("response_complete") is True and not receipt.get("error_type")
                 and receipt.get("http_status") in (200, 204) and receipt.get("response") in ({}, None))
    return {"status": "passed" if confirmed else "failed", "accepted": confirmed,
            "deleted": confirmed, "receipt": receipt}


class _ObservedResponse:
    def __init__(self, response, observer):
        self._response, self._observer = response, observer

    def __getattr__(self, name):
        return getattr(self._response, name)

    def __enter__(self):
        self._response.__enter__()
        return self

    def __exit__(self, *args):
        return self._response.__exit__(*args)

    def iter_content(self, *args, **kwargs):
        raw = bytearray()
        for chunk in self._response.iter_content(*args, **kwargs):
            if isinstance(chunk, bytes) and len(raw) + len(chunk) <= _MAX_RESPONSE_BYTES:
                raw.extend(chunk)
            else:
                self._observer = None
            yield chunk
        # Commit a usable ID before Response.__exit__/close can throw, while all
        # network IO is still owned and counted by the common HttpDispatcher.
        if self._observer is not None:
            try:
                payload = _response_json(bytes(raw))
            except (ValueError, UnicodeError):
                return
            self._observer(payload, self._response.status_code)


class _ObservedSession:
    def __init__(self, session, observer):
        self._session, self._observer = session, observer
        self._sent = False

    @property
    def trust_env(self):
        return self._session.trust_env

    @trust_env.setter
    def trust_env(self, value):
        self._session.trust_env = value

    def mount(self, *args, **kwargs):
        return self._session.mount(*args, **kwargs)

    def __enter__(self):
        self._session.__enter__()
        return self

    def __exit__(self, *args):
        return self._session.__exit__(*args)

    def request(self, *args, **kwargs):
        if self._sent:
            raise IntegrityError("Gateway resource operation attempted an additional uncounted send")
        self._sent = True
        return _ObservedResponse(self._session.request(*args, **kwargs), self._observer)


class GatewayResourceDispatcher:
    def __init__(self, config, plan, *, credential_factory=credential_from_config, session_factory=requests.Session, cleanup_only=False):
        if type(cleanup_only) is not bool:
            raise ValueError("cleanup_only must be boolean")
        self.plan = validate_plan(plan)
        _verify_configuration(config, self.plan, cleanup_only=cleanup_only)
        self._live_config, self._frozen_config = config, copy.deepcopy(config)
        self._credential_factory, self._session_factory = credential_factory, session_factory
        self._credential = None
        self._cleanup_only = cleanup_only
        self._active = None
        self._http = HttpDispatcher(self._frozen_config, PROVIDER, TRANSPORT, response_byte_limit=_MAX_RESPONSE_BYTES,
                                    credential_factory=self._get_credential, session_factory=self._session)

    def _get_credential(self, config, provider):
        if provider != PROVIDER:
            raise IntegrityError("Gateway credential provider changed")
        if self._credential is None:
            credential = self._credential_factory(config, provider)
            if credential_scope_digest(credential) != self.plan["target"]["credential_scope_sha256"]:
                raise IntegrityError("Gateway credential account/key differs from the frozen scope")
            self._credential = credential
        return self._credential

    def _session(self):
        context, operation = self._active
        def observed(payload, status):
            # Reject accidental secret echo in a resource name, without ever
            # registering or exposing a credential as a cleanup identifier.
            clean = self._credential.redact(payload)
            if clean.get("name") != payload.get("name"):
                return
            _observe_create(context, clean, status)
        return _ObservedSession(self._session_factory(), observed if operation == "cache_create" else None)

    def __call__(self, request, *, timeout, context):
        try:
            return self._dispatch(request, timeout=timeout, context=context)
        except IntegrityError:
            # Credential/session implementations may raise their own errors.
            # Core journals exception messages, so never forward their text.
            raise IntegrityError("Gateway resource request failed its frozen integrity checks") from None
        except Exception as exc:
            raise RuntimeError("Gateway resource operation failed (" + type(exc).__name__ + ")") from None

    def _dispatch(self, request, *, timeout, context):
        target = self.plan["target"]
        if self._cleanup_only and context.phase != "cleanup":
            if request.get("operation") == "cache_create" and context.last_creation_attempt_id:
                context.creation_failed(definitely_not_created=True)
            raise IntegrityError("Cleanup-only gateway dispatcher cannot send business requests")
        if (context._ledger.state["plan_digest"] != self.plan["plan_digest"]
                or digest_json(context._ledger.state["target"]) != digest_json(target)):
            raise IntegrityError("Gateway dispatcher does not belong to the frozen original run")
        operation = request.get("operation")
        if context.phase == "cleanup":
            if operation != "cache_delete":
                raise IntegrityError("Cleanup cannot send a gateway business operation")
            name = request.get("resource_id")
            _owned_resource(context, name)
            expected = _delete_request(target, name)
        else:
            _verify_configuration(self._live_config, self.plan)
            if operation == "cache_create":
                expected = self.plan["ordered_steps"][0]["inputs"]["request"]
            elif operation == "cache_use":
                name = request.get("body", {}).get("cachedContent")
                _owned_resource(context, name)
                expected = json_copy(self.plan["ordered_steps"][1]["inputs"]["request"])
                expected["body"]["cachedContent"] = name
            else:
                raise IntegrityError("Unknown gateway cache operation")
        if digest_json(request) != digest_json(expected):
            raise IntegrityError("Gateway request body, resource identity or routing model query changed")
        try:
            self._get_credential(self._frozen_config, PROVIDER)
        except Exception:
            if operation == "cache_create":
                context.creation_failed(definitely_not_created=True)
            raise
        self._active = context, operation
        try:
            bound = self.plan["limits"]["cleanup_deadline_seconds"] if context.phase == "cleanup" else self.plan["limits"]["request_timeout_seconds"]
            return self._http(request, timeout=min(timeout, bound), context=context)
        finally:
            self._active = None


def make_gateway_dispatcher(config, plan, *, credential_factory=credential_from_config, session_factory=requests.Session, cleanup_only=False):
    return GatewayResourceDispatcher(config, plan, credential_factory=credential_factory,
                                     session_factory=session_factory, cleanup_only=cleanup_only)
