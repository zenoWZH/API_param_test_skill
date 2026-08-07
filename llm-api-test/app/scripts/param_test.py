from __future__ import annotations

import json
import os
import random
import re
import statistics
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_PARAM_TEST_RUNS = 3
MAX_PARAM_TEST_RUNS = 1000

from lib.client import DeepSeekClient
from lib.credential_security import redact_secrets
from lib.config import (
    default_reports_root,
    ensure_dir,
    get_active_provider_name,
    get_model_api_form,
    get_model_api_forms,
    get_model_family,
    get_model_route_profile,
    get_provider_config,
    get_provider_interface,
    get_selected_model,
    load_config,
)
from lib.deepseek_params import (
    build_claude_tool_followup_request,
    build_native_tool_followup_request,
    build_openai_responses_tool_followup_request,
    build_request,
    build_tool_followup_request,
    extract_content,
    extract_tool_calls,
)
from lib.metrics import percentile, write_json
from lib.model_identity import (
    audit_model_identity,
    combine_model_identity_audits,
    summarize_model_identity_audits,
)
from lib.profile_validation import (
    CLAUDE_NATIVE_TOOL_PROFILES,
    NATIVE_TOOL_PROFILES,
    OPENAI_RESPONSES_TOOL_PROFILES,
    OPENAI_TOOL_PROFILES,
    validate_profile_response,
    validate_tool_followup_response,
)
from lib.param_outcome import compatibility_pass_from_statuses, map_probe_outcome
from lib.reference_specs import (
    capability_profile_snapshot,
    comparison_reference_source_for_model,
    default_reference_source_for_model,
    family_for_reference,
    get_reference_source,
    model_reference_spec_payload,
    parameter_label_for_profile,
    reference_sources_for_model,
    resolve_profile_expectation,
    test_profiles_for_reference as reference_test_profiles,
    tested_params_for_reference as reference_tested_params,
    untested_params_for_reference,
)
from lib.token_audit import (
    audit_exchange,
    combine_exchange_audits,
    flatten_token_audits,
    normalize_usage,
    summarize_token_audits,
)


_CLASSIFICATION_LABELS = {
    "json_parse": "响应不是合法 JSON object",
    "json_not_object": "响应 JSON 不是 object",
    "tool_calls_missing": "响应未返回 tool_calls",
    "native_function_call_missing": "响应未返回 Native tool/function call",
    "tool_call_malformed": "工具调用结构不完整",
    "tool_call_id_missing": "工具调用缺少 id",
    "tool_call_name_missing": "工具调用缺少函数名",
    "tool_call_unknown_function": "调用了未声明的函数",
    "tool_call_arguments_invalid": "工具调用 arguments 不是合法 JSON object",
    "stream_usage_missing": "流式响应缺少末块 usage",
    "logprobs_missing": "响应缺少 choices[].logprobs",
    "reasoning_content_missing": "Thinking 响应缺少 reasoning_content",
    "reasoning_content_unexpected": "关闭 Thinking 后仍返回 reasoning_content",
    "reasoning_context_mismatch": "响应未确认请求的 reasoning.context",
    "preserved_thinking_mismatch": "响应未使用历史轮 reasoning_content",
    "thought_summary_missing": "Gemini Native 响应缺少标记为 thought 的思考摘要",
    "http_4xx": "上游 4xx 拒绝（参数不被接受）",
    "http_5xx": "上游 5xx 错误",
    "http_429": "被限流 429",
    "request_failed": "请求失败",
    "tool_followup_failed": "tool 多轮跟进失败",
    "tool_followup_content_missing": "tool 多轮跟进未返回最终文本",
    "tool_followup_unresolved_call": "tool 多轮跟进仍返回未处理工具调用",
    "vertex_traffic_type_missing": "响应缺少 usageMetadata.trafficType（非 Vertex 指纹）",
    "vertex_service_tier_unexpected": "响应出现 AI Studio 的 usageMetadata.serviceTier（更像 AI Studio）",
    "unexpected_acceptance": "期望拒绝的参数被上游接受",
    "expected_rejection": "参数按预期被拒绝",
}

_REASONING_INPUT_PROFILES = {
    "thinking_low",
    "thinking_enabled",
    "thinking_max",
    "gemini_reasoning_minimal",
    "gemini_reasoning_low",
    "gemini_reasoning_medium",
    "gemini_reasoning_high",
    "gemini_thinking_config",
    "gemini_native_thinking_minimal",
    "gemini_native_thinking_low",
    "gemini_native_thinking_medium",
    "gemini_native_thinking_high",
    "claude_native_thinking_adaptive",
    "claude_native_effort_low",
    "claude_native_effort_medium",
    "claude_native_effort_high",
    "claude_native_effort_xhigh",
    "claude_native_effort_max",
    "glm_thinking_enabled",
    "glm_reasoning_low",
    "glm_reasoning_medium",
    "glm_reasoning_high",
    "glm_reasoning_xhigh",
    "glm_reasoning_max",
    "qwen_thinking_enabled",
    "qwen_thinking_budget",
    "kimi_k3_reasoning_low",
    "kimi_k3_reasoning_high",
    "kimi_k3_reasoning_max",
    "gpt5_chat_reasoning_low",
    "gpt5_chat_reasoning_medium",
    "gpt5_chat_reasoning_high",
    "gpt5_chat_reasoning_xhigh",
    "gpt5_chat_reasoning_max",
    "openai_responses_reasoning_low",
    "openai_responses_reasoning",
    "openai_responses_reasoning_high",
    "openai_responses_reasoning_xhigh",
    "openai_responses_reasoning_max",
    "openai_responses_reasoning_context_all_turns",
    "openai_responses_reasoning_context_current_turn",
    "openai_responses_pro_medium",
}


def _api_error_message(response_json: dict[str, Any] | None) -> str | None:
    if not isinstance(response_json, dict):
        return None
    error = response_json.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("code") or error.get("type")
        if message:
            return str(message)
    elif isinstance(error, str) and error.strip():
        return error.strip()
    message = response_json.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return None


def _human_reason(
    status: str,
    status_code: int | None,
    failure_classification: str | None,
    response_json: dict[str, Any] | None,
    raw_text: str | None,
) -> str:
    parts: list[str] = []
    if status == "incompatible":
        parts.append("参数不兼容（期望支持却被拒绝/校验失败）")
    elif status == "unexpected_acceptance":
        parts.append("意外接受（期望不支持却返回 2xx）")
    elif status == "expected_rejection":
        parts.append("按预期拒绝")
    elif status == "fail":
        parts.append("请求失败")
    if status_code is not None:
        parts.append(f"HTTP {status_code}")
    label = _CLASSIFICATION_LABELS.get(str(failure_classification)) if failure_classification else None
    if label:
        parts.append(label)
    elif failure_classification:
        parts.append(str(failure_classification))
    api_message = _api_error_message(response_json)
    if api_message:
        parts.append(api_message)
    elif not label and raw_text:
        snippet = " ".join(str(raw_text).split())
        if snippet:
            parts.append(snippet[:300])
    return " | ".join(part for part in parts if part) or "未知原因"


