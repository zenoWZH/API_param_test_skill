from __future__ import annotations

import argparse
import base64
import binascii
import copy
import getpass
import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import quote, urlparse

try:
    from datetime import UTC
except ImportError:  # Python < 3.11
    UTC = timezone.utc

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.image_validation import (  # noqa: E402
    ImageInfo,
    ImageTestCase,
    MAX_IMAGE_PAYLOAD_BYTES,
    apply_capability_expectations,
    banana_variant_cases,
    evaluate_case,
    gemini_flash_31_lite_image_profile_cases,
    gpt_image_2_cases,
    grok_imagine_cases,
    infer_postprocess_suspicion,
    infer_resolution_correspondence,
    inspect_image_bytes,
)
from lib.image_url_safety import fetch_remote_image, validate_image_payload  # noqa: E402
from lib.gpt_image_25 import (  # noqa: E402
    decode_image_stream, edit_fixtures, fixture_manifest, gpt_image_25_cases,
    is_gpt_image_25, observation_outcome, response_parameter_evidence, summarize_observations,
    token_expectation_request, validate_case_binding,
    rejection_attribution as gpt_image_25_rejection_attribution,
)
from lib.image_reference_candidate import diagnostic_summary, image_exchange_evidence, separate_image_observations  # noqa: E402
from lib.image_matrix_reference import (  # noqa: E402
    evaluate_matrix_observation, finalize_matrix_observation, load_image_matrix_candidate,
    persist_image_matrix_candidate, select_image_matrix_cases,
)
from lib.banana_generate_content import (  # noqa: E402
    BANANA_GC_MODELS, banana_gc_comparison_metadata,
    build_banana_generate_content_cases, has_exact_banana_gc_reference,
)
from lib.gemini_api_version import (  # noqa: E402
    is_ai_studio_origin, require_ai_studio_v1beta_url, resolve_gemini_api_version,
)
from lib.credential_security import ProviderCredential, credential_from_config, redact_secrets  # noqa: E402
from lib.config import (  # noqa: E402
    api_form_for_transport,
    default_reports_root,
    get_image_endpoint,
    get_image_model_config,
    get_provider_config,
    load_config,
)
from lib.reference_specs import (  # noqa: E402
    capability_profile_snapshot,
    load_model_capability_profile,
)
from lib.job_spec import load_job_spec  # noqa: E402
from lib.model_identity import (  # noqa: E402
    audit_model_identity,
    combine_model_identity_audits,
    summarize_model_identity_audits,
)
from lib.model_profile_catalog import (  # noqa: E402
    binding_from_database_snapshot,
    capability_profile_from_database_snapshot,
    resolve_runtime_parameter_config,
)
from lib.token_audit import (  # noqa: E402
    TOKEN_AUDIT_SCHEMA_VERSION,
    audit_image_usage,
    combine_exchange_audits,
    summarize_token_audits,
)
from lib.image_token_expectations import image_output_token_expectation  # noqa: E402
from lib.client import DeepSeekClient  # noqa: E402


DEFAULT_PROMPT = (
    "A clean technical resolution test chart on a neutral gray background. "
    "Include a black and white checkerboard, thin diagonal lines, concentric circles, "
    "fine fabric texture, and the text RESOLUTION TEST in a simple sans-serif font."
)
IMAGE_EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
IMAGE_TRANSPORTS = (
    "images-generations",
    "images-edits",
    "openai-responses-image",
    "chat-completions",
    "gemini-interactions",
    "gemini-generate-content",
)
GEMINI_IMAGE_TRANSPORTS = {"gemini-interactions", "gemini-generate-content"}
GEMINI_API_VERSIONS = ("v1", "v1beta")
MAX_IMAGE_BASE64_CHARS = 4 * ((MAX_IMAGE_PAYLOAD_BYTES + 2) // 3)
MAX_IMAGE_DATA_URL_HEADER_CHARS = 128
SAFE_RESPONSE_HEADERS = {
    "content-type",
    "openai-processing-ms",
    "retry-after",
    "x-request-id",
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
}


def _result_test_profiles(results: list[dict[str, Any]]) -> list[str]:
    profiles: list[str] = []
    for item in results:
        metadata = item.get("metadata")
        profile = (
            metadata.get("test_profile")
            if isinstance(metadata, dict)
            else None
        ) or item.get("case")
        value = str(profile or "")
        if value and value not in profiles:
            profiles.append(value)
    return profiles


def _validate_image_request_input(
    body: dict[str, Any], prompt: str, transport: str, model: str
) -> None:
    """Keep the measured input equal to the selected, single-prompt case."""
    if "model" in body and body["model"] != model:
        raise ValueError("image parameter request model differs from the selected case")
    expected_input = {
        "images-generations": {"prompt": prompt},
        "images-edits": {"prompt": prompt},
        "chat-completions": {"messages": [{"role": "user", "content": prompt}]},
        "gemini-interactions": {"input": [{"type": "text", "text": prompt}]},
        "gemini-generate-content": {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}]
        },
    }[transport]
    if any(body.get(key) != value for key, value in expected_input.items()):
        raise ValueError("image parameter request input differs from the selected prompt")
    context_fields = {
        "prompt", "messages", "input", "contents", "system", "system_prompt",
        "systemInstruction", "system_instruction", "instructions", "suffix",
        "tools", "tool_resources", "previous_response_id", "previous_interaction_id",
        "cachedContent", "cached_content", "image", "images", "mask",
    }
    unexpected = sorted((set(body) & context_fields) - set(expected_input))
    if unexpected:
        raise ValueError(
            "image parameter request contains undeclared input context: "
            + ", ".join(unexpected)
        )


def _count_image_input_safely(
    config: dict[str, Any], provider: str | None, model: str,
    request_body: dict[str, Any], transport: str,
) -> dict[str, Any] | None:
    if not provider:
        return None
    client = None
    try:
        provider_cfg = get_provider_config(config, provider)
        interface = (provider_cfg.get("api_interfaces") or {}).get("token_count")
        if not isinstance(interface, dict):
            return None
        supported = interface.get("transports")
        if isinstance(supported, list) and transport not in supported:
            return None
        client = DeepSeekClient.from_config(config, provider)
        count_body = copy.deepcopy(request_body)
        count_snapshot = copy.deepcopy(count_body)
        try:
            result = client.count_tokens(transport, model, count_body)
        except Exception:
            result = None
        if count_body != count_snapshot:
            return {
                "tokens": None, "evidence_level": "unavailable",
                "covers_full_input": False, "kind": "provider_count",
                "request_integrity": "fail",
                "note": "token-count request changed during dispatch",
            }
        return result if isinstance(result, dict) else None
    except Exception:
        return None
    finally:
        if client is not None:
            client.session.close()


