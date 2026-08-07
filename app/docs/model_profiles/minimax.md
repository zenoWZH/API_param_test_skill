# MiniMax 模型家族 Profile 说明

<!-- 由 scripts/generate_test_docs.py 从 schema v4 生成，请勿手工维护表格。 -->

使用精简的 Chat Completions 基础矩阵验证流式、采样、JSON、停止词和工具。

本文档回答三个问题：这个家族有哪些模型身份、在不同 route/API Form 下使用哪份测试契约、每个 profile 实际发送什么并检查什么。

## 在界面中使用本手册

![文字参数测试界面示意](../assets/ui/parameter-testing-console.svg)

先在界面按 Provider → Model → Route Profile → API Form → Reference Source 选择组合，再用下文表格确认本次会运行哪些 profile。

## 先理解判读规则

- `应支持`：期望 HTTP 2xx，且响应结构、内容语义、usage、returned-model 均通过校验。
- `应拒绝`：期望明确的 400/422；若仍返回 2xx，记为 `unexpected_acceptance`。
- `按模型/route 变化`：同一 profile 对家族内不同模型或 route 的期望不同，运行前以控制台展开的 model profile 为准。
- 动态聚合 route 即使全部通过，也只证明 adapter 兼容，不能证明物理上游或原厂合同。

## 模型与 alias

| 规范模型 | 显式 alias |
|---|---|
| `minimax-m2.5` | — |
| `minimax-m2.7` | — |
| `MiniMax-M3` | — |

## Route 与 API Form

| Route Profile | API Form | 内部 transport | 已注册模型数 | Reference Source |
|---|---|---|---:|---|
| `vendor_direct` | `openai_chat_completions` | `chat_completions` | 3 | `minimax_openai_compat` |
| `dynamic_aggregator` | `openai_chat_completions` | `chat_completions` | 3 | `minimax_dynamic_aggregator` |

## Reference Source

| Reference Source | 说明 | Route / API Form | 认证范围 | Profile 数 | 官方资料 |
|---|---|---|---|---:|---|
| `minimax_dynamic_aggregator` | MiniMax through an unpinned aggregator | `dynamic_aggregator` / `openai_chat_completions` | `adapter_only` | 7 | [资料1](https://platform.minimaxi.com/document/ChatCompletion%20v2) |
| `minimax_openai_compat` | MiniMax OpenAI-compatible Chat Completions | `vendor_direct` / `openai_chat_completions` | `raw_route_contract` | 7 | [资料1](https://platform.minimaxi.com/document/ChatCompletion%20v2) |

## 全部参数 Profile

| Profile | 类别 | 具体测试目的 | 关键请求设置 | 期望 | 通过时还要检查 |
|---|---|---|---|---|---|
| `basic_stream` | 流式 | 验证 SSE 流式响应、结束标记和返回文本能够完整解析。<br>来源：`minimax_dynamic_aggregator`、`minimax_openai_compat` | `stream=true`<br>`thinking.type="disabled"` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `stream_with_usage` | 流式 | 验证 SSE 分块可解析，并在结束前得到独立、算术一致的 usage 信息。<br>来源：`minimax_dynamic_aggregator`、`minimax_openai_compat` | `stream=true`<br>`thinking.type="disabled"`<br>`stream_options.include_usage=true` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `sampling_non_thinking` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`temperature`、`top_p`。<br>来源：`minimax_dynamic_aggregator`、`minimax_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`temperature=0.7`<br>`top_p=0.9` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `json_output` | 结构化输出 | 验证结构化输出参数 `max_tokens`、`messages`、`response_format`，并确认最终内容是可解析且符合约束的 JSON。<br>来源：`minimax_dynamic_aggregator`、`minimax_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`response_format.type="json_object"` | 应支持 | 内容必须能解析为 JSON；有 schema 时还要满足 schema。 |
| `stop_sequences` | 基础能力 | 验证停止序列参数 `messages`、`stop` 会影响结束位置或按契约被拒绝。<br>来源：`minimax_dynamic_aggregator`、`minimax_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`stop=["\n\n","END"]` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `tool_calls` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`minimax_dynamic_aggregator`、`minimax_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `tool_choice_required` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`minimax_dynamic_aggregator`、`minimax_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true`<br>`tool_choice="required"` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |

## 去哪里看结果

- Web 控制台会展示当前 model/route/API Form 的 profile 状态和最近一次结果。
- 文字参数结果：`reports/param_tests/<provider>/<model>/verdict.json` 或 Web Job 目录。
- 图片参数结果：`reports/jobs/<job_id>/summary.json`、`plan.json` 和逐 case 文件。
- 总体解释方法见 [参数测试说明](../parameter_testing.md)，不要只看顶层 `pass`。