def _failure_detail(
    profile: str,
    status: str,
    status_code: int | None,
    failure_classification: str | None,
    response_json: dict[str, Any] | None,
    raw_text: str | None,
) -> dict[str, Any]:
    reason = _human_reason(status, status_code, failure_classification, response_json, raw_text)
    detail: dict[str, Any] = {
        "failure_reason": reason,
        "failed_check": failure_classification or "unknown",
        "failed_item": "response",
        "expected": "request succeeds and response matches the selected reference profile",
        "actual": reason,
    }

    if failure_classification == "json_parse":
        content = extract_content(response_json or {})
        detail.update(
            {
                "failed_check": "json_output.parse_content",
                "failed_item": "choices[0].message.content",
                "expected": "valid JSON object string",
                "actual": content,
            }
        )
    elif failure_classification == "json_not_object":
        content = extract_content(response_json or {})
        detail.update(
            {
                "failed_check": "json_output.object_type",
                "failed_item": "choices[0].message.content",
                "expected": "JSON value whose top-level type is object",
                "actual": content,
            }
        )
    elif failure_classification == "tool_calls_missing":
        detail.update(
            {
                "failed_check": "tool_calls.present",
                "failed_item": "choices[0].message.tool_calls",
                "expected": "non-empty tool_calls list",
                "actual": extract_tool_calls(response_json or {}),
            }
        )
    elif failure_classification == "stream_usage_missing":
        usage = (response_json or {}).get("usage")
        detail.update(
            {
                "failed_check": "stream_options.include_usage",
                "failed_item": "usage",
                "expected": "usage object present in streamed response",
                "actual": usage,
            }
        )
    elif failure_classification == "logprobs_missing":
        choices = (response_json or {}).get("choices") or []
        logprobs = choices[0].get("logprobs") if choices and isinstance(choices[0], dict) else None
        detail.update(
            {
                "failed_check": "logprobs.present",
                "failed_item": "choices[0].logprobs",
                "expected": "non-null logprobs object",
                "actual": logprobs,
            }
        )
    elif failure_classification == "vertex_traffic_type_missing":
        usage = (response_json or {}).get("usageMetadata")
        detail.update(
            {
                "failed_check": "usageMetadata.trafficType",
                "failed_item": "usageMetadata.trafficType",
                "expected": "non-empty Vertex trafficType (e.g. ON_DEMAND)",
                "actual": usage,
            }
        )
    elif failure_classification == "vertex_service_tier_unexpected":
        usage = (response_json or {}).get("usageMetadata")
        detail.update(
            {
                "failed_check": "usageMetadata.serviceTier.absent",
                "failed_item": "usageMetadata.serviceTier",
                "expected": "serviceTier absent; Vertex should report trafficType instead",
                "actual": usage,
            }
        )
    elif status_code is not None and status_code >= 400:
        detail.update(
            {
                "failed_check": "http_status",
                "failed_item": "HTTP status / error body",
                "expected": "2xx response",
                "actual": {"status_code": status_code, "error": _api_error_message(response_json) or raw_text or ""},
            }
        )
    elif profile:
        detail["failed_check"] = f"{profile}.{failure_classification or 'response_validation'}"

    return detail


