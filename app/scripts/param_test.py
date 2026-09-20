from __future__ import annotations

import copy
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

DEFAULT_PARAM_TEST_RUNS = 1
MAX_PARAM_TEST_RUNS = 1000

from lib.parameter_job_controls import bind_parameter_execution, bound_parameter_execution
from lib.client import DeepSeekClient, ChatResult
from lib.deepseek_beta_reference import (
    API_FORM as DEEPSEEK_BETA_FORM, CASE_IDS as DEEPSEEK_BETA_CASES,
    build_deepseek_beta_reference_plan,
    decode_beta_response,
)
from lib.deepseek_beta_runner import execute_deepseek_beta_plan
from lib.deepseek_fim_reference import SUITE_ID as FIM_SUITE_ID
from lib.deepseek_fim_runner import execute_fim_plan
from lib.anthropic_prefill_runner import execute_prefill_plan
from lib.anthropic_prefill_reference import SUITE_IDS as PREFILL_SUITE_IDS
from lib.anthropic_cache_reference import SUITE_IDS as CACHE_SUITE_IDS, CachePlan, canonical_bytes as cache_canonical_bytes
from lib.anthropic_cache_runner import execute_cache_plan
from lib.fixed_parameter_specs import build_fixed_plan, fixed_spec_payload, fixed_suite_descriptor
from lib.credential_security import redact_secrets
from lib.config import (
    deep_merge,
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
    BuiltRequest,
    build_claude_tool_followup_request,
    build_native_tool_followup_request,
    build_openai_responses_tool_followup_request,
    build_request,
    build_tool_followup_request,
    extract_claude_tool_uses,
    extract_content,
    extract_openai_responses_function_calls,
    extract_tool_calls,
)
from lib.metrics import percentile, write_json
from lib.job_spec import load_job_spec
from lib.model_identity import (
    audit_model_identity,
    combine_model_identity_audits,
    summarize_model_identity_audits,
)
from lib.model_profile_catalog import (
    database_snapshot,
    resolve_runtime_profile_binding,
    binding_from_database_snapshot,
    resolve_runtime_parameter_config,
)
from lib.profile_validation import (
    CLAUDE_NATIVE_TOOL_PROFILES,
    GEMINI_INTERACTIONS_TOOL_NONE_PROFILES,
    GEMINI_INTERACTIONS_TOOL_PROFILES,
    NATIVE_TOOL_PROFILES,
    OPENAI_RESPONSES_TOOL_PROFILES,
    OPENAI_TOOL_PROFILES,
    validate_profile_response,
    validate_parameter_stop_request,
    validate_tool_followup_response,
)
from lib.param_outcome import (
    apply_any_run_success,
    compatibility_pass_from_statuses,
    map_probe_outcome,
    normalize_run_success_mode,
)
from lib.gemini_interactions_validation import REJECTION_PROFILES, validate_interactions_rejection
from lib.parameter_output_limit import (
    configured_parameter_test_min_output_tokens,
    configured_parameter_test_output_budget,
    enforce_parameter_test_output_limit,
    parameter_targets_output_limit,
)
from lib.reference_specs import (
    capability_profile_snapshot,
    comparison_reference_source_for_model,
    default_reference_source_for_model,
    family_for_reference,
    get_reference_source,
    load_model_capability_profile,
    model_reference_spec_payload,
    parameter_label_for_profile,
    reference_param_rows,
    reference_sources_for_model,
    resolve_profile_expectation,
    test_profiles_for_reference as reference_test_profiles,
    tested_params_for_reference as reference_tested_params,
    untested_params_for_reference,
)
from lib.parameter_reference_policy import parameter_coverage_for_profiles, validate_reference_request, validate_reference_response
from lib.token_audit import (
    TOKEN_AUDIT_SCHEMA_VERSION,
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
    "fim_completion_missing": "FIM 响应缺少 choices[0].text",
    "responses_output_missing": "Responses 响应缺少 output item",
    "responses_usage_missing": "Responses 响应缺少 usage",
    "responses_fixed_field_mismatch": "Responses 固定兼容字段与官方声明不符",
    "responses_reasoning_missing": "Responses 响应缺少 reasoning 证据",
    "responses_reasoning_none_mismatch": "reasoning.effort=none 仍产生 reasoning",
    "responses_reasoning_summary_unexpected": "DeepSeek 返回了官方声明不会生成的 reasoning summary",
    "responses_logprobs_missing": "Responses 输出缺少 logprobs",
    "tool_calls_unexpected": "响应在 tool_choice=none 时仍返回工具调用",
    "interaction_id_missing": "Interactions 响应缺少 id",
    "interaction_model_missing": "Interactions 响应缺少 model",
    "interaction_status_mismatch": "Interactions 响应状态不符合 profile 预期",
    "interaction_steps_missing": "Interactions 响应缺少 steps",
    "interaction_usage_missing": "Interactions 响应缺少 usage",
    "interaction_text_missing": "Interactions 响应缺少文本输出",
    "anthropic_content_missing": "Anthropic Messages 响应缺少 content block",
    "anthropic_usage_missing": "Anthropic Messages 响应缺少 usage",
    "deepseek0813_contract_mismatch": "DeepSeek 0813 用例与来源合同或协议不匹配",
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
    "gemini_3_7_flash_interactions_reject_thinking_minimal",
    "gemini_3_7_flash_interactions_thinking_low",
    "gemini_3_7_flash_interactions_thinking_medium",
    "gemini_3_7_flash_interactions_thinking_high",
    "gemini_3_7_flash_interactions_thinking_summaries_auto",
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
    "gpt6_chat_reasoning_low",
    "gpt6_chat_reasoning_medium",
    "gpt6_chat_reasoning_high",
    "gpt6_chat_reasoning_xhigh",
    "gpt6_chat_reject_reasoning_max",
    "gpt6_chat_reject_reasoning_none",
    "gpt6_chat_reject_reasoning_minimal",
    "gpt6_responses_reasoning_low",
    "gpt6_responses_reasoning_medium",
    "gpt6_responses_reasoning_high",
    "gpt6_responses_reasoning_xhigh",
    "gpt6_responses_reasoning_max",
    "gpt6_responses_reasoning_summary_auto",
    "gpt6_responses_reasoning_context_current_turn",
    "gpt6_responses_pro_medium",
    "gpt6_responses_reject_reasoning_none",
    "gpt6_responses_reject_reasoning_minimal",
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



_REASONING_INPUT_PROFILES.update({
    'deepseek0813_anthropic_effort_high',
    'deepseek0813_anthropic_effort_max',
    'deepseek0813_anthropic_thinking_budget_ignored',
    'deepseek0813_responses_reasoning_high',
    'deepseek0813_responses_reasoning_low',
    'deepseek0813_responses_reasoning_max',
    'deepseek0813_responses_reasoning_medium',
    'deepseek0813_responses_reasoning_minimal',
    'deepseek0813_responses_reasoning_none',
    'deepseek0813_responses_reasoning_summary_accepted_ignored',
    'deepseek0813_responses_reasoning_xhigh',
    'deepseek0813_responses_temperature_thinking_accepted_ignored',
    'deepseek0813_responses_tools_required_thinking_reject',
    'deepseek0813_responses_top_p_thinking_accepted_ignored',
})

_DEEPSEEK_0813_RESPONSES_REQUIRED_TOOL_PROFILES = {
    "deepseek0813_responses_tools",
    "deepseek0813_responses_tools_named",
}


_DEEPSEEK_0813_RESPONSES_OPTIONAL_TOOL_PROFILES = {
    "deepseek0813_responses_tools_auto",
    "deepseek0813_responses_tool_choice_none",
    "deepseek0813_responses_tools_required_thinking_reject",
}


_DEEPSEEK_0813_ANTHROPIC_TOOL_PROFILES = {
    "deepseek0813_anthropic_tools_auto",
    "deepseek0813_anthropic_tools_any",
    "deepseek0813_anthropic_tools_named",
}


_DEEPSEEK_0813_TOOL_PROFILES = (
    _DEEPSEEK_0813_RESPONSES_REQUIRED_TOOL_PROFILES
    | _DEEPSEEK_0813_RESPONSES_OPTIONAL_TOOL_PROFILES
    | _DEEPSEEK_0813_ANTHROPIC_TOOL_PROFILES
)


_DEEPSEEK_0813_RESPONSES_REASONING_PROFILES = {
    "deepseek0813_responses_reasoning_minimal",
    "deepseek0813_responses_reasoning_low",
    "deepseek0813_responses_reasoning_medium",
    "deepseek0813_responses_reasoning_high",
    "deepseek0813_responses_reasoning_xhigh",
    "deepseek0813_responses_reasoning_max",
    "deepseek0813_responses_temperature_thinking_accepted_ignored",
    "deepseek0813_responses_top_p_thinking_accepted_ignored",
    "deepseek0813_responses_reasoning_summary_accepted_ignored",
}


_DEEPSEEK_0813_RESPONSES_STORE_PROFILES = {
    "deepseek0813_responses_store_false",
    "deepseek0813_responses_store_false_accepted_ignored",
    "deepseek0813_responses_store_true_accepted_ignored",
}



def _parameter_coverage_for_profiles(
    reference_source: str, selected_profiles: list[str], *,
    capability_profile: dict[str, Any] | None = None,
    results: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    return parameter_coverage_for_profiles(reference_param_rows(reference_source), selected_profiles,
                                          capability=capability_profile, results=results)


def _selected_param_profiles(available: list[str]) -> list[str]:
    """Return a source-scoped subset for low-cost targeted retries.

    The filter cannot introduce a profile that is absent from the selected
    official reference contract, so it narrows an authorized matrix without
    weakening the catalog policy gate.
    """
    raw = str(os.getenv("LOADTEST_PARAM_PROFILES") or "").strip()
    if not raw:
        return list(available)
    requested = list(
        dict.fromkeys(item.strip() for item in raw.split(",") if item.strip())
    )
    if not requested:
        raise ValueError("LOADTEST_PARAM_PROFILES did not select any profiles.")
    unknown = [item for item in requested if item not in available]
    if unknown:
        raise ValueError(
            "LOADTEST_PARAM_PROFILES contains profiles outside the selected "
            f"official reference contract: {unknown}."
        )
    selected = [item for item in available if item in set(requested)]
    if not selected:
        raise ValueError("LOADTEST_PARAM_PROFILES did not select any profiles.")
    return selected



def _require_parameter_matrix_enabled(
    capability_profile: dict[str, Any],
    family: str,
    model: str,
) -> None:
    """Enforce catalog test policy before any profile matrix is dispatched."""
    catalog_policy = _catalog_parameter_test_policy(capability_profile)
    disabled = capability_profile.get("parameter_test_enabled") is not True
    policy_disabled = (
        capability_profile.get("test_policy_parameter_test_enabled") is False
        or catalog_policy.get("parameter_test_enabled") is False
    )
    identity_only = (
        str(
            capability_profile.get("test_scope")
            or catalog_policy.get("test_scope")
            or ""
        )
        == "identity_smoke"
    )
    if not (disabled or policy_disabled or identity_only):
        return
    reasons: list[str] = []
    if disabled:
        reasons.append("parameter_test_enabled=false")
    if policy_disabled:
        reasons.append("catalog parameter-test policy=false")
    if identity_only:
        reasons.append("test_scope=identity_smoke")
    raise ValueError(
        f"Text parameter testing is disabled for {family}/{model}: "
        f"{capability_profile.get('disabled_reason') or ', '.join(reasons)}."
    )



def _catalog_parameter_test_policy(
    capability_profile: dict[str, Any],
) -> dict[str, Any]:
    database = capability_profile.get("model_profile_database")
    if not isinstance(database, dict):
        return {}
    direct = database.get("test_policy") or database.get("test_binding")
    if (
        isinstance(direct, dict)
        and direct.get("extension_type") == "model_test_policy"
    ):
        return direct
    interface = database.get("interface")
    if not isinstance(interface, dict):
        return {}
    bindings = interface.get("test_bindings")
    if not isinstance(bindings, list):
        return {}
    expected_id = str(database.get("test_binding_id") or "")
    candidates = [
        item
        for item in bindings
        if isinstance(item, dict)
        and item.get("extension_type") == "model_test_policy"
    ]
    if expected_id:
        for item in candidates:
            if str(item.get("test_binding_id") or "") == expected_id:
                return item
    return candidates[0] if len(candidates) == 1 else {}



def _claim_workflow_dispatch(output_dir: Path, plan: dict[str, Any]) -> None:
    """A frozen functional plan owns one business execution in its report directory."""
    path = output_dir.absolute()
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("Parameter report directory must not traverse a symlink")
    descriptor = os.open(path / "dispatch_started.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"plan_digest": plan["plan_digest"], "target": plan["target"]}, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())


def _registered_workflow_entry(config: dict[str, Any], *, job_spec: dict[str, Any] | None = None, report_dir: Path | None = None) -> int | None:
    """Route registered full suites before legacy executable-preset resolution."""
    from lib.test_runner.service import maybe_preview_test_plan
    job = job_spec if job_spec is not None else load_job_spec(os.getenv("LOADTEST_JOB_SPEC"))
    if job and job.get("execution_plan", {}).get("definition", {}).get("factory", {}).get("factory_id") == "legacy_parameter":
        from lib.test_runner.service import validate_current_parameter_job
        validate_current_parameter_job(config, job)
        config["_test_execution_plan"] = job["execution_plan"]
        config["_test_workflow_job"] = job
        return None
    if job and job.get("execution_plan", {}).get("definition", {}).get("factory", {}).get("factory_id") == "source_fixed_parameter":
        from lib.job_spec import _current_job_snapshot_integrity
        valid, reasons = _current_job_snapshot_integrity(job)
        if not valid:
            raise ValueError("Invalid fixed workflow identity: " + ", ".join(reasons))
        from lib.test_runner.adapters.fixed_parameter import registry_for_fixed_parameter_plan
        registry_for_fixed_parameter_plan(job["execution_plan"])
    if job and "test_workflow_snapshot" not in job:
        if job.get("schema_version") == 6:
            config["_test_execution_plan"] = job["execution_plan"]
        return None
    if job is None:
        # Existing explicit preset selectors keep their documented namespace.
        if os.getenv("LOADTEST_PARAM_PROFILES") or os.getenv("LOADTEST_PARAM_TEST_PROFILES") or os.getenv("LOADTEST_PARAMETER_SUITE"):
            return None
        provider = get_active_provider_name(config)
        model = get_selected_model(config, provider)
        contract = str(os.getenv("LOADTEST_REFERENCE_CONTRACT_ID") or "").strip()
        legacy_contract = str(os.getenv("LOADTEST_REFERENCE_SOURCE") or "").strip()
        if contract and legacy_contract and contract != legacy_contract:
            raise ValueError("Conflicting canonical and legacy reference Contract selectors")
        requested = {
            "provider": provider, "model": model,
            "api_form": os.getenv("LOADTEST_API_FORM") or None,
            "route_profile": os.getenv("LOADTEST_ROUTE_PROFILE") or None,
            "reference_contract_id": contract or legacy_contract or None,
            "workflow_id": os.getenv("LOADTEST_WORKFLOW_ID") or None,
            "suite": os.getenv("LOADTEST_TEST_SUITE") or "full", "runs": _param_test_runs(config),
            "plan_digest": os.getenv("LOADTEST_TEST_PLAN_DIGEST") or None,
        }
        if os.getenv("LOADTEST_TEST_CASES"):
            requested["cases"] = json.loads(os.environ["LOADTEST_TEST_CASES"])
        preview = maybe_preview_test_plan(config, requested)
        if preview is None:
            return None
        job = preview["job_spec"]
    if job.get("execution_plan", {}).get("definition", {}).get("factory", {}).get("factory_id") == "legacy_parameter":
        config["_test_execution_plan"] = job["execution_plan"]
        config["_test_workflow_job"] = job
        return None
    from scripts.workflow_test import execute_job
    output_dir = Path(report_dir or os.getenv("LOADTEST_REPORT_DIR") or _default_report_dir(job["provider"], job["model"]))
    result = execute_job(job, config, output_dir)
    print(json.dumps({"pass": result["pass"], "status": result["workflow_result"]["status"],
                      "report_dir": str(output_dir), "plan_digest": result["workflow_result"]["plan_digest"]}, ensure_ascii=False))
    return 0 if result["pass"] else 1


def _prepare_fixed_parameter_workflow(config, domain, frozen=None):
    """Freeze source bodies and validate the route before constructing credentials."""
    from lib.test_runner.adapters.fixed_parameter import prepare_fixed_parameter_plan, registry_for_fixed_parameter_plan
    from lib.test_runner.fixed_service import validate_fixed_configuration
    validate_fixed_configuration(config, domain)
    expected, _ = prepare_fixed_parameter_plan(domain,
        minimum_output_tokens=configured_parameter_test_min_output_tokens(config))
    if frozen is not None:
        registry_for_fixed_parameter_plan(frozen)
        if frozen != expected:
            raise ValueError("Frozen fixed execution plan differs from its source snapshot, nonces or controls")
        return copy.deepcopy(frozen)
    return expected


def main(*, config: dict[str, Any] | None = None, job_spec: dict[str, Any] | None = None,
         output_dir: Path | None = None) -> int:
    config = copy.deepcopy(config) if config is not None else load_config()
    if job_spec is not None:
        if job_spec.get("schema_version") == 6:
            config.setdefault("api", {})["timeout_sec"] = job_spec["execution_plan"]["limits"]["request_timeout_seconds"]
        config["_parameter_test_exact_input"] = True
        config["_functional_parameter_target"] = {key: job_spec[key] for key in ("provider", "model", "route_profile")}
    workflow_exit = _registered_workflow_entry(config, job_spec=job_spec, report_dir=output_dir)
    if workflow_exit is not None:
        return workflow_exit
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
    job_spec = config.pop("_test_workflow_job", None) or job_spec or load_job_spec(os.getenv("LOADTEST_JOB_SPEC"))
    bind_parameter_execution(config, job_spec, os.environ)
    environment_suite = os.getenv("LOADTEST_PARAMETER_SUITE") or None
    if isinstance(job_spec, dict) and environment_suite not in (None, job_spec.get("parameter_suite")):
        raise ValueError("Parameter suite conflicts with the immutable job selection")
    parameter_suite = (job_spec.get("parameter_suite") if isinstance(job_spec, dict) else environment_suite)
    if parameter_suite is not None:
        fixed_suite_descriptor(parameter_suite)
    job_snapshot = (
        job_spec.get("model_profile_database")
        if isinstance(job_spec, dict)
        else None
    )
    if isinstance(job_snapshot, dict):
        snapshot_binding = binding_from_database_snapshot(job_snapshot)
        target = snapshot_binding.get("execution_target") or {}
        expected_target = {
            "provider_id": provider,
            "request_model_id": model,
            "route_profile": route_profile,
            "api_form": api_form,
        }
        conflicts = [
            field
            for field, expected in expected_target.items()
            if target.get(field) not in (None, "", expected)
        ]
        if conflicts:
            raise ValueError(
                "Parameter runtime conflicts with immutable MPDB snapshot: "
                + ", ".join(conflicts)
            )
        if str(job_snapshot.get("reference_contract_id") or "") != reference_source:
            raise ValueError(
                "Parameter Contract conflicts with immutable MPDB snapshot."
            )
        model_profile_database = copy.deepcopy(job_snapshot)
    else:
        parameter_config = resolve_runtime_parameter_config(
            config,
            provider,
            model,
            family,
            route_profile,
            api_form,
            modality="text",
            contract_id=reference_source,
        )
        model_profile_database = copy.deepcopy(
            parameter_config["model_profile_database"]
        )
        if str(parameter_config.get("contract_id") or "") != reference_source:
            raise ValueError(
                "Selected parameter Contract conflicts with the strict MPDB binding."
            )
    config["_parameter_identity_snapshot"] = copy.deepcopy(model_profile_database)
    catalog_model = str(
        model_profile_database.get("model_slug")
        or ((model_profile_database.get("profile") or {}).get("model_slug"))
        or model
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
        model_profile_database=model_profile_database,
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
    runs = (1 if (api_form == DEEPSEEK_BETA_FORM or parameter_suite) and not bound_parameter_execution(config) and not os.getenv("LOADTEST_PARAM_TEST_RUNS")
            else _param_test_runs(config))
    cache_selected = parameter_suite in CACHE_SUITE_IDS
    if cache_selected:
        from lib.approved_report_retention import cache_report_directory, initialize_cache_report, register_cache_immutable
        frozen_cache = job_spec.get("fixed_parameter_plan") if isinstance(job_spec, dict) else None
        if isinstance(job_spec, dict) and not isinstance(frozen_cache, dict):
            raise ValueError("Cache job snapshot is missing its frozen nonce/body plan")
        output_dir = cache_report_directory(output_dir or os.getenv("LOADTEST_REPORT_DIR"), reports_root=default_reports_root())
        initialize_cache_report(output_dir, parameter_suite, reports_root=default_reports_root())
        if any(output_dir.glob("anthropic_cache_*.json")) or (output_dir / "fixed_parameter_plan.json").exists():
            raise ValueError("Cache job already owns a frozen plan or dispatch ledger; create a new job")
        plan_file_created = False
        try:
            with (output_dir / "fixed_parameter_plan.json").open("xb") as frozen_file:
                plan_file_created = True
                if not isinstance(job_spec, dict):
                    from lib.job_spec import make_job_spec, build_result_validation_contract
                    job_spec = make_job_spec(job_type="param_test", provider=provider, model=model,
                        workload="param_test", request_mode="fixed", target_rpm=0.0, target_tpm=0.0,
                        model_family=family, api_form=api_form, route_profile=route_profile,
                        model_profile_id=str(capability_profile.get("model_api_profile_id") or ""),
                        reference_contract_id=reference_source, model_profile_database=model_profile_database,
                        transport="claude_messages", reference_route_profile="vendor_direct",
                        model_capability_profile=capability_profile,
                        result_contract=build_result_validation_contract(config), parameter_suite=parameter_suite,
                        param_test_runs=runs, tool_validation_mode=_tool_validation_mode(config))
                    bind_parameter_execution(config, job_spec, os.environ)
                    with (output_dir / "job_spec.json").open("xb") as job_file:
                        job_file.write(cache_canonical_bytes(job_spec) + b"\n")
                    register_cache_immutable(output_dir, "job_spec.json", reports_root=default_reports_root())
                    frozen_cache = job_spec["fixed_parameter_plan"]
                fixed_plan = build_fixed_plan(model_profile_database, suite_id=parameter_suite, runs=runs,
                    job_type=job_spec.get("type") if isinstance(job_spec, dict) else "param_test",
                    frozen_plan=frozen_cache)
                frozen_file.write(cache_canonical_bytes(fixed_plan.frozen_payload) + b"\n")
        finally:
            if plan_file_created:
                register_cache_immutable(output_dir, "fixed_parameter_plan.json", reports_root=default_reports_root())
    else:
        fixed_plan = (build_fixed_plan(model_profile_database, suite_id=parameter_suite, runs=runs,
            job_type=job_spec.get("type") if isinstance(job_spec, dict) else "param_test") if parameter_suite else None)
    fim_plan = fixed_plan if parameter_suite == FIM_SUITE_ID else None
    prefill_plan = fixed_plan if parameter_suite in PREFILL_SUITE_IDS else None
    cache_fixed_plan = fixed_plan if cache_selected else None
    if fixed_plan:
        suite_profiles = [r.case_id for r in fixed_plan.requests]
        capability_profile["fixed_parameter_suite"] = parameter_suite
        capability_profile["selected_fixed_case_ids"] = suite_profiles
    if not fixed_plan and api_form != DEEPSEEK_BETA_FORM:
        suite_profiles = _selected_param_profiles(suite_profiles)
        _profile_run_success_modes(config, suite_profiles)
    tool_validation_mode = _tool_validation_mode(config)
    if not cache_selected:
        output_dir = ensure_dir(Path(output_dir or os.getenv("LOADTEST_REPORT_DIR") or _default_report_dir(provider, model)))
    workflow_result: dict[str, Any] = {}
    identity_probes: list[dict[str, Any]] = []
    execution_plan = None
    beta_fixed_plan = None
    if fixed_plan or api_form == DEEPSEEK_BETA_FORM:
        if fixed_plan is None:
            from lib.deepseek_beta_reference import ENDPOINT as BETA_ENDPOINT
            beta_fixed_plan = build_deepseek_beta_reference_plan(model_profile_database, endpoint=BETA_ENDPOINT, runs=runs)
        execution_plan = _prepare_fixed_parameter_workflow(config, fixed_plan or beta_fixed_plan,
            config.get("_test_execution_plan"))
        from lib.test_runner.adapters.fixed_parameter import _kind as fixed_plan_kind
        if any(output_dir.glob(fixed_plan_kind(fixed_plan or beta_fixed_plan) + "_*.json")) or (output_dir / "workflow_runs").exists():
            raise ValueError("The fixed report directory already contains an execution ledger")
    else:
        from lib.test_runner.adapters.legacy_parameter import prepare_legacy_parameter_plan, legacy_parameter_registry
        from lib.test_runner import validate_plan
        execution_plan = config.get("_test_execution_plan")
        if execution_plan is None:
            execution_plan, _ = prepare_legacy_parameter_plan(
                config, provider, model, family, reference_source, reference_family,
                runs=runs, profiles=suite_profiles, capability_profile=capability_profile,
                runner_module=sys.modules[__name__],
            )
        else:
            execution_plan = validate_plan(execution_plan, legacy_parameter_registry(config, runner_module=sys.modules[__name__]))
            suite_profiles = [step["inputs"]["profile"] for step in execution_plan["ordered_steps"] if step["handler"] == "legacy.parameter_profile.v1"]

    beta_observations = None
    fim_observations = None
    prefill_observations = None
    cache_observations = None
    if execution_plan is not None:
        if job_spec and job_spec.get("schema_version") == 6:
            capability_profile = copy.deepcopy(job_spec["model_capability_profile"])
        if not fixed_plan and beta_fixed_plan is None:
            _claim_workflow_dispatch(output_dir, execution_plan)
    try:
        client = DeepSeekClient.from_config(config, provider)
        if cache_fixed_plan:
            identity_probe = None
            results, cache_observations = run_anthropic_cache_params(config, client, cache_fixed_plan, output_dir, execution_plan=execution_plan)
        elif prefill_plan:
            identity_probe = None
            results, prefill_observations = run_anthropic_prefill_params(config, client, prefill_plan, output_dir, execution_plan=execution_plan)
        elif fim_plan:
            identity_probe = None
            results, fim_observations = run_deepseek_fim_params(config, client, fim_plan, output_dir, execution_plan=execution_plan)
        elif api_form == DEEPSEEK_BETA_FORM:
            identity_probe = None
            results, beta_observations = run_deepseek_beta_params(config, client, beta_fixed_plan, output_dir, execution_plan=execution_plan)
        else:
            results = run_param_tests(
                config, client, provider, model, family, reference_source, reference_family,
                runs, output_dir, capability_profile=capability_profile,
                execution_plan=execution_plan, workflow_report=workflow_result,
            )
            identity_probes = list(workflow_result.get("identity_probes") or [])
            identity_probe = workflow_result.get("identity_probe")
        fixed_observations = cache_observations or prefill_observations or fim_observations or beta_observations
        if fixed_observations:
            workflow_result = copy.deepcopy(fixed_observations["workflow_result"])
    except Exception as exc:
        identity_probes = list(workflow_result.get("identity_probes") or [])
        identity_probe = workflow_result.get("identity_probe")
        results = list(workflow_result.get("legacy_results") or []) or [
            {
                "name": "param_test:init",
                "status": "fail",
                "pass": False,
                "compatibility_status": "fail",
                "compatibility_pass": False,
                "token_validation_status": "not_applicable",
                "token_validation_pass": True,
                "overall_status": "fail",
                "overall_pass": False,
                "expectation": "supported",
                "failure_classification": exc.__class__.__name__,
                "message": str(exc),
                "token_audit": combine_exchange_audits([]),
                "model_identity_audit": combine_model_identity_audits([]),
            }
        ]

    tested_params, untested_params = _parameter_coverage_for_profiles(
        reference_source, suite_profiles, capability_profile=capability_profile, results=results
    )
    if fixed_plan:
        fixed_specs = fixed_spec_payload(fixed_plan)
        tested_params, untested_params = fixed_specs["tested_params"], fixed_specs["untested_params"]
    failed = [item for item in results if item.get("status") == "fail"]
    incompatible = [item for item in results if item.get("status") == "incompatible"]
    unexpected_acceptance = [
        item for item in results if item.get("status") == "unexpected_acceptance"
    ]
    expected_rejection = [
        item for item in results if item.get("status") == "expected_rejection"
    ]
    passed = [item for item in results if item.get("status") == "pass"]
    token_failures = [
        item for item in results if item.get("token_validation_pass") is False
    ]
    overall_failures = [
        item
        for item in results
        if item.get("overall_pass", item.get("pass")) is not True
    ]
    overall_passed = len(results) - len(overall_failures)
    total = len(results)
    token_audit_results = (
        [*identity_probes, *results] if identity_probes else [identity_probe, *results] if isinstance(identity_probe, dict) else results
    )
    if cache_fixed_plan:
        from lib.anthropic_cache_reference import generation_token_audit_results
        token_audit_results = generation_token_audit_results(cache_fixed_plan, token_audit_results)
    token_audit_summary = summarize_token_audits(token_audit_results)
    model_identity_summary = summarize_model_identity_audits(token_audit_results) if workflow_result else summarize_model_identity_audits(results, identity_probe)
    compatibility_pass = compatibility_pass_from_statuses(
        [str(item.get("status") or "fail") for item in results]
    )
    if api_form == DEEPSEEK_BETA_FORM:
        compatibility_pass = bool(beta_observations and beta_observations.get("pass"))
    if fim_plan:
        compatibility_pass = bool(fim_observations and fim_observations.get("pass"))
    if cache_fixed_plan:
        compatibility_pass = bool(cache_observations and cache_observations.get("pass"))
    token_validation_pass = bool(token_audit_summary.get("pass", False))
    token_accuracy_pass = token_validation_pass
    model_identity_pass = bool(model_identity_summary.get("pass", True))
    certification_scope = str(
        reference.get("certification_scope")
        or capability_profile.get("certification_scope")
        or "raw_route_contract"
    )
    adapter_pass = compatibility_pass and token_accuracy_pass and model_identity_pass
    if workflow_result:
        adapter_pass = adapter_pass and workflow_result.get("status") == "passed"
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
        "token_validation_pass": token_validation_pass,
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
        "reference_contract_id": reference_source,
        "reference_label": reference["label"],
        "reference_family": reference_family,
        "official_sources": reference["official_sources"],
        "model_capability_profile": capability_profile,
        "model_profile_database": model_profile_database,
        "tested_params": tested_params,
        "untested_params": untested_params,
        "param_test_runs": runs,
        "tool_validation_mode": tool_validation_mode,
        "total": total,
        "passed": compatibility_ok_count,
        "overall_passed": overall_passed,
        "passed_supported": len(passed),
        "expected_rejection": len(expected_rejection),
        "incompatible": len(incompatible),
        "unexpected_acceptance": len(unexpected_acceptance),
        "failed": len(overall_failures),
        "overall_failed": len(overall_failures),
        "compatibility_failed": len(failed),
        "token_failed": len(token_failures),
        "overall_success_rate": overall_passed / total if total else 0.0,
        "compatibility_success_rate": compatibility_ok_count / total if total else 0.0,
        "performance_summary": _performance_summary(results),
        "token_audit_summary": token_audit_summary,
        "model_identity_summary": model_identity_summary,
        "identity_probe": identity_probe,
        "identity_probes": identity_probes,
        "workflow_result": workflow_result,
        "incompatibilities": incompatible,
        "unexpected_acceptances": unexpected_acceptance,
        "expected_rejections": expected_rejection,
        "failures": overall_failures,
        "compatibility_failures": failed,
        "token_failures": token_failures,
        "param_specs": model_reference_spec_payload(
            "text",
            family,
            catalog_model,
            reference_source,
            api_form=api_form,
            route_profile=route_profile,
        ),
    }
    if api_form == DEEPSEEK_BETA_FORM:
        verdict["bounded_beta_observations"] = beta_observations
        verdict["planned_requests"] = len(DEEPSEEK_BETA_CASES)
        verdict["identity_probe_requests"] = 0
        verdict["full_parameter_matrix_verified"] = False
    if fim_plan:
        verdict.update(parameter_suite=parameter_suite, bounded_fim_observations=fim_observations,
                       planned_requests=8, identity_probe_requests=0, full_parameter_matrix_verified=False,
                       param_specs=fixed_specs)
    if prefill_plan:
        verdict.update(parameter_suite=parameter_suite, bounded_prefill_observations=prefill_observations,
                       planned_requests=3, identity_probe_requests=0, full_parameter_matrix_verified=False,
                       param_specs=fixed_specs)
    if cache_fixed_plan:
        verdict.update(parameter_suite=parameter_suite, bounded_cache_observations=cache_observations,
            fixed_parameter_plan_digest=cache_fixed_plan.plan_digest, planned_requests=cache_fixed_plan.request_cap,
            planned_count_requests=sum(r.kind == "count" for r in cache_fixed_plan.requests), planned_generation_requests=3,
            count_requests=(cache_observations or {}).get("count_requests_sent", 0),
            generation_requests=(cache_observations or {}).get("generation_requests_sent", 0),
            identity_probe_requests=0, full_parameter_matrix_verified=False,
            count_precision=("not_requested" if cache_fixed_plan.schema_version == 2 else "official_estimate"),
            token_exact_proof=False, param_specs=fixed_specs)
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
    if cache_fixed_plan:
        from lib.approved_report_retention import finalize_cache_report
        finalize_cache_report(output_dir, reports_root=default_reports_root(), include_job_log=False)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if verdict["pass"] else 1


def run_deepseek_beta_params(config, client, plan, output_dir, *, execution_plan=None):
    return _run_fixed_parameter_params(config, client, plan, output_dir, execution_plan=execution_plan, is_fim=False)


def run_deepseek_fim_params(config, client, plan, output_dir, *, execution_plan=None):
    return _run_fixed_parameter_params(config, client, plan, output_dir, execution_plan=execution_plan, is_fim=True)


def run_anthropic_prefill_params(config, client, plan, output_dir, *, execution_plan=None):
    return _run_fixed_parameter_params(config, client, plan, output_dir, execution_plan=execution_plan, is_fim=False, is_prefill=True)


def run_anthropic_cache_params(config, client, plan, output_dir, *, execution_plan=None):
    return _run_fixed_parameter_params(config, client, plan, output_dir, execution_plan=execution_plan, is_fim=False, is_cache=True)


def _run_fixed_parameter_params(config, client, plan, output_dir, *, is_fim, is_prefill=False, is_cache=False, execution_plan=None):
    from lib.test_runner.fixed_results import run_fixed_parameter_params
    execute = execute_cache_plan if is_cache else execute_prefill_plan if is_prefill else execute_fim_plan if is_fim else execute_deepseek_beta_plan
    return run_fixed_parameter_params(config, client, plan, output_dir, runner_module=sys.modules[__name__],
        execution_plan=execution_plan, execute=execute)


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
    execution_plan: dict[str, Any] | None = None,
    workflow_report: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    from lib.test_runner.adapters.legacy_parameter import run_legacy_parameter_tests
    return run_legacy_parameter_tests(
        config, client, provider, model, family, reference_source, reference_family,
        runs=runs, output_dir=output_dir, capability_profile=capability_profile,
        runner_module=sys.modules[__name__], execution_plan=execution_plan, workflow_report=workflow_report,
    )


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


def prepare_identity_probe_request(
    config: dict[str, Any], provider: str, model: str, family: str,
    reference_source: str, reference_family: str, *, route_profile_override: str | None = None,
) -> BuiltRequest:
    """Prepare the exact identity envelope without credentials or network calls."""
    prompt = "Reply with OK."
    reference = get_reference_source(reference_source)
    minimum_output_tokens = configured_parameter_test_min_output_tokens(config)
    api_form = str(reference.get("api_form") or "openai_chat_completions")
    reference_route_profile = str(
        reference.get("route_profile") or ""
    )
    route_profile = get_model_route_profile(config, model, provider, route_profile=route_profile_override)
    api_form = get_model_api_form(
        config,
        model,
        provider,
        route_profile=route_profile,
        api_form=api_form,
    )
    if api_form == "openai_responses":
        transport = "openai_responses"
        body = {
            "model": model,
            "input": prompt,
            "max_output_tokens": minimum_output_tokens,
            "stream": False,
        }
    elif api_form == "anthropic_messages":
        transport = "claude_messages"
        body = {
            "model": model,
            "max_tokens": minimum_output_tokens,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }
    elif api_form == "gemini_generate_content":
        transport = "gemini_generate_content"
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": minimum_output_tokens},
        }
    elif api_form == "openai_fim_completions_beta":
        transport = "fim_completions"
        body = {
            "model": model,
            "prompt": prompt,
            "suffix": "\n",
            "max_tokens": minimum_output_tokens,
            "stream": False,
        }
    elif api_form == "gemini_interactions":
        transport = "gemini_interactions"
        body = {
            "model": model,
            "input": prompt,
            "generation_config": {"max_output_tokens": minimum_output_tokens},
            "stream": False,
            "store": False,
        }
    elif api_form == "openai_chat_completions":
        transport = "chat_completions"
        output_key = _preferred_chat_output_limit_field(reference)
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            output_key: minimum_output_tokens,
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
        elif reference_source == "openai_gpt6_astra_chat":
            body["reasoning_effort"] = "low"
    else:
        raise ValueError(f"Unsupported identity-probe API form: {api_form!r}")
    output_token_limits = enforce_parameter_test_output_limit(
        body,
        transport,
        minimum=configured_parameter_test_output_budget(config, body),
        chat_completion_field=_preferred_chat_output_limit_field(reference),
    )
    return BuiltRequest("identity_probe", "identity_probe", body, metadata={
        "transport": transport, "api_form": api_form, "route_profile": route_profile,
        "reference_route_profile": reference_route_profile,
        "minimum_output_tokens": minimum_output_tokens, "output_token_limits": output_token_limits,
        "request_endpoint": str(get_provider_interface(config, transport, provider).get("path") or ""),
    })



def run_identity_probe(
    config: dict[str, Any],
    client: DeepSeekClient,
    provider: str,
    model: str,
    family: str,
    reference_source: str,
    reference_family: str,
    prepared_request: BuiltRequest | None = None,
) -> dict[str, Any]:
    reference = get_reference_source(reference_source)
    if prepared_request is None:
        prepared_request = prepare_identity_probe_request(
            config, provider, model, family, reference_source, reference_family,
        )
    if not isinstance(prepared_request, BuiltRequest) or prepared_request.group != "identity_probe" or prepared_request.profile != "identity_probe":
        raise ValueError("Frozen identity request does not match its identity-probe profile")
    built = copy.deepcopy(prepared_request)
    body = built.body
    transport = built.metadata["transport"]
    api_form = built.metadata["api_form"]
    route_profile = built.metadata["route_profile"]
    reference_route_profile = built.metadata["reference_route_profile"]
    minimum_output_tokens = built.metadata["minimum_output_tokens"]
    output_token_limits = built.metadata["output_token_limits"]
    checked = copy.deepcopy(body)
    enforce_parameter_test_output_limit(
        checked, transport, minimum=configured_parameter_test_output_budget(config, checked),
        chat_completion_field=_preferred_chat_output_limit_field(reference),
    )
    if checked != body:
        raise ValueError("Frozen identity request would change during execution")
    request_snapshot = copy.deepcopy(body)
    if transport == "openai_responses":
        result = client.openai_responses(body)
    elif transport == "claude_messages":
        result = client.claude_messages(body)
    elif transport == "gemini_generate_content":
        result = client.gemini_generate_content(model, body)
    elif transport == "fim_completions":
        result = client.fim_completion(body)
    elif transport == "gemini_interactions":
        result = client.gemini_interactions(body)
    else:
        result = client.chat_completion(body)
    observed_request_body = copy.deepcopy(body)
    body = request_snapshot
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
                accounting_source_id=str(reference.get("source_id") or ""),
                accounting_contract_id=reference_source,
                independent_input_count=_count_input_safely(
                    client, transport, model, body
                ),
                observed_request_body=observed_request_body,
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
                model_profile_database=config.get("_parameter_identity_snapshot"),
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
        "minimum_output_tokens": minimum_output_tokens,
        "output_token_limits": output_token_limits,
        "request_body": body,
        "response_json": result.response_json,
        "status_code": result.status_code,
        "usage": result.usage,
        "token_audit": token_audit,
        "token_validation_status": token_audit.get("validation_status"),
        "token_validation_pass": bool(token_audit.get("validation_pass", False)),
        "overall_pass": bool(result.success)
        and bool(token_audit.get("validation_pass", False)),
        "model_identity_audit": identity_audit,
        "response_model": result.response_json.get("model")
        or result.response_json.get("modelVersion"),
    }


def _responses_reasoning_tokens(response_json: dict[str, Any]) -> int | None:
    usage = response_json.get("usage")
    if not isinstance(usage, dict):
        return None
    details = usage.get("output_tokens_details")
    if not isinstance(details, dict):
        return None
    value = details.get("reasoning_tokens")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _responses_reasoning_items(response_json: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in response_json.get("output") or []
        if isinstance(item, dict) and item.get("type") == "reasoning"
    ]


def _responses_has_output_logprobs(response_json: dict[str, Any]) -> bool:
    for item in response_json.get("output") or []:
        if not isinstance(item, dict):
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("logprobs"):
                return True
    return False


def _validate_deepseek0813_responses_shape(
    profile: str,
    response_json: dict[str, Any],
    result: Any,
    request_body: dict[str, Any],
) -> str | None:
    output = response_json.get("output")
    if not isinstance(output, list) or not output:
        return "responses_output_missing"
    if not isinstance(result.usage, dict) or not result.usage:
        return "responses_usage_missing"

    # The non-streaming API exposes these documented fixed compatibility
    # values. The local SSE reducer intentionally keeps only output/usage, so
    # do not claim a fixed-field failure when those terminal fields were not
    # retained by the streaming transport.
    if request_body.get("stream") is not True:
        if response_json.get("store") is not False:
            return "responses_fixed_field_mismatch"
        if response_json.get("parallel_tool_calls") is not True:
            return "responses_fixed_field_mismatch"
        if (
            "previous_response_id" not in response_json
            or response_json.get("previous_response_id") is not None
        ):
            return "responses_fixed_field_mismatch"

    reasoning_tokens = _responses_reasoning_tokens(response_json)
    reasoning_items = _responses_reasoning_items(response_json)
    request_reasoning = request_body.get("reasoning")
    requested_effort = (
        str(request_reasoning.get("effort") or "").casefold()
        if isinstance(request_reasoning, dict)
        else ""
    )
    if requested_effort == "none":
        if (reasoning_tokens or 0) > 0 or reasoning_items:
            return "responses_reasoning_none_mismatch"
    if profile in _DEEPSEEK_0813_RESPONSES_REASONING_PROFILES:
        if not ((reasoning_tokens or 0) > 0 or reasoning_items):
            return "responses_reasoning_missing"
    if profile == "deepseek0813_responses_reasoning_summary_accepted_ignored":
        if any(item.get("summary") for item in reasoning_items):
            return "responses_reasoning_summary_unexpected"
    if (
        profile == "deepseek0813_responses_top_logprobs"
        and not _responses_has_output_logprobs(response_json)
    ):
        return "responses_logprobs_missing"
    if (
        profile in _DEEPSEEK_0813_RESPONSES_STORE_PROFILES
        and response_json.get("store") is not False
    ):
        return "responses_fixed_field_mismatch"
    if (
        profile == "deepseek0813_responses_parallel_tool_calls_accepted_ignored"
        and response_json.get("parallel_tool_calls") is not True
    ):
        return "responses_fixed_field_mismatch"
    return None


def _validate_deepseek0813_profile_response(
    profile: str,
    response_json: dict[str, Any],
    result: Any,
    *,
    request_body: dict[str, Any],
    transport: str,
    tool_validation_mode: str,
    reference_source: str,
) -> str | None:
    """Apply protocol-shape checks for the source-scoped 0813 profiles.

    The shared validator deliberately owns generic profile names.  These
    route-specific names stay local to the parameter runner while delegating
    their tool-call structure checks to the existing protocol validators.
    """
    for prefix, contract, expected_transport in (
        ("deepseek0813_responses_", "deepseek_v4_pro_0813_responses", "openai_responses"),
        ("deepseek0813_anthropic_", "deepseek_v4_pro_0813_anthropic", "claude_messages"),
        ("deepseek0813_fim_", "deepseek_v4_pro_0813_fim_beta", "fim_completions"),
    ):
        if profile.startswith(prefix) and (
            reference_source != contract or transport != expected_transport
        ):
            return "deepseek0813_contract_mismatch"
    if profile.startswith("deepseek0813_responses_"):
        shape_error = _validate_deepseek0813_responses_shape(
            profile,
            response_json,
            result,
            request_body,
        )
        if shape_error:
            return shape_error
    if profile == "deepseek0813_responses_text_json":
        return validate_profile_response(
            "openai_responses_json",
            response_json,
            result,
            request_body=request_body,
            transport=transport,
            tool_validation_mode=tool_validation_mode,
            reference_source=reference_source,
        )
    if profile in _DEEPSEEK_0813_RESPONSES_REQUIRED_TOOL_PROFILES:
        return validate_profile_response(
            "openai_responses_tools",
            response_json,
            result,
            request_body=request_body,
            transport=transport,
            tool_validation_mode=tool_validation_mode,
            reference_source=reference_source,
        )
    if profile == "deepseek0813_responses_tool_choice_none":
        if extract_openai_responses_function_calls(response_json):
            return "tool_calls_unexpected"
        return None
    if profile.startswith("deepseek0813_anthropic_"):
        if not isinstance(response_json.get("content"), list) or not response_json["content"]:
            return "anthropic_content_missing"
        if not isinstance(result.usage, dict) or not result.usage:
            return "anthropic_usage_missing"
    if profile in _DEEPSEEK_0813_ANTHROPIC_TOOL_PROFILES:
        return validate_profile_response(
            "claude_native_tools",
            response_json,
            result,
            request_body=request_body,
            transport=transport,
            tool_validation_mode=tool_validation_mode,
            reference_source=reference_source,
        )
    if profile == "deepseek0813_anthropic_tool_choice_none":
        if extract_claude_tool_uses(response_json):
            return "tool_calls_unexpected"
        return None
    return _validate_deepseek0813_fim_response(profile, response_json, result)


def _validate_deepseek0813_fim_response(
    profile: str,
    response_json: dict[str, Any],
    result: Any,
) -> str | None:
    """Validate FIM completion evidence after the runner's source/form checks."""
    if not profile.startswith("deepseek0813_fim_"):
        return None

    choices = response_json.get("choices") or []
    if (
        not choices
        or not isinstance(choices[0], dict)
        or not isinstance(choices[0].get("text"), str)
        or not choices[0]["text"].strip()
    ):
        return "fim_completion_missing"
    if (
        profile == "deepseek0813_fim_logprobs"
        and choices[0].get("logprobs") is None
    ):
        return "logprobs_missing"
    if profile == "deepseek0813_fim_stream_usage" and not result.usage:
        return "stream_usage_missing"
    return None



def prepare_one_profile_request(
    config: dict[str, Any], model: str, family: str, reference_source: str,
    profile: str, input_sample: dict[str, str], expectation: str,
    capability_profile: dict[str, Any] | None = None,
    prepared_request: BuiltRequest | None = None,
    api_form_override: str | None = None,
    provider_override: str | None = None,
    execution_route_override: str | None = None,
) -> tuple[BuiltRequest, dict[str, int]]:
    """Build/finalize once at compilation, or validate a frozen request unchanged."""
    reference = get_reference_source(reference_source)
    api_form = str(api_form_override or reference.get("api_form") or "openai_chat_completions")
    reference_route_profile = str(reference.get("route_profile") or "")
    parameter = parameter_label_for_profile(reference_source, profile)
    expected = expectation
    transport = "chat_completions"
    selected_reference_capability = capability_profile
    if selected_reference_capability is None and family == "kimi" and profile == "sampling_non_thinking":
        selected_reference_capability = load_model_capability_profile(
            "text", family, model, api_form=api_form,
            route_profile=reference_route_profile, reference_source=reference_source,
        )
    if prepared_request is not None:
        if not isinstance(prepared_request, BuiltRequest) or prepared_request.group != "compatibility_profiles" or prepared_request.profile != profile:
            raise ValueError("Frozen parameter request does not match its profile")
        built = copy.deepcopy(prepared_request)
        frozen_body = copy.deepcopy(built.body)
    else:
        built = build_request(
            config,
            "compatibility_profiles",
            profile,
            overrides=(
                {"model": model}
                if input_sample["id"].startswith("profile_defined:")
                else {"model": model, "prompt": input_sample["prompt"]}
            ),
            model_family_override=family,
            api_form_override=api_form,
            route_profile_override=reference_route_profile,
            reference_source=reference_source,
            enforce_model_capabilities=False,
            parameter_reference_profile=selected_reference_capability,
            parameter_test=True,
            provider_override=provider_override,
            execution_route_override=execution_route_override,
        )
    source_probe_policy = built.metadata.get("parameter_reference_policy") or {}
    if source_probe_policy:
        expected = source_probe_policy["expectation"]
        parameter = source_probe_policy["target_parameter"]
    transport = str(built.metadata.get("transport") or transport)
    output_token_limits = enforce_parameter_test_output_limit(
        built.body,
        transport,
        minimum=configured_parameter_test_output_budget(
            config, built.body,
            preserve_declared_limit=parameter_targets_output_limit(parameter, profile=profile),
            reasoning_expected=_input_group_for_profile(config, profile) == "reasoning",
        ),
        chat_completion_field=_preferred_chat_output_limit_field(reference),
    )
    validate_reference_request(built.body, source_probe_policy)
    stop_request_error = validate_parameter_stop_request(profile, built.body)
    if stop_request_error:
        raise ValueError(stop_request_error)
    if (
        expected == "supported"
        and transport in {"chat_completions", "fim_completions"}
        and built.body.get("stream") is True
    ):
        stream_options = built.body.setdefault("stream_options", {})
        if not isinstance(stream_options, dict):
            raise ValueError("stream_options must be an object for token usage capture")
        stream_options["include_usage"] = True
    if prepared_request is not None and json.dumps(built.body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) != json.dumps(frozen_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False):
        raise ValueError("Frozen parameter request would change during execution")
    return built, output_token_limits



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
    capability_profile: dict[str, Any] | None = None,
    prepared_request: BuiltRequest | None = None,
    route_profile_override: str | None = None,
) -> dict[str, Any]:
    name = f"{provider}:{model}:{reference_source}:{profile}:run_{run_index}"
    parameter = parameter_label_for_profile(reference_source, profile)
    expected = expectation or resolve_profile_expectation(
        "text",
        family,
        model,
        profile,
        capability_profile=capability_profile,
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
    route_profile = get_model_route_profile(config, model, provider, route_profile=route_profile_override)
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
    minimum_output_tokens = configured_parameter_test_min_output_tokens(config)
    output_token_limits: dict[str, int] = {}

    try:
        built, output_token_limits = prepare_one_profile_request(
            config, model, family, reference_source, profile, input_sample, expected,
            capability_profile=capability_profile, prepared_request=prepared_request,
            api_form_override=api_form,
            provider_override=provider,
            execution_route_override=route_profile,
        )
        source_probe_policy = built.metadata.get("parameter_reference_policy") or {}
        if source_probe_policy:
            expected = source_probe_policy["expectation"]
            parameter = source_probe_policy["target_parameter"]
        transport = str(built.metadata.get("transport") or transport)
        request_endpoint = str(built.metadata.get("request_endpoint") or request_endpoint)
        request_snapshot = copy.deepcopy(built.body)
        request_body = request_snapshot
        if transport == "gemini_interactions":
            result = client.gemini_interactions(built.body)
        elif transport == "gemini_generate_content":
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
        elif transport == "fim_completions":
            result = client.fim_completion(built.body)
        elif transport == "chat_completions":
            result = client.chat_completion(built.body)
        else:
            raise ValueError(
                f"Unsupported parameter-test transport: {transport!r}."
            )
        observed_request_body = copy.deepcopy(built.body)
        built.body = request_snapshot
        exchange_audits = [
            _audit_exchange_safely(
                config,
                built.body,
                result,
                transport,
                "initial",
                provider=provider,
                model=model,
                accounting_source_id=str(reference.get("source_id") or ""),
                accounting_contract_id=reference_source,
                independent_input_count=_count_input_safely(
                    client, transport, model, built.body
                ),
                observed_request_body=observed_request_body,
            )
        ]
        identity_audits = [
            audit_model_identity(
                requested_model=model,
                result=result,
                transport=transport,
                provider_cfg=provider_cfg,
                model_profile_database=config.get("_parameter_identity_snapshot"),
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
            tool_validation_mode=_tool_validation_mode(config),
            reference_source=reference_contract_source,
            request_context=(
                {"requested_model": model, "request_url": client._gemini_native_url(model)}
                if result.success and reference_contract_source == "gemini_3_7_flash_generate_content"
                and transport == "gemini_generate_content"
                and callable(getattr(client, "_gemini_native_url", None)) else
                {"requested_model": model, "request_url": client._transport_url("gemini_interactions")}
                if result.success and reference_contract_source == "gemini_3_7_flash_interactions"
                and transport == "gemini_interactions"
                and callable(getattr(client, "_transport_url", None)) else None
            ),
        )
        if validation_error is None:
            validation_error = _validate_deepseek0813_profile_response(
                profile,
                result.response_json,
                result,
                request_body=built.body,
                transport=transport,
                tool_validation_mode=_tool_validation_mode(config),
                reference_source=reference_contract_source,
            )
        source_validation_error = validate_reference_response(
            source_probe_policy, result.response_json, result.status_code
        )
        if source_validation_error:
            validation_error = source_validation_error
        validation_ok = bool(result.success) and validation_error is None
        # Unsupported probes only care about HTTP rejection vs acceptance.
        outcome = map_probe_outcome(
            expected,
            status_code=result.status_code,
            validation_ok=True if expected == "unsupported" else validation_ok,
        )
        if (expected == "unsupported" and profile in REJECTION_PROFILES
                and reference_contract_source == "gemini_3_7_flash_interactions"
                and transport == "gemini_interactions" and outcome["status"] == "expected_rejection"):
            context = (
                {"requested_model": model, "request_url": client._transport_url("gemini_interactions")}
                if callable(getattr(client, "_transport_url", None)) else None
            )
            rejection_error = validate_interactions_rejection(
                profile, result.response_json, result.status_code, body=built.body,
                transport=transport, reference_source=reference_contract_source, request_context=context,
            )
            validation_error = rejection_error
            if rejection_error:
                outcome.update(status="incompatible", **{"pass": False, "compatibility_ok": False})
        if (source_probe_policy and expected == "unsupported"
                and result.status_code in {400, 422} and source_validation_error):
            outcome.update(status="incompatible", **{"pass": False, "compatibility_ok": False})
        if (expected == "unsupported" and profile == "claude_thinking_adaptive"
                and reference_contract_source == "claude_openai_compat"
                and transport == "chat_completions" and result.status_code in {400, 422}):
            from lib.anthropic_compat_rejections import claude_compat_adaptive_rejection_matches
            if not claude_compat_adaptive_rejection_matches(result.response_json, built.body):
                validation_error = "claude_compat_adaptive_rejection_unattributed"
                outcome.update(status="incompatible", **{"pass": False, "compatibility_ok": False})
        if (expected == "unsupported" and profile in {"claude_sampling", "claude_top_p"}
                and reference_contract_source == "claude_openai_compat"
                and transport == "chat_completions" and result.status_code in {400, 422}):
            from lib.anthropic_compat_rejections import claude_compat_sampling_rejection_matches
            if not claude_compat_sampling_rejection_matches(profile, result.response_json, built.body):
                validation_error = "claude_compat_sampling_rejection_unattributed"
                outcome.update(status="incompatible", **{"pass": False, "compatibility_ok": False})
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
            and exchange_audits[0].get("validation_pass") is True
            and built.metadata.get("multi_turn")
        ):
            if transport == "gemini_generate_content":
                followup_body = build_native_tool_followup_request(
                    built.body,
                    result.response_json,
                )
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
            elif transport == "chat_completions":
                followup_body = build_tool_followup_request(
                    built.body,
                    result.response_json,
                    pass_reasoning_content=bool(built.metadata.get("pass_reasoning_content")),
                )
            else:
                raise ValueError(
                    "Unsupported multi-turn parameter-test transport: "
                    f"{transport!r}."
                )
            enforce_parameter_test_output_limit(
                followup_body,
                transport,
                minimum=configured_parameter_test_output_budget(config, followup_body),
                chat_completion_field=_preferred_chat_output_limit_field(reference),
            )
            followup_snapshot = copy.deepcopy(followup_body)
            if transport == "gemini_generate_content":
                followup = client.gemini_generate_content(model, followup_body)
            elif transport == "claude_messages":
                followup = client.claude_messages(followup_body)
            elif transport == "openai_responses":
                followup = client.openai_responses(followup_body)
            else:
                followup = client.chat_completion(followup_body)
            observed_followup_body = copy.deepcopy(followup_body)
            followup_body = followup_snapshot
            followup_request_body = followup_body
            followup_response_json = followup.response_json
            exchange_audits.append(
                _audit_exchange_safely(
                    config,
                    followup_body,
                    followup,
                    transport,
                    "followup",
                    provider=provider,
                    model=model,
                    accounting_source_id=str(reference.get("source_id") or ""),
                    accounting_contract_id=reference_source,
                    independent_input_count=_count_input_safely(
                        client, transport, model, followup_body
                    ),
                    observed_request_body=observed_followup_body,
                )
            )
            identity_audits.append(
                audit_model_identity(
                    requested_model=model,
                    result=followup,
                    transport=transport,
                    provider_cfg=provider_cfg,
                    model_profile_database=config.get("_parameter_identity_snapshot"),
                    exchange="followup",
                    request_endpoint=request_endpoint,
                )
            )
            followup_error = validate_tool_followup_response(
                followup.response_json,
                followup,
                transport=transport,
                tool_validation_mode=_tool_validation_mode(config),
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
        token_audit = combine_exchange_audits(exchange_audits)
        performance_metrics = _performance_metrics(
            result,
            transport,
            usage_accounting=(exchange_audits[0].get("usage_accounting") or {}),
        )
        token_validation_pass = bool(token_audit.get("validation_pass", False))
        token_validation_status = str(
            token_audit.get("validation_status") or "not_available"
        )
        payload = {
            "name": name,
            "profile": profile,
            "parameter": parameter,
            "expectation": expected,
            "run_index": run_index,
            "status": status,
            "pass": passed,
            "compatibility_status": status,
            "compatibility_pass": passed,
            "token_validation_status": token_validation_status,
            "token_validation_pass": token_validation_pass,
            "token_validation_failures": list(
                token_audit.get("validation_failures") or []
            ),
            "overall_status": (
                status
                if not passed
                else "pass"
                if token_validation_pass
                else "token_validation_failed"
            ),
            "overall_pass": passed and token_validation_pass,
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
            "minimum_output_tokens": minimum_output_tokens,
            "output_token_limits": output_token_limits,
            "request_body": built.body,
            "response_json": result.response_json,
            "followup_request_body": followup_request_body,
            "followup_response_json": followup_response_json,
            "input_sample": input_sample["id"],
            "tool_validation_mode": _tool_validation_mode(config),
            "status_code": reported_status_code,
            "latency_ms": result.latency_ms,
            "ttft_ms": result.ttft_ms,
            "tpot_ms": performance_metrics.get("tpot_ms"),
            "throughput_output_tokens_per_sec": performance_metrics.get("throughput_output_tokens_per_sec"),
            "throughput_total_tokens_per_sec": performance_metrics.get("throughput_total_tokens_per_sec"),
            "performance_metrics": performance_metrics,
            "finish_reason": result.finish_reason,
            "usage": result.usage,
            "token_audit": token_audit,
            "model_identity_audit": combine_model_identity_audits(identity_audits),
            "warnings": built.warnings,
            **({"parameter_reference_policy": source_probe_policy} if source_probe_policy else {}),
            "response_model": result.response_json.get("model")
            or result.response_json.get("modelVersion"),
            "failure_classification": failure_classification,
            "failure_reason": reason,
            "failure_detail": failure_detail,
            "reason": reason,
            "message": None if passed else reported_message,
        }
        if passed and not token_validation_pass:
            token_reason = "; ".join(
                str(item) for item in token_audit.get("validation_failures") or []
            ) or "required input/output token validation failed"
            payload.update(
                {
                    "failure_classification": "token_validation_failed",
                    "failure_reason": token_reason,
                    "reason": token_reason,
                    "message": token_reason,
                    "failure_detail": {
                        "failure_reason": token_reason,
                        "failed_check": "token_validation",
                        "failed_item": "usage input/output token counts",
                        "expected": "valid input/output token counts within the declared tolerance and a complete output",
                        "actual": token_audit,
                        "expectation": expected,
                    },
                    "failed_check": "token_validation",
                    "failed_item": "usage input/output token counts",
                    "expected": "valid input/output token counts and a complete output",
                    "actual": token_audit,
                }
            )
        if failure_detail:
            payload.update(
                {
                    "failed_check": failure_detail.get("failed_check"),
                    "failed_item": failure_detail.get("failed_item"),
                    "expected": failure_detail.get("expected"),
                    "actual": failure_detail.get("actual"),
                }
            )
        if not payload["overall_pass"]:
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
            "compatibility_status": "fail",
            "compatibility_pass": False,
            "token_validation_status": "not_applicable",
            "token_validation_pass": True,
            "token_validation_failures": [],
            "overall_status": "fail",
            "overall_pass": False,
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
            "minimum_output_tokens": minimum_output_tokens,
            "output_token_limits": output_token_limits,
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
    accounting_source_id: str | None = None,
    accounting_contract_id: str | None = None,
    independent_input_count: dict[str, Any] | None = None,
    observed_request_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status_code = getattr(result, "status_code", None)
    usage_required = bool(
        isinstance(status_code, int)
        and not isinstance(status_code, bool)
        and 200 <= status_code <= 299
    )
    if status_code is None:
        usage_required = bool(getattr(result, "success", False))
    response = getattr(result, "response_json", None)
    response = response if isinstance(response, dict) else {}
    usage_required |= any(
        isinstance(value, dict) and bool(value)
        for value in (
            getattr(result, "usage", None),
            response.get("usage"),
            response.get("usageMetadata"),
            response.get("usage_metadata"),
        )
    ) or any(
        bool(response.get(key))
        for key in ("choices", "content", "output", "candidates", "steps", "outputs")
    )
    request_changed = (
        observed_request_body is not None
        and request_body != observed_request_body
    )
    count_changed = bool(
        isinstance(independent_input_count, dict)
        and independent_input_count.get("request_integrity") == "fail"
    )
    try:
        audit = audit_exchange(
            request_body,
            result,
            transport,
            config,
            exchange,
            provider=provider,
            model=model,
            accounting_source_id=accounting_source_id,
            accounting_contract_id=accounting_contract_id,
            independent_input_count=independent_input_count,
            usage_required=usage_required,
        )
        integrity = {
            "status": "fail" if request_changed or count_changed else "pass",
            "dispatch_body_changed": request_changed,
            "count_body_changed": count_changed,
        }
        if request_changed:
            integrity["expected_request"] = copy.deepcopy(request_body)
            integrity["observed_request"] = copy.deepcopy(observed_request_body)
        audit["request_integrity"] = integrity
        if request_changed or count_changed:
            note = "parameter request input changed during dispatch or token counting"
            audit["validation_status"] = "fail"
            audit["validation_pass"] = False
            audit["validation_failures"] = [
                *(audit.get("validation_failures") or []), note,
            ]
        return audit
    except Exception as exc:
        note = f"token audit error: {exc.__class__.__name__}"
        integrity_required = usage_required or request_changed or count_changed
        unavailable = {"status": "not_available", "note": note}
        validation_status = "fail" if integrity_required else "not_applicable"
        return {
            "schema_version": TOKEN_AUDIT_SCHEMA_VERSION,
            "exchange": exchange,
            "status": "not_available",
            "validation_status": validation_status,
            "validation_pass": not integrity_required,
            "validation_failures": [note] if integrity_required else [],
            "usage_required": usage_required,
            "input": dict(unavailable),
            "output": dict(unavailable),
            "reported": {},
            "independent_count": {},
            "usage_arithmetic": dict(unavailable),
            "usage_presence": {
                "required": usage_required,
                "status": validation_status,
                "input_present": False,
                "output_present": False,
                "missing_fields": ["input_tokens", "output_tokens"]
                if usage_required
                else [],
                "note": note,
            },
            "gross_plausibility": {
                "status": "not_available",
                "input": dict(unavailable),
                "output": dict(unavailable),
            },
            "input_accuracy": dict(unavailable),
            "output_accuracy": dict(unavailable),
            "evidence_level": "unavailable",
            "usage_accounting": {},
            "output_completion": {
                "status": "fail" if usage_required else "not_applicable",
                "complete": False,
                "note": note,
            },
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
    count_body = copy.deepcopy(body)
    try:
        value = counter(transport, model, count_body)
    except Exception:
        value = None
    if count_body != body:
        return {
            "tokens": None,
            "evidence_level": "unavailable",
            "request_integrity": "fail",
            "note": "token counter changed the declared request body",
        }
    return value if isinstance(value, dict) else None


def _performance_metrics(
    result: Any,
    transport: str | None = None,
    *,
    usage_accounting: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(usage_accounting, dict):
        accounting = usage_accounting
    else:
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
        original_outcome = item.get("pre_aggregation_outcome") or {}
        if (
            item.get("status") not in {"incompatible", "fail", "unexpected_acceptance"}
            and item.get("overall_pass", item.get("pass")) is not False
            and not original_outcome
        ):
            continue
        cases.append(
            {
                "status": original_outcome.get("overall_status") or item.get("overall_status") or item.get("status"),
                "compatibility_status": original_outcome.get("status", item.get("status")),
                "compatibility_pass": original_outcome.get("compatibility_pass", item.get("compatibility_pass", item.get("pass"))),
                **({
                    "pre_aggregation_outcome": original_outcome,
                    "satisfied_by_sibling_run": True,
                    "run_success_mode": item.get("run_success_mode"),
                    "aggregated_status": item.get("status"),
                } if original_outcome else {}),
                "token_validation_status": item.get("token_validation_status"),
                "token_validation_pass": item.get("token_validation_pass"),
                "token_validation_failures": item.get("token_validation_failures") or [],
                "token_audit": item.get("token_audit") or {},
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
        lines.append(f"compatibility_pass: {case.get('compatibility_pass')}")
        if case.get("satisfied_by_sibling_run"):
            lines.append(f"run_success_mode: {case.get('run_success_mode')}")
            lines.append(f"aggregated_status: {case.get('aggregated_status')} (satisfied by sibling run)")
        lines.append(f"token_validation_status: {case.get('token_validation_status')}")
        lines.append(f"token_validation_pass: {case.get('token_validation_pass')}")
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
        token_audit = case.get("token_audit") or {}
        if token_audit:
            lines.append("")
            lines.append("token_audit:")
            lines.append(_pretty_json(token_audit))
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


def _tool_validation_mode(config: dict[str, Any] | None = None) -> str:
    controls = bound_parameter_execution(config)
    if controls:
        return controls["tool_validation_mode"]
    mode = str(os.getenv("LOADTEST_TOOL_VALIDATION_MODE") or "auto")
    if mode not in {
        "auto",
        "openai_compat",
        "gemini_native",
        "gemini_interactions",
        "claude_native",
        "openai_responses",
    }:
        raise ValueError(
            "LOADTEST_TOOL_VALIDATION_MODE must be auto, openai_compat, gemini_native, "
            "gemini_interactions, claude_native, or openai_responses"
        )
    return mode


def _param_test_runs(config: dict[str, Any] | None = None) -> int:
    controls = bound_parameter_execution(config)
    if controls:
        return controls["runs"]
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
    settings = _lookup_profile_settings(config, profile)
    if isinstance(settings.get("messages"), list):
        return [
            {
                "id": f"inline_messages:{profile}",
                "prompt": f"Profile-defined inline messages in compatibility_profiles.{profile}.",
            }
            for _ in range(count)
        ]
    if any(key in settings for key in ("prompt", "prompt_fixture", "fixture", "input")):
        return [
            {
                "id": f"profile_defined:{profile}",
                "prompt": str(settings.get("prompt") or
                              f"Profile-defined input in compatibility_profiles.{profile}."),
            }
            for _ in range(count)
        ]
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

    # Repeated runs keep the same declared input; run identity belongs in the
    # report, not in additional provider-visible text.
    return expanded[:count]


def _profile_run_success_modes(config: dict[str, Any], profiles: list[str]) -> dict[str, str]:
    """Validate all selected repeat policies before any provider requests."""
    return {
        profile: normalize_run_success_mode(
            _lookup_profile_settings(config, profile).get("run_success_mode")
        )
        for profile in profiles
    }


def _lookup_profile_settings(config: dict[str, Any], profile: str) -> dict[str, Any]:
    for group in ("compatibility_profiles", "throughput_profiles", "cache_profiles"):
        profiles = config.get(group) or {}
        settings = profiles.get(profile)
        if isinstance(settings, dict):
            def inherited(name: str, chain: tuple[str, ...] = ()) -> dict[str, Any]:
                if name in chain:
                    raise ValueError(f"Profile inheritance cycle: {' -> '.join(chain + (name,))}")
                item = profiles.get(name)
                if not isinstance(item, dict):
                    raise ValueError(f"Unknown inherited parameter profile: {name}")
                parent = item.get("extends")
                base = inherited(str(parent), chain + (name,)) if parent else {}
                return deep_merge(base, item)
            return inherited(profile)
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
    if profile.startswith("deepseek0813_fim_"):
        return "fim"
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
        "gemini_3_7_flash_interactions_response_format_json",
    }:
        return "json_output"
    if (
        profile in OPENAI_TOOL_PROFILES
        or profile in NATIVE_TOOL_PROFILES
        or profile in CLAUDE_NATIVE_TOOL_PROFILES
        or profile in OPENAI_RESPONSES_TOOL_PROFILES
        or profile in GEMINI_INTERACTIONS_TOOL_PROFILES
        or profile in _DEEPSEEK_0813_TOOL_PROFILES
    ):
        return "tool_calls"
    if profile in GEMINI_INTERACTIONS_TOOL_NONE_PROFILES:
        return "general"
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
    if group == "fim":
        return [
            {"id": "fim:python_add", "prompt": "def add(a, b):\n    return"},
            {"id": "fim:python_square", "prompt": "def square(value):\n    return"},
            {"id": "fim:python_greeting", "prompt": "def greeting(name):\n    return f\"Hello,"},
        ]
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

    return [
        {"id": "general:api_loadtest", "prompt": "用两句话解释 API 压测为什么要区分业务请求和控制请求。"},
        {"id": "general:streaming", "prompt": "列出流式响应兼容性测试中最重要的三个检查点，每项不超过二十字。"},
        {"id": "general:cache", "prompt": "用一句话说明上下文缓存命中率应该如何解读。"},
    ]


def _default_report_dir(provider: str, model: str) -> Path:
    safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "_", model)
    return default_reports_root() / "param_tests" / provider / safe_model


if __name__ == "__main__":
    raise SystemExit(main())
