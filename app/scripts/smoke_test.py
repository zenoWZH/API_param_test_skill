from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.cache_suite import run_cache_suite
from lib.client import DeepSeekClient
from lib.config import (
    default_reports_root,
    ensure_dir,
    get_active_provider_name,
    get_model_api_form,
    get_model_family,
    get_model_route_profile,
    get_selected_model,
    load_config,
)
from lib.deepseek_params import (
    build_claude_tool_followup_request,
    build_native_tool_followup_request,
    build_openai_responses_tool_followup_request,
    build_request,
    build_tool_followup_request,
    profile_names,
)
from lib.metrics import write_json
from lib.model_profile_catalog import (
    database_snapshot,
    require_official_reference_binding,
    resolve_runtime_profile_binding,
    resolve_runtime_test_policy,
)
from lib.parameter_output_limit import (
    configured_parameter_test_output_budget,
    enforce_parameter_test_output_limit,
    parameter_targets_output_limit,
)
from lib.profile_validation import validate_profile_response, validate_tool_followup_response
from lib.reference_specs import (
    get_reference_source,
    parameter_label_for_profile,
    pressure_profiles_for_model,
)
from lib.threshold import check_cache, check_smoke
from lib.token_audit import audit_exchange, combine_exchange_audits


def main() -> int:
    config = load_config()
    smoke_dir = ensure_dir(Path(os.getenv("LOADTEST_REPORT_DIR") or default_reports_root() / "smoke"))
    results: list[dict[str, Any]] = []

    provider = get_active_provider_name(config)
    model = get_selected_model(config, provider)
    family = get_model_family(config, model, provider)
    route_profile = get_model_route_profile(config, model, provider)
    api_form = get_model_api_form(
        config, model, provider, route_profile=route_profile
    )
    try:
        model_database_binding = resolve_runtime_profile_binding(
            config,
            provider,
            model,
            family,
            route_profile,
            api_form,
            modality="text",
        )
        require_official_reference_binding(model_database_binding)
        resolve_runtime_test_policy(model_database_binding)
        model_profile_database = database_snapshot(model_database_binding)
    except (KeyError, RuntimeError, ValueError) as exc:
        results.append(
            {
                "name": "model_profile_database:binding",
                "pass": False,
                "failure_classification": exc.__class__.__name__,
                "message": str(exc),
            }
        )
        write_json(smoke_dir / "profile_results.json", results)
        verdict = check_smoke(results, config, smoke_dir)
        return 0 if verdict["pass"] else 1
    reference_source = str(model_profile_database["reference_contract_id"])
    config["_model_profile_database"] = model_profile_database
    write_json(
        smoke_dir / "model_profile_database.json",
        model_profile_database,
    )

    try:
        client = DeepSeekClient.from_config(config)
    except Exception as exc:
        results.append(
            {
                "name": "client:init",
                "pass": False,
                "failure_classification": exc.__class__.__name__,
                "message": str(exc),
            }
        )
        write_json(smoke_dir / "profile_results.json", results)
        verdict = check_smoke(results, config, smoke_dir)
        _attach_model_profile_identity(verdict, model_profile_database)
        write_json(smoke_dir / "verdict.json", verdict)
        return 0 if verdict["pass"] else 1

    model_result = client.list_models()
    results.append(
        {
            "name": "control:list_models",
            "pass": model_result.success,
            "status_code": model_result.status_code,
            "latency_ms": model_result.latency_ms,
            "failure_classification": model_result.failure_classification,
            "message": None if model_result.success else model_result.raw_text[:500],
        }
    )

    for profile in profile_names(config, "throughput_profiles"):
        results.append(
            run_profile_smoke(
                client,
                config,
                "throughput_profiles",
                profile,
                api_form=api_form,
                route_profile=route_profile,
            )
        )
    try:
        compatibility_profiles = pressure_profiles_for_model(
            family,
            model,
            reference_source,
            api_form=api_form,
            route_profile=route_profile,
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        compatibility_profiles = []
        results.append(
            {
                "name": "capability:profile",
                "pass": False,
                "failure_classification": exc.__class__.__name__,
                "message": str(exc),
            }
        )
    for profile in compatibility_profiles:
        results.append(
            run_profile_smoke(
                client,
                config,
                "compatibility_profiles",
                profile,
                reference_source=reference_source,
                api_form=api_form,
                route_profile=route_profile,
            )
        )

    try:
        cache_result = run_cache_suite(config, client, default_reports_root() / "cache")
        cache_verdict = check_cache(cache_result, config, default_reports_root() / "cache")
        results.append(
            {
                "name": "cache:suite",
                "pass": bool(cache_verdict.get("pass")),
                "failure_classification": None if cache_verdict.get("pass") else "cache_threshold",
                "message": None if cache_verdict.get("pass") else json.dumps(cache_verdict.get("failures"), ensure_ascii=False),
            }
        )
    except Exception as exc:
        results.append(
            {
                "name": "cache:suite",
                "pass": False,
                "failure_classification": exc.__class__.__name__,
                "message": str(exc),
            }
        )

    write_json(smoke_dir / "profile_results.json", results)
    verdict = check_smoke(results, config, smoke_dir)
    _attach_model_profile_identity(verdict, model_profile_database)
    write_json(smoke_dir / "verdict.json", verdict)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if verdict["pass"] else 1


def _attach_model_profile_identity(
    target: dict[str, Any], snapshot: dict[str, Any]
) -> None:
    target.update(
        {
            "source_id": snapshot.get("source_id"),
            "profile_id": snapshot.get("profile_id"),
            "interface_id": snapshot.get("interface_id"),
            "test_binding_id": snapshot.get("test_binding_id"),
            "model_profile_database": copy.deepcopy(snapshot),
        }
    )


def _count_parameter_smoke_input(client, transport, model, body):
    counter = getattr(client, "count_tokens", None)
    if not callable(counter):
        return None
    copied = copy.deepcopy(body)
    try:
        result = counter(transport, model, copied)
    except Exception:
        result = None
    if copied != body:
        return {"tokens": None, "evidence_level": "unavailable", "request_integrity": "fail"}
    return result if isinstance(result, dict) else None


def _preferred_chat_output_limit_field(reference: dict[str, Any]) -> str:
    params = reference.get("params")
    params = params if isinstance(params, dict) else {}
    completion = params.get("max_completion_tokens")
    legacy = params.get("max_tokens")
    completion_supported = isinstance(completion, dict) and bool(
        completion.get("supported", completion.get("state") == "supported")
    )
    legacy_supported = isinstance(legacy, dict) and bool(
        legacy.get("supported", legacy.get("state") == "supported")
    )
    if completion_supported and not legacy_supported:
        return "max_completion_tokens"
    return "max_tokens"


def run_profile_smoke(
    client: DeepSeekClient,
    config: dict[str, Any],
    group: str,
    profile: str,
    *,
    reference_source: str | None = None,
    api_form: str | None = None,
    route_profile: str | None = None,
) -> dict[str, Any]:
    name = f"{group}:{profile}"
    try:
        built = build_request(
            config,
            group,
            profile,
            api_form_override=api_form,
            route_profile_override=route_profile,
            reference_source=reference_source,
            parameter_test=group == "compatibility_profiles",
        )
        transport = str(built.metadata.get("transport") or "chat_completions")
        output_token_limits: dict[str, int] = {}
        chat_completion_field = "max_tokens"
        if group == "compatibility_profiles":
            reference = get_reference_source(reference_source)
            chat_completion_field = _preferred_chat_output_limit_field(reference)
            output_token_limits = enforce_parameter_test_output_limit(
                built.body,
                transport,
                minimum=configured_parameter_test_output_budget(
                    config, built.body,
                    preserve_declared_limit=parameter_targets_output_limit(
                        parameter_label_for_profile(reference_source, profile), profile=profile,
                    ),
                ),
                chat_completion_field=chat_completion_field,
            )
        request_snapshot = copy.deepcopy(built.body)
        result = _send_transport_request(
            client,
            transport,
            str(built.metadata.get("requested_model")),
            built.body,
        )
        audits: list[dict[str, Any]] = []
        if group == "compatibility_profiles":
            audits.append(audit_exchange(
                request_snapshot, result, transport, config, "initial",
                independent_input_count=_count_parameter_smoke_input(
                    client, transport, str(built.metadata.get("requested_model") or ""), request_snapshot,
                ),
                provider=str(getattr(client, "provider", "") or "") or None,
                model=str(built.metadata.get("requested_model") or ""),
                accounting_source_id=str(reference.get("source_id") or ""),
                accounting_contract_id=reference_source,
                usage_required=bool((result.status_code and 200 <= result.status_code <= 299) or result.usage or result.text),
            ))
            if built.body != request_snapshot:
                audits[-1].update(validation_pass=False, validation_status="fail")
                audits[-1]["validation_failures"].append("request payload changed during dispatch")
        validation_error = validate_profile_response(
            profile,
            result.response_json,
            result,
            request_body=built.body,
            transport=transport,
            reference_source=reference_source,
        )
        passed = result.success and validation_error is None

        if passed and built.metadata.get("multi_turn") and all(item["validation_pass"] for item in audits):
            if transport == "gemini_generate_content":
                followup_body = build_native_tool_followup_request(built.body, result.response_json)
            elif transport == "claude_messages":
                followup_body = build_claude_tool_followup_request(
                    built.body,
                    result.response_json,
                )
            elif transport == "openai_responses":
                followup_body = build_openai_responses_tool_followup_request(
                    built.body,
                    result.response_json,
                )
            else:
                followup_body = build_tool_followup_request(
                    built.body,
                    result.response_json,
                    pass_reasoning_content=bool(built.metadata.get("pass_reasoning_content")),
                )
            if group == "compatibility_profiles":
                enforce_parameter_test_output_limit(
                    followup_body,
                    transport,
                    minimum=configured_parameter_test_output_budget(config, followup_body),
                    chat_completion_field=chat_completion_field,
                )
            followup_snapshot = copy.deepcopy(followup_body)
            followup = _send_transport_request(
                client,
                transport,
                str(built.metadata.get("requested_model")),
                followup_body,
            )
            if group == "compatibility_profiles":
                audits.append(audit_exchange(
                    followup_snapshot, followup, transport, config, "followup",
                    independent_input_count=_count_parameter_smoke_input(
                        client, transport, str(built.metadata.get("requested_model") or ""), followup_snapshot,
                    ),
                    provider=str(getattr(client, "provider", "") or "") or None,
                    model=str(built.metadata.get("requested_model") or ""),
                    accounting_source_id=str(reference.get("source_id") or ""),
                    accounting_contract_id=reference_source,
                    usage_required=bool((followup.status_code and 200 <= followup.status_code <= 299) or followup.usage or followup.text),
                ))
                if followup_body != followup_snapshot:
                    audits[-1].update(validation_pass=False, validation_status="fail")
                    audits[-1]["validation_failures"].append("request payload changed during dispatch")
            validation_error = validate_tool_followup_response(
                followup.response_json,
                followup,
                transport=transport,
            )
            if validation_error:
                passed = False

        retry_note = None
        if not passed and profile == "json_output" and validation_error == "json_parse" and all(item["validation_pass"] for item in audits):
            retry = _send_transport_request(
                client,
                transport,
                str(built.metadata.get("requested_model")),
                built.body,
            )
            if group == "compatibility_profiles":
                audits.append(audit_exchange(
                    request_snapshot, retry, transport, config, "retry",
                    independent_input_count=_count_parameter_smoke_input(
                        client, transport, str(built.metadata.get("requested_model") or ""), request_snapshot,
                    ),
                    provider=str(getattr(client, "provider", "") or "") or None,
                    model=str(built.metadata.get("requested_model") or ""),
                    accounting_source_id=str(reference.get("source_id") or ""),
                    accounting_contract_id=reference_source,
                    usage_required=bool((retry.status_code and 200 <= retry.status_code <= 299) or retry.usage or retry.text),
                ))
                if built.body != request_snapshot:
                    audits[-1].update(validation_pass=False, validation_status="fail")
                    audits[-1]["validation_failures"].append("request payload changed during dispatch")
            retry_error = validate_profile_response(
                profile,
                retry.response_json,
                retry,
                request_body=built.body,
                transport=transport,
                reference_source=reference_source,
            )
            if retry.success and retry_error is None:
                result = retry
                passed = True
                validation_error = None
                retry_note = "json_output retry passed"

        token_audit = combine_exchange_audits(audits)
        if group == "compatibility_profiles" and not token_audit["validation_pass"]:
            passed = False
            validation_error = "token_validation_failed"
        return {
            "name": name,
            "pass": passed,
            "status_code": result.status_code,
            "latency_ms": result.latency_ms,
            "ttft_ms": result.ttft_ms,
            "finish_reason": result.finish_reason,
            "usage": result.usage,
            "model_family": built.metadata.get("model_family"),
            "api_form": built.metadata.get("api_form"),
            "route_profile": built.metadata.get("route_profile"),
            "reference_source": reference_source,
            "output_token_limits": output_token_limits,
            **({"token_audit": token_audit,
                "token_validation_pass": token_audit["validation_pass"]}
               if group == "compatibility_profiles" else {}),
            "warnings": built.warnings,
            "capability_profile_id": built.metadata.get(
                "capability_profile_id"
            ),
            "capability_profile_status": built.metadata.get(
                "capability_profile_status"
            ),
            "capability_omitted_params": built.metadata.get(
                "capability_omitted_params"
            ),
            "failure_classification": None
            if passed
            else validation_error or result.failure_classification or result.error_type,
            "message": retry_note if passed else result.raw_text[:500],
        }
    except Exception as exc:
        return {
            "name": name,
            "pass": False,
            "failure_classification": exc.__class__.__name__,
            "message": str(exc),
        }


def _send_transport_request(
    client: DeepSeekClient,
    transport: str,
    model: str,
    body: dict[str, Any],
) -> Any:
    if transport == "gemini_generate_content":
        return client.gemini_generate_content(model, body)
    if transport == "claude_messages":
        return client.claude_messages(body)
    if transport == "openai_responses":
        return client.openai_responses(body)
    if transport == "chat_completions":
        return client.chat_completion(body)
    raise ValueError(f"Unsupported transport: {transport!r}")


if __name__ == "__main__":
    raise SystemExit(main())