def main() -> int:
    config = load_config()
    provider = get_active_provider_name(config)
    model = get_selected_model(config, provider)
    family = get_model_family(config, model, provider)
    route_profile = get_model_route_profile(config, model, provider)
    api_form = get_model_api_form(
        config, model, provider, route_profile=route_profile
    )
    reference_source = _select_reference_source(
        config,
        family,
        model,
        provider,
        api_form=api_form,
        route_profile=route_profile,
    )
    allowed_reference_sources = reference_sources_for_model(
        config,
        family,
        model,
        provider,
        api_form=api_form,
        route_profile=route_profile,
    )
    if reference_source not in allowed_reference_sources:
        raise ValueError(
            f"Reference source {reference_source!r} is not part of the "
            f"{family}/{model} family suite; allowed={allowed_reference_sources}."
        )
    reference = get_reference_source(reference_source)
    reference_family = family_for_reference(reference_source)
    if reference_family != family:
        raise ValueError(
            f"Reference source {reference_source!r} belongs to {reference_family!r}, "
            f"not requested family {family!r}."
        )
    suite_profiles = reference_test_profiles(reference_source)
    capability_profile = capability_profile_snapshot(
        "text",
        family,
        model,
        suite_profiles,
        reference_source=reference_source,
        api_form=api_form,
        route_profile=route_profile,
    )
    if capability_profile.get("parameter_test_enabled") is not True:
        raise ValueError(
            f"Text parameter testing is disabled for {family}/{model}: "
            f"{capability_profile.get('disabled_reason') or 'model profile policy'}."
        )
    if (
        capability_profile.get("known_model") is not True
        or capability_profile.get("known_api_profile") is not True
        or capability_profile.get("route_profile_known") is not True
    ):
        raise ValueError(
            f"Missing registered text model/API/route profile for "
            f"{family}/{api_form}/{model}/{route_profile}."
        )
    runs = _param_test_runs()
    tool_validation_mode = _tool_validation_mode()
    output_dir = ensure_dir(Path(os.getenv("LOADTEST_REPORT_DIR") or _default_report_dir(provider, model)))

    try:
        client = DeepSeekClient.from_config(config, provider)
        identity_probe = run_identity_probe(
            config,
            client,
            provider,
            model,
            family,
            reference_source,
            reference_family,
        )
        results = run_param_tests(
            config,
            client,
            provider,
            model,
            family,
            reference_source,
            reference_family,
            runs,
            output_dir,
            capability_profile=capability_profile,
        )
    except Exception as exc:
        identity_probe = None
        results = [
            {
                "name": "param_test:init",
                "status": "fail",
                "pass": False,
                "expectation": "supported",
                "failure_classification": exc.__class__.__name__,
                "message": str(exc),
                "token_audit": combine_exchange_audits([]),
                "model_identity_audit": combine_model_identity_audits([]),
            }
        ]

    failed = [item for item in results if item.get("status") == "fail"]
    incompatible = [item for item in results if item.get("status") == "incompatible"]
    unexpected_acceptance = [
        item for item in results if item.get("status") == "unexpected_acceptance"
    ]
    expected_rejection = [
        item for item in results if item.get("status") == "expected_rejection"
    ]
    passed = [item for item in results if item.get("status") == "pass"]
    total = len(results)
    token_audit_results = (
        [identity_probe, *results] if isinstance(identity_probe, dict) else results
    )
    token_audit_summary = summarize_token_audits(token_audit_results)
    model_identity_summary = summarize_model_identity_audits(results, identity_probe)
    compatibility_pass = compatibility_pass_from_statuses(
        [str(item.get("status") or "fail") for item in results]
    )
    token_accuracy_pass = bool(token_audit_summary.get("pass", True))
    model_identity_pass = bool(model_identity_summary.get("pass", True))
    certification_scope = str(
        reference.get("certification_scope")
        or capability_profile.get("certification_scope")
        or "raw_route_contract"
    )
    adapter_pass = compatibility_pass and token_accuracy_pass and model_identity_pass
    certified_route_contract_pass = (
        adapter_pass if certification_scope != "adapter_only" else False
    )
    compatibility_ok_count = len(passed) + len(expected_rejection)
    verdict = {
        "pass": adapter_pass,
        "adapter_pass": adapter_pass,
        "certified_route_contract_pass": certified_route_contract_pass,
        "certification_scope": certification_scope,
        "route_stability_required": bool(
            reference.get("route_stability_required")
            or capability_profile.get("route_stability_required")
        ),
        "provenance_status": (
            "unverifiable"
            if certification_scope == "adapter_only"
            else "configured_route_contract"
        ),
        "compatibility_pass": compatibility_pass,
        "token_accuracy_pass": token_accuracy_pass,
        "model_identity_pass": model_identity_pass,
        "stage": "param_test",
        "provider": provider,
        "provider_label": (get_provider_config(config, provider).get("label") or provider),
        "model": model,
        "model_family": family,
        "api_form": api_form,
        "route_profile": route_profile,
        "reference_route_profile": reference.get("route_profile"),
        "reference_source": reference_source,
        "reference_label": reference["label"],
        "reference_family": reference_family,
        "official_sources": reference["official_sources"],
        "model_capability_profile": capability_profile,
        "tested_params": reference_tested_params(reference_source),
        "untested_params": untested_params_for_reference(reference_source),
        "param_test_runs": runs,
        "tool_validation_mode": tool_validation_mode,
        "total": total,
        "passed": compatibility_ok_count,
        "passed_supported": len(passed),
        "expected_rejection": len(expected_rejection),
        "incompatible": len(incompatible),
        "unexpected_acceptance": len(unexpected_acceptance),
        "failed": len(failed),
        "overall_success_rate": compatibility_ok_count / total if total else 0.0,
        "performance_summary": _performance_summary(results),
        "token_audit_summary": token_audit_summary,
        "model_identity_summary": model_identity_summary,
        "identity_probe": identity_probe,
        "incompatibilities": incompatible,
        "unexpected_acceptances": unexpected_acceptance,
        "expected_rejections": expected_rejection,
        "failures": failed,
        "param_specs": model_reference_spec_payload(
            "text",
            family,
            model,
            reference_source,
            api_form=api_form,
            route_profile=route_profile,
        ),
    }
    write_json(output_dir / "param_results.json", results)
    write_json(output_dir / "token_audit.json", flatten_token_audits(token_audit_results))
    write_json(output_dir / "model_identity.json", {
        "summary": model_identity_summary,
        "probe": identity_probe,
        "results": [
            {
                "name": item.get("name"),
                "profile": item.get("profile"),
                "run_index": item.get("run_index"),
                "model_identity_audit": item.get("model_identity_audit"),
            }
            for item in results
        ],
    })
    write_json(output_dir / "verdict.json", verdict)
    _write_failed_cases(output_dir, results)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if verdict["pass"] else 1


def _select_reference_source(
    config: dict[str, Any],
    family: str,
    model: str,
    provider: str,
    *,
    api_form: str | None = None,
    route_profile: str | None = None,
) -> str:
    explicit = str(os.getenv("LOADTEST_REFERENCE_SOURCE") or "").strip()
    comparison_mode = str(
        os.getenv("LOADTEST_MODEL_COMPARISON") or ""
    ).strip().casefold() in {"1", "true", "yes", "on"}
    if not comparison_mode:
        return explicit or default_reference_source_for_model(
            config,
            family,
            model,
            provider,
            api_form=api_form,
            route_profile=route_profile,
        )
    canonical = comparison_reference_source_for_model(
        "text",
        family,
        model,
        api_form=api_form,
        route_profile=route_profile,
    )
    if explicit and explicit != canonical:
        raise ValueError(
            f"LOADTEST_REFERENCE_SOURCE={explicit!r} conflicts with the "
            f"canonical comparison source {canonical!r} for {family}/{model}."
        )
    return canonical