def _audit_image_usage_safely(
    request_body: dict[str, Any],
    response_json: dict[str, Any],
    usage: dict[str, Any],
    config: dict[str, Any],
    *,
    provider: str | None,
    model: str,
    transport: str,
    usage_required: bool,
    image_output_expectation: dict[str, Any] | None = None,
    independent_input_count: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return audit_image_usage(
            request_body,
            response_json,
            usage,
            config,
            provider=provider,
            model=model,
            transport=transport,
            usage_required=usage_required,
            image_output_expectation=image_output_expectation,
            independent_input_count=independent_input_count,
        )
    except Exception as exc:
        note = f"token audit error: {exc.__class__.__name__}"
        validation_status = "fail" if usage_required else "not_applicable"
        return combine_exchange_audits(
            [
                {
                    "schema_version": TOKEN_AUDIT_SCHEMA_VERSION,
                    "exchange": "initial",
                    "status": "not_available",
                    "validation_status": validation_status,
                    "validation_pass": not usage_required,
                    "validation_failures": [note] if usage_required else [],
                    "usage_required": usage_required,
                }
            ]
        )


TRANSIENT_IMAGE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


def _transient_image_failure(result: dict[str, Any]) -> bool:
    status = result.get("status_code")
    return status in TRANSIENT_IMAGE_STATUS_CODES or (
        status is None and result.get("request_network_failure") is True
    )


def _image_retry_delay(result: dict[str, Any], attempt: int, initial_delay: float) -> float:
    delay = min(initial_delay * 2 ** min(attempt - 1, 30), 120.0)
    headers = result.get("response_headers") or {}
    raw = next((value for name, value in headers.items() if str(name).lower() == "retry-after"), None)
    try:
        retry_after = float(raw)
    except (TypeError, ValueError):
        return delay
    return max(delay, retry_after) if math.isfinite(retry_after) and retry_after >= 0 else delay


def _sleep_for_image_retry(delay: float) -> None:
    while delay > 0:
        interval = min(delay, 60.0)
        time.sleep(interval)
        delay -= interval


def _execute_image_cases(
    cases: list[ImageTestCase],
    execute_case: Callable[[ImageTestCase], dict[str, Any]],
    *,
    report_dir: Path,
    reference_observation: bool,
    max_generation_requests: int | None = None,
    transient_retries: int = 0,
    retry_initial_delay: float = 15.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run a finite generation budget; only final attempts become case results."""
    limit = len(cases) if max_generation_requests is None else max_generation_requests
    results: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    sent = 0
    exhausted = False
    stop_reason = None
    for index, case in enumerate(cases, start=1):
        if sent >= limit:
            exhausted = True
            stop_reason = "generation_request_budget"
            break
        print(f"[image-param] {index}/{len(cases)} {case.name} "
              f"size={_case_size_label(case)} expected={case.expected_outcome}", flush=True)
        case_attempt = 0
        attempt_exchanges: list[dict[str, Any]] = []
        while True:
            # Reserve an attempt before invoking the sender, including network failures.
            sent += 1
            case_attempt += 1
            result = redact_secrets(execute_case(case))
            eligible = reference_observation and _transient_image_failure(result)
            may_retry = eligible and case_attempt <= transient_retries
            retry = may_retry and sent < limit
            if may_retry and not retry:
                exhausted = True
                stop_reason = "generation_request_budget"
            delay = _image_retry_delay(result, case_attempt, retry_initial_delay) if retry else None
            if reference_observation:
                attempt_audit = copy.deepcopy(result.get("token_audit") or {})
                exchanges = attempt_audit.get("exchanges") or []
                if not exchanges:
                    exchanges = [{
                        "schema_version": TOKEN_AUDIT_SCHEMA_VERSION,
                        "exchange": "initial", "validation_status": "fail",
                        "validation_pass": False,
                        "validation_failures": ["image attempt contains no token audit exchange"],
                    }]
                attempt_exchanges.extend({
                    **copy.deepcopy(exchange),
                    "exchange": f"attempt_{case_attempt}/{exchange.get('exchange') or 'initial'}",
                    "generation_request_number": sent,
                } for exchange in exchanges)
                if not retry and case_attempt > 1:
                    combined = combine_exchange_audits(attempt_exchanges)
                    passed = combined.get("validation_pass") is True
                    result.update({
                        "token_audit": combined,
                        "token_validation_pass": passed,
                        "token_validation_status": combined.get("validation_status"),
                        "token_validation_failures": combined.get("validation_failures") or [],
                        "overall_pass": result.get("overall_pass") is True and passed,
                    })
                    if not passed:
                        result["overall_status"] = "token_validation_failed"
                        result["overall_failures"] = list(dict.fromkeys([
                            *list(result.get("overall_failures") or []),
                            "token_validation_failed",
                        ]))
                        parameter_reference = case.metadata.get("image_reference_candidate") is True
                        if not parameter_reference:
                            result["diagnostic_pass"] = False
                        if result.get("status") == "observed_acceptance" and not parameter_reference:
                            result["status"] = "unresolved"
                result = {
                    **result, "case_index": index, "case_attempt": case_attempt,
                    "generation_request_number": sent, "retry_scheduled": retry,
                    "retry_delay_seconds": delay, "final_for_case": not retry,
                    "attempt_token_audit": attempt_audit,
                }
                attempts.append(copy.deepcopy(result))
                _write_json(report_dir / "attempt_results.json", attempts)
            print(f"[image-param] {case.name} attempt={case_attempt} "
                  f"status={result.get('status')} http={result.get('status_code')} "
                  f"generation_requests={sent}/{limit}", flush=True)
            if not retry:
                break
            print(f"[image-param] transient retry in {delay:g}s", flush=True)
            _sleep_for_image_retry(float(delay))
        results.append(result)
        _write_json(report_dir / "case_results.json", results)
        if case.metadata.get("gpt_image_25") and result.get("status_code") in {401, 403, 404}:
            stop_reason = "model_access_blocked"
            break
        if exhausted:
            break
        # Existing baseline and consecutive-failure controls inspect final attempts.
        if reference_observation and (
            (index == 1 and (result.get("status") != "observed_acceptance"
                             or result.get("diagnostic_pass") is not True
                             or result.get("dimension_validation_pass") is False))
            or result.get("status_code") in {401, 403, 404, 429}
            or (len(results) >= 3 and all(item.get("diagnostic_pass") is not True for item in results[-3:]))
        ):
            stop_reason = "baseline_or_service_failure"
            break
    if sent >= limit and len(results) < len(cases):
        exhausted = True
    return results, {
        "max_generation_requests": limit,
        "generation_request_count": sent,
        "remaining_generation_requests": max(0, limit - sent),
        "budget_exhausted": exhausted,
        "planned_case_count": len(cases), "executed_case_count": len(results),
        "not_executed_count": len(cases) - len(results), "stop_reason": stop_reason,
        "transient_retries": transient_retries,
        "retry_initial_delay_seconds": retry_initial_delay,
        "retry_backoff_cap_seconds": 120,
    }


def main(argv: list[str] | None = None, *, config: dict[str, Any] | None = None,
         job_spec: dict[str, Any] | None = None, _dispatch_claimed: bool = False) -> int:
    parser = _parser()
    supplied_arguments = list(argv) if argv is not None else sys.argv[1:]
    default_full_suite = not any(value == "--suite" or value.startswith("--suite=") for value in supplied_arguments)
    args = parser.parse_args(supplied_arguments)
    if args.credential_provider:
        if args.provider and args.provider != args.credential_provider:
            parser.error("Credential provider must match the selected image provider")
        args.provider = args.credential_provider
    if args.transient_retries < 0:
        parser.error("--transient-retries must be nonnegative.")
    if args.max_generation_requests is not None and args.max_generation_requests <= 0:
        parser.error("--max-generation-requests must be positive.")
    if not math.isfinite(args.retry_initial_delay) or args.retry_initial_delay < 0:
        parser.error("--retry-initial-delay must be a finite nonnegative number.")
    if not args.reference_candidate and (
        args.transient_retries or args.max_generation_requests is not None or args.retry_initial_delay != 15
    ):
        parser.error("Generation budgets and transient retries require --reference-candidate diagnostics.")
    config = copy.deepcopy(config) if config is not None else load_config()
    job_spec = job_spec if job_spec is not None else load_job_spec(os.getenv("LOADTEST_JOB_SPEC"))
    if job_spec and job_spec.get("schema_version") == 6:
        from lib.parameter_job_controls import bind_parameter_execution
        bind_parameter_execution(config, job_spec, os.environ)
    if job_spec and job_spec.get("schema_version") == 6:
        frozen_prompt = job_spec["execution_plan"]["definition"].get("factory_arguments", {}).get("prompt")
        if frozen_prompt is not None:
            explicit_prompt = any(value == "--prompt" or value.startswith("--prompt=") for value in supplied_arguments)
            if explicit_prompt and args.prompt != frozen_prompt:
                parser.error("CLI prompt conflicts with the frozen image job")
            args.prompt = frozen_prompt
    configured_provider = False
    try:
        provider_cfg = (
            get_provider_config(config, args.provider)
            if args.provider
            else {"name": "image-cli", "backend": "unknown", "models": {}}
        )
        configured_provider = bool(args.provider)
    except KeyError:
        # A caller may supply a fully resolved endpoint/model and an ephemeral
        # provider label (for example an isolated console integration test).
        # Auditing remains available but cannot apply provider-local aliases.
        provider_cfg = {
            "name": args.provider or "image-cli",
            "backend": "unknown",
            "models": {},
        }
    model = args.model or {
        "gpt-image-2": "gpt-image-2",
        "banana": (
            "gemini-3.1-flash-image"
            if args.transport in GEMINI_IMAGE_TRANSPORTS
            else "nano-banana-pro-{resolution_lower}"
        ),
        "grok-imagine": "grok-imagine-image",
    }[args.family]
    api_version: str | None = None
    if args.transport in GEMINI_IMAGE_TRANSPORTS:
        try:
            old_plan = (job_spec or {}).get("image_plan") or {}
            api_version = resolve_gemini_api_version(
                args.base_url or os.getenv("IMAGE_TEST_BASE_URL") or "",
                old_plan.get("api_version") or args.api_version,
            ) or "v1"
        except ValueError as exc:
            parser.error(str(exc))
    elif args.api_version is not None:
        parser.error("--api-version applies only to native Gemini image transports.")
    try:
        endpoint = normalize_image_endpoint(
            args.base_url or os.getenv("IMAGE_TEST_BASE_URL") or "",
            args.transport,
            api_version=api_version or "v1",
            model=model,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if configured_provider:
        try:
            validate_configured_image_endpoint(
                config,
                provider=str(args.provider),
                endpoint=endpoint,
                transport=args.transport,
                model=str(model),
                api_version=api_version or "v1",
            )
        except (KeyError, ValueError) as exc:
            parser.error(str(exc))

    if (
        args.transport
        in {"chat-completions", "gemini-interactions", "gemini-generate-content"}
        and args.family != "banana"
    ):
        parser.error(
            f"The {args.transport} image transport supports only --family banana."
        )
    if args.transport in {"images-edits", "openai-responses-image"} and not is_gpt_image_25(str(model)):
        parser.error("This image transport requires an exact GPT Image 2.5 model.")
    args.auth_mode = args.auth_mode or (
        "google_api_key"
        if args.transport in GEMINI_IMAGE_TRANSPORTS
        else "bearer"
    )
    args.output_format = args.output_format or (
        "jpeg" if args.transport == "gemini-interactions" else "png"
    )
    if args.transport == "gemini-interactions" and args.output_format != "jpeg":
        parser.error(
            "Gemini Interactions image output currently supports only --output-format jpeg."
        )
    if default_full_suite:
        if args.family == "grok-imagine":
            args.include_2k = True
        else:
            args.include_4k = True
    if args.family == "grok-imagine" and args.include_4k:
        parser.error("Grok Imagine supports 1K/2K tiers; use --include-2k instead of --include-4k.")
    if args.family != "grok-imagine" and args.include_2k:
        parser.error("--include-2k currently applies only to --family grok-imagine.")
    try:
        deferred_gc = (
            args.family == "banana" and args.transport == "gemini-generate-content"
            and str(model) in BANANA_GC_MODELS
        )
        exact_gc = False
        deferred_gpt_responses = args.transport == "openai-responses-image"
        if deferred_gc or deferred_gpt_responses or args.reference_candidate:
            cases = []  # Select the factory only after restoring the exact binding.
        elif (
            args.family == "banana"
            and str(model) == "gemini-3.1-flash-lite-image"
            and args.transport == "gemini-generate-content"
        ):
            cases = gemini_flash_31_lite_image_profile_cases(
                args.suite,
                api_form="gemini_generate_content",
                include_4k=args.include_4k,
                include_negative=not args.no_negative,
            )
        elif args.family == "banana":
            cases = banana_variant_cases(
                args.suite,
                model_template=model,
                include_4k=args.include_4k,
                include_cross_control=not args.no_cross_control,
                include_negative=not args.no_negative,
                transport=(
                    "gemini-interactions"
                    if args.transport == "gemini-generate-content"
                    else args.transport
                ),
            )
        elif is_gpt_image_25(str(model)):
            cases = gpt_image_25_cases(
                str(model), args.suite,
                operation="edit" if args.transport == "images-edits" else "generation",
                include_4k=args.include_4k, include_negative=not args.no_negative,
            )
        elif args.family == "gpt-image-2":
            cases = gpt_image_2_cases(
                args.suite,
                include_4k=args.include_4k,
                include_negative=not args.no_negative,
            )
        else:
            cases = grok_imagine_cases(
                args.suite,
                include_2k=args.include_2k,
                include_negative=not args.no_negative,
            )
    except ValueError as exc:
        parser.error(str(exc))
    if not deferred_gc and not deferred_gpt_responses and not args.reference_candidate:
        cases = _select_cases(cases, args.case)
    transport_api_form = api_form_for_transport(args.transport, modality="image")
    api_form = str(args.api_form or transport_api_form)
    if api_form != transport_api_form:
        parser.error(
            f"--api-form {api_form!r} conflicts with --transport "
            f"{args.transport!r} ({transport_api_form!r})."
        )
    route_profile = str(args.route_profile or "").strip()
    model_profile_database: dict[str, Any]
    if configured_provider:
        try:
            selected_model_cfg = get_image_model_config(
                config,
                str(args.provider),
                str(model),
                route_profile=route_profile or None,
                api_form=api_form,
            )
        except (KeyError, ValueError) as exc:
            parser.error(str(exc))
        route_profile = str(selected_model_cfg.get("route_profile") or "")
        if str(selected_model_cfg.get("transport") or "") != args.transport:
            parser.error(
                f"API form {api_form!r} on route {route_profile!r} maps to "
                f"transport {selected_model_cfg.get('transport')!r}, not "
                f"{args.transport!r}."
            )
        try:
            parameter_config = resolve_runtime_parameter_config(
                config,
                str(args.provider),
                str(model),
                str(args.family),
                route_profile,
                api_form,
                modality="image",
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        policy = parameter_config.get("test_binding")
        parameter_binding = parameter_config.get("parameter_test_binding")
        if not args.reference_candidate and args.family in {"gpt-image-2", "grok-imagine"} and (
            not isinstance(policy, dict) or policy.get("parameter_test_enabled") is not True
        ):
            parser.error(f"Image parameter testing is disabled for {args.family}/{model} on {api_form}.")
        if args.transport == "gemini-generate-content" and (
            not isinstance(policy, dict)
            or policy.get("parameter_test_enabled") is not True
            or not isinstance(parameter_binding, dict)
            or not parameter_binding.get("test_cases")
        ):
            parser.error(
                f"Image parameter testing is disabled for {args.family}/{model} "
                f"on {api_form}."
            )
        model_profile_database = copy.deepcopy(
            parameter_config["model_profile_database"]
        )
    else:
        try:
            capability = load_model_capability_profile(
                "image",
                args.family,
                str(model),
                route_profile=route_profile or None,
                api_form=api_form,
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        if capability.get("profile_status") != "registered":
            parser.error(
                "Missing registered image model/API/route profile for "
                f"{args.family}/{api_form}/{model}/{route_profile or '<default>'}."
            )
        if not args.reference_candidate and args.family in {"gpt-image-2", "grok-imagine"} and (
            capability.get("parameter_test_enabled") is not True
            or capability.get("test_policy_parameter_test_enabled") is not True
        ):
            parser.error(f"Image parameter testing is disabled for {args.family}/{model} on {api_form}.")
        route_profile = str(capability.get("route_profile") or "")
        if str(capability.get("transport") or "") != args.transport:
            parser.error(
                f"API form {api_form!r} on route {route_profile!r} maps to "
                f"transport {capability.get('transport')!r}, not {args.transport!r}."
            )
        if args.transport == "gemini-generate-content" and (
            capability.get("parameter_test_enabled") is not True
            or capability.get("test_policy_parameter_test_enabled") is not True
        ):
            parser.error(
                f"Image parameter testing is disabled for {args.family}/{model} "
                f"on {api_form}."
            )
        model_profile_database = copy.deepcopy(
            capability["model_profile_database"]
        )
    matrix_reference_candidate = None
    if args.reference_candidate:
        try:
            if job_spec:
                raise ValueError("Reference candidates are CLI-only; an existing job snapshot cannot grant diagnostic execution.")
            capability = load_model_capability_profile(
                "image", args.family, str(model), route_profile=route_profile, api_form=api_form,
            )
            matrix_reference_candidate = load_image_matrix_candidate(
                args.reference_candidate, model=str(model), family=args.family,
                route_profile=route_profile, api_form=api_form, endpoint=endpoint, capability=capability,
            )
            if args.auth_mode != "bearer":
                raise ValueError("Official GPT/Grok image candidates require bearer authentication")
            cases = _select_cases(select_image_matrix_cases(
                str(model), api_form, suite=args.suite, include_4k=args.include_4k,
                include_2k=args.include_2k, include_negative=not args.no_negative,
            ), args.case)
        except (OSError, KeyError, ValueError) as exc:
            parser.error(str(exc))
    job_database = (
        job_spec.get("model_profile_database")
        if isinstance(job_spec, dict)
        else None
    )
    if isinstance(job_database, dict):
        snapshot_binding = binding_from_database_snapshot(job_database)
        target = snapshot_binding.get("execution_target") or {}
        expected_target = {
            "provider_id": args.provider,
            "request_model_id": model,
            "route_profile": route_profile,
            "api_form": api_form,
        }
        conflicts = [
            field
            for field, expected in expected_target.items()
            if expected not in (None, "") and target.get(field) != expected
        ]
        if conflicts:
            parser.error(
                "Image runtime conflicts with immutable MPDB snapshot: "
                + ", ".join(conflicts)
            )
        model_profile_database = copy.deepcopy(job_database)
    runtime_capability = capability_profile_from_database_snapshot(model_profile_database)
    runtime_expected = {"modality": "image", "family": str(args.family),
                        "model": str(model), "api_form": api_form,
                        "route_profile": route_profile, "transport": str(args.transport)}
    runtime_conflicts = [field for field, expected in runtime_expected.items()
                         if str(runtime_capability.get(field) or "") != expected]
    if runtime_conflicts:
        parser.error("Image runtime conflicts with immutable MPDB capability: " + ", ".join(runtime_conflicts))
    if deferred_gpt_responses:
        from lib.test_runner.adapters.image import run_responses_image_cli as main_image_cli
        selected_binding = binding_from_database_snapshot(model_profile_database)
        selected_capability = {
            **copy.deepcopy(selected_binding.get("test_binding") or {}),
            "api_form": api_form, "transport": args.transport, "route_profile": route_profile,
            "model_profile_database": copy.deepcopy(model_profile_database),
            "profile_id": selected_binding.get("profile_id"),
            "interface_id": selected_binding.get("interface_id"),
        }
        return main_image_cli(
            args, model=str(model), endpoint=endpoint, capability=selected_capability,
            model_database_binding=selected_binding, job_spec=job_spec, config=config,
            reference_candidate=matrix_reference_candidate, dispatch_claimed=_dispatch_claimed,
        )
    try:
        if is_gpt_image_25(str(model)) and not matrix_reference_candidate:
            validate_case_binding(binding_from_database_snapshot(model_profile_database), str(model),
                                  "edit" if args.transport == "images-edits" else "generation")
        if deferred_gc:
            exact_binding = binding_from_database_snapshot(model_profile_database)
            exact_policy = exact_binding.get("test_binding") or {}
            exact_parameter_binding = exact_binding.get("parameter_test_binding") or {}
            exact_capability = {
                **copy.deepcopy(exact_policy),
                "source_id": exact_binding.get("source_id"),
                "default_reference_source": exact_binding.get("reference_contract_id"),
                "api_form": (exact_binding.get("interface") or {}).get("api_form"),
                "route_profile": (exact_binding.get("execution_target") or {}).get("route_profile"),
                "parameter_test_enabled": bool(
                    exact_policy.get("parameter_test_enabled") is True
                    and exact_parameter_binding.get("test_cases")
                ),
                "test_policy_parameter_test_enabled": exact_policy.get("parameter_test_enabled"),
                "image_case_expectations": copy.deepcopy(exact_policy.get("image_case_expectations") or {}),
                "parameter_constraints": copy.deepcopy((exact_binding.get("interface") or {}).get("parameter_constraints") or {}),
            }
            exact_gc = has_exact_banana_gc_reference(str(model), exact_capability)
            if exact_gc:
                cases = build_banana_generate_content_cases(
                    str(model), args.suite, include_4k=args.include_4k,
                    include_negative=not args.no_negative,
                    capability_profile=exact_capability,
                )
            elif str(model) == "gemini-3.1-flash-lite-image":
                cases = gemini_flash_31_lite_image_profile_cases(
                    args.suite, api_form=api_form, include_4k=args.include_4k,
                    include_negative=not args.no_negative, capability_profile=exact_capability,
                )
            else:
                cases = banana_variant_cases(
                    args.suite, model_template=model, include_4k=args.include_4k,
                    include_cross_control=not args.no_cross_control,
                    include_negative=not args.no_negative, transport="gemini-interactions",
                )
            cases = _select_cases(cases, args.case)
        if not exact_gc and not is_gpt_image_25(str(model)) and not matrix_reference_candidate:
            cases = apply_capability_expectations(
                cases, family=args.family, model=str(model), api_form=api_form,
                route_profile=route_profile,
                capability_profile=runtime_capability,
            )
    except (KeyError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    comparison_metadata = banana_gc_comparison_metadata(str(model), exact_capability, endpoint) if exact_gc else {}
    if comparison_metadata.get("comparison_scope") == "adapter_only":
        cases = [replace(case, metadata={**case.metadata, **comparison_metadata}) for case in cases]
    if args.family != "grok-imagine":
        cases = [
            _with_output_options(
                case,
                args.quality,
                args.output_format,
                transport=args.transport,
            )
            for case in cases
        ]

    if matrix_reference_candidate:
        prompts = {case.metadata.get("prompt") for case in cases}
        if len(prompts) != 1 or not isinstance(next(iter(prompts)), str):
            parser.error("Image matrix candidate must pin one exact public prompt")
        args.prompt = next(iter(prompts))
    public_plan = {
        "endpoint": endpoint,
        **comparison_metadata,
        "provider": args.provider,
        "transport": args.transport,
        "api_form": api_form,
        "api_version": api_version,
        "route_profile": route_profile,
        "auth_mode": args.auth_mode,
        "family": args.family,
        "model": model,
        "requested_models": sorted({case.model_override or model for case in cases}),
        "suite": args.suite,
        "include_2k": args.include_2k,
        "include_4k": args.include_4k,
        "visual_forensics": not args.no_visual_forensics,
        "prompt_sha256": hashlib.sha256(args.prompt.encode("utf-8")).hexdigest(),
        "prompt": args.prompt if args.store_prompt else None,
        "cases": [case.public() for case in cases],
        "model_profile_database": model_profile_database,
    }
    if matrix_reference_candidate:
        public_plan.update({key: matrix_reference_candidate[key] for key in
                            ("model", "api_form", "source_id", "endpoint", "route_profile", "execution_mode", "profile_id", "interface_id")})
        public_plan.update(reference_candidate=matrix_reference_candidate, certified=False,
                           execution_mode="reference_observation", model_capability_profile=copy.deepcopy(capability),
                           max_generation_requests=args.max_generation_requests or len(cases),
                           transient_retries=args.transient_retries)
    from lib.test_runner.adapters.image import prepare_cases_plan, execute_image_cli_cases
    workflow_report_dir = _report_dir(args.output_dir, model)
    workflow_plan, workflow_registry = prepare_cases_plan(
        config, {**public_plan, "timeout_sec": args.timeout}, cases,
        prompt=args.prompt, output_dir=workflow_report_dir,
    )
    if job_spec and job_spec.get("schema_version") == 6:
        from lib.test_runner.adapters.image import bind_frozen_image_plan
        workflow_plan, workflow_registry = bind_frozen_image_plan(
            workflow_plan, config, job_spec, output_dir=workflow_report_dir,
        )
    public_plan["workflow"] = {
        "workflow_id": workflow_plan["workflow_id"], "plan_digest": workflow_plan["plan_digest"],
        "selected_cases": workflow_plan["selected_cases"], "limits": workflow_plan["limits"],
    }
    if args.store_prompt:
        public_plan["execution_plan"] = workflow_plan
    if args.dry_run:
        print(json.dumps(public_plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if workflow_report_dir.is_symlink() or workflow_report_dir.resolve() != workflow_report_dir.absolute():
        parser.error("Image report directory must not traverse a symlink")
    if any((workflow_report_dir / name).exists() for name in ("plan.json", "case_results.json", "summary.json", "workflow_report.json")):
        parser.error("Image report directory already contains execution evidence; use a new output directory")
    from lib.test_runner.adapters.image import claim_image_execution
    claim_image_execution(workflow_plan, workflow_report_dir, job_spec=job_spec, already_claimed=_dispatch_claimed)


    if any(case.metadata.get("banana_gc_candidate") is True for case in cases):
        parser.error("Selected GenerateContent cases lack reviewed v1beta evidence; use the root reference-candidate diagnostic CLI or select only verified cases.")

    credential = None
    if args.credential_provider:
        credential = credential_from_config(config, args.credential_provider)
    else:
        api_key = getpass.getpass("Image provider API key: ") if args.api_key_stdin else os.getenv(args.api_key_env)
        if not api_key:
            parser.error(f"Missing API key. Set {args.api_key_env}; raw API keys are intentionally not accepted as CLI arguments.")
        credential = ProviderCredential.create(provider=f"image:{model}", secret=api_key, base_urls=[endpoint])

    report_dir = workflow_report_dir
    if matrix_reference_candidate:
        if report_dir.is_symlink() or report_dir.resolve() != report_dir.absolute():
            parser.error("Image reference report directory must not traverse a symlink")
        if any((report_dir / name).exists() for name in ("plan.json", "case_results.json", "summary.json", "reference_candidate.json")):
            parser.error("Image reference report directory already contains execution evidence")
    images_dir = report_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    if matrix_reference_candidate:
        persist_image_matrix_candidate(args.reference_candidate, report_dir / "reference_candidate.json", matrix_reference_candidate)
    _write_json(report_dir / "plan.json", public_plan)

    results, request_budget = execute_image_cli_cases(
        config, workflow_plan, workflow_registry, credential=credential, report_dir=report_dir,
    )
    model_check = json.loads((report_dir / "model_check.json").read_text(encoding="utf-8"))
    execution_stop_reason = request_budget.get("stop_reason")

    postprocess = infer_postprocess_suspicion(results)
    resolution_correspondence = infer_resolution_correspondence(results)
    compatibility_failures = [item for item in results if not item.get("pass")]
    token_failures = [
        item for item in results if item.get("token_validation_pass") is False
    ]
    failures = [
        item
        for item in results
        if item.get("overall_pass", item.get("pass")) is not True
    ]
    token_audit_summary = summarize_token_audits(results)
    model_identity_summary = summarize_model_identity_audits(results)
    token_validation_pass = bool(token_audit_summary.get("pass", False))
    token_accuracy_pass = token_validation_pass
    model_identity_pass = bool(model_identity_summary.get("pass", True))
    capability_snapshot = copy.deepcopy(capability) if matrix_reference_candidate else capability_profile_snapshot(
        "image",
        args.family,
        str(model),
        _result_test_profiles(results),
        api_form=api_form,
        route_profile=route_profile,
        model_profile_database=model_profile_database,
    )
    capability_snapshot["model_profile_database"] = copy.deepcopy(
        model_profile_database
    )
    summary = {
        "pass": not failures and token_accuracy_pass and model_identity_pass and request_budget["workflow_status"] == "passed",
        "workflow_result": request_budget["workflow_result"],
        "compatibility_pass": not compatibility_failures,
        "token_accuracy_pass": token_accuracy_pass,
        "token_validation_pass": token_validation_pass,
        "model_identity_pass": model_identity_pass,
        **comparison_metadata,
        "family": args.family,
        "model": model,
        "api_form": api_form,
        "route_profile": capability_snapshot.get("route_profile"),
        "endpoint": endpoint,
        "transport": args.transport,
        "api_version": api_version,
        "suite": args.suite,
        "case_count": len(results),
        **({"planned_case_count": len(cases), "executed_case_count": len(results),
            "not_executed_count": len(cases) - len(results),
            "not_executed_cases": [case.name for case in cases[len(results):]],
            "stop_reason": execution_stop_reason}
           if is_gpt_image_25(str(model)) else {}),
        "pass_count": len(results) - len(failures),
        "failure_count": len(failures),
        "compatibility_failure_count": len(compatibility_failures),
        "token_failure_count": len(token_failures),
        "failed_cases": [item.get("case") for item in failures],
        "compatibility_failed_cases": [
            item.get("case") for item in compatibility_failures
        ],
        "token_failed_cases": [item.get("case") for item in token_failures],
        "model_capability_profile": capability_snapshot,
        "model_profile_database": model_profile_database,
        "model_check": model_check,
        "token_audit_summary": token_audit_summary,
        "model_identity_summary": model_identity_summary,
        "postprocess_inference": postprocess,
        "resolution_correspondence": resolution_correspondence,
        "report_dir": str(report_dir),
    }
    separate_image_observations(summary, results)
    summarize_observations(summary, results)
    if matrix_reference_candidate:
        summary.update({key: matrix_reference_candidate[key] for key in
                        ("model", "api_form", "source_id", "endpoint", "route_profile", "execution_mode", "profile_id", "interface_id")})
        diagnostic = diagnostic_summary(results, len(cases))
        summary.update(pass_count=0, certified=False, certified_route_contract_pass=False,
                       compatibility_pass=False, execution_mode="reference_observation",
                       reference_candidate=matrix_reference_candidate, diagnostic_summary=diagnostic,
                       diagnostic_pass=diagnostic["complete"], failure_count=diagnostic["unresolved_count"],
                       compatibility_failure_count=0, compatibility_failed_cases=[],
                       failed_cases=[item.get("case") for item in results if item.get("diagnostic_pass") is not True],
                       **request_budget)
        summary["pass"] = False
    summary["workflow_status"] = request_budget["workflow_status"]
    if request_budget["workflow_status"] != "passed":
        summary["pass"] = False
        summary["overall_pass"] = False
    _write_json(report_dir / "summary.json", summary)
    from lib.test_runner.adapters.image import register_image_execution
    register_image_execution(report_dir, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    completed = summary.get("diagnostic_pass") and not summary.get("budget_exhausted") if matrix_reference_candidate else summary["pass"]
    return 0 if completed else 1


def run_case(
    session: requests.Session,
    endpoint: str,
    model: str,
    prompt: str,
    case: ImageTestCase,
    *,
    timeout: int,
    images_dir: Path,
    visual_forensics: bool,
    credential: ProviderCredential | None = None,
    transport: str = "images-generations",
    auth_mode: str = "bearer",
    config: dict[str, Any] | None = None,
    provider: str | None = None,
    provider_cfg: dict[str, Any] | None = None,
    api_version: str | None = None,
    reference_model: str | None = None,
    reference_source: str | None = None,
    image_fetcher: Callable[[str, int], bytes] | None = None,
    input_counter: Callable[..., dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    effective_model = case.model_override or model
    if case.metadata.get("gpt_image_25") or case.metadata.get("image_reference_candidate"):
        prompt = str(case.metadata.get("prompt") or prompt)
    if transport == "images-edits" and (
        not is_gpt_image_25(effective_model) or case.metadata.get("operation") != "edit"
    ):
        raise ValueError("images-edits requires an authored GPT Image 2.5 edit case")
    if transport in GEMINI_IMAGE_TRANSPORTS:
        api_version = resolve_gemini_api_version(endpoint, api_version) or "v1"
    body = _request_body(case, model, prompt, transport)
    _validate_image_request_input(body, prompt, transport, effective_model)
    request_endpoint = (
        normalize_image_endpoint(
            endpoint,
            transport,
            api_version=api_version,
            model=effective_model,
        )
        if transport in GEMINI_IMAGE_TRANSPORTS
        else endpoint
    )
    require_ai_studio_v1beta_url(request_endpoint)
    status_code: int | None = None
    latency_ms: float | None = None
    usage: dict[str, Any] = {}
    error: dict[str, Any] | str | None = None
    images: list[ImageInfo] = []
    artifacts: list[str] = []
    response_keys: list[str] = []
    response_headers: dict[str, str] = {}
    revised_prompt_present = False
    payload: dict[str, Any] = {}
    stream_metadata: dict[str, Any] = {}
    input_fixtures = fixture_manifest(case) if transport == "images-edits" else []
    multipart_integrity = True
    request_started_at: str | None = None
    request_network_failure = False
    request_body_snapshot = copy.deepcopy(body)

    try:
        started = time.perf_counter()
        headers = (
            credential.auth_headers(url=request_endpoint, auth_mode=auth_mode)
            if credential
            else {}
        )
        if transport == "images-edits":
            # Let requests supply the actual multipart boundary, including when
            # a caller supplied a session with a JSON default header.
            headers["Content-Type"] = None
        request_started_at = datetime.now(UTC).isoformat()
        request_options: dict[str, Any] = {"json": body}
        if transport == "images-edits":
            request_options = {
                "data": {key: str(value).lower() if isinstance(value, bool) else str(value)
                         for key, value in body.items()},
                "files": edit_fixtures(case),
            }
        if body.get("stream") is True:
            request_options["stream"] = True
        wire_snapshot = copy.deepcopy(request_options)
        response = session.post(
            request_endpoint,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
            **request_options,
        )
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        multipart_integrity = request_options == wire_snapshot
        status_code = response.status_code
        response_headers = _safe_headers(response.headers)
        if body.get("stream") is True and 200 <= status_code <= 299:
            try:
                payload, stream_metadata = decode_image_stream(response, case)
            except (ValueError, TypeError) as exc:
                stream_metadata = {"failures": ["image_stream_parse_failed"], "error": str(exc)}
                payload = {}
            finally:
                response.close()
        else:
            payload = _safe_json(response)
        response_keys = sorted(payload) if isinstance(payload, dict) else []
        usage_key = (
            "usageMetadata" if transport == "gemini-generate-content" else "usage"
        )
        usage = (
            payload.get(usage_key)
            if isinstance(payload.get(usage_key), dict)
            else {}
        )

        if 200 <= response.status_code <= 299:
            data = _response_image_items(payload, transport)
            if not data:
                error = {
                    "chat-completions": "chat response contains no supported image data",
                    "gemini-interactions": (
                        "Gemini interaction contains no image content in model-output steps"
                    ),
                    "gemini-generate-content": (
                        "Gemini GenerateContent response contains no inline image data"
                    ),
                }.get(transport, "response.data is not a non-empty list")
            else:
                for image_index, item in enumerate(data):
                    if not isinstance(item, dict):
                        error = f"response.data[{image_index}] is not an object"
                        continue
                    revised_prompt_present = revised_prompt_present or bool(item.get("revised_prompt"))
                    try:
                        raw, delivery = (_image_bytes(item, timeout, fetcher=image_fetcher)
                                         if image_fetcher is not None else _image_bytes(item, timeout))
                        info = inspect_image_bytes(raw, visual_forensics=visual_forensics)
                        extension = IMAGE_EXTENSIONS.get(info.format, ".bin")
                        artifact = images_dir / f"{case.name}_{image_index + 1}{extension}"
                        artifact.write_bytes(raw)
                        images.append(info)
                        artifacts.append(str(artifact.relative_to(images_dir.parent)))
                    except Exception as exc:
                        error = (
                            f"image_decode_failed:index={image_index}:"
                            f"{exc.__class__.__name__}:{exc}"
                        )
                    else:
                        if delivery == "url":
                            response_headers["image_delivery"] = "url_without_forwarded_authorization"
        else:
            error = _error_payload(payload, response.text)
    except requests.RequestException as exc:
        error = f"{exc.__class__.__name__}: {exc}"
        request_network_failure = True

    evaluator = evaluate_matrix_observation if case.metadata.get("image_reference_candidate") is True else evaluate_case
    result = evaluator(
        case,
        status_code=status_code,
        images=images,
        usage=usage,
        latency_ms=latency_ms,
        error=error,
    )
    if request_network_failure:
        result["request_network_failure"] = True
    result = observation_outcome(case, result, images)
    rejection_attribution = result.get("parameter_rejection_attribution") or _lite_image_rejection_attribution(case, error)
    if case.metadata.get("gpt_image_25") and case.expected_outcome != "observation":
        rejection_attribution = gpt_image_25_rejection_attribution(case, error)
    if (
        result.get("status") == "expected_rejection"
        and rejection_attribution["required"]
        and rejection_attribution["matched"] is not True
    ):
        result["pass"] = False
        result["status"] = "fail"
        result["verification_level"] = "none"
        result["failures"] = [
            *list(result.get("failures") or []),
            "parameter_rejection_not_attributed",
        ]
    interaction_id_present = (
        transport == "gemini-interactions"
        and isinstance(payload.get("id"), str)
        and bool(str(payload.get("id") or "").strip())
    )
    interaction_status = (
        str(payload.get("status") or "")
        if transport == "gemini-interactions"
        else None
    )
    generate_content_finish_reasons = (
        [
            str(candidate.get("finishReason") or "")
            for candidate in payload.get("candidates") or []
            if isinstance(candidate, dict)
        ]
        if transport == "gemini-generate-content"
        else []
    )
    protocol_failures: list[str] = list(stream_metadata.get("failures") or [])
    parameter_evidence, parameter_failures = response_parameter_evidence(case, payload, images)
    if isinstance(status_code, int) and 200 <= status_code <= 299:
        protocol_failures.extend(parameter_failures)
    request_input_integrity = {
        "status": "pass" if body == request_body_snapshot and multipart_integrity else "fail",
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "source": "selected_case_prompt",
    }
    if request_input_integrity["status"] != "pass":
        protocol_failures.append("image_request_mutated_during_send")
    if (
        transport == "gemini-interactions"
        and isinstance(status_code, int)
        and 200 <= status_code <= 299
        and not interaction_id_present
    ):
        protocol_failures.append("interaction_id_missing")
    if (
        transport == "gemini-interactions"
        and isinstance(status_code, int)
        and 200 <= status_code <= 299
        and interaction_status != "completed"
    ):
        protocol_failures.append("interaction_status_not_completed")
    if (
        transport == "gemini-generate-content"
        and isinstance(status_code, int)
        and 200 <= status_code <= 299
        and (
            not generate_content_finish_reasons
            or any(reason != "STOP" for reason in generate_content_finish_reasons)
            or len(generate_content_finish_reasons) != len(payload.get("candidates") or [])
        )
    ):
        protocol_failures.append("generate_content_finish_reason_not_stop")
    if protocol_failures:
        result["pass"] = False
        result["status"] = "fail"
        result["verification_level"] = "protocol_contract_failed"
        result["failures"] = [
            *list(result.get("failures") or []),
            *protocol_failures,
        ]
    identity_payload = payload
    if transport == "gemini-generate-content":
        returned_model = payload.get("modelVersion")
        if isinstance(returned_model, str) and returned_model.startswith("models/"):
            identity_payload = {
                **payload,
                "modelVersion": returned_model.removeprefix("models/"),
            }
    usage_required = bool(
        (
            isinstance(status_code, int)
            and not isinstance(status_code, bool)
            and 200 <= status_code <= 299
        )
        or usage
        or _response_image_items(payload, transport)
    )
    audit_transport = {
        "gemini-interactions": "gemini_interactions",
        "gemini-generate-content": "gemini_generate_content",
        "chat-completions": "chat_completions",
    }.get(transport, "image_generation")
    audit_request_snapshot = copy.deepcopy(request_body_snapshot)
    if input_fixtures:
        audit_request_snapshot["image"] = copy.deepcopy(input_fixtures)
    token_audit = _audit_image_usage_safely(
        audit_request_snapshot,
        payload,
        usage,
        config or {},
        provider=provider,
        model=effective_model,
        transport=(
            "gemini_interactions"
            if transport == "gemini-interactions"
            else "gemini_generate_content"
            if transport == "gemini-generate-content"
            else "chat_completions"
            if transport == "chat-completions"
            else "image_generation"
        ),
        usage_required=usage_required,
        independent_input_count=(input_counter or _count_image_input_safely)(
            config or {}, provider, effective_model, request_body_snapshot, audit_transport,
        ) if usage_required and transport != "images-edits" else None,
        image_output_expectation=image_output_token_expectation(
            reference_model if reference_model and effective_model == model else effective_model,
            token_expectation_request(request_body_snapshot, payload)
            if case.metadata.get("gpt_image_25") else request_body_snapshot,
            [info.public() for info in images],
            reference_source=reference_source,
            observed_partial_images=(
                len(stream_metadata.get("partial_image_indices") or [])
                if body.get("stream") is True and not stream_metadata.get("failures") else None
            ),
        ),
    )
    token_validation_pass = bool(token_audit.get("validation_pass", False))
    result.update(
        {
            "compatibility_status": result.get("status"),
            "compatibility_pass": bool(result.get("pass")),
            "token_validation_status": token_audit.get("validation_status"),
            "token_validation_pass": token_validation_pass,
            "token_validation_failures": list(
                token_audit.get("validation_failures") or []
            ),
            "overall_status": (
                result.get("status")
                if not result.get("pass")
                else "pass"
                if token_validation_pass
                else "token_validation_failed"
            ),
            "overall_pass": bool(result.get("pass")) and token_validation_pass,
            "model": effective_model,
            "api_form": api_form_for_transport(transport, modality="image"),
            "api_version": (
                api_version if transport in GEMINI_IMAGE_TRANSPORTS else None
            ),
            "transport": transport,
            "effective_request_parameters": _effective_request_parameters(
                body,
                transport,
            ),
            "parameter_rejection_attribution": rejection_attribution,
            "interaction_id_present": interaction_id_present,
            "interaction_status": interaction_status,
            "generate_content_finish_reasons": generate_content_finish_reasons,
            "response_keys": response_keys,
            "response_headers": response_headers,
            "revised_prompt_present": revised_prompt_present,
            "request_input_integrity": request_input_integrity,
            **({"response_parameter_evidence": parameter_evidence} if parameter_evidence else {}),
            **({"input_fixtures": input_fixtures} if input_fixtures else {}),
            **({"image_stream": stream_metadata} if body.get("stream") is True else {}),
            **({"semantic_validation": {"status": "manual_review_required",
                                        "criteria": case.metadata["semantic_review"], "pass": None}}
               if case.metadata.get("semantic_review") else {}),
            "artifacts": artifacts,
            "token_audit": token_audit,
            "model_identity_audit": combine_model_identity_audits(
                [
                    audit_model_identity(
                        requested_model=effective_model,
                        result=SimpleNamespace(
                            response_json=identity_payload,
                            headers=response_headers,
                        ),
                        transport=(
                            "gemini_interactions"
                            if transport == "gemini-interactions"
                            else "gemini_generate_content"
                            if transport == "gemini-generate-content"
                            else "chat_completions"
                            if transport == "chat-completions"
                            else "image_generation"
                        ),
                        provider_cfg=provider_cfg
                        or {"backend": "unknown", "models": {}},
                        exchange="initial",
                        request_endpoint=urlparse(request_endpoint).path,
                    )
                ]
            ),
        }
    )
    if (error and case.expected_outcome == "success" and not result["failures"]
            and case.metadata.get("image_reference_candidate") is not True):
        result["pass"] = False
        result["status"] = "fail"
        result["verification_level"] = "none"
        result["failures"] = ["response_or_image_decode_error"]
        result["compatibility_pass"] = False
        result["compatibility_status"] = "fail"
        result["overall_pass"] = False
        result["overall_status"] = "fail"
    if case.metadata.get("semantic_review"):
        input_hashes = {item["sha256"] for item in input_fixtures if item["field"] == "image[]"}
        unchanged = any(info.sha256 in input_hashes for info in images)
        result["semantic_validation"].update({
            "status": "failed_unchanged_input" if unchanged else "manual_review_required",
            "unchanged_input_detected": unchanged,
            "pass": False if unchanged else None,
        })
        result["overall_pass"] = False
        if result.get("compatibility_pass"):
            result["overall_status"] = "semantic_review_pending" if not unchanged else "semantic_validation_failed"
        result["pass"] = False
        result["verification_level"] = "semantic_review_required"
        result["failures"] = list(result.get("failures") or []) + [
            "edit_input_unchanged" if unchanged else "semantic_review_pending"
        ]
    result["overall_failures"] = [
        *list(result.get("failures") or []),
        *(
            ["token_validation_failed"]
            if result.get("token_validation_pass") is False
            else []
        ),
    ]
    if case.metadata.get("gpt_image_25") and case.expected_outcome == "observation":
        diagnostic_pass = result.get("diagnostic_pass") is True and not protocol_failures and token_validation_pass
        result.update({"diagnostic_pass": diagnostic_pass,
                       "pass": False, "compatibility_pass": False, "overall_pass": False,
                       "compatibility_status": "not_certified", "certified_route_contract_pass": False})
        if not diagnostic_pass:
            result["status"] = "unresolved"
        result["overall_status"] = result["status"]
    if case.metadata.get("banana_gc_candidate") is True:
        identity = result.get("model_identity_audit") or {}
        result["diagnostic_pass"] = bool(result.get("diagnostic_pass")) and (
            not protocol_failures and token_validation_pass
            and (not usage_required or (identity.get("status") == "match" and bool(payload.get("modelVersion"))))
        )
        if result["diagnostic_pass"] is not True:
            result["status"] = "unresolved"
        result.update({
            "pass": False, "compatibility_pass": False, "overall_pass": False,
            "certified_route_contract_pass": False,
            "compatibility_status": "not_certified", "overall_status": result["status"],
        })
    if case.metadata.get("banana_gc_exact") is True:
        result["reference_api_version"] = "v1beta"
        if not is_ai_studio_origin(request_endpoint) or case.metadata.get("comparison_scope") == "adapter_only":
            result.update({"comparison_scope": "adapter_only", "certification_scope": "adapter_only",
                           "certified_route_contract_pass": False})
    if case.metadata.get("image_reference_candidate") is True:
        finalize_matrix_observation(case, result, payload=payload, protocol_failures=protocol_failures)
    result.update(image_exchange_evidence(
        endpoint=request_endpoint, request_body=audit_request_snapshot,
        response_payload=payload, request_started_at=request_started_at,
    ))
    return redact_secrets(result)


def normalize_image_endpoint(
    value: str,
    transport: str,
    *,
    api_version: str | None = None,
    model: str | None = None,
) -> str:
    if transport not in IMAGE_TRANSPORTS:
        raise ValueError(f"Unsupported image transport: {transport!r}")
    value = str(value or "").strip().rstrip("/")
    if not value:
        raise ValueError("Set --base-url or IMAGE_TEST_BASE_URL.")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid image provider URL: {value!r}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Image provider URL must not contain user information, a query, or a fragment.")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Remote image provider URLs must use HTTPS.")
    openai_suffixes = {
        "images-generations": "/images/generations",
        "images-edits": "/images/edits",
        "openai-responses-image": "/responses",
        "chat-completions": "/chat/completions",
    }
    gemini_routes = {
        "gemini-interactions": re.search(
            r"/(v1(?:beta)?)/interactions$", value
        ),
        "gemini-generate-content": re.search(
            r"/(v1(?:beta)?)/models/([^/]+):generateContent$", value
        ),
    }
    if transport in GEMINI_IMAGE_TRANSPORTS:
        api_version = resolve_gemini_api_version(value, api_version) or "v1"
        if is_ai_studio_origin(value) and re.search(r"/v1(?:/|$)", parsed.path):
            raise ValueError("AI Studio v1 endpoints cannot execute; select v1beta explicitly.")
        if api_version not in GEMINI_API_VERSIONS:
            raise ValueError(
                f"Unsupported Gemini API version {api_version!r}; "
                f"choose one of {GEMINI_API_VERSIONS}."
            )
        if any(value.endswith(suffix) for suffix in openai_suffixes.values()):
            raise ValueError(
                f"The supplied endpoint does not match --transport {transport}."
            )
        for candidate, match in gemini_routes.items():
            if match is not None and candidate != transport:
                raise ValueError(
                    f"The supplied endpoint does not match --transport {transport}."
                )
        route_match = gemini_routes[transport]
        if route_match is not None:
            base = value[: route_match.start()]
            endpoint_model = (
                route_match.group(2)
                if transport == "gemini-generate-content"
                else None
            )
        else:
            version_base = re.search(r"/(v1(?:beta)?)$", value)
            base = value[: version_base.start()] if version_base else value
            endpoint_model = None
        if transport == "gemini-interactions":
            return f"{base}/{api_version}/interactions"
        selected_model = str(model or endpoint_model or "").removeprefix("models/")
        if not selected_model:
            raise ValueError(
                "Gemini GenerateContent endpoint normalization requires a model."
            )
        return (
            f"{base}/{api_version}/models/"
            f"{quote(selected_model, safe='')}:generateContent"
        )

    suffix = openai_suffixes[transport]
    mismatched = [
        candidate
        for candidate in openai_suffixes.values()
        if candidate != suffix and value.endswith(candidate)
    ]
    if mismatched or any(match is not None for match in gemini_routes.values()):
        raise ValueError(
            f"The supplied endpoint does not match --transport {transport}."
        )
    if value.endswith(suffix):
        return value
    if value.endswith("/v1"):
        return value + suffix
    return value + "/v1" + suffix


def normalize_image_generation_endpoint(value: str) -> str:
    return normalize_image_endpoint(value, "images-generations")


def validate_configured_image_endpoint(
    config: dict[str, Any],
    *,
    provider: str,
    endpoint: str,
    transport: str,
    model: str | None = None,
    api_version: str = "v1",
) -> None:
    """Bind a credential-bearing image endpoint to its configured interface."""
    configured_endpoint = normalize_image_endpoint(
        get_image_endpoint(config, provider, transport),
        transport,
        api_version=api_version,
        model=model,
    )
    if _image_endpoint_contract(endpoint) != _image_endpoint_contract(configured_endpoint):
        raise ValueError(
            "Refusing image endpoint that does not match the configured provider "
            f"interface for transport {transport!r}."
        )
    provider_cfg = get_provider_config(config, provider)
    if str(provider_cfg.get("name") or provider) == "gemini":
        _validate_official_gemini_endpoint(
            endpoint,
            transport=transport,
            model=str(model or ""),
            api_version=api_version,
        )


def _image_endpoint_contract(value: str) -> tuple[str, str, int, str]:
    parsed = urlparse(str(value))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid image provider URL: {value!r}")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Image provider URL must not contain user information, parameters, "
            "a query, or a fragment."
        )
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"Invalid image provider URL: {value!r}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Invalid image provider URL port: {value!r}") from exc
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), hostname.lower(), effective_port, parsed.path


def _validate_official_gemini_endpoint(
    endpoint: str,
    *,
    transport: str,
    model: str,
    api_version: str,
) -> None:
    require_ai_studio_v1beta_url(endpoint)
    resolve_gemini_api_version(endpoint, api_version)
    if transport not in GEMINI_IMAGE_TRANSPORTS:
        raise ValueError(
            "The official Gemini image provider requires a native Gemini transport."
        )
    if api_version not in GEMINI_API_VERSIONS:
        raise ValueError(
            f"Unsupported Gemini API version {api_version!r}; "
            f"choose one of {GEMINI_API_VERSIONS}."
        )
    scheme, hostname, port, path = _image_endpoint_contract(endpoint)
    selected_model = str(model).removeprefix("models/")
    expected_path = (
        f"/{api_version}/interactions"
        if transport == "gemini-interactions"
        else (
            f"/{api_version}/models/"
            f"{quote(selected_model, safe='')}:generateContent"
        )
    )
    if (
        scheme != "https"
        or hostname != "generativelanguage.googleapis.com"
        or port != 443
        or path != expected_path
    ):
        raise ValueError(
            "Refusing a non-official Gemini image endpoint; expected HTTPS on "
            "generativelanguage.googleapis.com:443 with path "
            f"{expected_path!r}."
        )


def models_endpoint(
    image_endpoint: str,
    transport: str = "images-generations",
    family: str | None = None,
) -> str:
    if transport in GEMINI_IMAGE_TRANSPORTS:
        route = (
            r"/(v1(?:beta)?)/interactions$"
            if transport == "gemini-interactions"
            else r"/(v1(?:beta)?)/models/[^/]+:generateContent$"
        )
        match = re.search(route, image_endpoint)
        if match is None:
            raise ValueError(
                f"Unexpected {transport} image endpoint: {image_endpoint!r}"
            )
        return f"{image_endpoint[: match.start()]}/{match.group(1)}/models"
    suffix = {
        "images-generations": "/images/generations",
        "images-edits": "/images/edits",
        "openai-responses-image": "/responses",
        "chat-completions": "/chat/completions",
    }.get(transport)
    if suffix is None or not image_endpoint.endswith(suffix):
        raise ValueError(f"Unexpected {transport} image endpoint: {image_endpoint!r}")
    route = "/image-generation-models" if family == "grok-imagine" else "/models"
    return image_endpoint[: -len(suffix)] + route


def _request_body(
    case: ImageTestCase,
    model: str,
    prompt: str,
    transport: str,
) -> dict[str, Any]:
    if transport in {"images-generations", "images-edits"}:
        return case.request_body(model, prompt)
    if transport == "gemini-interactions":
        response_format = case.parameters.get("response_format")
        if not isinstance(response_format, dict):
            raise ValueError(
                "Gemini Interactions image cases require response_format."
            )
        body: dict[str, Any] = {
            "model": case.model_override or model,
            "input": [{"type": "text", "text": prompt}],
            "response_format": copy.deepcopy(response_format),
        }
        if case.metadata.get("stateless") is True:
            body["store"] = False
        thinking_level = case.metadata.get("thinking_level")
        if isinstance(thinking_level, str) and thinking_level:
            body["generation_config"] = {"thinking_level": thinking_level}
        return body
    if transport == "gemini-generate-content":
        generation_config = case.parameters.get("generationConfig")
        if not isinstance(generation_config, dict):
            raise ValueError(
                "Gemini GenerateContent image cases require generationConfig."
            )
        generation_config = copy.deepcopy(generation_config)
        thinking_level = case.metadata.get("thinking_level")
        if isinstance(thinking_level, str) and thinking_level:
            generation_config["thinkingConfig"] = {
                "thinkingLevel": thinking_level,
            }
        return {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
    if transport != "chat-completions":
        raise ValueError(f"Unsupported image transport: {transport!r}")
    extra_body = case.parameters.get("extra_body")
    google = extra_body.get("google") if isinstance(extra_body, dict) else None
    image_config = google.get("image_config") if isinstance(google, dict) else None
    if not isinstance(image_config, dict):
        raise ValueError(
            "Chat image cases require extra_body.google.image_config."
        )
    return {
        "model": case.model_override or model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "extra_body": {"google": {"image_config": image_config}},
    }


def _effective_request_parameters(
    body: dict[str, Any],
    transport: str,
) -> dict[str, Any]:
    fields = {
        "images-generations": (
            "model",
            "n",
            "quality",
            "size",
            "output_format",
            "background",
            "moderation",
            "output_compression",
        ),
        "chat-completions": ("model", "stream", "extra_body"),
        "gemini-interactions": (
            "model",
            "response_format",
            "store",
            "generation_config",
        ),
        "gemini-generate-content": ("generationConfig",),
    }.get(transport, ())
    if transport in {"images-generations", "images-edits"}:
        fields = ("model", "n", "quality", "size", "resolution", "aspect_ratio", "output_format", "background", "user", "response_format",
                  "moderation", "output_compression", "stream", "partial_images", "input_fidelity")
    return {
        field: copy.deepcopy(body[field])
        for field in fields
        if field in body
    }


def _lite_image_rejection_attribution(
    case: ImageTestCase,
    error: dict[str, Any] | str | None,
) -> dict[str, Any]:
    metadata = case.metadata or {}
    if (
        metadata.get("profile_driven") is not True
        or metadata.get("model_scope") != "gemini-3.1-flash-lite-image"
        or case.expected_outcome != "rejection"
    ):
        return {"required": False, "matched": None, "target": None}
    aspect_ratio = str(metadata.get("aspect_ratio") or "1:1")
    requested_resolution = str(metadata.get("requested_resolution") or "1K")
    if aspect_ratio != "1:1":
        target = "aspect_ratio"
        markers = ("aspect_ratio", "aspect ratio", "aspectratio")
    elif requested_resolution != "1K":
        target = "image_size"
        markers = ("image_size", "image size", "imagesize")
    else:
        return {"required": False, "matched": None, "target": None}
    serialized_error = json.dumps(error, ensure_ascii=False, sort_keys=True).casefold()
    return {
        "required": True,
        "matched": any(marker in serialized_error for marker in markers),
        "target": target,
    }


def _response_image_items(
    payload: dict[str, Any],
    transport: str,
) -> list[dict[str, Any]]:
    if transport in {"images-generations", "images-edits"}:
        data = payload.get("data")
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    if transport == "gemini-interactions":
        return _gemini_interaction_image_items(payload)
    if transport == "gemini-generate-content":
        return _gemini_generate_content_image_items(payload)

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return []
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return []

    image_items: list[dict[str, Any]] = []
    images = message.get("images")
    if isinstance(images, list):
        for item in images:
            if not isinstance(item, dict):
                continue
            image_url = item.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if isinstance(image_url, str) and image_url:
                image_items.append({"url": image_url})

    content = message.get("content")
    if isinstance(content, str):
        for match in re.finditer(
            r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+",
            content,
        ):
            image_items.append({"url": match.group(0)})
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if isinstance(image_url, str) and image_url:
                image_items.append({"url": image_url})

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in image_items:
        url = str(item["url"])
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        if digest not in seen:
            seen.add(digest)
            unique.append(item)
    return unique


def _gemini_generate_content_image_items(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Extract and deduplicate native GenerateContent inlineData images."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData")
            if not isinstance(inline, dict):
                continue
            data = inline.get("data")
            if not isinstance(data, str) or not data:
                continue
            digest = hashlib.sha256(data.encode("ascii", errors="ignore")).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            item: dict[str, Any] = {"b64_json": data}
            mime_type = inline.get("mimeType")
            if isinstance(mime_type, str) and mime_type:
                item["mime_type"] = mime_type
            items.append(item)
    return items


def _gemini_interaction_image_items(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Extract inline or URI images from native Interactions model-output steps."""
    candidates: list[dict[str, Any]] = []
    convenience = payload.get("output_image")
    if isinstance(convenience, dict):
        candidates.append(convenience)
    for step in payload.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        content = step.get("content")
        if not isinstance(content, list):
            continue
        candidates.extend(
            item
            for item in content
            if isinstance(item, dict) and item.get("type") == "image"
        )

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        data = candidate.get("data")
        uri = candidate.get("uri")
        item: dict[str, Any] | None = None
        identity: str | None = None
        if isinstance(data, str) and data:
            item = {"b64_json": data}
            identity = hashlib.sha256(data.encode("ascii", errors="ignore")).hexdigest()
        elif isinstance(uri, str) and uri:
            item = {"url": uri}
            identity = hashlib.sha256(uri.encode("utf-8")).hexdigest()
        if item is not None and identity not in seen:
            seen.add(str(identity))
            items.append(item)
    return items


def _image_bytes(item: dict[str, Any], timeout: int, *, fetcher: Callable[[str, int], bytes] | None = None) -> tuple[bytes, str]:
    b64_json = item.get("b64_json")
    if isinstance(b64_json, str) and b64_json:
        decoded = _decode_bounded_image_base64(b64_json, "b64_json")
        validate_image_payload(decoded)
        return decoded, "b64_json"
    url = item.get("url")
    if isinstance(url, str) and url:
        if url.startswith("data:image/"):
            if len(url) > MAX_IMAGE_DATA_URL_HEADER_CHARS + MAX_IMAGE_BASE64_CHARS:
                raise ValueError("image data URL exceeds the encoded image byte limit")
            header, separator, encoded = url.partition(";base64,")
            if (
                not separator
                or len(header) > MAX_IMAGE_DATA_URL_HEADER_CHARS
                or re.fullmatch(r"data:image/[A-Za-z0-9.+-]+", header) is None
            ):
                raise ValueError("invalid image data URL")
            decoded = _decode_bounded_image_base64(encoded, "image data URL")
            validate_image_payload(decoded)
            return decoded, "data_url"
        # Remote delivery uses an isolated unauthenticated session with SSRF,
        # DNS-rebinding, redirect, MIME, size, and decoder resource guards.
        return (fetcher(url, timeout) if fetcher is not None else fetch_remote_image(url, timeout).data), "url"
    raise ValueError("image item has neither b64_json nor url")


def _decode_bounded_image_base64(encoded: str, label: str) -> bytes:
    if len(encoded) > MAX_IMAGE_BASE64_CHARS:
        raise ValueError(f"{label} exceeds the encoded image byte limit")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if len(decoded) > MAX_IMAGE_PAYLOAD_BYTES:
        raise ValueError(f"{label} exceeds the decoded image byte limit")
    return decoded


def _list_models(
    session: requests.Session,
    endpoint: str,
    timeout: int,
    credential: ProviderCredential,
    auth_mode: str = "bearer",
) -> dict[str, Any]:
    try:
        started = time.perf_counter()
        response = session.get(
            endpoint,
            headers=credential.auth_headers(url=endpoint, auth_mode=auth_mode),
            timeout=min(timeout, 60),
            allow_redirects=False,
        )
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        payload = _safe_json(response)
        raw_models = payload.get("data")
        if not isinstance(raw_models, list):
            raw_models = payload.get("models")
        model_ids = []
        if isinstance(raw_models, list):
            for item in raw_models:
                if not isinstance(item, dict):
                    continue
                value = item.get("id") or item.get("name")
                if not value:
                    continue
                model_id = str(value)
                if model_id.startswith("models/"):
                    model_id = model_id[len("models/") :]
                model_ids.append(model_id)
        return {
            "status_code": response.status_code,
            "latency_ms": latency_ms,
            "model_count": len(model_ids),
            "model_ids": model_ids,
            "response_headers": _safe_headers(response.headers),
        }
    except requests.RequestException as exc:
        return {
            "status_code": None,
            "error": f"{exc.__class__.__name__}: {exc}",
            "model_count": 0,
            "model_ids": [],
        }


def _safe_json(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_headers(headers: Any) -> dict[str, str]:
    return {
        str(name).lower(): str(value)
        for name, value in headers.items()
        if str(name).lower() in SAFE_RESPONSE_HEADERS
    }


def _error_payload(payload: dict[str, Any], raw_text: str) -> dict[str, Any] | str:
    error = payload.get("error")
    if isinstance(error, dict):
        return {
            "type": error.get("type"),
            "code": error.get("code"),
            "param": error.get("param"),
            "message": str(error.get("message") or "")[:1000],
        }
    return " ".join(str(raw_text or "").split())[:1000]


def _with_output_options(
    case: ImageTestCase,
    quality: str,
    output_format: str,
    *,
    transport: str = "images-generations",
) -> ImageTestCase:
    if (case.metadata.get("gpt_image_25") or case.metadata.get("image_reference_candidate")) and case.metadata.get("pin_parameters"):
        return case
    if transport in {
        "chat-completions",
        "gemini-interactions",
        "gemini-generate-content",
    }:
        requested_resolution = case.metadata.get("requested_resolution")
        omit_image_size = (
            transport == "gemini-generate-content"
            and case.metadata.get("banana_gc_exact") is True
            and requested_resolution is None
        )
        if not isinstance(requested_resolution, str) and not omit_image_size:
            raise ValueError(
                f"{transport} image cases require requested_resolution metadata."
            )
        aspect_ratio = str(case.metadata.get("aspect_ratio") or "1:1")
        if transport == "gemini-interactions":
            if output_format != "jpeg":
                raise ValueError(
                    "Gemini Interactions image output currently supports only jpeg."
                )
            return replace(
                case,
                parameters={
                    "response_format": {
                        "type": "image",
                        "mime_type": "image/jpeg",
                        "aspect_ratio": aspect_ratio,
                        "image_size": requested_resolution,
                    },
                },
                expected_format=(
                    "JPEG"
                    if case.expected_outcome in {"success", "observation"}
                    else None
                ),
            )
        if transport == "gemini-generate-content":
            return replace(
                case,
                parameters={
                    "generationConfig": {
                        "responseModalities": ["TEXT", "IMAGE"],
                        "imageConfig": {
                            "aspectRatio": aspect_ratio,
                            **({} if omit_image_size else {"imageSize": requested_resolution}),
                        },
                    }
                },
                expected_format=None,
            )
        return replace(
            case,
            parameters={
                "extra_body": {
                    "google": {
                        "image_config": {
                            "aspect_ratio": aspect_ratio,
                            "image_size": requested_resolution,
                        }
                    }
                },
            },
            expected_format=None,
        )
    forced_output_format = case.metadata.get("forced_output_format")
    effective_output_format = (
        str(forced_output_format)
        if forced_output_format
        else output_format
    )
    parameters = {
        **case.parameters,
        "quality": quality,
        "output_format": effective_output_format,
    }
    expected_format = {
        "png": "PNG",
        "jpeg": "JPEG",
        "webp": "WEBP",
    }[effective_output_format]
    return replace(
        case,
        parameters=parameters,
        expected_format=(
            expected_format
            if case.expected_outcome in {"success", "observation"}
            else None
        ),
    )


def _select_cases(cases: list[ImageTestCase], selected: list[str]) -> list[ImageTestCase]:
    if not selected:
        return cases
    by_name = {case.name: case for case in cases}
    missing = [name for name in selected if name not in by_name]
    if missing:
        raise SystemExit(f"Unknown image test case(s): {', '.join(missing)}")
    return [by_name[name] for name in selected]


def _case_size_label(case: ImageTestCase) -> str | None:
    aspect_ratio = case.parameters.get("aspect_ratio")
    resolution = case.parameters.get("resolution")
    if aspect_ratio is not None or resolution is not None:
        return f"{resolution or '?'} / {aspect_ratio or '?'}"
    direct = case.parameters.get("size")
    if direct is not None:
        return str(direct)
    extra_body = case.parameters.get("extra_body")
    google = extra_body.get("google") if isinstance(extra_body, dict) else None
    image_config = google.get("image_config") if isinstance(google, dict) else None
    value = image_config.get("image_size") if isinstance(image_config, dict) else None
    if value is not None:
        return str(value)
    response_format = case.parameters.get("response_format")
    if isinstance(response_format, dict):
        size = response_format.get("image_size")
        ratio = response_format.get("aspect_ratio")
        if size is not None or ratio is not None:
            return f"{size or '?'} / {ratio or '?'}"
    return None


def _report_dir(value: str | None, model: str) -> Path:
    if value:
        target = Path(value)
        return target if target.is_absolute() else PROJECT_ROOT / target
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", model).strip("_") or "image-model"
    return default_reports_root() / "image_param" / f"{timestamp}-{slug}"


def _write_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(redact_secrets(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run reusable GPT Image 2, Banana, or Grok Imagine parameter tests. "
            "Postprocess results are heuristic and never reported as confirmed."
        )
    )
    parser.add_argument(
        "--base-url",
        help="Provider root, /v1 base, or full endpoint selected by --transport.",
    )
    parser.add_argument(
        "--provider",
        help="Configured provider name used for token-counter and model-identity aliases.",
    )
    parser.add_argument("--api-key-env", default="IMAGE_TEST_API_KEY")
    parser.add_argument("--credential-provider", help="Use the configured credential of the same selected image provider.")
    parser.add_argument(
        "--api-key-stdin",
        action="store_true",
        help="Read the API key from an interactive hidden prompt instead of the environment.",
    )
    parser.add_argument(
        "--family",
        choices=("gpt-image-2", "banana", "grok-imagine"),
        default="gpt-image-2",
        help="Select the parameter contract and case matrix.",
    )
    parser.add_argument(
        "--transport",
        choices=IMAGE_TRANSPORTS,
        default="images-generations",
        help=(
            "Image API shape. chat-completions is for Banana providers that return "
            "images from choices[].message and accept extra_body.google.image_config; "
            "gemini-interactions and gemini-generate-content are Google's native APIs."
        ),
    )
    parser.add_argument(
        "--api-form",
        choices=(
            "openai_images_generations",
            "openai_images_edits",
            "openai_responses",
            "openai_chat_completions",
            "gemini_interactions",
            "gemini_generate_content",
        ),
        help=(
            "Public image API form selected within --route-profile. The internal "
            "--transport must map to the same form."
        ),
    )
    parser.add_argument(
        "--api-version",
        choices=GEMINI_API_VERSIONS,
        default=None,
        help="Native Gemini API version; AI Studio requires v1beta.",
    )
    parser.add_argument(
        "--route-profile",
        default=os.getenv("LOADTEST_ROUTE_PROFILE"),
        help="Route contract selected before the image API form.",
    )
    parser.add_argument(
        "--auth-mode",
        choices=("bearer", "google_api_key"),
        help=(
            "Credential header mode. Defaults to google_api_key for "
            "native Gemini transports and bearer for compatible image transports."
        ),
    )
    parser.add_argument(
        "--model",
        default=os.getenv("IMAGE_TEST_MODEL"),
        help=(
            "GPT/Grok model name, fixed Banana model ID, or Banana alias template containing "
            "{resolution} or {resolution_lower}. Native Gemini image APIs default to "
            "gemini-3.1-flash-image; compatible Banana defaults to "
            "nano-banana-pro-{resolution_lower}."
        ),
    )
    parser.add_argument("--reference-candidate", help="Exact official GPT/Grok image candidate JSON for diagnostic observation.")
    parser.add_argument("--max-generation-requests", type=int)
    parser.add_argument("--transient-retries", type=int, default=0)
    parser.add_argument("--retry-initial-delay", type=float, default=15)
    parser.add_argument("--suite", choices=("smoke", "resolution", "full"), default="full")
    parser.add_argument(
        "--include-2k",
        action="store_true",
        help="Acknowledge Grok Imagine 2K charges and include its 2K cases.",
    )
    parser.add_argument("--include-4k", action="store_true", help="Acknowledge the billable 4K case.")
    parser.add_argument("--no-negative", action="store_true", help="Skip family-specific invalid-parameter rejection cases.")
    parser.add_argument(
        "--no-cross-control",
        action="store_true",
        help="Skip Banana cases that intentionally conflict alias suffix and size.",
    )
    parser.add_argument("--case", action="append", default=[], help="Run only the named case; repeatable.")
    parser.add_argument(
        "--quality",
        choices=("low", "medium", "high", "xhigh", "max", "auto"),
        default="low",
        help="GPT/Banana Images option; not sent for Grok Imagine.",
    )
    parser.add_argument(
        "--output-format",
        choices=("png", "jpeg", "webp"),
        help=(
            "GPT/Banana Images option (default png). Gemini Interactions defaults "
            "to and currently accepts only jpeg; GenerateContent and Grok record "
            "the actual encoded format."
        ),
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--store-prompt", action="store_true", help="Store the prompt in plan.json; default stores only SHA-256.")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--output-dir")
    parser.add_argument("--no-visual-forensics", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print the request plan without reading a key or sending requests.")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
