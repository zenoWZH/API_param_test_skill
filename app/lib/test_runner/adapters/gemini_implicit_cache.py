"""One native Gemini implicit-cache trio for the designated gateway.

The pure cases/observe/summarize logic originates in
scripts/run_gemini_native_cache_group_reference.py. Its former source-selection
label is not an execution provider: this adapter names inferenceai_gemini in
every new case and freezes Google Vertex only as the reference source.
"""
from __future__ import annotations

import copy
import hashlib
import math
from pathlib import Path
import re

import requests

from lib.cache_acceptance import cache_telemetry, is_pro_model, validate_scenario
from lib.cache_coverage import POLICY as CACHE_POLICY
from lib.credential_security import credential_from_config
from lib.model_profile_catalog import get_model_profile_catalog
from lib.test_runner import HandlerRegistry, IntegrityError, compile_plan, digest_json, validate_plan
from lib.test_runner.common import json_copy
from lib.test_runner.transport import HttpDispatcher
from . import gemini_gateway_resources as gateway

MODEL, FORM = "gemini-3.7-flash", "gemini_generate_content"
PROVIDER = gateway.PROVIDER
CAP, REQUEST_CAP, TIMEOUT, MAX_BYTES = 2048, 3, 180, 2 * 1024 * 1024
LABELS = ("cold", "repeat", "negative")
FACTORY_ID, FACTORY_VERSION = "gemini_implicit_cache", "2"
OBSERVE, EVALUATE = "gemini.implicit.observe.v1", "gemini.implicit.evaluate.v1"
credential_scope_digest = gateway.credential_scope_digest
digest = digest_json


def require(condition, message):
    if not condition:
        raise ValueError(message)


def cases(nonces):
    """Preserve the existing full-request repeat and isolated initial nonce."""
    require(type(nonces) is list and len(nonces) == 2 and len(set(nonces)) == 2
            and all(type(n) is str and re.fullmatch("[0-9a-f]{32}", n) for n in nonces),
            "Two distinct prefix nonces required")
    corpus = "\n".join(f"Fictional library shelf {n:03d} contains blue notebooks, cedar bookmarks and silver pencils." for n in range(512))
    result = []
    for ordinal, label in enumerate(LABELS, 1):
        prefix = nonces[label == "negative"] + "\n" + corpus + "\nThe shelf catalogue is context only. Answer the arithmetic question briefly."
        body = {"contents": [{"role": "user", "parts": [{"text": prefix},
                {"text": "Calculate 6 * 7. Reply with only the integer result, without explanation."}]}],
                "generationConfig": {"maxOutputTokens": CAP}}
        result.append({"ordinal": ordinal, "case_id": PROVIDER + "/" + MODEL + "/cache_group/" + label,
            "label": label, "source_id": "google_vertex",
            "interface_id": "text/google_vertex/gemini/" + MODEL + "#gemini-generate-content-default",
            "contract_id": "gemini_3_7_flash_vertex_generate_content_reference", "api_form": FORM,
            "url": gateway.BASE + "/models/" + MODEL + ":generateContent", "body": body, "body_sha256": digest(body)})
    return result