def run_param_tests(
    config: dict[str, Any],
    client: DeepSeekClient,
    provider: str,
    model: str,
    family: str,
    reference_source: str,
    reference_family: str,
    runs: int,
    output_dir: Path | None = None,
    capability_profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    profiles = reference_test_profiles(reference_source)
    total = len(profiles) * runs
    completed = 0
    rng = random.SystemRandom()
    capability = capability_profile or capability_profile_snapshot(
        "text",
        family,
        model,
        profiles,
        reference_source=reference_source,
    )
    for profile in profiles:
        expectation = resolve_profile_expectation(
            "text",
            family,
            model,
            profile,
            capability_profile=capability,
            reference_source=reference_source,
        )
        input_samples = _sample_inputs_for_profile(config, profile, runs, rng)
        for run_index in range(1, runs + 1):
            input_sample = input_samples[run_index - 1]
            completed += 1
            print(
                f"[{completed}/{total}] running {reference_source}:{profile} "
                f"expectation={expectation} run={run_index}",
                flush=True,
            )
            results.append(
                run_one_profile(
                    config,
                    client,
                    provider,
                    model,
                    family,
                    reference_source,
                    reference_family,
                    profile,
                    run_index,
                    input_sample,
                    expectation=expectation,
                )
            )
            if output_dir is not None:
                write_json(output_dir / "param_results.json", results)
                write_json(output_dir / "token_audit.json", flatten_token_audits(results))
                _write_failed_cases(output_dir, results)
    return results


def run_identity_probe(
    config: dict[str, Any],
    client: DeepSeekClient,
    provider: str,
    model: str,
    family: str,
    reference_source: str,
    reference_family: str,
) -> dict[str, Any]:
    prompt = "Reply with OK."
    reference = get_reference_source(reference_source)
    api_form = str(reference.get("api_form") or "openai_chat_completions")
    reference_route_profile = str(
        reference.get("route_profile") or ""
    )
    route_profile = get_model_route_profile(config, model, provider)
    api_form = get_model_api_form(
        config,
        model,
        provider,
        route_profile=route_profile,
        api_form=api_form,
    )
    if api_form == "openai_responses":
        transport = "openai_responses"
        body = {"model": model, "input": prompt, "max_output_tokens": 16, "stream": False}
        result = client.openai_responses(body)
    elif api_form == "anthropic_messages":
        transport = "claude_messages"
        body = {
            "model": model,
            "max_tokens": 16,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }
        result = client.claude_messages(body)
    elif api_form == "gemini_generate_content":
        transport = "gemini_generate_content"
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 16},
        }
        result = client.gemini_generate_content(model, body)
    else:
        transport = "chat_completions"
        output_key = (
            "max_completion_tokens"
            if reference_source in {"openai_gpt5_chat", "kimi_k3_openai_compat"}
            else "max_tokens"
        )
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            output_key: 16,
            "stream": False,
        }
        if reference_source == "kimi_k3_openai_compat":
            body.update(
                {
                    "temperature": 1.0,
                    "top_p": 0.95,
                    "reasoning_effort": "low",
                }
            )
        result = client.chat_completion(body)
    provider_cfg = get_provider_config(config, provider)
    request_endpoint = str(
        get_provider_interface(config, transport, provider).get("path") or ""
    )
    token_audit = combine_exchange_audits(
        [
            _audit_exchange_safely(
                config,
                body,
                result,
                transport,
                "identity_probe",
                provider=provider,
                model=model,
                independent_input_count=_count_input_safely(
                    client, transport, model, body
                ),
            )
        ]
    )
    identity_audit = combine_model_identity_audits(
        [
            audit_model_identity(
                requested_model=model,
                result=result,
                transport=transport,
                provider_cfg=provider_cfg,
                exchange="identity_probe",
                request_endpoint=request_endpoint,
            )
        ]
    )
    return {
        "name": f"{provider}:{model}:identity_probe",
        "profile": "identity_probe",
        "parameter": "model identity",
        "run_index": 0,
        "status": "pass" if result.success else "fail",
        "pass": bool(result.success),
        "identity_probe": True,
        "provider": provider,
        "model": model,
        "model_family": family,
        "api_form": api_form,
        "route_profile": route_profile,
        "reference_route_profile": reference_route_profile,
        "reference_source": reference_source,
        "reference_family": reference_family,
        "transport": transport,
        "request_endpoint": request_endpoint,
        "status_code": result.status_code,
        "usage": result.usage,
        "token_audit": token_audit,
        "model_identity_audit": identity_audit,
        "response_model": result.response_json.get("model")
        or result.response_json.get("modelVersion"),
    }


