# GPT 模型家族 Profile 说明

<!-- 由 scripts/generate_test_docs.py 从 schema v4 生成，请勿手工维护表格。 -->

区分通用 Chat、GPT-5.x Chat 与 Responses，并覆盖 reasoning、工具、JSON 和负向约束。

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
| `gpt-4o` | — |
| `gpt-5-mini` | — |
| `gpt-5.2` | — |
| `gpt-5.2-codex` | — |
| `gpt-5.3-codex` | — |
| `gpt-5.4` | `gpt-5.4-2026-03-05`, `gpt-5.4-openai-compact` |
| `gpt-5.4-mini` | `gpt-5.4-mini-2026-03-17` |
| `gpt-5.4-nano` | `gpt-5.4-nano-2026-03-17` |
| `gpt-5.5` | `gpt-5.5-2026-04-23`, `gpt-5.5-openai-compact` |
| `gpt-5.6-luna` | — |
| `gpt-5.6-sol` | — |
| `gpt-5.6-terra` | — |
| `gpt-5.6` | — |
| `gpt-image-2` | — |

## Route 与 API Form

| Route Profile | API Form | 内部 transport | 已注册模型数 | Reference Source |
|---|---|---|---:|---|
| `vendor_direct` | `openai_chat_completions` | `chat_completions` | 12 | `openai_chat_base`<br>`openai_gpt56_chat`<br>`openai_gpt5_chat` |
| `vendor_direct` | `openai_responses` | `openai_responses` | 11 | `openai_gpt56_responses`<br>`openai_responses` |
| `dynamic_aggregator` | `openai_chat_completions` | `chat_completions` | 12 | `gpt56_dynamic_chat`<br>`gpt5_dynamic_chat`<br>`gpt_dynamic_chat_base` |
| `dynamic_aggregator` | `openai_responses` | `openai_responses` | 11 | `gpt56_dynamic_responses`<br>`gpt_dynamic_responses` |

## Reference Source

