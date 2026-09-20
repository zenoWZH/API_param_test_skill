"""Exact source-local AWS Mantle JSON samples through counted workflow steps.

The registered generation case is the default. CountTokens is an explicit helper
choice; historical IAM denial reports never supply new requests or permission.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from lib.config import get_provider_interface, get_model_route_profile
from lib.model_profile_catalog import get_model_profile_catalog
from scripts import run_bedrock_mantle_approved_proofs as domain

from .. import HandlerRegistry, IntegrityError, compile_plan, digest_json, validate_plan
from ..transport import HttpDispatcher

FACTORY_ID = "aws_bedrock_mantle_json"
FACTORY_VERSION = "1"
PROVIDER = "bedrock_mantle"
_FATAL_CODES = [*range(300, 400), 401, 402, 403, 408, 429, *range(500, 600)]


def source_digest():
    root = Path(__file__).resolve().parents[3]
    names = ("scripts/run_bedrock_mantle_approved_proofs.py",)
    return digest_json({"adapter": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                        **{name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}})


def execution_digest():
    root = Path(__file__).resolve().parents[3]
    names = ("lib/test_runner/transport.py", "lib/credential_security.py", "lib/parameter_output_limit.py",
             "lib/model_profile_catalog.py", "lib/config.py", "scripts/workflow_test.py")
    return digest_json({"factory": source_digest(), **{
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}})


def register_handlers(registry):
    registry.register("mantle.observe", _observe, version=FACTORY_VERSION, digest=execution_digest)


def _exact_interface(config):
    interface = get_provider_interface(config, "claude_messages", PROVIDER)
    if (str(interface.get("base_url", "")).rstrip("/") != domain.BASE_URL
            or interface.get("path") != "/messages" or interface.get("auth") not in {"anthropic", "bearer"}
            or interface.get("anthropic_version") != domain.API_VERSION):
        raise ValueError("Mantle workflow requires the exact configured source, endpoint and version")
    return interface


def _definition(config, model, catalog, *, include_count):
    if model not in domain.MODELS or type(include_count) is not bool:
        raise ValueError("Exact enabled Mantle model and boolean count selection are required")
    _exact_interface(config)
    if get_model_route_profile(config, model, PROVIDER) != "aws_bedrock":
        raise ValueError("Mantle workflow requires the exact AWS Bedrock route")
    binding = domain.exact_bindings(catalog)[model]
    target = {**copy.deepcopy(binding), "provider": PROVIDER, "model": model, "modality": "text",
              "base_url": domain.BASE_URL, "route_profile": "aws_bedrock", "reference_contract_id": domain.CONTRACT,
              "execution_target": {"provider_id": PROVIDER, "request_model_id": model,
                                   "transport_adapter_id": "claude_messages", "api_form": "anthropic_messages"}}
    count, generate = [copy.deepcopy(row) for row in domain.cases() if row["body"]["model"] == model]
    case_id = domain.CASE
    steps = []
    if include_count:
        steps.append({"id": case_id + "/count", "case_id": case_id, "handler": "mantle.observe",
                      "inputs": {"case": count}, "request_cap": 1})
    step = {"id": case_id, "case_id": case_id, "handler": "mantle.observe",
            "inputs": {"case": generate}, "request_cap": 1}
    if include_count:
        step.update(depends_on=[case_id + "/count"],
                    requires=[{"step": case_id + "/count", "path": ["accepted"], "equals": True}])
        step["inputs"]["counter"] = {"$ref": {"step": case_id + "/count", "path": ["observation"], "type": "object"}}
        step["inputs"]["count_case"] = count
    steps.append(step)
    request_cap = 2 if include_count else 1
    return {"workflow_schema_version": 1, "id": f"mantle/{model}/json", "target": target,
            "cases": [{"id": case_id, "name": case_id}], "steps": steps,
            "limits": {"max_requests": request_cap, "request_timeout_seconds": domain.RESPONSE_SECONDS,
                       "deadline_seconds": request_cap * domain.RESPONSE_SECONDS,
                       "cleanup_max_requests": 0, "cleanup_deadline_seconds": 15},
            "policy": {"fatal_status_codes": _FATAL_CODES},
            "factory": {"factory_id": FACTORY_ID, "version": FACTORY_VERSION, "source_sha256": source_digest()},
            "factory_arguments": {"model": model, "include_count": include_count},
            "reference": {"catalog_digest": catalog.digest, "test_extension_digest": catalog.payload.get("test_extension_digest"),
                          "binding": binding},
            "proof_scope": "source_local_prompted_json_sample", "full_parameter_certification": False,
            "physical_identity_attestation": False, "native_aws_runtime_certification": False}


def prepare_mantle_plan(config, model, catalog=None, *, include_count=False, runs=1):
    catalog = catalog or get_model_profile_catalog()
    workflow = _definition(config, model, catalog, include_count=include_count)
    registry = HandlerRegistry()
    register_handlers(registry)
    return compile_plan(workflow, registry, run_count=runs), registry


def registry_for_mantle_plan(config, plan, catalog=None):
    arguments = plan["definition"]["factory_arguments"]
    expected, registry = prepare_mantle_plan(config, arguments["model"], catalog,
        include_count=arguments["include_count"], runs=plan["run_count"])
    validate_plan(plan, registry)
    if expected != plan:
        raise IntegrityError("Frozen Mantle workflow differs from its exact source binding")
    return registry


def make_dispatcher(config, **kwargs):
    _exact_interface(config)
    return HttpDispatcher(config, PROVIDER, "claude_messages", response_byte_limit=domain.RESPONSE_BYTES,
                          operation_auth={"/anthropic/v1/messages": "anthropic"}, **kwargs)


def _observe(context, inputs):
    case = inputs["case"]
    expected = [row for row in domain.cases() if row["id"] == case.get("id")]
    if len(expected) != 1 or expected[0] != case or case["body"]["model"] != context.target["execution_target"]["request_model_id"]:
        raise IntegrityError("Mantle outbound request differs from its exact registered body")
    request = {"method": "POST", "path": case["path"], "body": copy.deepcopy(case["body"]),
               "headers": {"anthropic-version": domain.API_VERSION}, "capture_raw": True}
    receipt = context.dispatch(request, kind="count" if case["kind"] == "count" else "api")
    payload = receipt.get("response")
    if not isinstance(payload, dict):
        receipt = {**receipt, "error_type": receipt.get("error_type") or "InvalidJsonEnvelope"}
    elif receipt.get("stream_observed"):
        receipt = {**receipt, "error_type": "UnexpectedStreamingResponse"}
    observation = domain.observe(case, receipt.get("http_status"), payload if isinstance(payload, dict) else {},
                                 complete=receipt.get("response_complete", False))
    if receipt.get("stream_observed"):
        observation["envelope_valid"] = False
        observation["sample_pass"] = False
    passed = observation.get("envelope_valid") is True if case["kind"] == "count" else observation.get("sample_pass") is True
    comparison = domain.compare_pair(inputs.get("counter"), observation, inputs.get("count_case", {}), case) if inputs.get("counter") else {"status": "not_compared", "exact_token_proof": False}
    record = {**receipt, "request": copy.deepcopy(case["body"]), "observation": observation,
              "verdict": {"pass": passed, "token_comparison": comparison,
                          "structured_output_parameter_proof": False, "full_parameter_certification": False}}
    # A missing/incomplete response is fatal to the remaining business work, just
    # as in the original bounded sender; model failures retain their observation.
    if not receipt.get("response_complete") or receipt.get("error_type"):
        context.cancellation.request("mantle_transport_incomplete")
    return {"status": "passed" if passed else "failed", "accepted": observation["envelope_valid"],
            "observation": observation, "record": record, "verdict": record["verdict"]}