def observe(status, payload, complete=True):
    result={'native_response_verified':False,'semantic_answer_verified':False,'telemetry':None}
    if status!=200 or not complete or type(payload) is not dict:return result
    try:
        feedback=payload.get('promptFeedback')
        require(feedback is None or type(feedback) is dict,'Invalid prompt feedback')
        require(payload.get('modelVersion')==MODEL and type(payload.get('responseId')) is str and bool(payload['responseId'])
                and not payload.get('error') and not (feedback or {}).get('blockReason'),
                'Invalid Gemini identity or blocked prompt')
        candidates=payload.get('candidates');require(type(candidates) is list and len(candidates)==1 and type(candidates[0]) is dict,'Invalid candidates')
        candidate=candidates[0];content=candidate.get('content')
        # Candidate.index is a zero-based protobuf scalar. ProtoJSON may omit
        # its default zero; the single-candidate envelope above removes any
        # ordering ambiguity. Explicit values still require an actual int 0.
        valid_index = 'index' not in candidate or (type(candidate['index']) is int and candidate['index'] == 0)
        require(candidate.get('finishReason')=='STOP' and valid_index and type(content) is dict
                and content.get('role')=='model','Invalid Gemini terminal')
        parts=content.get('parts');require(type(parts) is list and bool(parts),'Missing native text')
        text=''
        for part in parts:
            require(type(part) is dict and type(part.get('text')) is str and not set(part)-{'text','thought','thoughtSignature'}
                    and ('thought' not in part or type(part['thought']) is bool)
                    and ('thoughtSignature' not in part or type(part['thoughtSignature']) is str),
                    'Unexpected nontext response')
            if part.get('thought') is not True:text+=part['text']
        require(bool(text.strip()),'Missing visible text')
        usage=payload.get('usageMetadata');require(type(usage) is dict,'Missing native usage')
        require(all(type(usage.get(k)) is int and usage[k]>0 for k in ('promptTokenCount','candidatesTokenCount','totalTokenCount'))
                and usage['promptTokenCount']<=32768 and usage['candidatesTokenCount']<=CAP,'Invalid usage bounds')
        base=usage['promptTokenCount']+usage['candidatesTokenCount'];thoughts=usage.get('thoughtsTokenCount',0)
        require(type(thoughts) is int and thoughts>=0 and usage['totalTokenCount']==base+thoughts
                and usage['candidatesTokenCount']+thoughts<=CAP
                and ('toolUsePromptTokenCount' not in usage or type(usage['toolUsePromptTokenCount']) is int and usage['toolUsePromptTokenCount']==0),
                'Invalid native token arithmetic or output bound')
        telemetry=cache_telemetry(usage,FORM)
        result.update(native_response_verified=True,semantic_answer_verified=text.strip()=='42',telemetry=telemetry,
                      returned_model=payload['modelVersion'],usage=usage,text_sha256=hashlib.sha256(text.encode()).hexdigest())
    except (ValueError,TypeError,KeyError):pass
    return result


def summarize(package, rows, fatal=None):
    result = {'status': 'insufficient_evidence', 'reason': 'three complete native controls required'}
    if len(rows) == 3 and all(r['observation']['native_response_verified'] for r in rows):
        parsed = []
        for case in package['cases']:
            settings = copy.deepcopy(case['body']); prefix = settings['contents'][0]['parts'][0]['text']
            settings['contents'][0]['parts'][0]['text'] = '<CACHE_CONTROL_PREFIX>'; parsed.append((prefix,settings))
        if any(r['observation']['telemetry']['status']=='invalid' for r in rows):
            result={'status':'control_failed','reason':'invalid cache telemetry'}
        else:
            result = validate_scenario(cache_reads=[r['observation']['telemetry']['cached_input_tokens'] for r in rows],
                input_tokens=[r['observation']['telemetry']['input_tokens'] for r in rows],
                prefixes=[p[0] for p in parsed], settings=[p[1] for p in parsed],
                bindings=[package['binding']]*3, expected_binding=package['binding'])
    return {'package_sha256': digest(package), 'requests_sent': sum(r['client_entered'] for r in rows),
            'observations': rows, 'acceptance': result, 'fatal_error_type': fatal,
            'terminal_state': 'completed' if len(rows) == 3 and not fatal else 'stopped_early',
            'not_sent_case_ids': [c['case_id'] for c in package['cases'][len(rows):]],
            'all_models_or_forms_verified': False, 'report_retention': 'P1M'}


def source_digest():
    root = Path(__file__).resolve().parents[3]
    files = ("lib/test_runner/adapters/gemini_implicit_cache.py", "lib/cache_acceptance.py", "lib/cache_coverage.py")
    return digest({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files})


def execution_digest():
    return digest({"factory": source_digest(), "gateway_helpers": gateway.execution_digest()})


def _binding(config, catalog):
    try:
        return digest({"execution": execution_digest(), "configuration": gateway._configuration(config, MODEL),
                       "reference": gateway._reference(catalog, MODEL)})
    except Exception:
        raise IntegrityError("Implicit-cache source, configuration or reference changed") from None


def _registry(config, catalog):
    registry = HandlerRegistry()
    declared = lambda: _binding(config, catalog)
    registry.register(OBSERVE, _observe_step, version=FACTORY_VERSION, digest=declared)
    registry.register(EVALUATE, _evaluate_step, version=FACTORY_VERSION, digest=declared)
    return registry


def _request(case):
    return {"operation": "implicit_cache_generate", "method": "POST",
            "path": "/v1beta/models/" + MODEL + ":generateContent", "body": json_copy(case["body"]), "capture_raw": True}