| Reference Source | 说明 | Route / API Form | 认证范围 | Profile 数 | 官方资料 |
|---|---|---|---|---:|---|
| `gpt56_dynamic_chat` | GPT-5.6 Chat Completions through an unpinned aggregator | `dynamic_aggregator` / `openai_chat_completions` | `adapter_only` | 13 | [资料1](https://developers.openai.com/api/docs/models/gpt-5.6-sol) [资料2](https://developers.openai.com/api/docs/guides/model-guidance?model=gpt-5.6) [资料3](https://platform.openai.com/docs/api-reference/chat/create) |
| `gpt56_dynamic_responses` | GPT-5.6 Responses through an unpinned aggregator | `dynamic_aggregator` / `openai_responses` | `adapter_only` | 18 | [资料1](https://developers.openai.com/api/docs/models/gpt-5.6-sol) [资料2](https://developers.openai.com/api/docs/guides/model-guidance?model=gpt-5.6) [资料3](https://developers.openai.com/api/reference/resources/responses) |
| `gpt5_dynamic_chat` | GPT-5.x Chat Completions through an unpinned aggregator | `dynamic_aggregator` / `openai_chat_completions` | `adapter_only` | 7 | [资料1](https://platform.openai.com/docs/api-reference/chat/create) [资料2](https://developers.openai.com/api/docs/guides/reasoning) [资料3](https://developers.openai.com/api/docs/guides/migrate-to-responses) |
| `gpt_dynamic_chat_base` | GPT Chat Completions through an unpinned aggregator | `dynamic_aggregator` / `openai_chat_completions` | `adapter_only` | 7 | [资料1](https://platform.openai.com/docs/api-reference/chat/create) |
| `gpt_dynamic_responses` | GPT Responses through an unpinned aggregator | `dynamic_aggregator` / `openai_responses` | `adapter_only` | 8 | [资料1](https://developers.openai.com/api/reference/resources/responses) [资料2](https://developers.openai.com/api/docs/guides/migrate-to-responses) [资料3](https://developers.openai.com/api/docs/guides/reasoning) |
| `openai_chat_base` | GPT baseline over OpenAI Chat Completions | `vendor_direct` / `openai_chat_completions` | `raw_route_contract` | 7 | [资料1](https://platform.openai.com/docs/api-reference/chat/create) |
| `openai_gpt56_chat` | OpenAI GPT-5.6 Chat Completions | `vendor_direct` / `openai_chat_completions` | `raw_route_contract` | 13 | [资料1](https://developers.openai.com/api/docs/models/gpt-5.6-sol) [资料2](https://developers.openai.com/api/docs/guides/model-guidance?model=gpt-5.6) [资料3](https://platform.openai.com/docs/api-reference/chat/create) |
| `openai_gpt56_responses` | OpenAI GPT-5.6 Responses API | `vendor_direct` / `openai_responses` | `raw_route_contract` | 18 | [资料1](https://developers.openai.com/api/docs/models/gpt-5.6-sol) [资料2](https://developers.openai.com/api/docs/guides/model-guidance?model=gpt-5.6) [资料3](https://developers.openai.com/api/reference/resources/responses) |
| `openai_gpt5_chat` | OpenAI GPT-5.x Chat Completions | `vendor_direct` / `openai_chat_completions` | `raw_route_contract` | 7 | [资料1](https://platform.openai.com/docs/api-reference/chat/create) [资料2](https://developers.openai.com/api/docs/guides/reasoning) [资料3](https://developers.openai.com/api/docs/guides/migrate-to-responses) |
| `openai_responses` | OpenAI Responses API | `vendor_direct` / `openai_responses` | `raw_route_contract` | 8 | [资料1](https://developers.openai.com/api/reference/resources/responses) [资料2](https://developers.openai.com/api/docs/guides/migrate-to-responses) [资料3](https://developers.openai.com/api/docs/guides/reasoning) |

## 全部参数 Profile

| Profile | 类别 | 具体测试目的 | 关键请求设置 | 期望 | 通过时还要检查 |
|---|---|---|---|---|---|
| `gpt5_chat_stream` | 流式 | 验证 SSE 流式响应、结束标记和返回文本能够完整解析。<br>来源：`gpt56_dynamic_chat`、`gpt5_dynamic_chat`、`openai_gpt56_chat`、`openai_gpt5_chat` | `stream=true`<br>`max_completion_tokens=128`<br>`reasoning_effort="none"` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `gpt5_chat_stream_usage` | 流式 | 验证 SSE 分块可解析，并在结束前得到独立、算术一致的 usage 信息。<br>来源：`gpt56_dynamic_chat`、`gpt5_dynamic_chat`、`openai_gpt56_chat`、`openai_gpt5_chat` | `stream=true`<br>`max_completion_tokens=128`<br>`reasoning_effort="none"`<br>`stream_options.include_usage=true` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `gpt5_chat_max_completion_tokens` | 基础能力 | 验证 `max_completion_tokens`、`messages` 的请求兼容性以及对应响应字段是否正常。<br>来源：`gpt56_dynamic_chat`、`gpt5_dynamic_chat`、`openai_gpt56_chat`、`openai_gpt5_chat` | `stream=false`<br>`max_completion_tokens=128`<br>`reasoning_effort="none"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gpt5_chat_reasoning_none` | 负向/边界 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `max_completion_tokens`、`messages`、`reasoning_effort`。<br>来源：`gpt56_dynamic_chat`、`gpt5_dynamic_chat`、`openai_gpt56_chat`、`openai_gpt5_chat` | `stream=false`<br>`max_completion_tokens=128`<br>`reasoning_effort="none"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `gpt5_chat_reasoning_low` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `max_completion_tokens`、`messages`、`reasoning_effort`。<br>来源：`gpt56_dynamic_chat`、`gpt5_dynamic_chat`、`openai_gpt56_chat`、`openai_gpt5_chat` | `stream=false`<br>`max_completion_tokens=128`<br>`reasoning_effort="low"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `gpt5_chat_reasoning_medium` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `max_completion_tokens`、`messages`、`reasoning_effort`。<br>来源：`gpt56_dynamic_chat`、`openai_gpt56_chat` | `stream=false`<br>`max_completion_tokens=256`<br>`reasoning_effort="medium"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `gpt5_chat_reasoning_high` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `max_completion_tokens`、`messages`、`reasoning_effort`。<br>来源：`gpt56_dynamic_chat`、`openai_gpt56_chat` | `stream=false`<br>`max_completion_tokens=512`<br>`reasoning_effort="high"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `gpt5_chat_reasoning_xhigh` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `max_completion_tokens`、`messages`、`reasoning_effort`。<br>来源：`gpt56_dynamic_chat`、`openai_gpt56_chat` | `stream=false`<br>`max_completion_tokens=768`<br>`reasoning_effort="xhigh"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `gpt5_chat_reasoning_max` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `max_completion_tokens`、`messages`、`reasoning_effort`。<br>来源：`gpt56_dynamic_chat`、`openai_gpt56_chat` | `stream=false`<br>`max_completion_tokens=1024`<br>`reasoning_effort="max"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `gpt5_chat_json` | 结构化输出 | 验证结构化输出参数 `max_completion_tokens`、`messages`、`response_format`，并确认最终内容是可解析且符合约束的 JSON。<br>来源：`gpt56_dynamic_chat`、`gpt5_dynamic_chat`、`openai_gpt56_chat`、`openai_gpt5_chat` | `stream=false`<br>`max_completion_tokens=256`<br>`reasoning_effort="none"`<br>`response_format.type="json_object"` | 应支持 | 内容必须能解析为 JSON；有 schema 时还要满足 schema。 |
| `gpt5_chat_tools` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`gpt56_dynamic_chat`、`gpt5_dynamic_chat`、`openai_gpt56_chat`、`openai_gpt5_chat` | `stream=false`<br>`max_completion_tokens=256`<br>`reasoning_effort="none"`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`tool_choice="required"`<br>`multi_turn=true` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `gpt5_chat_reject_temperature` | 负向/边界 | 负向探针：发送文档不允许的 `max_completion_tokens`、`messages`、`temperature`，确认网关明确拒绝而不是静默吞掉。<br>来源：`gpt56_dynamic_chat`、`openai_gpt56_chat` | `stream=false`<br>`max_completion_tokens=64`<br>`reasoning_effort="low"`<br>`preserve_rejected_params=true`<br>`temperature=0.7` | 按模型/route 变化 | 应得到明确 400/422；若 2xx 则是 unexpected_acceptance。 |
| `gpt5_chat_reject_stop` | 负向/边界 | 负向探针：发送文档不允许的 `max_completion_tokens`、`messages`、`stop`，确认网关明确拒绝而不是静默吞掉。<br>来源：`gpt56_dynamic_chat`、`openai_gpt56_chat` | `stream=false`<br>`max_completion_tokens=64`<br>`reasoning_effort="low"`<br>`preserve_rejected_params=true`<br>`stop=["END"]` | 按模型/route 变化 | 应得到明确 400/422；若 2xx 则是 unexpected_acceptance。 |
| `openai_responses_basic` | 基础能力 | 验证 `input`、`max_output_tokens`、`reasoning.effort` 的请求兼容性以及对应响应字段是否正常。<br>来源：`gpt56_dynamic_responses`、`gpt_dynamic_responses`、`openai_gpt56_responses`、`openai_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=128`<br>`reasoning.effort="low"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `openai_responses_instructions` | 基础能力 | 验证系统指令字段 `input`、`instructions`、`max_output_tokens` 的协议位置和实际响应语义。<br>来源：`gpt56_dynamic_responses`、`gpt_dynamic_responses`、`openai_gpt56_responses`、`openai_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=64`<br>`instructions="You are a concise assistant for API compatibility testin...`<br>`reasoning.effort="none"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `openai_responses_stream` | 流式 | 验证 SSE 流式响应、结束标记和返回文本能够完整解析。<br>来源：`gpt56_dynamic_responses`、`gpt_dynamic_responses`、`openai_gpt56_responses`、`openai_responses` | `transport="openai_responses"`<br>`stream=true`<br>`store=false`<br>`max_output_tokens=128`<br>`reasoning.effort="none"` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `openai_responses_stream_usage` | 流式 | 验证 SSE 分块可解析，并在结束前得到独立、算术一致的 usage 信息。<br>来源：`gpt56_dynamic_responses`、`gpt_dynamic_responses`、`openai_gpt56_responses`、`openai_responses` | `transport="openai_responses"`<br>`stream=true`<br>`store=false`<br>`max_output_tokens=128`<br>`reasoning.effort="none"` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `openai_responses_reasoning_none` | 负向/边界 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `input`、`max_output_tokens`、`reasoning.effort`。<br>来源：`gpt56_dynamic_responses`、`openai_gpt56_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=128`<br>`reasoning.effort="none"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `openai_responses_reasoning_low` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `input`、`max_output_tokens`、`reasoning.effort`。<br>来源：`gpt56_dynamic_responses`、`openai_gpt56_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=256`<br>`reasoning.effort="low"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `openai_responses_reasoning` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `input`、`max_output_tokens`、`reasoning.effort`。<br>来源：`gpt56_dynamic_responses`、`gpt_dynamic_responses`、`openai_gpt56_responses`、`openai_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=256`<br>`reasoning.effort="medium"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `openai_responses_reasoning_high` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `input`、`max_output_tokens`、`reasoning.effort`。<br>来源：`gpt56_dynamic_responses`、`openai_gpt56_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=512`<br>`reasoning.effort="high"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `openai_responses_reasoning_xhigh` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `input`、`max_output_tokens`、`reasoning.effort`。<br>来源：`gpt56_dynamic_responses`、`openai_gpt56_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=768`<br>`reasoning.effort="xhigh"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `openai_responses_reasoning_max` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `input`、`max_output_tokens`、`reasoning.effort`。<br>来源：`gpt56_dynamic_responses`、`openai_gpt56_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=1024`<br>`reasoning.effort="max"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `openai_responses_reasoning_context_all_turns` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `input`、`max_output_tokens`、`reasoning.context`、`reasoning.effort`。<br>来源：`gpt56_dynamic_responses`、`openai_gpt56_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=256`<br>`reasoning.effort="medium"`<br>`reasoning.context="all_turns"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `openai_responses_reasoning_context_current_turn` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `input`、`max_output_tokens`、`reasoning.context`、`reasoning.effort`。<br>来源：`gpt56_dynamic_responses`、`openai_gpt56_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=256`<br>`reasoning.effort="medium"`<br>`reasoning.context="current_turn"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `openai_responses_pro_medium` | 基础能力 | 验证 `input`、`max_output_tokens`、`reasoning.effort`、`reasoning.mode` 的请求兼容性以及对应响应字段是否正常。<br>来源：`gpt56_dynamic_responses`、`openai_gpt56_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=512`<br>`reasoning.mode="pro"`<br>`reasoning.effort="medium"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `openai_responses_explicit_cache` | 缓存参数 | 验证请求级缓存标识/缓存内容参数 `input`、`max_output_tokens`、`prompt_cache_key`、`prompt_cache_options.mode`、`prompt_cache_options.ttl` 能被正确接收和报告。<br>来源：`gpt56_dynamic_responses`、`openai_gpt56_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=128`<br>`prompt_cache_key="param-test-gpt56-sol"`<br>`prompt_cache_options.mode="explicit"`<br>`prompt_cache_options.ttl="30m"`<br>`reasoning.effort="none"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `openai_responses_verbosity` | 基础能力 | 验证 `input`、`max_output_tokens`、`text.verbosity` 的请求兼容性以及对应响应字段是否正常。<br>来源：`gpt56_dynamic_responses`、`openai_gpt56_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=128`<br>`reasoning.effort="none"`<br>`text.verbosity="low"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `openai_responses_json` | 结构化输出 | 验证结构化输出参数 `input`、`max_output_tokens`、`text.format`，并确认最终内容是可解析且符合约束的 JSON。<br>来源：`gpt56_dynamic_responses`、`gpt_dynamic_responses`、`openai_gpt56_responses`、`openai_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=256`<br>`reasoning.effort="none"`<br>`text.format.type="json_object"` | 应支持 | 内容必须能解析为 JSON；有 schema 时还要满足 schema。 |
| `openai_responses_store_false` | 基础能力 | 验证 `input`、`max_output_tokens`、`store` 的请求兼容性以及对应响应字段是否正常。<br>来源：`gpt56_dynamic_responses`、`gpt_dynamic_responses`、`openai_gpt56_responses`、`openai_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=64`<br>`reasoning.effort="none"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `openai_responses_tools` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`gpt56_dynamic_responses`、`gpt_dynamic_responses`、`openai_gpt56_responses`、`openai_responses` | `transport="openai_responses"`<br>`stream=false`<br>`store=false`<br>`max_output_tokens=256`<br>`reasoning.effort="low"`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`tool_choice="required"`<br>`multi_turn=true` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `basic_stream` | 流式 | 验证 SSE 流式响应、结束标记和返回文本能够完整解析。<br>来源：`gpt_dynamic_chat_base`、`openai_chat_base` | `stream=true`<br>`thinking.type="disabled"` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `stream_with_usage` | 流式 | 验证 SSE 分块可解析，并在结束前得到独立、算术一致的 usage 信息。<br>来源：`gpt_dynamic_chat_base`、`openai_chat_base` | `stream=true`<br>`thinking.type="disabled"`<br>`stream_options.include_usage=true` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `sampling_non_thinking` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`temperature`、`top_p`。<br>来源：`gpt_dynamic_chat_base`、`openai_chat_base` | `stream=false`<br>`thinking.type="disabled"`<br>`temperature=0.7`<br>`top_p=0.9` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `json_output` | 结构化输出 | 验证结构化输出参数 `max_tokens`、`messages`、`response_format`，并确认最终内容是可解析且符合约束的 JSON。<br>来源：`gpt_dynamic_chat_base`、`openai_chat_base` | `stream=false`<br>`thinking.type="disabled"`<br>`response_format.type="json_object"` | 应支持 | 内容必须能解析为 JSON；有 schema 时还要满足 schema。 |
| `stop_sequences` | 基础能力 | 验证停止序列参数 `messages`、`stop` 会影响结束位置或按契约被拒绝。<br>来源：`gpt_dynamic_chat_base`、`openai_chat_base` | `stream=false`<br>`thinking.type="disabled"`<br>`stop=["\n\n","END"]` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `tool_calls` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`gpt_dynamic_chat_base`、`openai_chat_base` | `stream=false`<br>`thinking.type="disabled"`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `tool_choice_required` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`gpt_dynamic_chat_base`、`openai_chat_base` | `stream=false`<br>`thinking.type="disabled"`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true`<br>`tool_choice="required"` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |

## 去哪里看结果

- Web 控制台会展示当前 model/route/API Form 的 profile 状态和最近一次结果。
- 文字参数结果：`reports/param_tests/<provider>/<model>/verdict.json` 或 Web Job 目录。
- 图片参数结果：`reports/jobs/<job_id>/summary.json`、`plan.json` 和逐 case 文件。
- 总体解释方法见 [参数测试说明](../parameter_testing.md)，不要只看顶层 `pass`。
