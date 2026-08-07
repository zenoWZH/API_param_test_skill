from __future__ import annotations

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
from lib.profile_validation import validate_profile_response, validate_tool_followup_response
from lib.reference_specs import (
    default_reference_source_for_model,
    pressure_profiles_for_model,
)
from lib.threshold import check_cache, check_smoke


def main() -> int:
    config = load_config()
    smoke_dir = ensure_dir(Path(os.getenv("LOADTEST_REPORT_DIR") or default_reports_root() / "smoke"))
    results: list[dict[str, Any]] = []

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

    provider = get_active_provider_name(config)
    model = get_selected_model(config, provider)
    family = get_model_family(config, model, provider)
    route_profile = get_model_route_profile(config, model, provider)
    api_form = get_model_api_form(
        config, model, provider, route_profile=route_profile
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
        reference_source = default_reference_source_for_model(
            config,
            family,
            model,
            provider,
            api_form=api_form,
            route_profile=route_profile,
        )
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
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if verdict["pass"] else 1


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
        )
        transport = str(built.metadata.get("transport") or "chat_completions")
        result = _send_transport_request(
            client,
            transport,
            str(built.metadata.get("requested_model")),
            built.body,
        )
        validation_error = validate_profile_response(
            profile,
            result.response_json,
            result,
            request_body=built.body,
            transport=transport,
            reference_source=reference_source,
        )
        passed = result.success and validation_error is None

        if passed and built.metadata.get("multi_turn"):
            if transport == "gemini_generate_content":
                followup_body = build_native_tool_followup_request(built.body, result.response_json)
                followup = client.gemini_generate_content(
                    str(built.metadata.get("requested_model")),
                    followup_body,
                )
            elif transport == "claude_messages":
                followup_body = build_claude_tool_followup_request(
                    built.body,
                    result.response_json,
                )
                followup = client.claude_messages(followup_body)
            elif transport == "openai_responses":
                followup_body = build_openai_responses_tool_followup_request(
                    built.body,
                    result.response_json,
                )
                followup = client.openai_responses(followup_body)
            else:
                followup_body = build_tool_followup_request(
                    built.body,
                    result.response_json,
                    pass_reasoning_content=bool(built.metadata.get("pass_reasoning_content")),
                )
                followup = client.chat_completion(followup_body)
            validation_error = validate_tool_followup_response(
                followup.response_json,
                followup,
                transport=transport,
            )
            if validation_error:
                passed = False

        retry_note = None
        if not passed and profile == "json_output" and validation_error == "json_parse":
            retry = _send_transport_request(
                client,
                transport,
                str(built.metadata.get("requested_model")),
                built.body,
            )
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