def run_one_profile(
    config: dict[str, Any],
    client: DeepSeekClient,
    provider: str,
    model: str,
    family: str,
    reference_source: str,
    reference_family: str,
    profile: str,
    run_index: int,
    input_sample: dict[str, str],
    expectation: str | None = None,
) -> dict[str, Any]:
    name = f"{provider}:{model}:{reference_source}:{profile}:run_{run_index}"
    parameter = parameter_label_for_profile(reference_source, profile)
    expected = expectation or resolve_profile_expectation(
        "text",
        family,
        model,
        profile,
        reference_source=reference_source,
    )
    provider_cfg = get_provider_config(config, provider)
    reference = get_reference_source(reference_source)
    reference_contract_source = str(
        reference.get("contract_reference_source") or reference_source
    )
    api_form = str(reference.get("api_form") or "openai_chat_completions")
    reference_route_profile = str(
        reference.get("route_profile") or ""
    )
    route_profile = get_model_route_profile(config, model, provider)
    api_form = get_model_api_form(
        config,
        model,
        provider,
        route_profile=route_profile,
        api_form=api_form,
    )
    request_body: dict[str, Any] | None = None
    transport = "chat_completions"
    request_endpoint = "/chat/completions"

    try:
        built = build_request(
            config,
            "compatibility_profiles",
            profile,
            overrides={"model": model, "prompt": input_sample["prompt"]},
            model_family_override=family,
            api_form_override=api_form,
            route_profile_override=reference_route_profile,
            reference_source=reference_source,
            enforce_model_capabilities=False,
        )
        request_body = built.body
        transport = str(built.metadata.get("transport") or transport)
        request_endpoint = str(built.metadata.get("request_endpoint") or request_endpoint)
        if transport == "gemini_generate_content":
            extra_headers = built.metadata.get("request_headers")
            result = client.gemini_generate_content(
                model,
                built.body,
                headers=extra_headers if isinstance(extra_headers, dict) else None,
            )
        elif transport == "claude_messages":
            result = client.claude_messages(built.body)
        elif transport == "openai_responses":
            result = client.openai_responses(built.body)
        else:
            result = client.chat_completion(built.body)
        exchange_audits = [
            _audit_exchange_safely(
                config,
                built.body,
                result,
                transport,
                "initial",
                provider=provider,
                model=model,
                independent_input_count=_count_input_safely(
                    client, transport, model, built.body
                ),
            )
        ]
        identity_audits = [
            audit_model_identity(
                requested_model=model,
                result=result,
                transport=transport,
                provider_cfg=provider_cfg,
                exchange="initial",
                request_endpoint=request_endpoint,
            )
        ]
        validation_error = validate_profile_response(
            profile,
            result.response_json,
            result,
            request_body=built.body,
            transport=transport,
            tool_validation_mode=_tool_validation_mode(),
            reference_source=reference_contract_source,
        )
        validation_ok = bool(result.success) and validation_error is None
        # Unsupported probes only care about HTTP rejection vs acceptance.
        outcome = map_probe_outcome(
            expected,
            status_code=result.status_code,
            validation_ok=True if expected == "unsupported" else validation_ok,
        )
        status = str(outcome["status"])
        passed = bool(outcome["pass"])
        failure_classification = None
        if not passed:
            if status == "unexpected_acceptance":
                failure_classification = "unexpected_acceptance"
            else:
                failure_classification = validation_error or result.failure_classification or result.error_type
        reported_status_code = result.status_code
        reported_message = result.raw_text
        failed_request_body = built.body
        failed_response_json = result.response_json
        failed_response_headers = result.headers
        failed_cache_headers = result.cache_headers
        followup_request_body = None
        followup_response_raw = None
        followup_response_json = None
        followup_response_headers = None

        # Only follow up when we expected and received a successful supported tool call.
        if (
            expected == "supported"
            and validation_ok
            and built.metadata.get("multi_turn")
        ):
            if transport == "gemini_generate_content":
                followup_body = build_native_tool_followup_request(
                    built.body,
                    result.response_json,
                )
                followup = client.gemini_generate_content(model, followup_body)
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
            exchange_audits.append(
                _audit_exchange_safely(
                    config,
                    followup_body,
                    followup,
                    transport,
                    "followup",
                    provider=provider,
                    model=model,
                    independent_input_count=_count_input_safely(
                        client, transport, model, followup_body
                    ),
                )
            )
            identity_audits.append(
                audit_model_identity(
                    requested_model=model,
                    result=followup,
                    transport=transport,
                    provider_cfg=provider_cfg,
                    exchange="followup",
                    request_endpoint=request_endpoint,
                )
            )
            followup_error = validate_tool_followup_response(
                followup.response_json,
                followup,
                transport=transport,
                tool_validation_mode=_tool_validation_mode(),
            )
            if followup_error:
                validation_error = followup_error
                followup_outcome = map_probe_outcome(
                    "supported",
                    status_code=followup.status_code,
                    validation_ok=False,
                )
                status = str(followup_outcome["status"])
                passed = bool(followup_outcome["pass"])
                failure_classification = validation_error
                reported_status_code = followup.status_code
                reported_message = followup.raw_text
                failed_request_body = followup_body
                failed_response_json = followup.response_json
                failed_response_headers = followup.headers
                failed_cache_headers = followup.cache_headers
                followup_request_body = followup_body
                followup_response_raw = followup.raw_text
                followup_response_json = followup.response_json
                followup_response_headers = followup.headers

        failure_detail = None if passed else _failure_detail(
            profile, status, reported_status_code, failure_classification, failed_response_json, reported_message
        )
        if failure_detail is not None:
            failure_detail["expectation"] = expected
            if status == "unexpected_acceptance":
                failure_detail["expected"] = "parameter rejection (HTTP 400/422)"
                failure_detail["actual"] = {
                    "status_code": reported_status_code,
                    "note": "upstream accepted an unsupported parameter probe",
                }
            elif expected == "supported" and status == "incompatible":
                failure_detail["expected"] = "2xx response matching the selected reference profile"
        reason = None if passed else str((failure_detail or {}).get("failure_reason") or "")
        performance_metrics = _performance_metrics(result, transport)
        payload = {
            "name": name,
            "profile": profile,
            "parameter": parameter,
            "expectation": expected,
            "run_index": run_index,
            "status": status,
            "pass": passed,
            "provider": provider,
            "model": model,
            "model_family": family,
            "api_form": api_form,
            "route_profile": route_profile,
            "reference_route_profile": reference_route_profile,
            "reference_source": reference_source,
            "reference_family": reference_family,
            "transport": transport,
            "request_endpoint": request_endpoint,
            "input_sample": input_sample["id"],
            "tool_validation_mode": _tool_validation_mode(),
            "status_code": reported_status_code,
            "latency_ms": result.latency_ms,
            "ttft_ms": result.ttft_ms,
            "tpot_ms": performance_metrics.get("tpot_ms"),
            "throughput_output_tokens_per_sec": performance_metrics.get("throughput_output_tokens_per_sec"),
            "throughput_total_tokens_per_sec": performance_metrics.get("throughput_total_tokens_per_sec"),
            "performance_metrics": performance_metrics,
            "finish_reason": result.finish_reason,
            "usage": result.usage,
            "token_audit": combine_exchange_audits(exchange_audits),
            "model_identity_audit": combine_model_identity_audits(identity_audits),
            "warnings": built.warnings,
            "response_model": result.response_json.get("model")
            or result.response_json.get("modelVersion"),
            "failure_classification": failure_classification,
            "failure_reason": reason,
            "failure_detail": failure_detail,
            "reason": reason,
            "message": None if passed else reported_message,
        }
        if failure_detail:
            payload.update(
                {
                    "failed_check": failure_detail.get("failed_check"),
                    "failed_item": failure_detail.get("failed_item"),
                    "expected": failure_detail.get("expected"),
                    "actual": failure_detail.get("actual"),
                }
            )
        if not passed:
            payload.update(
                {
                    "input": {
                        "sample_id": input_sample["id"],
                        "prompt": input_sample["prompt"],
                    },
                    "request_body": built.body,
                    "response_raw": result.raw_text,
                    "response_json": result.response_json,
                    "response_headers": result.headers,
                    "cache_headers": result.cache_headers,
                    "failed_request_body": failed_request_body,
                    "failed_response_raw": reported_message,
                    "failed_response_json": failed_response_json,
                    "failed_response_headers": failed_response_headers,
                    "failed_cache_headers": failed_cache_headers,
                }
            )
            if followup_request_body is not None:
                payload.update(
                    {
                        "followup_request_body": followup_request_body,
                        "followup_response_raw": followup_response_raw,
                        "followup_response_json": followup_response_json,
                        "followup_response_headers": followup_response_headers,
                    }
                )
        return payload
    except Exception as exc:
        failure_detail = {
            "failure_reason": f"请求构建或执行异常 | {exc.__class__.__name__}: {exc}",
            "failed_check": "exception",
            "failed_item": "request build/send path",
            "expected": "profile request can be built and sent",
            "actual": f"{exc.__class__.__name__}: {exc}",
            "expectation": expected,
        }
        return {
            "name": name,
            "profile": profile,
            "parameter": parameter,
            "expectation": expected,
            "run_index": run_index,
            "status": "fail",
            "pass": False,
            "provider": provider,
            "model": model,
            "model_family": family,
            "api_form": api_form,
            "route_profile": route_profile,
            "reference_route_profile": reference_route_profile,
            "reference_source": reference_source,
            "reference_family": reference_family,
            "transport": transport,
            "request_endpoint": request_endpoint,
            "input_sample": input_sample["id"],
            "input": {
                "sample_id": input_sample["id"],
                "prompt": input_sample["prompt"],
            },
            "request_body": request_body,
            "failed_request_body": request_body,
            "failure_classification": exc.__class__.__name__,
            "failure_reason": failure_detail["failure_reason"],
            "failure_detail": failure_detail,
            "failed_check": failure_detail["failed_check"],
            "failed_item": failure_detail["failed_item"],
            "expected": failure_detail["expected"],
            "actual": failure_detail["actual"],
            "message": str(exc),
            "response_raw": "",
            "failed_response_raw": "",
            "response_json": {},
            "failed_response_json": {},
            "performance_metrics": {},
            "token_audit": combine_exchange_audits([]),
            "model_identity_audit": combine_model_identity_audits([]),
        }