def prepare_implicit_cache_plan(config, model=MODEL, *, catalog=None, nonces, credential_scope_sha256,
                                request_timeout_seconds=TIMEOUT):
    """Compile the fixed non-Pro source/family representative; never load keys."""
    require(model == MODEL and not is_pro_model(model), "Only the approved non-Pro Gemini representative is in scope")
    require(isinstance(credential_scope_sha256, str) and re.fullmatch("[0-9a-f]{64}", credential_scope_sha256),
            "A frozen credential account/key SHA256 is required")
    require(type(request_timeout_seconds) in (int, float) and math.isfinite(request_timeout_seconds)
            and 1 <= request_timeout_seconds <= TIMEOUT, "Implicit-cache timeout exceeds its bounded allowance")
    config_binding = gateway._configuration(config, model)
    catalog = catalog or get_model_profile_catalog()
    reference = gateway._reference(catalog, model)
    controls = cases(nonces)
    require(all(case["interface_id"] == reference["interface_id"] and case["contract_id"] == reference["contract_id"]
                for case in controls), "Original implicit-cache reference binding changed")
    target = {"provider": PROVIDER, "model": MODEL, "source_id": "google_vertex", "family_id": "gemini",
        "modality": "text", "route_profile": "google_vertex", "api_form": FORM, "base_url": gateway.BASE,
        "profile_id": reference["profile_id"], "interface_id": reference["interface_id"], "contract_id": reference["contract_id"],
        "credential_scope_sha256": credential_scope_sha256, "supplier_wire_version": "v1beta",
        "execution_target": {"provider_id": PROVIDER, "request_model_id": MODEL, "transport_adapter_id": FORM, "api_form": FORM}}
    binding = {key: target[key] for key in ("source_id", "profile_id", "interface_id", "api_form")}
    binding.update(default_contract_id=target["contract_id"], request_model_id=MODEL)
    package = {"schema_version": 1, "kind": "source_family_cache_representative", "selection": PROVIDER,
        "source_id": "google_vertex", "provider_id": PROVIDER, "family_id": "gemini", "nonces": list(nonces),
        "binding": binding, "cases": controls, "cache_policy": copy.deepcopy(CACHE_POLICY),
        "execution_target": {"provider_id": PROVIDER, "source_id": "google_vertex", "origin": gateway.ORIGIN,
            "endpoint": controls[0]["url"], "auth_mode": "bearer", "supplier_wire_version": "v1beta",
            "google_reference_api_version": "v1", "google_direct": False, "physical_upstream_verified": False,
            "user_designated_vertex_resource": True, "observation_authority": "user_designated_vertex_resource_via_inferenceai"},
        "policy": {"request_cap": REQUEST_CAP, "output_cap": CAP, "concurrency": 1, "retries": 0,
            "timeout_seconds": request_timeout_seconds, "response_byte_cap": MAX_BYTES, "allow_redirects": False,
            "report_retention": "P1M", "tools": False, "state_resources": False, "ordinary_mpdb_gates_changed": False,
            "official_numeric_reference_required": False, "expected_hits": [False, True, False]}}
    steps = []
    for index, case in enumerate(controls):
        steps.append({"id": case["label"], "case_id": case["case_id"], "handler": OBSERVE,
            "depends_on": [LABELS[index - 1]] if index else [], "request_cap": 1, "cleanup_request_cap": 0,
            "inputs": {"case": case, "request": _request(case)}})
    steps.append({"id": "evaluate", "case_id": controls[-1]["case_id"], "handler": EVALUATE,
        "request_cap": 0, "cleanup_request_cap": 0, "inputs": {"rows": [
            {"$result_ref": {"step": label, "path": ["row"], "type": "object"}} for label in LABELS]}})
    definition = {"workflow_schema_version": 1, "id": "gemini-implicit-cache/" + PROVIDER + "/" + MODEL,
        "target": target, "factory": {"factory_id": FACTORY_ID, "version": FACTORY_VERSION, "source_sha256": source_digest()},
        "factory_arguments": {"model": model, "nonces": list(nonces), "credential_scope_sha256": credential_scope_sha256,
                              "request_timeout_seconds": request_timeout_seconds},
        "configuration_binding": config_binding, "reference": reference, "reference_package": package,
        "proof_scope": "non_pro_source_family_implicit_cache_controls", "google_direct": False,
        "physical_upstream_verified": False, "native_vertex_certification": False, "state_resources": False,
        "cases": [{"id": case["case_id"]} for case in controls], "steps": steps, "cleanup_handlers": [],
        "limits": {"max_requests": REQUEST_CAP, "cleanup_max_requests": 0,
            "request_timeout_seconds": request_timeout_seconds, "deadline_seconds": REQUEST_CAP * request_timeout_seconds,
            "cleanup_deadline_seconds": 1},
        "policy": {"fatal_status_codes": [*range(300, 400), 401, 402, 403, 404, 408, 429, *range(500, 600)]}}
    registry = _registry(config, catalog)
    return compile_plan(definition, registry, run_count=1), registry


