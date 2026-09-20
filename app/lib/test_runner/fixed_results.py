"""Fixed native observations projected through the existing CLI token and identity audits."""
from __future__ import annotations
import copy
import requests
from lib.client import ChatResult
from lib.config import get_provider_config
from lib.deepseek_beta_reference import API_FORM as DEEPSEEK_BETA_FORM, decode_beta_response
from lib.deepseek_params import extract_content
from lib.metrics import write_json
from lib.model_identity import audit_model_identity, combine_model_identity_audits
from lib.parameter_output_limit import configured_parameter_test_min_output_tokens
from lib.token_audit import combine_exchange_audits
from .adapters.fixed_parameter import _kind, execute_fixed_parameter_plan


def run_fixed_parameter_params(config, client, plan, output_dir, *, runner_module, execution_plan=None, execute=None, artifact_writer=None):
    """Project immutable paired observations through the shared token gate."""
    kind = _kind(plan)
    is_fim, is_prefill, is_cache = kind == "fim_causal", kind == "anthropic_prefill", kind == "anthropic_cache"
    native_anthropic = is_prefill or is_cache
    transport = "claude_messages" if native_anthropic else "fim_completions" if is_fim else "chat_completions"
    endpoint = "/v1/messages" if native_anthropic else "/beta/completions" if is_fim else "/beta/chat/completions"
    form = "anthropic_messages" if native_anthropic else "openai_fim_completions_beta" if is_fim else DEEPSEEK_BETA_FORM
    provider = "anthropic_official" if native_anthropic else "deepseek_official"
    model = plan.model if native_anthropic else "deepseek-v4-pro"
    source, family = ("anthropic", "claude") if native_anthropic else ("deepseek", "deepseek")
    provider_cfg = get_provider_config(config, provider)
    if provider_cfg.get("reference_source_id") != source:
        raise ValueError("The fixed suite requires its exact official source")
    def result_rows(records, observations):
        rows = []
        for request, record, observed in zip(plan.requests, records, observations["case_results"]):
            if is_cache and request.kind == "count":
                passed = observed["pass"] is True
                status = "pass" if passed else "incompatible"
                rows.append({"name": request.case_id, "profile": request.case_id, "run_index": 1,
                    "parameter": "cache_control", "expectation": "supported", "status": status, "pass": passed,
                    "compatibility_status": status, "compatibility_pass": passed, "overall_pass": passed, "overall_status": status,
                    "token_validation_pass": True, "token_validation_status": "not_applicable",
                    "token_audit": combine_exchange_audits([]), "model_identity_audit": combine_model_identity_audits([]),
                    "provider": provider, "model": model, "model_family": family, "api_form": form,
                    "route_profile": "vendor_direct", "reference_source": plan.snapshot["reference_contract_id"],
                    "reference_family": family, "transport": transport, "request_endpoint": "/v1/messages/count_tokens",
                    "request_kind": "count", "status_code": record["status_code"], "latency_ms": record["latency_ms"],
                    "request_body": request.body, "response_raw": record["response_raw"], "usage": {},
                    "input_estimate": observed.get("input_estimate"), "count_precision": "official_estimate",
                    "token_exact_proof": False, "generated_output_audit_applicable": False,
                    "bounded_observation": observed, "input_sample": "mpdb_fixed_case",
                    "fixed_parameter_plan_digest": plan.plan_digest,
                    "failure_reason": None if passed else "官方输入估算或资格检查未通过；未生成 token 输出。"})
                continue
            try:
                payload = decode_beta_response(record["response_raw"])
            except ValueError:
                payload = {}
            native = ChatResult(success=observed["response_valid"], status_code=record["status_code"],
                latency_ms=record["latency_ms"], timestamp=record["timestamp"], response_json=payload,
                text=("".join(b.get("text", "") for b in payload.get("content", []) if isinstance(b, dict)) if native_anthropic else extract_content(payload)),
                usage=payload.get("usage") or {},
                finish_reason=payload.get("stop_reason") if native_anthropic else (payload.get("choices") or [{}])[0].get("finish_reason")
                    if isinstance(payload.get("choices"), list) and payload.get("choices")
                    and isinstance(payload["choices"][0], dict) else None,
                raw_text=record["response_raw"],
                error_type="fixed_transport_incomplete" if record.get("failure_type")
                    or record.get("interrupted") or record.get("response_complete") is False else None)
            audit_native = native
            output_projection = None
            if (is_fim and observed["pass"] and observed["response_valid"]
                    and request.expectation["kind"] == "echo_prompt_plus_completion"):
                # Echoed input is not newly generated output. Keep raw response
                # and identity untouched; disclose this token-audit-only view.
                audit_native = copy.deepcopy(native)
                prefix = request.body["prompt"]
                text = audit_native.response_json["choices"][0]["text"]
                if not text.startswith(prefix):
                    raise ValueError("Verified FIM echo does not contain its exact prompt")
                audit_native.response_json["choices"][0]["text"] = text[len(prefix):]
                output_projection = {"kind": "remove_verified_echoed_input_for_output_audit",
                                     "removed_prompt_characters": len(prefix), "raw_response_preserved": True}
            audit = combine_exchange_audits([runner_module._audit_exchange_safely(config, request.body, audit_native,
                transport, "initial", provider=provider, model=model,
                accounting_source_id=source, accounting_contract_id=plan.snapshot["reference_contract_id"])])
            if output_projection:
                audit["output_projection"] = output_projection
            identity = combine_model_identity_audits([audit_model_identity(requested_model=model,
                result=native, transport=transport, provider_cfg=provider_cfg,
                exchange="initial", request_endpoint=endpoint)])
            passed = observed["pass"] is True
            expectation = ("unsupported" if request.expectation["kind"] == "attributed_rejection" else "supported") if is_fim or native_anthropic else request.expectation
            status = ("expected_rejection" if expectation == "unsupported" else "pass") if passed else "incompatible"
            token_pass = audit.get("validation_pass") is True
            rows.append({"name": request.case_id, "profile": request.case_id, "run_index": 1,
                "parameter": request.target_parameter, "expectation": expectation,
                "status": status, "pass": passed, "compatibility_status": status, "compatibility_pass": passed,
                "token_validation_pass": token_pass, "token_validation_status": audit.get("validation_status"),
                "overall_pass": passed and token_pass, "overall_status": status if not passed or token_pass else "token_validation_failed",
                "provider": provider, "model": model, "model_family": family,
                "api_form": form, "route_profile": "vendor_direct",
                "reference_source": plan.snapshot["reference_contract_id"], "reference_family": family,
                "transport": transport if is_fim or native_anthropic else "deepseek-beta-chat-prefix", "request_endpoint": endpoint,
                "status_code": record["status_code"], "latency_ms": record["latency_ms"],
                "finish_reason": native.finish_reason, "usage": native.usage,
                "request_body": request.body, "response_raw": record["response_raw"], "response_json": payload,
                "token_audit": audit, "model_identity_audit": identity,
                **({key: observed[key] for key in (
                    "output_semantics", "complete_json_output", "token_cap_truncation_accepted", "stop_boundary"
                ) if key in observed}),
                "bounded_observation": observed, "input_sample": "mpdb_fixed_case",
                **({"request_kind": "generation", "fixed_parameter_plan_digest": plan.plan_digest} if is_cache else {}),
                "failure_reason": None if passed and token_pass else "固定请求语义或 token 验证未通过；详见独立观察。"})
        return rows
    def progress(records, observations):
        write_json(output_dir / "param_results.json", result_rows(records, observations))
        write_json(output_dir / ("cache_observations.json" if is_cache else "prefill_observations.json" if is_prefill else "fim_observations.json" if is_fim else "beta_observations.json"), observations)
    if execute is None:
        def execute(client, plan, directory, **kwargs):
            with requests.Session() as session:
                session.trust_env = False
                session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
                return execute_fixed_parameter_plan(client, plan, directory, session.request,
                    artifact_writer=artifact_writer, **kwargs)
    records, observations = execute(client, plan, output_dir,
        minimum_output_tokens=configured_parameter_test_min_output_tokens(config), on_record=progress, execution_plan=execution_plan)
    return result_rows(records, observations), observations
