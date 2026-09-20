#!/usr/bin/env python3
"""Generate per-family parameter manuals from the shared MPDB projection.

The generated documents are intentionally checked in. Run with ``--check`` in
CI or review workflows to prove that every registered family/source/profile is
still documented.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
OUTPUT_DIR = PROJECT_ROOT / "docs" / "model_profiles"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.config import deep_merge  # noqa: E402
from lib.banana_generate_content import build_banana_generate_content_cases  # noqa: E402
from lib.gpt_image_25 import gpt_image_25_cases  # noqa: E402
from lib.gpt_image_25_responses import responses_image_cases, CURRENT_EXPECTATION_POLICY  # noqa: E402
from lib.model_profile_catalog import get_model_profile_catalog  # noqa: E402
from lib.image_validation import (  # noqa: E402
    banana_variant_cases,
    gpt_image_2_cases,
    grok_imagine_cases,
)
from lib.reference_specs import (  # noqa: E402
    get_reference_source,
    load_model_capability_profile,
    load_model_capability_profiles,
    load_reference_specs,
    parameters_for_profile,
    resolve_profile_expectation,
    test_profiles_for_reference,
)


def _official_reference_ids() -> set[str]:
    return set(load_reference_specs()["reference_sources"])


FAMILY_META: dict[tuple[str, str], dict[str, str]] = {
    ("text", "deepseek"): {
        "title": "DeepSeek",
        "slug": "deepseek",
        "summary": "覆盖思考档位、采样、JSON、停止词、logprobs、工具调用以及阿里云/动态聚合差异。",
    },
    ("text", "glm"): {
        "title": "GLM",
        "slug": "glm",
        "summary": "覆盖 GLM-5.2 七档 reasoning_effort，以及 GLM-5.3 始终思考（仅 low/high/max）的原厂合同。",
    },
    ("text", "qwen"): {
        "title": "Qwen",
        "slug": "qwen",
        "summary": "覆盖 Qwen thinking、搜索、代码解释器、采样、结构化输出和并行工具调用。",
    },
    ("text", "gemini"): {
        "title": "Gemini",
        "slug": "gemini",
        "summary": "分别说明 AI Studio Chat 兼容、AI Studio GenerateContent、Vertex GenerateContent 与动态聚合。",
    },
    ("text", "claude"): {
        "title": "Claude",
        "slug": "claude",
        "summary": "分别说明 Anthropic Messages、Bedrock、Vertex、动态 Messages 和 OpenAI 兼容接口。",
    },
    ("text", "claude_fable"): {
        "title": "Claude Fable",
        "slug": "claude_fable",
        "summary": "说明 Fable 的 Native Messages、云路由、动态路由和兼容接口 profile。",
    },
    ("text", "gpt"): {
        "title": "GPT",
        "slug": "gpt",
        "summary": "区分通用 Chat、GPT-5.x Chat 与 Responses，并覆盖 reasoning、工具、JSON 和负向约束。",
    },
    ("text", "kimi"): {
        "title": "Kimi",
        "slug": "kimi",
        "summary": "区分 K2.x、K3、阿里云、OpenRouter 与动态聚合，并记录 K3 的固定采样与身份约束。",
    },
    ("text", "minimax"): {
        "title": "MiniMax",
        "slug": "minimax",
        "summary": "使用精简的 Chat Completions 基础矩阵验证流式、采样、JSON、停止词和工具。",
    },
    ("text", "grok"): {
        "title": "Grok",
        "slug": "grok",
        "summary": "分别说明 Chat Completions 与 Responses 的 reasoning、JSON、工具和负向参数。",
    },
    ("image", "gpt-image-2"): {
        "title": "GPT Image",
        "slug": "gpt_image_2",
        "summary": "覆盖输出解码、格式、数量、任意尺寸、2K/4K 边界和无效参数拒绝。",
    },
    ("image", "banana"): {
        "title": "Banana / Gemini Image",
        "slug": "banana",
        "summary": "四个精确模型的 Google AI Studio GenerateContent v1beta 比例×分辨率矩阵共 142 个候选单元，逐项消费已发布的官方证据，缺失项保留观测。文档像素与稳定实网偏差分别展示，历史 v1 证据只读保留。Interactions、兼容 Chat 和 provider alias 使用各自独立探针。AI Studio 精确 Lite Image 的 function calling/tools 与 context caching 官方声明为 unsupported，保留 live_unverified，不新增工具或缓存执行。",
    },
    ("image", "grok-imagine"): {
        "title": "Grok Imagine",
        "slug": "grok_imagine",
        "summary": "覆盖 1K/2K、宽高比、批量数量、URL/b64 交付与越界拒绝。",
    },
}


# These manuals still contain pre-existing, execution-gated app parity work.
# The shared MPDB may expose the corresponding catalog-only contracts, but the
# app runtime configuration intentionally does not implement every referenced
# test profile yet.  Keep the existing manuals untouched until the relevant
# parity leaves are executed; other families remain deterministically checked.
DEFERRED_APP_PARITY_FAMILIES = {
    ("text", "claude"),
    ("text", "claude_fable"),
    ("text", "deepseek"),
    ("text", "gpt"),
}


PROFILE_INTERNAL_KEYS = {
    "extends",
    "prompt",
    "prompt_key",
    "prompt_fixture",
    "fixture",
    "fixture_chars",
    "fixture_repeat_to_chars",
    "run_success_mode",
}


IMAGE_PURPOSES = {
    "baseline_1024_square": "基线：确认返回内容可解码，并精确得到 1024×1024 图片。",
    "standard_portrait": "验证官方标准竖图尺寸。",
    "arbitrary_landscape": "验证边长为 16 倍数的自定义横图尺寸。",
    "square_2k": "验证 2K 方图像素对应关系，并为疑似后处理分析提供对照。",
    "batch_n2_1024_square": "验证 n=2 时返回两张都可解码且尺寸正确的图片。",
    "background_auto": "验证自动背景参数。",
    "moderation_low": "验证 low moderation 设置。",
    "jpeg_compression_50": "验证 JPEG 格式与显式压缩质量。",
    "landscape_4k": "验证文档允许的 4K 横图上边界。",
    "reject_non_multiple_of_16": "负向：非 16 倍数边长应被拒绝。",
    "reject_aspect_ratio_over_3_to_1": "负向：超过 3:1 的宽高比应被拒绝。",
    "reject_below_minimum_pixels": "负向：低于最小像素数应被拒绝。",
    "reject_edge_over_3840": "负向：单边超过 3840 应被拒绝。",
    "reject_transparent_background": "负向：不支持透明背景的模型应明确拒绝。",
    "banana_1k_aligned": "验证 1K 请求与模型/别名分辨率一致。",
    "banana_2k_aligned": "验证 2K 请求与模型/别名分辨率一致。",
    "banana_4k_aligned": "验证需显式计费确认的 4K 请求。",
    "banana_model_1k_request_2k": "交叉控制：1K 模型别名配 2K 请求，判断真正生效的控制来源。",
    "banana_model_2k_request_1k": "交叉控制：2K 模型别名配 1K 请求，判断真正生效的控制来源。",
    "banana_512_square": "验证 Gemini 3.1 Flash Image 的 generic 官方 512 分辨率档。Lite 不进入此 generic 成功用例；它的 GenerateContent 精确边界另行展示。",
    "banana_1k_landscape_16_9": "验证 Interactions 的 1K、16:9 组合。",
    "banana_reject_lowercase_1k": "负向：错误的小写分辨率枚举应被拒绝。",
    "banana_reject_aspect_ratio_7_5": "负向：未登记的 7:5 宽高比应被拒绝。",
    "grok_1k_square_b64": "基线：验证 1K 方图的 b64 解码与像素。",
    "grok_1k_landscape_16_9": "验证 1K、16:9 横图。",
    "grok_1k_portrait_9_16": "验证 1K、9:16 竖图。",
    "grok_1k_batch_n2": "验证 n=2 的批量生成和逐张解码。",
    "grok_1k_square_url": "验证临时 URL 交付可下载、可解码。",
    "grok_2k_square_b64": "验证需计费确认的 2K 方图。",
    "grok_2k_landscape_16_9": "验证需计费确认的 2K、16:9 横图。",
    "grok_2k_portrait_9_16": "验证需计费确认的 2K、9:16 竖图。",
    "grok_reject_aspect_ratio_7_5": "负向：非法宽高比枚举应被拒绝。",
    "grok_reject_resolution_4k": "负向：未支持的 4K 档应被拒绝。",
    "grok_reject_n11": "负向：超过最大批量数量 10 应被拒绝。",
}


def _read_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _markdown(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _resolve_profile_settings(
    profiles: dict[str, Any], name: str, stack: tuple[str, ...] = ()
) -> dict[str, Any]:
    if name in stack:
        raise RuntimeError(f"Profile inheritance cycle: {' -> '.join((*stack, name))}")
    raw = profiles.get(name)
    if not isinstance(raw, dict):
        raise KeyError(f"compatibility_profiles.{name} is missing")
    parent = str(raw.get("extends") or "").strip()
    base = (
        _resolve_profile_settings(profiles, parent, (*stack, name))
        if parent
        else {}
    )
    return deep_merge(base, {k: copy.deepcopy(v) for k, v in raw.items() if k != "extends"})


def _flatten_settings(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        rows: list[tuple[str, Any]] = []
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if not prefix and key in PROFILE_INTERNAL_KEYS:
                continue
            if isinstance(item, dict) and len(rows) < 12:
                rows.extend(_flatten_settings(item, name))
            else:
                rows.append((name, item))
        return rows
    return [(prefix, value)]


def _compact_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        if not value:
            return "[]"
        if all(not isinstance(item, (dict, list)) for item in value) and len(value) <= 3:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return f"[{len(value)} items]"
    if isinstance(value, dict):
        return f"{{{len(value)} fields}}"
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= 60 else text[:57] + "..."


def _compact_settings(settings: dict[str, Any]) -> str:
    settings = copy.deepcopy(settings)
    operations = settings.get("operations")
    if isinstance(operations, list) and operations:
        settings["operations"] = " → ".join(
            f"{operation.get('name', index)}:{operation.get('method', '?')}"
            for index, operation in enumerate(operations, 1)
            if isinstance(operation, dict)
        )
    rows = _flatten_settings(settings)
    rendered = [f"`{key}={_compact_value(value)}`" for key, value in rows[:9]]
    if len(rows) > 9:
        rendered.append(f"另 {len(rows) - 9} 项")
    return "<br>".join(rendered) if rendered else "—"


def _category(profile: str) -> str:
    name = profile.casefold()
    if name.startswith("gemini_3_7_flash_interactions_tools_"):
        return "工具调用"
    if "reject" in name or "disabled" in name or "none" in name:
        return "负向/边界"
    if "tool" in name:
        return "工具调用"
    if any(word in name for word in ("thinking", "reasoning", "effort")):
        return "推理"
    if any(word in name for word in ("json", "schema", "format", "modalities")):
        return "结构化输出"
    if "stream" in name:
        return "流式"
    if any(word in name for word in ("cache", "cached")):
        return "缓存参数"
    if any(
        word in name
        for word in (
            "temperature",
            "top_p",
            "top_k",
            "seed",
            "penalty",
            "sample",
            "repetition",
            "_n",
        )
    ):
        return "采样"
    if any(word in name for word in ("service", "labels", "traffic", "request_type")):
        return "Route 元数据"
    return "基础能力"


def _profile_purpose(profile: str, parameters: list[str]) -> str:
    name = profile.casefold()
    if name.startswith("gemini_3_7_flash_interactions_tools_"):
        return "验证当前 tool_choice 模式的文本/函数调用响应；此 profile 不执行外部工具或 follow-up。"
    target = "、".join(f"`{item}`" for item in parameters) or f"`{profile}` 对应能力"
    if "reject" in name:
        return f"负向探针：发送文档不允许的 {target}，确认网关明确拒绝而不是静默吞掉。"
    if "stream_usage" in name or "stream_with_usage" in name:
        return "验证 SSE 分块可解析，并在结束前得到独立、算术一致的 usage 信息。"
    if "stream" in name:
        return "验证 SSE 流式响应、结束标记和返回文本能够完整解析。"
    if "tool" in name:
        if any(word in name for word in ("thinking", "preserve")):
            return "验证推理模式下的结构化工具调用，并确认历史推理字段在 follow-up 中原样保留。"
        return "验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。"
    if any(word in name for word in ("thinking", "reasoning", "effort")):
        return f"验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 {target}。"
    if any(word in name for word in ("json", "schema", "response_format")):
        return f"验证结构化输出参数 {target}，并确认最终内容是可解析且符合约束的 JSON。"
    if "stop" in name:
        return f"验证停止序列参数 {target} 会影响结束位置或按契约被拒绝。"
    if "cache" in name or "cached" in name:
        return f"验证请求级缓存标识/缓存内容参数 {target} 能被正确接收和报告。"
    if any(word in name for word in ("max_token", "max_output")):
        return f"验证输出 token 上限字段 {target} 使用当前 API Form 的正确名称和位置。"
    if any(word in name for word in ("temperature", "top_p", "top_k", "seed", "penalty", "sample", "repetition")):
        return f"验证采样参数 {target} 的接受度；非思考模式下还检查返回值不是空壳。"
    if any(word in name for word in ("service", "labels", "traffic", "request_type", "metadata")):
        return f"验证 route 专属元数据 {target} 放在正确的 body 或 header 位置。"
    if "system" in name or "instructions" in name:
        return f"验证系统指令字段 {target} 的协议位置和实际响应语义。"
    if "candidate" in name or name.endswith("_n"):
        return f"验证候选数量 {target}，并核对响应实际返回的候选数。"
    return f"验证 {target} 的请求兼容性以及对应响应字段是否正常。"


def _validation_focus(profile: str) -> str:
    name = profile.casefold()
    if name.startswith("gemini_3_7_flash_interactions_tools_"):
        mode = name.rsplit("_", 1)[-1]
        if mode == "none":
            return "必须返回有效文本且没有函数调用。"
        if mode == "any":
            return "必须返回声明中的函数调用，并检查调用 ID、参数对象及 schema。"
        return "允许有效文本或声明中的函数调用；出现调用时检查调用 ID、参数对象及 schema。"
    if name == "gemini_3_7_flash_interactions_thinking_summaries_auto":
        return "检查原生 thought/usage 结构；auto 摘要允许为空，不据此判定失败。"
    if "reject" in name:
        return "应得到明确 400/422；若 2xx 则是 unexpected_acceptance。"
    if "tool" in name:
        return "不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。"
    if "json" in name or "schema" in name or "response_format" in name:
        return "内容必须能解析为 JSON；有 schema 时还要满足 schema。"
    if "stream" in name:
        return "检查 chunk 结构、结束标记、文本拼接与 usage 末块。"
    if name == "kimi_k3_preserved_thinking":
        return (
            "可见回复须同时含历史 reasoning_content 中的 215 与 222。"
            "使用官方 README 原句；同一配置重复轮次中一次语义与 token 校验成功可满足该语义检查。原始失败保留，接口、协议或 token 错误仍阻断。"
        )
    if any(word in name for word in ("thinking", "reasoning", "effort")):
        return "检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。"
    if "candidate" in name or name.endswith("_n"):
        return "核对响应候选数量，不以第一个候选成功代替整体成功。"
    return "2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。"


def _expectation_label(expectations: set[str]) -> str:
    if expectations == {"reference_only"}:
        return "只读记录（执行关闭）"
    if "reference_only" in expectations:
        return "按来源区分（含只读记录）"
    if expectations == {"supported"}:
        return "应支持"
    if expectations == {"unsupported"}:
        return "应拒绝"
    if expectations == {"supported", "unsupported"}:
        return "按模型/route 变化"
    return "由运行时 model profile 决定"


def _source_links(source: dict[str, Any]) -> str:
    links = []
    for index, url in enumerate(source.get("official_sources") or [], 1):
        links.append(f"[资料{index}]({url})")
    return " ".join(links) or "—"



def _model_policy_references(modality: str, family: str, model: str, route: str, api_form: str) -> dict[str, dict[str, Any]]:
    """Enumerate source-local policies without asking a runtime resolver to choose."""
    catalog = get_model_profile_catalog()
    references: dict[str, dict[str, Any]] = {}
    for row in catalog.list_profiles(modality=modality, model=model, api_form=api_form):
        profile = catalog.get_profile(str(row["profile_id"]))
        for interface_id in profile.get("interface_ids") or []:
            interface = catalog.get_interface(str(interface_id))
            if interface.get("api_form") != api_form or interface.get("routing_mode") != route:
                continue
            for policy in interface.get("test_bindings") or []:
                if (not isinstance(policy, dict) or policy.get("extension_type") != "model_test_policy"
                        or (policy.get("suite_family_id") or profile.get("family_id")) != family):
                    continue
                for reference in policy.get("reference_contract_ids") or []:
                    if reference not in (interface.get("contract_ids") or []):
                        continue
                    readonly = any(value is False for value in (
                        interface.get("enabled"), interface.get("executable"),
                        policy.get("enabled"), policy.get("executable"),
                    ))
                    previous = references.get(str(reference))
                    if previous and previous["interface_id"] != interface_id:
                        raise RuntimeError(f"Reference {reference!r} spans multiple Interfaces on the same route/API.")
                    references[str(reference)] = {"interface_id": interface_id, "read_only": readonly}
    if not references:
        raise RuntimeError(f"No MPDB policy references for {modality}/{family}/{model}/{route}/{api_form}.")
    return references


def _family_inventory(
    modality: str,
    family: str,
    family_cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, set[str]], set[str]]:
    combinations: list[dict[str, Any]] = []
    expectations: dict[str, set[str]] = defaultdict(set)
    source_ids: set[str] = set()
    for route, route_cfg in (family_cfg.get("route_profiles") or {}).items():
        for api_form, form_cfg in (route_cfg.get("api_forms") or {}).items():
            model_names = list((form_cfg.get("model_profiles") or {}).keys())
            combination_sources: set[str] = set()
            for model in model_names:
                references = _model_policy_references(modality, family, model, str(route), str(api_form))
                allowed = set(references)
                combination_sources.update(allowed)
                for source_id in allowed:
                    if source_id not in _official_reference_ids():
                        continue
                    source_ids.add(source_id)
                    source = get_reference_source(source_id)
                    if source.get("model_family") != family:
                        continue
                    if references[source_id]["read_only"]:
                        for profile in test_profiles_for_reference(source_id):
                            expectations[profile].add("reference_only")
                        continue
                    selected = load_model_capability_profile(
                        modality,
                        family,
                        model,
                        route_profile=str(route),
                        api_form=str(api_form),
                        reference_source=source_id,
                    )
                    for profile in test_profiles_for_reference(source_id):
                        expectations[profile].add(
                            resolve_profile_expectation(
                                modality,
                                family,
                                model,
                                profile,
                                capability_profile=selected,
                                reference_source=source_id,
                            )
                        )
            combinations.append(
                {
                    "route": str(route),
                    "api_form": str(api_form),
                    "transport": str(form_cfg.get("transport") or ""),
                    "models": model_names,
                    "sources": sorted(combination_sources),
                }
            )
    return combinations, expectations, source_ids


def _render_models(family_cfg: dict[str, Any]) -> list[str]:
    lines = ["| 规范模型 | 显式 alias |", "|---|---|"]
    models = family_cfg.get("models") or family_cfg.get("canonical_models") or {}
    for model, raw_cfg in models.items():
        cfg = raw_cfg if isinstance(raw_cfg, dict) else {}
        aliases = ", ".join(f"`{item}`" for item in cfg.get("aliases") or []) or "—"
        lines.append(f"| `{_markdown(model)}` | {aliases} |")
    return lines


def _render_routes(combinations: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Route Profile | API Form | 内部 transport | 已注册模型数 | Reference Contract |",
        "|---|---|---|---:|---|",
    ]
    for row in combinations:
        sources = "<br>".join(f"`{item}`" for item in row["sources"]) or "—"
        lines.append(
            f"| `{row['route']}` | `{row['api_form']}` | "
            f"`{row['transport'] or '由 API Form 映射'}` | {len(row['models'])} | {sources} |"
        )
    return lines


def _render_sources(source_ids: set[str]) -> list[str]:
    lines = [
        "| Reference Contract | 说明 | Route / API Form | 认证范围 | Test Case 数 | 官方资料 |",
        "|---|---|---|---|---:|---|",
    ]
    for source_id in sorted(source_ids):
        source = get_reference_source(source_id)
        lines.append(
            f"| `{source_id}` | {_markdown(source.get('label') or source_id)} | "
            f"`{source.get('route_profile')}` / `{source.get('api_form')}` | "
            f"`{source.get('certification_scope') or 'raw_route_contract'}` | "
            f"{len(test_profiles_for_reference(source_id))} | {_source_links(source)} |"
        )
    return lines



def _mpdb_case_settings(profile: str, source_ids: set[str]) -> dict[str, Any]:
    """Document source-authored cases without fabricating a config template."""
    bodies: dict[str, dict[str, Any]] = {}
    for source_id in sorted(source_ids):
        for binding in get_model_profile_catalog().list_test_bindings(
            contract_id=source_id, extension_type="parameter",
        ):
            for case in binding.get("case_definitions") or []:
                if isinstance(case, dict) and case.get("case_id") == profile:
                    body = case.get("body") or case.get("body_template") or case.get("request")
                    if isinstance(body, dict):
                        bodies[source_id] = copy.deepcopy(body)
                    elif isinstance(case.get("operations"), list) and case["operations"]:
                        # Dedicated lifecycle cases contain a sequence, including
                        # cleanup operations, rather than one generic request.
                        bodies[source_id] = {
                            "generic_parameter_runner_supported": case.get("generic_parameter_runner_supported", False),
                            "operations": copy.deepcopy(case["operations"]),
                            "limits": copy.deepcopy(case.get("limits") or {}),
                        }
    if not bodies:
        raise KeyError(f"No config template or source-authored MPDB body for {profile!r}.")
    return next(iter(bodies.values())) if len(bodies) == 1 else {"source_scoped_bodies": bodies}


def _render_text_profiles(
    source_ids: set[str],
    expectations: dict[str, set[str]],
    compatibility_profiles: dict[str, Any],
) -> list[str]:
    source_map: dict[str, set[str]] = defaultdict(set)
    parameter_map: dict[str, set[str]] = defaultdict(set)
    ordered_profiles: list[str] = []
    for source_id in sorted(source_ids):
        for profile in test_profiles_for_reference(source_id):
            if profile not in ordered_profiles:
                ordered_profiles.append(profile)
            source_map[profile].add(source_id)
            parameter_map[profile].update(parameters_for_profile(source_id, profile))

    lines = [
        "| Profile | 类别 | 具体测试目的 | 关键请求设置 | 期望 | 通过时还要检查 |",
        "|---|---|---|---|---|---|",
    ]
    for profile in ordered_profiles:
        settings = (
            _resolve_profile_settings(compatibility_profiles, profile)
            if profile in compatibility_profiles
            else _mpdb_case_settings(profile, source_map[profile])
        )
        parameters = sorted(parameter_map[profile])
        expectation = _expectation_label(expectations[profile])
        category = _category(profile)
        purpose = _profile_purpose(profile, parameters)
        focus = _validation_focus(profile)
        if expectation == "应拒绝":
            category = "负向/边界"
            focus = "应得到明确 400/422，并能归因于测试字段；若 2xx 则是 unexpected_acceptance。"
        elif expectation == "只读记录（执行关闭）":
            category = "历史/文档记录"
            purpose = "保留精确来源的已记录样本；普通参数执行门禁关闭。"
            focus = "历史结果不授予新的参数调用权限，也不记为本轮认证通过。"
        lines.append(
            f"| `{profile}` | {category} | "
            f"{purpose}<br>来源："
            f"{'、'.join(f'`{item}`' for item in sorted(source_map[profile]))} | "
            f"{_compact_settings(settings)} | {expectation} | "
            f"{focus} |"
        )
    return lines



def _banana_generate_content_cases() -> list[Any]:
    cases: list[Any] = []
    for model in (
        "gemini-2.5-flash-image", "gemini-3.1-flash-lite-image",
        "gemini-3.1-flash-image", "gemini-3-pro-image",
    ):
        capability = load_model_capability_profile(
            "image", "banana", model, route_profile="google_ai_studio",
            api_form="gemini_generate_content",
        )
        enabled = (
            capability.get("parameter_test_enabled") is True
            and capability.get("test_policy_parameter_test_enabled") is True
        )
        cases.extend(build_banana_generate_content_cases(
            model, "resolution", include_4k=True, include_negative=True,
            diagnostic=not enabled, capability_profile=capability,
        ))
    return cases


def _banana_pixel_label(value: Any) -> str:
    return "×".join(str(part) for part in value) if value else "文档未规定精确像素"


def _banana_generate_content_purpose(case: Any) -> str:
    group = "文档主矩阵" if case.metadata.get("matrix_group") == "documented" else "独立边界"
    if case.expected_outcome == "observation":
        return f"{group}；缺少该单元的已发布 v1beta 证据，保留观测，不计入认证。"
    if case.expected_outcome == "rejection":
        return f"{group}；同模型、同参数的官方 v1beta 拒绝证据；本次响应仍须归因到待测字段。"
    if case.metadata.get("documentation_match") is False:
        documented = _banana_pixel_label(case.metadata.get("documented_expected_size"))
        actual = _banana_pixel_label(case.expected_size)
        return f"{group}；文档/实网偏差：{documented} → {actual}；保留原文档与独立重复证据。"
    return f"{group}；按当前精确模型 v1beta 证据核对文档像素、解码、数量、usage 和身份。"


def _render_banana_generate_content_evidence(all_cases: list[Any]) -> list[str]:
    cases = [case for case in all_cases if case.metadata.get("banana_gc_exact") is True]
    groups: dict[str, list[Any]] = {}
    for case in cases:
        groups.setdefault(str(case.model_override), []).append(case)
    main_count = sum(case.metadata.get("matrix_group") == "documented" for case in cases)
    lines = [
        "### Google AI Studio GenerateContent v1beta",
        "",
        f"当前展开 {main_count} 个文档主矩阵单元和 {len(cases) - main_count} 个独立边界，共 {len(cases)} 个候选请求。每个模型先发送 1K/1:1 基线；2.5 的主矩阵省略 imageSize。",
        "普通执行只消费已发布的 image_case_expectations。模型门禁开启不等于所有单元已认证，缺失项保持 observation。",
        "",
        "| 模型 | 全矩阵单元 | 已固化 v1beta 单元 | 保留观测 |",
        "|---|---:|---:|---:|",
    ]
    for model, rows in groups.items():
        unresolved = sum(case.expected_outcome == "observation" for case in rows)
        lines.append(f"| `{model}` | {len(rows)} | {len(rows) - unresolved} | {unresolved} |")
    lines.extend([
        "",
        "文档和官方实网尺寸分别保留；稳定偏差只适用于精确模型、API 版本和参数单元，不修改其它模型的像素表。",
        "",
        "| 偏差 Case | 原文档像素 | 官方 v1beta 验收像素 | 独立证据引用数 |",
        "|---|---|---|---:|",
    ])
    for case in cases:
        if case.expected_outcome == "success" and case.metadata.get("documentation_match") is False:
            lines.append(
                f"| `{case.name}` | {_banana_pixel_label(case.metadata.get('documented_expected_size'))} | "
                f"{_banana_pixel_label(case.expected_size)} | {len(case.metadata.get('beta_evidence_refs') or [])} |"
            )
    lines.extend(["", "### 当前图片用例", ""])
    return lines


def _render_banana_generate_content_history() -> list[str]:
    catalog = get_model_profile_catalog()
    interface = catalog.get_interface(
        "image/google_ai_studio/banana/gemini-3.1-flash-lite-image#gemini-generate-content-default"
    )
    historical = (
        interface.get("source_conflicts", {}).get("api_version_execution_policy_20260908", {})
        .get("historical_versions", {}).get("v1")
    )
    if not isinstance(historical, dict):
        raise RuntimeError("Missing archived Lite GenerateContent v1 evidence for documentation.")
    archived_interface = historical["interface"]
    contract_id = archived_interface["default_contract_id"]
    catalog.get_contract(contract_id)  # The historical contract remains queryable.
    reference = historical["model_test_policy_evidence"]
    history_path = PROJECT_ROOT / reference["path"]
    if not history_path.is_file() and PROJECT_ROOT.name == "app":
        history_path = PROJECT_ROOT.parent / reference["path"]
    raw_history = history_path.read_bytes()
    if hashlib.sha256(raw_history).hexdigest() != reference["sha256"]:
        raise RuntimeError("Historical image policy evidence digest mismatch.")
    policy = json.loads(raw_history)["policies"][reference["policy_ids"][0]]
    lines = [
        "", "### 历史 v1 证据（只读）", "",
        "以下内容直接读取 Interface 的 historical_versions.v1 归档及原 Contract，不调用当前 Lite 用例展开器，也不授予 v1 或 v1beta 执行权限。",
        "",
        "| 模型 | 历史 Contract | 版本 | 历史 test_scope | 历史认证范围 |",
        "|---|---|---|---|---|",
        f"| `gemini-3.1-flash-lite-image` | `{contract_id}` | `v1` | `{policy.get('test_scope')}` | `{policy.get('certification_scope')}` |",
        "",
        "| 历史 Profile | 当时预期 |",
        "|---|---|",
    ]
    for profile, expectation in sorted((policy.get("expectations") or {}).items()):
        lines.append(f"| `{profile}` | `{expectation}` |")
    lines.extend(["", "历史 v1 的小写 1k、512/2K/4K 和比例结论不能替代当前 v1beta 的逐项证据。", ""])
    return lines


def _image_case_sets(family: str) -> dict[str, list[Any]]:
    if family == "gpt-image-2":
        return {
            "openai_images_generations": gpt_image_2_cases(
                "full", include_4k=True, include_negative=True
            ) + gpt_image_25_cases("gpt-image-2.5-sunburst", "full", include_4k=True),
            "openai_images_edits": gpt_image_25_cases("gpt-image-2.5-sunburst", "full", operation="edit", include_4k=True),
            "openai_responses": responses_image_cases("gpt-image-2.5-sunburst", "full", include_4k=True,
                                                       expectation_policy=CURRENT_EXPECTATION_POLICY),
        }
    if family == "grok-imagine":
        return {
            "openai_images_generations": grok_imagine_cases(
                "full", include_2k=True, include_negative=True
            )
        }
    return {
        "gemini_generate_content": _banana_generate_content_cases(),
        "openai_chat_completions": [
            *banana_variant_cases(
                "full",
                model_template="nano-banana-pro-{resolution_lower}",
                include_4k=True,
                include_cross_control=True,
                include_negative=True,
                transport="chat-completions",
            ),
            *banana_variant_cases(
                "full",
                model_template="gemini-3.1-flash-image",
                include_4k=True,
                include_cross_control=False,
                include_negative=True,
                transport="chat-completions",
            ),
        ],
        "gemini_interactions": banana_variant_cases(
            "full",
            model_template="gemini-3.1-flash-image",
            include_4k=True,
            include_cross_control=False,
            include_negative=True,
            transport="gemini-interactions",
        ),
    }


def _image_case_settings(case: Any, api_forms: set[str]) -> dict[str, Any]:
    if case.metadata.get("banana_gc_exact") is True:
        settings = {
            "model_path": case.model_override, "api_version": "v1beta",
            "contents": "safe text-to-image prompt", **copy.deepcopy(case.parameters),
        }
        if case.expected_size:
            key = "documented_reference_size" if case.expected_outcome == "observation" else "expected_size"
            settings[key] = list(case.expected_size)
        return settings
    if not case.metadata.get("profile_driven"):
        settings = dict(case.parameters)
        if case.model_override:
            settings["model_override"] = case.model_override
        if case.expected_size:
            settings["expected_size"] = list(case.expected_size)
        return settings
    api_form = str(case.metadata.get("api_form") or "")
    if api_form not in api_forms:
        raise RuntimeError(
            f"Profile-driven image case {case.name!r} has mismatched API form."
        )
    resolution = str(case.metadata.get("requested_resolution") or "1K")
    aspect_ratio = str(case.metadata.get("aspect_ratio") or "1:1")
    thinking_level = case.metadata.get("thinking_level")
    if api_form == "gemini_interactions":
        settings: dict[str, Any] = {
            "model": case.model_override,
            "input": "safe text-to-image prompt",
            "response_format": {
                "type": "image",
                "mime_type": "image/jpeg",
                "aspect_ratio": aspect_ratio,
                "image_size": resolution,
            },
            "store": False,
        }
        if thinking_level:
            settings["generation_config"] = {
                "thinking_level": thinking_level,
            }
    else:
        settings = {
            "model_path": case.model_override,
            "contents": "safe text-to-image prompt",
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {
                    "aspectRatio": aspect_ratio,
                    "imageSize": resolution,
                },
            },
        }
        if thinking_level:
            settings["generationConfig"]["thinkingConfig"] = {
                "thinkingLevel": thinking_level,
            }
    if case.expected_size:
        settings["expected_size"] = list(case.expected_size)
    return settings



def _render_image_profiles(
    family: str,
    family_cfg: dict[str, Any],
) -> list[str]:
    case_map: dict[str, dict[str, Any]] = {}
    case_forms: dict[str, set[str]] = defaultdict(set)
    for api_form, cases in _image_case_sets(family).items():
        for case in cases:
            case_map.setdefault(case.name, case)
            case_forms[case.name].add(api_form)

    expectations: dict[str, set[str]] = defaultdict(set)
    for route, route_cfg in (family_cfg.get("route_profiles") or {}).items():
        for api_form, form_cfg in (route_cfg.get("api_forms") or {}).items():
            for model in (form_cfg.get("model_profiles") or {}):
                cap = load_model_capability_profile(
                    "image",
                    family,
                    model,
                    route_profile=str(route),
                    api_form=str(api_form),
                )
                for case_name in case_map:
                    if case_map[case_name].metadata.get("banana_gc_exact") is True:
                        continue
                    if api_form in case_forms[case_name]:
                        case = case_map[case_name]
                        if case.metadata.get("profile_driven") and (
                            str(route) != "google_ai_studio"
                            or str(model) != case.metadata.get("model_scope")
                        ):
                            continue
                        expectation_profile = str(case.metadata.get("test_profile") or case_name)
                        expectations[case_name].add(
                            resolve_profile_expectation(
                                "image",
                                family,
                                model,
                                expectation_profile,
                                capability_profile=cap,
                            )
                        )

    lines = _render_banana_generate_content_evidence(list(case_map.values())) if family == "banana" else []
    lines.extend([
        "| Case / Profile | API Form | 具体测试目的 | 关键请求设置 | 期望 |",
        "|---|---|---|---|---|",
    ])
    for case_name, case in case_map.items():
        settings = _image_case_settings(case, case_forms[case_name])
        forms = "<br>".join(f"`{item}`" for item in sorted(case_forms[case_name]))
        if case.metadata.get("gpt_image_25") or case.metadata.get("gpt_image_25_responses"):
            expectation = {"success": "应支持", "rejection": "应拒绝", "observation": "观测（未裁决）"}[case.expected_outcome]
            purpose = "Sunburst / Flare 独立绑定；" + case.description
            if case.metadata.get("semantic_review"):
                purpose += " 需审查生成图片的语义效果。"
            lines.append(f"| `{case_name}` | {forms} | {_markdown(purpose)} | {_compact_settings(settings)} | {expectation} | `not_certified`，实测见 [2.5 审计](../gpt_image_25_param_audit.md) |")
            continue
        if case.metadata.get("banana_gc_exact") is True:
            authored = {"success": "应支持", "rejection": "应拒绝", "observation": "观测（未认证）"}[case.expected_outcome]
            purpose = _banana_generate_content_purpose(case)
            lines.append(f"| `{case_name}` | {forms} | {purpose} | {_compact_settings(settings)} | {authored} |")
            continue
        purpose = IMAGE_PURPOSES.get(case_name, case.description or "验证图片响应语义。")
        authored = "应拒绝" if case.expected_outcome == "rejection" else _expectation_label(expectations[case_name])
        lines.append(
            f"| `{case_name}` | {forms} | {purpose} | {_compact_settings(settings)} | {authored} |"
        )
    if family == "banana":
        lines.extend(_render_banana_generate_content_history())
    return lines


def _render_family_document(
    modality: str,
    family: str,
    family_cfg: dict[str, Any],
    compatibility_profiles: dict[str, Any],
) -> str:
    meta = FAMILY_META[(modality, family)]
    combinations, expectations, source_ids = _family_inventory(
        modality, family, family_cfg
    )
    lines = [
        f"# {meta['title']} 模型家族 Profile 说明",
        "",
        "<!-- 由 scripts/generate_test_docs.py 从 Model Profile Database 与测试扩展生成，请勿手工维护表格。 -->",
        "",
        f"{meta['summary']}",
        "",
        "本文档回答三个问题：这个家族有哪些模型身份、在不同 route/API Form 下使用哪份测试契约、每个 profile 实际发送什么并检查什么。",
        "",
        "## 在界面中使用本手册",
        "",
        (
            "![文字参数测试界面示意](../assets/ui/parameter-testing-console.svg)"
            if modality == "text"
            else "![图片参数测试界面示意](../assets/ui/image-parameter-console.svg)"
        ),
        "",
        (
            "先在界面按 Provider → Model → Route Profile → API Form → Reference Contract 选择组合，再用下文表格确认本次会运行哪些 Test Case。"
            if modality == "text"
            else "先在界面按 Provider → Model → Route Profile → API Form → Suite 选择组合，再用下文表格确认图片 case、费用确认和验收要求。"
        ),
        "",
        "## 先理解判读规则",
        "",
        "- `应支持`：期望 HTTP 2xx，且响应结构、内容语义、usage、returned-model 均通过校验。",
        "- `应拒绝`：期望明确的 400/422；若仍返回 2xx，记为 `unexpected_acceptance`。",
        "- `按模型/route 变化`：同一 profile 对家族内不同模型或 route 的期望不同，运行前以控制台展开的 model profile 为准。",
        "- 动态聚合 route 即使全部通过，也只证明 adapter 兼容，不能证明物理上游或原厂合同。",
        "",
        "## 模型与 alias",
        "",
        *_render_models(family_cfg),
        "",
        "## Route 与 API Form",
        "",
        *_render_routes(combinations),
    ]
    if modality == "text":
        lines.extend(
            [
                "",
                "## Reference Contract",
                "",
                *_render_sources(source_ids),
                "",
                "## 全部参数 Profile",
                "",
                *_render_text_profiles(
                    source_ids, expectations, compatibility_profiles
                ),
            ]
        )
    else:
        lines.extend(
            [
                "",
                "## 全部图片 Case / Profile",
                "",
                "图片测试的 2xx 还必须解码输出，并核对数量、格式和实际像素；仅收到 HTTP 200 不算通过。",
                "",
                *_render_image_profiles(family, family_cfg),
            ]
        )
    lines.extend(
        [
            "",
            "## 去哪里看结果",
            "",
            "- Web 控制台会展示当前 model/route/API Form 的 profile 状态和最近一次结果。",
            "- 文字参数结果：`reports/param_tests/<provider>/<model>/verdict.json` 或 Web Job 目录。",
            "- 图片参数结果：`reports/jobs/<job_id>/summary.json`、`plan.json` 和逐 case 文件。",
            "- 总体解释方法见 [参数测试说明](../parameter_testing.md)，不要只看顶层 `pass`。",
            "",
        ]
    )
    return "\n".join(lines)


def _render_index(
    capabilities: dict[str, Any],
    rendered: dict[Path, str],
) -> str:
    lines = [
        "# 模型家族 Profile 手册索引",
        "",
        "本目录由 Model Profile Database（含测试扩展）的普通运行投影和 `config.yaml` 自动生成。`identity_only`、禁用或研究绑定可能被过滤，不代表完整 catalog 的全部身份。Claude、Claude Fable、DeepSeek、GPT 属于暂缓 App 同步的家族，现有手册仅保留，正文不参与本生成器的重写与一致性检查。",
        "",
        "| 模态 | 模型家族 | 规范模型数 | Route/API Form 组合数 | Profile/Case 数 | 文档 |",
        "|---|---|---:|---:|---:|---|",
    ]
    for modality, modality_cfg in capabilities["modalities"].items():
        for family, family_cfg in modality_cfg["families"].items():
            key = (str(modality), str(family))
            meta = FAMILY_META[key]
            models = family_cfg.get("models") or family_cfg.get("canonical_models") or {}
            combos = sum(
                len((route_cfg.get("api_forms") or {}))
                for route_cfg in (family_cfg.get("route_profiles") or {}).values()
            )
            if modality == "text":
                _combinations, _expectations, source_ids = _family_inventory(
                    str(modality), str(family), family_cfg
                )
                item_count = len(
                    {
                        profile
                        for source_id in source_ids
                        for profile in test_profiles_for_reference(source_id)
                    }
                )
            else:
                item_count = len(
                    {
                        case.name
                        for cases in _image_case_sets(str(family)).values()
                        for case in cases
                    }
                )
            path = OUTPUT_DIR / f"{meta['slug']}.md"
            if path not in rendered and key not in DEFERRED_APP_PARITY_FAMILIES:
                raise RuntimeError(f"Missing rendered family document: {path}")
            if not path.exists():
                raise RuntimeError(f"Missing family document: {path}")
            lines.append(
                f"| {modality} | `{family}` | {len(models)} | {combos} | {item_count} | "
                f"[{meta['title']}](./{meta['slug']}.md) |"
            )
    lines.extend(
        [
            "",
            "## 更新方法",
            "",
            "```bash",
            "python scripts/generate_test_docs.py",
            "python scripts/generate_test_docs.py --check",
            "```",
            "",
            "`--check` 不改文件；本次实际生成的索引与非暂缓家族文档不一致时退出 1。它不验证暂缓家族正文、被过滤的身份/研究矩阵、手写指南或历史实网证据。",
            "",
            "## 专用研究与历史证据",
            "",
            "- [源码仓库的 Fable 5 / 5.1 专项审计](https://github.com/zenoWZH/api_pressure/blob/main/docs/fable_5_5_1_param_audit_20260912.md)：5.1 已登记，专用研究 runner 与普通参数入口分开，普通执行尚未开放。",
            "",
        ]
    )
    return "\n".join(lines)


def build_documents() -> dict[Path, str]:
    capabilities = load_model_capability_profiles()
    config = _read_yaml(CONFIG_PATH)
    compatibility_profiles = config.get("compatibility_profiles") or {}
    rendered: dict[Path, str] = {}
    actual_families: set[tuple[str, str]] = set()
    for modality, modality_cfg in capabilities["modalities"].items():
        for family, family_cfg in modality_cfg["families"].items():
            key = (str(modality), str(family))
            actual_families.add(key)
            if key not in FAMILY_META:
                raise RuntimeError(f"Missing FAMILY_META entry for {key}")
            if key in DEFERRED_APP_PARITY_FAMILIES:
                continue
            slug = FAMILY_META[key]["slug"]
            rendered[OUTPUT_DIR / f"{slug}.md"] = _render_family_document(
                str(modality),
                str(family),
                family_cfg,
                compatibility_profiles,
            )
    stale_meta = sorted(set(FAMILY_META) - actual_families)
    if stale_meta:
        raise RuntimeError(f"FAMILY_META contains stale families: {stale_meta}")
    rendered[OUTPUT_DIR / "README.md"] = _render_index(capabilities, rendered)
    return rendered


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate or verify per-family model profile documentation."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify checked-in documents match the current schema without writing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    rendered = build_documents()
    failures: list[str] = []
    for path, expected in rendered.items():
        expected = expected.rstrip() + "\n"
        if args.check:
            actual = path.read_text(encoding="utf-8") if path.exists() else ""
            if actual != expected:
                failures.append(str(path.relative_to(PROJECT_ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8")
    if failures:
        print("Outdated generated documentation:")
        for item in failures:
            print(f"- {item}")
        return 1
    if not args.check:
        print(f"Generated {len(rendered)} documents under {OUTPUT_DIR.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