def _audit_exchange_safely(
    config: dict[str, Any],
    request_body: dict[str, Any],
    result: Any,
    transport: str,
    exchange: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    independent_input_count: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return audit_exchange(
            request_body,
            result,
            transport,
            config,
            exchange,
            provider=provider,
            model=model,
            independent_input_count=independent_input_count,
        )
    except Exception as exc:
        note = f"token audit error: {exc.__class__.__name__}: {exc}"
        unavailable = {"status": "not_available", "note": note}
        return {
            "schema_version": 2,
            "exchange": exchange,
            "status": "not_available",
            "input": dict(unavailable),
            "output": dict(unavailable),
            "reported": {},
            "independent_count": {},
            "usage_arithmetic": dict(unavailable),
            "input_accuracy": dict(unavailable),
            "output_accuracy": dict(unavailable),
            "evidence_level": "unavailable",
            "usage_accounting": {},
            "settings": {},
        }


def _count_input_safely(
    client: Any,
    transport: str,
    model: str,
    body: dict[str, Any],
) -> dict[str, Any] | None:
    counter = getattr(client, "count_tokens", None)
    if not callable(counter):
        return None
    try:
        value = counter(transport, model, body)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _performance_metrics(result: Any, transport: str | None = None) -> dict[str, Any]:
    try:
        accounting = normalize_usage(result.usage or {}, transport)
    except Exception:
        accounting = {}
    input_tokens = accounting.get("input_tokens")
    answer_tokens = accounting.get("answer_tokens")
    thinking_tokens = accounting.get("thinking_tokens")
    output_tokens = accounting.get("output_tokens")
    total_tokens = accounting.get("total_tokens")
    latency_ms = _float_or_none(result.latency_ms)
    ttft_ms = _float_or_none(result.ttft_ms)
    latency_sec = latency_ms / 1000.0 if latency_ms and latency_ms > 0 else None

    tpot_ms = None
    tpot_basis = None
    if output_tokens is not None and output_tokens > 0 and latency_ms is not None:
        if ttft_ms is not None and output_tokens > 1 and latency_ms >= ttft_ms:
            tpot_ms = (latency_ms - ttft_ms) / max(output_tokens - 1, 1)
            tpot_basis = "stream_latency_after_ttft_per_output_token"
        else:
            tpot_ms = latency_ms / output_tokens
            tpot_basis = "end_to_end_latency_per_output_token"

    output_tps = (
        output_tokens / latency_sec
        if output_tokens is not None and latency_sec and latency_sec > 0
        else None
    )
    total_tps = (
        total_tokens / latency_sec
        if total_tokens is not None and latency_sec and latency_sec > 0
        else None
    )
    metrics: dict[str, Any] = {
        "latency_ms": _round_metric(latency_ms),
        "ttft_ms": _round_metric(ttft_ms),
        "tpot_ms": _round_metric(tpot_ms),
        "tpot_basis": tpot_basis,
        "throughput_output_tokens_per_sec": _round_metric(output_tps),
        "throughput_total_tokens_per_sec": _round_metric(total_tps),
        "throughput_output_tokens_per_min": _round_metric(output_tps * 60 if output_tps is not None else None),
        "throughput_total_tokens_per_min": _round_metric(total_tps * 60 if total_tps is not None else None),
        "input_tokens": input_tokens,
        "answer_tokens": answer_tokens,
        "thinking_tokens": thinking_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "thinking_share": (
            _round_metric(thinking_tokens / output_tokens)
            if thinking_tokens is not None and output_tokens
            else None
        ),
        "response_bytes": result.response_length,
    }
    return {key: value for key, value in metrics.items() if value is not None}


def _performance_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = [
        item.get("performance_metrics") or {}
        for item in results
        if isinstance(item.get("performance_metrics"), dict)
    ]
    successful_metrics = [
        item.get("performance_metrics") or {}
        for item in results
        if item.get("pass") and isinstance(item.get("performance_metrics"), dict)
    ]
    thinking_metrics = [
        item for item in successful_metrics if item.get("thinking_tokens") is not None
    ]
    thinking_tokens_total = sum(_metric_values(thinking_metrics, "thinking_tokens"))
    thinking_output_tokens_total = sum(_metric_values(thinking_metrics, "output_tokens"))
    return {
        "sample_count": len(metrics),
        "success_sample_count": len(successful_metrics),
        "latency_ms": _metric_stats(_metric_values(successful_metrics, "latency_ms")),
        "ttft_ms": _metric_stats(_metric_values(successful_metrics, "ttft_ms")),
        "tpot_ms": _metric_stats(_metric_values(successful_metrics, "tpot_ms")),
        "throughput_output_tokens_per_sec": _metric_stats(
            _metric_values(successful_metrics, "throughput_output_tokens_per_sec")
        ),
        "throughput_total_tokens_per_sec": _metric_stats(
            _metric_values(successful_metrics, "throughput_total_tokens_per_sec")
        ),
        "token_usage_sample_count": len(
            [item for item in successful_metrics if item.get("total_tokens") is not None]
        ),
        "answer_tokens": _metric_stats(_metric_values(successful_metrics, "answer_tokens")),
        "thinking_tokens": _metric_stats(_metric_values(successful_metrics, "thinking_tokens")),
        "output_tokens": _metric_stats(_metric_values(successful_metrics, "output_tokens")),
        "thinking_token_sample_count": len(thinking_metrics),
        "thinking_tokens_total": int(thinking_tokens_total) if thinking_metrics else None,
        "thinking_share": (
            thinking_tokens_total / thinking_output_tokens_total
            if thinking_output_tokens_total > 0
            else None
        ),
        "ttft_coverage": _coverage(successful_metrics, "ttft_ms"),
        "tpot_coverage": _coverage(successful_metrics, "tpot_ms"),
    }


def _metric_values(metrics: list[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for item in metrics:
        value = _float_or_none(item.get(key))
        if value is not None:
            values.append(value)
    return values


def _metric_stats(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "avg": _round_metric(statistics.fmean(values)) if values else None,
        "min": _round_metric(min(values)) if values else None,
        "p50": _round_metric(percentile(values, 50)),
        "p90": _round_metric(percentile(values, 90)),
        "p95": _round_metric(percentile(values, 95)),
        "max": _round_metric(max(values)) if values else None,
    }


def _coverage(metrics: list[dict[str, Any]], key: str) -> float:
    if not metrics:
        return 0.0
    return len(_metric_values(metrics, key)) / len(metrics)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_metric(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 3)


def _write_failed_cases(output_dir: Path, results: list[dict[str, Any]]) -> None:
    cases = _failed_cases(results)
    write_json(output_dir / "param_failed_cases.json", cases)
    safe_log = redact_secrets(_failed_cases_log(cases))
    (output_dir / "param_failed_cases.log").write_text(safe_log, encoding="utf-8")


def _failed_cases(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for item in results:
        if item.get("status") not in {"incompatible", "fail", "unexpected_acceptance"}:
            continue
        cases.append(
            {
                "status": item.get("status"),
                "profile": item.get("profile"),
                "parameter": item.get("parameter"),
                "expectation": item.get("expectation"),
                "run_index": item.get("run_index"),
                "provider": item.get("provider"),
                "model": item.get("model"),
                "reference_source": item.get("reference_source"),
                "reference_family": item.get("reference_family"),
                "transport": item.get("transport"),
                "request_endpoint": item.get("request_endpoint"),
                "input_sample": item.get("input_sample"),
                "tool_validation_mode": item.get("tool_validation_mode"),
                "input": item.get("input") or {},
                "request_body": item.get("request_body"),
                "response_raw": item.get("response_raw"),
                "response_json": item.get("response_json"),
                "response_headers": item.get("response_headers") or {},
                "cache_headers": item.get("cache_headers") or {},
                "failed_request_body": item.get("failed_request_body") or item.get("request_body"),
                "failed_response_raw": item.get("failed_response_raw") if item.get("failed_response_raw") is not None else item.get("message"),
                "failed_response_json": item.get("failed_response_json") or item.get("response_json") or {},
                "failed_response_headers": item.get("failed_response_headers") or item.get("response_headers") or {},
                "failed_cache_headers": item.get("failed_cache_headers") or item.get("cache_headers") or {},
                "followup_request_body": item.get("followup_request_body"),
                "followup_response_raw": item.get("followup_response_raw"),
                "followup_response_json": item.get("followup_response_json"),
                "followup_response_headers": item.get("followup_response_headers"),
                "status_code": item.get("status_code"),
                "latency_ms": item.get("latency_ms"),
                "performance_metrics": item.get("performance_metrics") or {},
                "failure_classification": item.get("failure_classification"),
                "failure_reason": item.get("failure_reason") or item.get("reason"),
                "failure_detail": item.get("failure_detail") or {},
                "failed_check": item.get("failed_check"),
                "failed_item": item.get("failed_item"),
                "expected": item.get("expected"),
                "actual": item.get("actual"),
                "warnings": item.get("warnings") or [],
                "message": item.get("message") or "",
            }
        )
    return cases


def _failed_cases_log(cases: list[dict[str, Any]]) -> str:
    if not cases:
        return "No failed, incompatible, or unexpected-acceptance parameter test cases.\n"

    lines: list[str] = []
    for index, case in enumerate(cases, start=1):
        lines.append(f"===== Case {index}: {case.get('status')} =====")
        lines.append(f"profile: {case.get('profile')}")
        lines.append(f"parameter: {case.get('parameter')}")
        lines.append(f"expectation: {case.get('expectation')}")
        lines.append(f"run_index: {case.get('run_index')}")
        lines.append(f"provider/model: {case.get('provider')} / {case.get('model')}")
        lines.append(f"reference: {case.get('reference_source')} ({case.get('reference_family')})")
        lines.append(f"transport: {case.get('transport')}")
        lines.append(f"request_endpoint: {case.get('request_endpoint')}")
        lines.append(f"input_sample: {case.get('input_sample')}")
        lines.append(f"tool_validation_mode: {case.get('tool_validation_mode')}")
        lines.append(f"status_code: {case.get('status_code')}")
        lines.append(f"latency_ms: {case.get('latency_ms')}")
        lines.append(f"failure_classification: {case.get('failure_classification')}")
        lines.append(f"failure_reason: {case.get('failure_reason')}")
        lines.append(f"failed_check: {case.get('failed_check')}")
        lines.append(f"failed_item: {case.get('failed_item')}")
        lines.append("expected:")
        lines.append(_pretty_json(case.get("expected")))
        lines.append("actual:")
        lines.append(_pretty_json(case.get("actual")))
        warnings = case.get("warnings") or []
        if warnings:
            lines.append(f"warnings: {json.dumps(warnings, ensure_ascii=False)}")
        lines.append("")
        lines.append("input:")
        lines.append(_pretty_json(case.get("input") or {"sample_id": case.get("input_sample")}))
        lines.append("")
        lines.append("failed_request_body:")
        lines.append(_pretty_json(case.get("failed_request_body")))
        lines.append("")
        lines.append("failed_response_status_code:")
        lines.append(str(case.get("status_code")))
        performance_metrics = case.get("performance_metrics") or {}
        if performance_metrics:
            lines.append("")
            lines.append("performance_metrics:")
            lines.append(_pretty_json(performance_metrics))
        lines.append("")
        lines.append("failed_response_headers:")
        lines.append(_pretty_json(case.get("failed_response_headers") or {}))
        failed_cache_headers = case.get("failed_cache_headers") or {}
        if failed_cache_headers:
            lines.append("")
            lines.append("failed_cache_headers:")
            lines.append(_pretty_json(failed_cache_headers))
        lines.append("")
        lines.append("failed_response_json:")
        lines.append(_pretty_json(case.get("failed_response_json") or {}))
        lines.append("")
        lines.append("failed_response_raw:")
        lines.append(str(case.get("failed_response_raw") or case.get("message") or ""))
        if case.get("followup_request_body") is not None:
            lines.append("")
            lines.append("initial_request_body:")
            lines.append(_pretty_json(case.get("request_body")))
            lines.append("")
            lines.append("initial_response_raw:")
            lines.append(str(case.get("response_raw") or ""))
            lines.append("")
            lines.append("followup_request_body:")
            lines.append(_pretty_json(case.get("followup_request_body")))
            lines.append("")
            lines.append("followup_response_json:")
            lines.append(_pretty_json(case.get("followup_response_json") or {}))
            lines.append("")
            lines.append("followup_response_raw:")
            lines.append(str(case.get("followup_response_raw") or ""))
        lines.append("")
    return "\n".join(lines)


def _pretty_json(value: Any) -> str:
    if value is None:
        return "null"
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except TypeError:
        return str(value)


def _non_pass_status(status_code: int | None, validation_error: str | None) -> str:
    """Legacy helper kept for callers; prefer map_probe_outcome with an expectation."""
    outcome = map_probe_outcome(
        "supported",
        status_code=status_code,
        validation_ok=False if validation_error else is_http_success_local(status_code),
    )
    return str(outcome["status"])


def is_http_success_local(status_code: int | None) -> bool:
    return status_code is not None and 200 <= status_code <= 299


def _tool_validation_mode() -> str:
    mode = str(os.getenv("LOADTEST_TOOL_VALIDATION_MODE") or "auto")
    if mode not in {"auto", "openai_compat", "gemini_native", "claude_native", "openai_responses"}:
        raise ValueError(
            "LOADTEST_TOOL_VALIDATION_MODE must be auto, openai_compat, gemini_native, "
            "claude_native, or openai_responses"
        )
    return mode


def _param_test_runs() -> int:
    raw = os.getenv("LOADTEST_PARAM_TEST_RUNS") or str(DEFAULT_PARAM_TEST_RUNS)
    try:
        return min(max(int(raw), 1), MAX_PARAM_TEST_RUNS)
    except ValueError:
        return DEFAULT_PARAM_TEST_RUNS


def _sample_inputs_for_profile(
    config: dict[str, Any],
    profile: str,
    count: int,
    rng: random.Random,
) -> list[dict[str, str]]:
    group = _input_group_for_profile(config, profile)
    pool = _configured_input_pool(config, group)
    if not pool:
        pool = _fallback_input_pool(config, group)
    if not pool:
        pool = [{"id": f"{group}:default", "prompt": "请用一句话完成这个 API 兼容性测试请求。"}]
    if group == "json_output":
        # OpenAI-compatible json_object requires the prompt to mention "JSON".
        pool = [sample for sample in pool if "json" in sample["prompt"].casefold()]
        if not pool:
            raise ValueError(
                f"Profile {profile!r} uses OpenAI-compatible JSON mode, "
                "but no param_test_inputs.json_output samples contain the word JSON."
            )

    shuffled = list(pool)
    rng.shuffle(shuffled)
    expanded: list[dict[str, str]] = []
    while len(expanded) < count:
        for sample in shuffled:
            if len(expanded) >= count:
                break
            expanded.append(dict(sample))
        if len(expanded) < count:
            rng.shuffle(shuffled)

    seen: dict[str, int] = {}
    unique: list[dict[str, str]] = []
    for index, sample in enumerate(expanded[:count], start=1):
        prompt = sample["prompt"]
        repeat = seen.get(prompt, 0)
        seen[prompt] = repeat + 1
        if repeat:
            prompt = f"{prompt}\n\n本次参数兼容性测试编号：{index}。请保持回答简洁。"
        unique.append({"id": sample["id"], "prompt": prompt})
    return unique


def _lookup_profile_settings(config: dict[str, Any], profile: str) -> dict[str, Any]:
    for group in ("compatibility_profiles", "throughput_profiles", "cache_profiles"):
        settings = (config.get(group) or {}).get(profile)
        if isinstance(settings, dict):
            return settings
    return {}


def _profile_uses_openai_json_object(settings: dict[str, Any]) -> bool:
    """OpenAI JSON mode: response_format.type=json_object or Responses text.format."""
    response_format = settings.get("response_format")
    if (
        isinstance(response_format, dict)
        and str(response_format.get("type") or "").casefold() == "json_object"
    ):
        return True
    text = settings.get("text")
    if isinstance(text, dict):
        fmt = text.get("format")
        if isinstance(fmt, dict) and str(fmt.get("type") or "").casefold() == "json_object":
            return True
    return False


def _profile_uses_gemini_json_mime(settings: dict[str, Any]) -> bool:
    for key in ("generationConfig", "native_generation_config"):
        generation = settings.get(key)
        if not isinstance(generation, dict):
            continue
        mime = str(generation.get("responseMimeType") or "").casefold()
        if mime == "application/json":
            return True
        response_format = generation.get("responseFormat")
        if isinstance(response_format, dict):
            text = response_format.get("text")
            if isinstance(text, dict) and "json" in str(text.get("mimeType") or "").casefold():
                return True
    return False


def _input_group_for_profile(config: dict[str, Any], profile: str) -> str:
    settings = _lookup_profile_settings(config, profile)
    if _profile_uses_openai_json_object(settings) or _profile_uses_gemini_json_mime(settings):
        return "json_output"
    # Fallback for JSON probes that rely on fixtures rather than response_format.
    if profile in {
        "gemini_chat_response_mime_type",
        "gemini_native_response_mime_type",
        "gemini_native_response_schema",
        "gemini_native_response_json_schema",
        "gemini_native_response_format",
        "openai_responses_json",
        "gpt5_chat_json",
    }:
        return "json_output"
    if (
        profile in OPENAI_TOOL_PROFILES
        or profile in NATIVE_TOOL_PROFILES
        or profile in CLAUDE_NATIVE_TOOL_PROFILES
        or profile in OPENAI_RESPONSES_TOOL_PROFILES
    ):
        return "tool_calls"
    if profile in _REASONING_INPUT_PROFILES:
        return "reasoning"
    return "general"


def _configured_input_pool(config: dict[str, Any], group: str) -> list[dict[str, str]]:
    raw = (config.get("param_test_inputs") or {}).get(group) or []
    pool: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return pool
    for index, item in enumerate(raw, start=1):
        if isinstance(item, str) and item.strip():
            pool.append({"id": f"{group}:{index}", "prompt": item.strip()})
        elif isinstance(item, dict) and str(item.get("prompt") or "").strip():
            sample_id = str(item.get("id") or f"{group}:{index}")
            pool.append({"id": sample_id, "prompt": str(item["prompt"]).strip()})
    return pool


def _fallback_input_pool(config: dict[str, Any], group: str) -> list[dict[str, str]]:
    if group == "json_output":
        return [
            {"id": "json_output:summary", "prompt": "请只返回一个合法 JSON object，不要输出 Markdown。主题：用 JSON 总结 Python 性能优化的三个方向。"},
            {"id": "json_output:latency", "prompt": "请只返回 JSON object，字段包含 summary 和 items。主题：总结 API 延迟优化的三个要点。"},
            {"id": "json_output:cache", "prompt": "请输出合法 JSON，不要输出 Markdown。主题：列出上下文缓存观测的三个注意事项。"},
        ]
    if group == "tool_calls":
        return [
            {"id": "tool_calls:beijing", "prompt": "请调用 get_weather 查询北京当前天气，单位使用 celsius。"},
            {"id": "tool_calls:shanghai", "prompt": "请调用 get_weather 查询上海当前天气，单位使用 celsius。"},
            {"id": "tool_calls:hangzhou", "prompt": "请调用 get_weather 查询杭州当前天气，单位使用 celsius。"},
        ]
    if group == "reasoning":
        return [
            {"id": "reasoning:decimal", "prompt": "请先分析再回答：9.11 和 9.8 哪个更大？说明小数位对齐过程。"},
            {"id": "reasoning:arithmetic", "prompt": "请逐步计算并核验：(17 × 23) - (19 × 11) 的结果。"},
            {"id": "reasoning:logic", "prompt": "甲比乙早到，丙比甲晚到但比乙早到。请分析三人的到达顺序。"},
        ]

    pool: list[dict[str, str]] = []
    prompts = config.get("prompts") or {}
    if isinstance(prompts, dict):
        for key, value in prompts.items():
            if str(value).strip():
                pool.append({"id": f"prompts:{key}", "prompt": str(value).strip()})
    pool.extend(
        [
            {"id": "general:api_loadtest", "prompt": "用两句话解释 API 压测为什么要区分业务请求和控制请求。"},
            {"id": "general:streaming", "prompt": "列出流式响应兼容性测试中最重要的三个检查点。"},
            {"id": "general:cache", "prompt": "用一句话说明上下文缓存命中率应该如何解读。"},
        ]
    )
    return pool


def _default_report_dir(provider: str, model: str) -> Path:
    safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "_", model)
    return default_reports_root() / "param_tests" / provider / safe_model


if __name__ == "__main__":
    raise SystemExit(main())
