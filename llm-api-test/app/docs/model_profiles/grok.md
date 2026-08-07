# Grok 模型家族 Profile 说明

<!-- 由 scripts/generate_test_docs.py 从 schema v4 生成，请勿手工维护表格。 -->

分别说明 Chat Completions 与 Responses 的 reasoning、JSON、工具和负向参数。

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
| `grok-4.5` | — |
| `grok-4.3` | — |
| `grok-4.20-0309-reasoning` | — |
| `grok-4.20-0309-non-reasoning` | — |
| `grok-4.20-multi-agent-0309` | `grok-4.20-multi-agent` |

## Route 与 API Form

| Route Profile | API Form | 内部 transport | 已注册模型数 | Reference Source |
|---|---|---|---:|---|
| `vendor_direct` | `openai_chat_completions` | `chat_completions` | 4 | `grok_chat_completions` |
| `vendor_direct` | `openai_responses` | `openai_responses` | 5 | `grok_responses` |
| `dynamic_aggregator` | `openai_chat_completions` | `chat_completions` | 4 | `grok_dynamic_chat` |
| `dynamic_aggregator` | `openai_responses` | `openai_responses` | 5 | `grok_dynamic_responses` |

## Reference Source

| Reference Source | 说明 | Route / API Form | 认证范围 | Profile 数 | 官方资料 |
|---|---|---|---|---:|---|
| `grok_chat_completions` | xAI Grok Chat Completions | `vendor_direct` / `openai_chat_completions` | `raw_route_contract` | 11 | [资料1](https://docs.x.ai/developers/rest-api-reference/inference/chat) [资料2](https://docs.x.ai/developers/model-capabilities/text/reasoning) [资料3](https://docs.x.ai/developers/grok-4-5) [资料4](https://docs.x.ai/developers/model-capabilities/legacy/chat-completions) |
| `grok_dynamic_chat` | Grok Chat Completions through an unpinned aggregator | `dynamic_aggregator` / `openai_chat_completions` | `adapter_only` | 11 | [资料1](https://docs.x.ai/developers/rest-api-reference/inference/chat) [资料2](https://docs.x.ai/developers/model-capabilities/text/reasoning) [资料3](https://docs.x.ai/developers/grok-4-5) [资料4](https://docs.x.ai/developers/model-capabilities/legacy/chat-completions) |
| `grok_dynamic_responses` | Grok Responses through an unpinned aggregator | `dynamic_aggregator` / `openai_responses` | `adapter_only` | 11 | [资料1](https://docs.x.ai/developers/model-capabilities/text/generate-text) [资料2](https://docs.x.ai/developers/model-capabilities/text/reasoning) [资料3](https://docs.x.ai/developers/model-capabilities/text/comparison) [资料4](https://docs.x.ai/developers/grok-4-5) |
| `grok_responses` | xAI Grok Responses | `vendor_direct` / `openai_responses` | `raw_route_contract` | 11 | [资料1](https://docs.x.ai/developers/model-capabilities/text/generate-text) [资料2](https://docs.x.ai/developers/model-capabilities/text/reasoning) [资料3](https://docs.x.ai/developers/model-capabilities/text/comparison) [资料4](https://docs.x.ai/developers/grok-4-5) |

## 全部参数 Profile

| Profile | 类别 | 具体测试目的 | 关键请求设置 | 期望 | 通过时还要检查 |
|---|---|---|---|---|---|
| `grok_stream` | 流式 | 验证 SSE 流式响应、结束标记和返回文本能够完整解析。<br>来源：`grok_chat_completions`、`grok_dynamic_chat` | `stream=true`<br>`max_completion_tokens=128`<br>`reasoning_effort="low"` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `grok_stream_usage` | 流式 | 验证 SSE 分块可解析，并在结束前得到独立、算术一致的 usage 信息。<br>来源：`grok_chat_completions`、`grok_dynamic_chat` | `stream=true`<br>`max_completion_tokens=128`<br>`reasoning_effort="low"`<br>`stream_options.include_usage=true` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `grok_max_completion_tokens` | 基础能力 | 验证 `max_completion_tokens`、`messages` 的请求兼容性以及对应响应字段是否正常。<br>来源：`grok_chat_completions`、`grok_dynamic_chat` | `stream=false`<br>`max_completion_tokens=128`<br>`reasoning_effort="low"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `grok_reasoning_effort_low` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`reasoning_effort`。<br>来源：`grok_chat_completions`、`grok_dynamic_chat` | `stream=false`<br>`max_completion_tokens=256`<br>`reasoning_effort="low"` | 按模型/route 变化 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `grok_reasoning_effort_medium` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`reasoning_effort`。<br>来源：`grok_chat_completions`、`grok_dynamic_chat` | `stream=false`<br>`max_completion_tokens=256`<br>`reasoning_effort="medium"` | 按模型/route 变化 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `grok_reasoning_effort_high` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`reasoning_effort`。<br>来源：`grok_chat_completions`、`grok_dynamic_chat` | `stream=false`<br>`max_completion_tokens=512`<br>`reasoning_effort="high"` | 按模型/route 变化 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `grok_reasoning_effort_none` | 负向/边界 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`reasoning_effort`。<br>来源：`grok_chat_completions`、`grok_dynamic_chat` | `stream=false`<br>`max_completion_tokens=64`<br>`reasoning_effort="none"` | 按模型/route 变化 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `grok_json` | 结构化输出 | 验证结构化输出参数 `max_completion_tokens`、`messages`、`response_format`，并确认最终内容是可解析且符合约束的 JSON。<br>来源：`grok_chat_completions`、`grok_dynamic_chat` | `stream=false`<br>`max_completion_tokens=256`<br>`reasoning_effort="low"`<br>`response_format.type="json_object"` | 应支持 | 内容必须能解析为 JSON；有 schema 时还要满足 schema。 |
| `grok_tools` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`grok_chat_completions`、`grok_dynamic_chat` | `stream=false`<br>`max_completion_tokens=256`<br>`reasoning_effort="low"`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`tool_choice="required"`<br>`multi_turn=true` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `grok_reject_stop` | 负向/边界 | 负向探针：发送文档不允许的 `messages`、`stop`，确认网关明确拒绝而不是静默吞掉。<br>来源：`grok_chat_completions`、`grok_dynamic_chat` | `stream=false`<br>`max_completion_tokens=64`<br>`reasoning_effort="low"`<br>`preserve_rejected_params=true`<br>`stop=["END"]` | 按模型/route 变化 | 应得到明确 400/422；若 2xx 则是 unexpected_acceptance。 |
| `grok_reject_presence_penalty` | 负向/边界 | 负向探针：发送文档不允许的 `messages`、`presence_penalty`，确认网关明确拒绝而不是静默吞掉。<br>来源：`grok_chat_completions`、`grok_dynamic_chat` | `stream=false`<br>`max_completion_tokens=64`<br>`reasoning_effort="low"`<br>`preserve_rejected_params=true`<br>`presence_penalty=0.5` | 按模型/route 变化 | 应得到明确 400/422；若 2xx 则是 unexpected_acceptance。 |
| `grok_responses_basic` | 基础能力 | 验证 `input`、`max_output_tokens` 的请求兼容性以及对应响应字段是否正常。<br>来源：`grok_dynamic_responses`、`grok_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=128`<br>`reasoning.effort="low"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `grok_responses_instructions` | 基础能力 | 验证系统指令字段 `input`、`instructions` 的协议位置和实际响应语义。<br>来源：`grok_dynamic_responses`、`grok_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=64`<br>`instructions="You are a concise assistant for API compatibility testin...`<br>`reasoning.effort="low"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `grok_responses_stream` | 流式 | 验证 SSE 流式响应、结束标记和返回文本能够完整解析。<br>来源：`grok_dynamic_responses`、`grok_responses` | `transport="openai_responses"`<br>`stream=true`<br>`store=false`<br>`max_output_tokens=128`<br>`reasoning.effort="low"` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `grok_responses_stream_usage` | 流式 | 验证 SSE 分块可解析，并在结束前得到独立、算术一致的 usage 信息。<br>来源：`grok_dynamic_responses`、`grok_responses` | `transport="openai_responses"`<br>`stream=true`<br>`store=false`<br>`max_output_tokens=128`<br>`reasoning.effort="low"` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `grok_responses_reasoning_low` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `input`、`reasoning.effort`。<br>来源：`grok_dynamic_responses`、`grok_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=256`<br>`reasoning.effort="low"` | 按模型/route 变化 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `grok_responses_reasoning_medium` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `input`、`reasoning.effort`。<br>来源：`grok_dynamic_responses`、`grok_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=256`<br>`reasoning.effort="medium"` | 按模型/route 变化 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `grok_responses_reasoning_high` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `input`、`reasoning.effort`。<br>来源：`grok_dynamic_responses`、`grok_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=512`<br>`reasoning.effort="high"` | 按模型/route 变化 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `grok_responses_reasoning_effort_none` | 负向/边界 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `input`、`reasoning.effort`。<br>来源：`grok_dynamic_responses`、`grok_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=64`<br>`reasoning.effort="none"` | 按模型/route 变化 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `grok_responses_json` | 结构化输出 | 验证结构化输出参数 `input`、`max_output_tokens`、`text.format`，并确认最终内容是可解析且符合约束的 JSON。<br>来源：`grok_dynamic_responses`、`grok_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=256`<br>`reasoning.effort="low"`<br>`text.format.type="json_object"` | 应支持 | 内容必须能解析为 JSON；有 schema 时还要满足 schema。 |
| `grok_responses_store_false` | 基础能力 | 验证 `input`、`store` 的请求兼容性以及对应响应字段是否正常。<br>来源：`grok_dynamic_responses`、`grok_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=64`<br>`reasoning.effort="low"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `grok_responses_tools` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`grok_dynamic_responses`、`grok_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=256`<br>`reasoning.effort="low"`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`tool_choice="required"`<br>`multi_turn=true` | 按模型/route 变化 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |

## 去哪里看结果

- Web 控制台会展示当前 model/route/API Form 的 profile 状态和最近一次结果。
- 文字参数结果：`reports/param_tests/<provider>/<model>/verdict.json` 或 Web Job 目录。
- 图片参数结果：`reports/jobs/<job_id>/summary.json`、`plan.json` 和逐 case 文件。
- 总体解释方法见 [参数测试说明](../parameter_testing.md)，不要只看顶层 `pass`。
