#!/usr/bin/env python3
"""Generate readable per-family parameter-profile manuals from schema v4.

The generated documents are intentionally checked in. Run with ``--check`` in
CI or review workflows to prove that every registered family/source/profile is
still documented.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAPABILITY_PATH = PROJECT_ROOT / "model_capability_profiles.yaml"
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
OUTPUT_DIR = PROJECT_ROOT / "docs" / "model_profiles"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.config import deep_merge  # noqa: E402
from lib.image_validation import (  # noqa: E402
    banana_variant_cases,
    gpt_image_2_cases,
    grok_imagine_cases,
)
from lib.reference_specs import (  # noqa: E402
    get_reference_source,
    load_model_capability_profile,
    load_model_capability_profiles,
    parameters_for_profile,
    resolve_profile_expectation,
    test_profiles_for_reference,
)


FAMILY_META: dict[tuple[str, str], dict[str, str]] = {
    ("text", "deepseek"): {
        "title": "DeepSeek",
        "slug": "deepseek",
        "summary": "覆盖思考档位、采样、JSON、停止词、logprobs、工具调用以及阿里云/动态聚合差异。",
    },
    ("text", "glm"): {
        "title": "GLM",
        "slug": "glm",
        "summary": "覆盖 GLM thinking、完整 reasoning_effort 档位、采样、结构化输出和工具流扩展。",
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
        "summary": "区分兼容 Chat 与 Gemini Interactions，并验证分辨率、宽高比和模型别名控制。",
    },
    ("image", "grok-imagine"): {
        "title": "Grok Imagine",
        "slug": "grok_imagine",
        "summary": "覆盖 1K/2K、宽高比、批量数量、URL/b64 交付与越界拒绝。",
    },
}


PROFILE_INTERNAL_KEYS = {
    "extends",
    "prompt",
    "prompt_key",
    "prompt_fixture",
    "fixture",
    "fixture_chars",
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
    "banana_512_square": "验证 Interactions 官方 512 分辨率档。",
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
    rows = _flatten_settings(settings)
    rendered = [f"`{key}={_compact_value(value)}`" for key, value in rows[:9]]
    if len(rows) > 9:
        rendered.append(f"另 {len(rows) - 9} 项")
    return "<br>".join(rendered) if rendered else "—"


def _category(profile: str) -> str:
    name = profile.casefold()
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
    if "reject" in name:
        return "应得到明确 400/422；若 2xx 则是 unexpected_acceptance。"
    if "tool" in name:
        return "不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。"
    if "json" in name or "schema" in name or "response_format" in name:
        return "内容必须能解析为 JSON；有 schema 时还要满足 schema。"
    if "stream" in name:
        return "检查 chunk 结构、结束标记、文本拼接与 usage 末块。"
    if any(word in name for word in ("thinking", "reasoning", "effort")):
        return "检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。"
    if "candidate" in name or name.endswith("_n"):
        return "核对响应候选数量，不以第一个候选成功代替整体成功。"
    return "2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。"


def _expectation_label(expectations: set[str]) -> str:
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
                capability = load_model_capability_profile(
                    modality,
                    family,
                    model,
                    path=CAPABILITY_PATH,
                    route_profile=str(route),
                    api_form=str(api_form),
                )
                allowed = set(capability.get("allowed_reference_sources") or [])
                combination_sources.update(allowed)
                source_ids.update(allowed)
                for source_id in allowed:
                    source = get_reference_source(source_id)
                    if source.get("model_family") != family:
                        continue
                    selected = load_model_capability_profile(
                        modality,
                        family,
                        model,
                        path=CAPABILITY_PATH,
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
        "| Route Profile | API Form | 内部 transport | 已注册模型数 | Reference Source |",
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
        "| Reference Source | 说明 | Route / API Form | 认证范围 | Profile 数 | 官方资料 |",
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
        settings = _resolve_profile_settings(compatibility_profiles, profile)
        parameters = sorted(parameter_map[profile])
        lines.append(
            f"| `{profile}` | {_category(profile)} | "
            f"{_profile_purpose(profile, parameters)}<br>来源："
            f"{'、'.join(f'`{item}`' for item in sorted(source_map[profile]))} | "
            f"{_compact_settings(settings)} | {_expectation_label(expectations[profile])} | "
            f"{_validation_focus(profile)} |"
        )
    return lines


def _image_case_sets(family: str) -> dict[str, list[Any]]:
    if family == "gpt-image-2":
        return {
            "openai_images_generations": gpt_image_2_cases(
                "full", include_4k=True, include_negative=True
            )
        }
    if family == "grok-imagine":
        return {
            "openai_images_generations": grok_imagine_cases(
                "full", include_2k=True, include_negative=True
            )
        }
    return {
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
                    path=CAPABILITY_PATH,
                    route_profile=str(route),
                    api_form=str(api_form),
                )
                for case_name in case_map:
                    if api_form in case_forms[case_name]:
                        expectations[case_name].add(
                            resolve_profile_expectation(
                                "image",
                                family,
                                model,
                                case_name,
                                capability_profile=cap,
                            )
                        )

    lines = [
        "| Case / Profile | API Form | 具体测试目的 | 关键请求设置 | 期望 |",
        "|---|---|---|---|---|",
    ]
    for case_name, case in case_map.items():
        settings = dict(case.parameters)
        if case.model_override:
            settings["model_override"] = case.model_override
        if case.expected_size:
            settings["expected_size"] = list(case.expected_size)
        forms = "<br>".join(f"`{item}`" for item in sorted(case_forms[case_name]))
        purpose = IMAGE_PURPOSES.get(case_name, case.description or "验证图片响应语义。")
        authored = "应拒绝" if case.expected_outcome == "rejection" else _expectation_label(expectations[case_name])
        lines.append(
            f"| `{case_name}` | {forms} | {purpose} | {_compact_settings(settings)} | {authored} |"
        )
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
        "<!-- 由 scripts/generate_test_docs.py 从 schema v4 生成，请勿手工维护表格。 -->",
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
            "先在界面按 Provider → Model → Route Profile → API Form → Reference Source 选择组合，再用下文表格确认本次会运行哪些 profile。"
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
                "## Reference Source",
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
        "本目录由 `model_capability_profiles.yaml`、`api_reference_specs.yaml` 和 `config.yaml` 自动生成。每个已注册模型家族必须有且只有一份说明文档；每个文字 Reference Source 的可执行 profile、每个图片 case 都必须出现在对应家族文档中。",
        "",
        "| 模态 | 模型家族 | 规范模型数 | Route/API Form 组合数 | Profile/Case 数 | 文档 |",
        "|---|---|---:|---:|---:|---|",
    ]
    for modality, modality_cfg in capabilities["modalities"].items():
        for family, family_cfg in modality_cfg["families"].items():
            meta = FAMILY_META[(modality, family)]
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
            if path not in rendered:
                raise RuntimeError(f"Missing rendered family document: {path}")
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
            "`--check` 不改文件；只要 schema 新增家族、Reference Source、profile 或图片 case 而文档尚未重生成，就会退出 1。",
            "",
        ]
    )
    return "\n".join(lines)


def build_documents() -> dict[Path, str]:
    capabilities = load_model_capability_profiles(CAPABILITY_PATH)
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