def registry_for_implicit_plan(config, plan, *, catalog=None):
    frozen = validate_plan(plan)
    if frozen["definition"].get("factory", {}).get("factory_id") != FACTORY_ID:
        raise IntegrityError("Not the designated implicit-cache factory")
    expected, registry = prepare_implicit_cache_plan(config, catalog=catalog, **frozen["definition"]["factory_arguments"])
    if digest(expected) != digest(frozen):
        raise IntegrityError("Implicit-cache plan differs from its current exact source, configuration or requests")
    return registry


def _row(case, receipt):
    complete = receipt.get("response_complete") is True
    usable = complete and not receipt.get("error_type") and not receipt.get("stream_observed")
    return {"case_id": case["case_id"], "ordinal": case["ordinal"], "label": case["label"],
        "client_entered": receipt.get("client_entered", receipt.get("http_status") is not None) is True,
        "request_sha256": case["body_sha256"], "status_code": receipt.get("http_status"),
        "response_complete": complete, "failure_type": receipt.get("error_type"),
        "observation": observe(receipt.get("http_status"), receipt.get("response"), usable),
        "response_sha256": receipt.get("captured_bytes_sha256", receipt.get("response_bytes_sha256")),
        "response_redacted": receipt.get("raw_redacted", False)}


def _observe_step(context, inputs):
    row = _row(inputs["case"], context.dispatch(inputs["request"]))
    observation = row["observation"]
    valid = observation["native_response_verified"] and observation["semantic_answer_verified"]
    return {"status": "passed" if valid else "failed", "accepted": bool(valid), "row": row,
        "reason": None if valid else "native_response_or_semantic_answer_not_verified",
        "identity_audit": {"status": "match" if observation["native_response_verified"] else "unverified"},
        "token_audit": {"status": "passed" if observation["native_response_verified"] else "unverified",
            "scope": "returned_usage_arithmetic_and_cache_telemetry", "independent_count_verified": False,
            "usage": observation.get("usage"), "cache_telemetry": observation.get("telemetry")}}


def _evaluate_step(context, inputs):
    summary = summarize(context._plan["definition"]["reference_package"], inputs["rows"])
    status = summary["acceptance"]["status"]
    outcome = "verified" if status == "pass" else "unknown" if status == "insufficient_evidence" else status
    summary = _scoped_summary(summary, workflow_status="passed" if status == "pass" else "failed", cache_outcome=outcome)
    return {"status": "passed" if status == "pass" else "inconclusive" if status == "insufficient_evidence" else "failed",
        "accepted": status == "pass", "cache_outcome": outcome,
        "summary": summary, "all_models_or_forms_verified": False}


def _scoped_summary(summary, *, workflow_status, cache_outcome):
    # Preserve the legacy cache-only verdict, which does not itself establish
    # correct arithmetic answers or successful completion of the workflow.
    rows = summary["observations"]
    return {**summary, "workflow_status": workflow_status, "cache_outcome": cache_outcome,
        "semantic_controls_verified": len(rows) == REQUEST_CAP and all(
            row["observation"]["native_response_verified"] and row["observation"]["semantic_answer_verified"] for row in rows)}


def summarize_implicit_report(plan, report):
    """Keep the original summary shape even when dispatch stops before evaluation."""
    frozen = validate_plan(plan)
    if report.get("plan_digest") != frozen["plan_digest"] or len(report.get("runs", [])) != 1:
        raise IntegrityError("Implicit-cache summary requires its exact single frozen run")
    run = report["runs"][0]
    if run.get("cleanup_only") or run.get("cleanup_request_count") or run.get("cleanup", {}).get("resources"):
        raise IntegrityError("An implicit-cache trio cannot contain resource cleanup")
    rows = []
    for case in frozen["definition"]["reference_package"]["cases"]:
        step = run["steps"].get(case["label"], {})
        if "row" in step:
            rows.append(json_copy(step["row"]))
            continue
        attempts = [attempt for attempt in run.get("attempts", []) if attempt["step_id"] == case["label"]]
        if attempts:
            if len(attempts) != 1:
                raise IntegrityError("Implicit-cache observation has more than one dispatch")
            attempt = attempts[0]
            receipt = attempt.get("receipt", {"error_type": attempt.get("error", {}).get("type")})
            rows.append(_row(case, receipt))
    evaluation = run["steps"].get("evaluate", {})
    outcome = evaluation.get("cache_outcome", "unknown") if "summary" in evaluation else "unknown"
    return _scoped_summary(summarize(frozen["definition"]["reference_package"], rows, run.get("fatal_reason")),
                           workflow_status=report["status"], cache_outcome=outcome)


class _SingleSendSession:
    def __init__(self, session, owner):
        self._session, self._owner, self._sent = session, owner, False

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
            raise IntegrityError("Implicit-cache operation attempted an additional uncounted send")
        self._sent, self._owner._client_entered = True, True
        response = self._session.request(*args, **kwargs)
        self._owner._http_status = response.status_code
        return response


class ImplicitCacheDispatcher:
    def __init__(self, config, plan, *, credential_factory=credential_from_config, session_factory=requests.Session):
        registry_for_implicit_plan(config, plan)
        self.plan = validate_plan(plan)
        self._config, self._frozen_config = config, copy.deepcopy(config)
        self._credential_factory, self._session_factory = credential_factory, session_factory
        self._credential = None
        self._client_entered, self._http_status = False, None
        self._http = HttpDispatcher(self._frozen_config, PROVIDER, FORM, response_byte_limit=MAX_BYTES,
            credential_factory=self._get_credential, session_factory=lambda: _SingleSendSession(self._session_factory(), self))

    def _get_credential(self, config, provider):
        if provider != PROVIDER:
            raise IntegrityError("Implicit-cache credential provider changed")
        if self._credential is None:
            credential = self._credential_factory(config, provider)
            if credential_scope_digest(credential) != self.plan["target"]["credential_scope_sha256"]:
                raise IntegrityError("Implicit-cache credential account/key differs from the frozen scope")
            self._credential = credential
        return self._credential

    def __call__(self, request, *, timeout, context):
        self._client_entered, self._http_status = False, None
        try:
            if (context.phase != "business" or context.step_id not in LABELS or context.resources
                    or context._ledger.state["creations"] or context._ledger.state["plan_digest"] != self.plan["plan_digest"]
                    or digest(context._ledger.state["target"]) != digest(self.plan["target"])):
                raise IntegrityError("Implicit-cache dispatcher requires its original resource-free business run")
            try:
                actual = gateway._configuration(self._config, MODEL)
            except Exception:
                raise IntegrityError("Implicit-cache configured route changed") from None
            if actual != self.plan["definition"]["configuration_binding"]:
                raise IntegrityError("Implicit-cache provider configuration changed")
            case = self.plan["definition"]["reference_package"]["cases"][LABELS.index(context.step_id)]
            if digest(request) != digest(_request(case)):
                raise IntegrityError("Implicit-cache body, URL query, method or operation changed")
            receipt = self._http(request, timeout=min(timeout, self.plan["limits"]["request_timeout_seconds"]), context=context)
            receipt["client_entered"] = self._client_entered
            return receipt
        except IntegrityError:
            raise IntegrityError("Implicit-cache dispatch failed its frozen integrity checks") from None
        except Exception as exc:
            # Neither an injected session/close error nor a credential loader can
            # put its raw exception text into the common durable evidence.
            return {"http_status": self._http_status, "response": None, "response_complete": False,
                    "client_entered": self._client_entered, "error_type": type(exc).__name__}


def make_implicit_dispatcher(config, plan, *, credential_factory=credential_from_config, session_factory=requests.Session):
    return ImplicitCacheDispatcher(config, plan, credential_factory=credential_factory, session_factory=session_factory)
