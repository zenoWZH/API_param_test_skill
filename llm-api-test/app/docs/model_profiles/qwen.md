# Qwen 模型家族 Profile 说明

<!-- 由 scripts/generate_test_docs.py 从 schema v4 生成，请勿手工维护表格。 -->

覆盖 Qwen thinking、搜索、代码解释器、采样、结构化输出和并行工具调用。

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
| `qwen3.7-max` | `qwen3.7max`, `qwen3.7-max-2026-06-08` |
| `qwen3.7-plus` | — |
| `qwen3.6-plus` | — |
| `qwen3.5-plus` | — |
| `qwen3.5-flash` | — |

## Route 与 API Form

| Route Profile | API Form | 内部 transport | 已注册模型数 | Reference Source |
|---|---|---|---:|---|
| `aliyun_maas` | `openai_chat_completions` | `chat_completions` | 5 | `qwen_openai_compat` |
| `dynamic_aggregator` | `openai_chat_completions` | `chat_completions` | 5 | `qwen_dynamic_aggregator` |

## Reference Source

| Reference Source | 说明 | Route / API Form | 认证范围 | Profile 数 | 官方资料 |
|---|---|---|---|---:|---|
| `qwen_dynamic_aggregator` | Qwen through an unpinned aggregator | `dynamic_aggregator` / `openai_chat_completions` | `adapter_only` | 25 | [资料1](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions) [资料2](https://help.aliyun.com/zh/model-studio/deep-thinking) [资料3](https://help.aliyun.com/zh/model-studio/qwen3-7-max) [资料4](https://help.aliyun.com/zh/model-studio/qwen-structured-output) [资料5](https://help.aliyun.com/zh/model-studio/models) |
| `qwen_openai_compat` | Qwen DashScope OpenAI-compatible Chat Completions (官方原生扩展) | `aliyun_maas` / `openai_chat_completions` | `raw_route_contract` | 25 | [资料1](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions) [资料2](https://help.aliyun.com/zh/model-studio/deep-thinking) [资料3](https://help.aliyun.com/zh/model-studio/qwen3-7-max) [资料4](https://help.aliyun.com/zh/model-studio/qwen-structured-output) [资料5](https://help.aliyun.com/zh/model-studio/models) |

## 全部参数 Profile

| Profile | 类别 | 具体测试目的 | 关键请求设置 | 期望 | 通过时还要检查 |
|---|---|---|---|---|---|
| `basic_stream` | 流式 | 验证 SSE 流式响应、结束标记和返回文本能够完整解析。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=true`<br>`thinking.type="disabled"` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `stream_with_usage` | 流式 | 验证 SSE 分块可解析，并在结束前得到独立、算术一致的 usage 信息。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=true`<br>`thinking.type="disabled"`<br>`stream_options.include_usage=true` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `qwen_thinking_enabled` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `enable_thinking`、`max_completion_tokens`、`messages`、`response.reasoning_content`、`stream`。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `omit_params=["max_tokens"]`<br>`stream=true`<br>`enable_thinking=true`<br>`max_completion_tokens=128` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `qwen_thinking_disabled` | 负向/边界 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `enable_thinking`、`messages`、`response.reasoning_content`。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=false`<br>`enable_thinking=false` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `qwen_thinking_budget` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `enable_thinking`、`max_completion_tokens`、`messages`、`response.reasoning_content`、`stream`、`thinking_budget`。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `omit_params=["max_tokens"]`<br>`stream=true`<br>`enable_thinking=true`<br>`thinking_budget=64`<br>`max_completion_tokens=128` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `qwen_preserve_thinking` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `enable_thinking`、`max_completion_tokens`、`messages`、`preserve_thinking`、`response.reasoning_content`、`stream`。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `omit_params=["max_tokens"]`<br>`stream=false`<br>`enable_thinking=true`<br>`preserve_thinking=true`<br>`max_completion_tokens=512`<br>`messages=[3 items]` | 按模型/route 变化 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `qwen_max_tokens` | 基础能力 | 验证输出 token 上限字段 `max_tokens`、`messages` 使用当前 API Form 的正确名称和位置。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`max_tokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `qwen_max_completion_tokens` | 基础能力 | 验证 `max_completion_tokens`、`messages` 的请求兼容性以及对应响应字段是否正常。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `omit_params=["max_tokens"]`<br>`stream=false`<br>`enable_thinking=false`<br>`max_completion_tokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `stop_sequences` | 基础能力 | 验证停止序列参数 `messages`、`stop` 会影响结束位置或按契约被拒绝。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`stop=["\n\n","END"]` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `qwen_temperature` | 采样 | 验证采样参数 `messages`、`temperature` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`temperature=0.7` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `qwen_top_p` | 采样 | 验证采样参数 `messages`、`top_p` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`top_p=0.8` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `qwen_top_k` | 采样 | 验证采样参数 `messages`、`top_k` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`top_k=20` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `qwen_repetition_penalty` | 采样 | 验证采样参数 `messages`、`repetition_penalty` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`repetition_penalty=1.05` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `qwen_presence_penalty` | 采样 | 验证采样参数 `messages`、`presence_penalty` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`presence_penalty=0.2` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `qwen_seed` | 采样 | 验证采样参数 `messages`、`seed` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`seed=123456789` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `qwen_n` | 采样 | 验证候选数量 `max_tokens`、`messages`、`n`，并核对响应实际返回的候选数。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`n=2`<br>`max_tokens=64` | 应支持 | 核对响应候选数量，不以第一个候选成功代替整体成功。 |
| `qwen_logprobs` | 基础能力 | 验证 `logprobs`、`max_tokens`、`messages`、`top_logprobs` 的请求兼容性以及对应响应字段是否正常。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`logprobs=true`<br>`top_logprobs=5`<br>`max_tokens=64` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `qwen_response_format` | 结构化输出 | 验证结构化输出参数 `messages`、`response_format`，并确认最终内容是可解析且符合约束的 JSON。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`response_format.type="json_object"` | 应支持 | 内容必须能解析为 JSON；有 schema 时还要满足 schema。 |
| `qwen_enable_search` | 基础能力 | 验证 `enable_search`、`messages` 的请求兼容性以及对应响应字段是否正常。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`enable_search=false` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `qwen_search_options` | 基础能力 | 验证 `enable_search`、`messages`、`search_options` 的请求兼容性以及对应响应字段是否正常。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`enable_search=true`<br>`search_options.forced_search=false`<br>`search_options.search_strategy="turbo"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `qwen_enable_code_interpreter` | 基础能力 | 验证 `enable_code_interpreter`、`messages`、`stream` 的请求兼容性以及对应响应字段是否正常。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=true`<br>`enable_thinking=true`<br>`enable_code_interpreter=false` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `qwen_tools` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `qwen_tool_choice_auto` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true`<br>`tool_choice="auto"` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `qwen_parallel_tool_calls` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`parallel_tool_calls=true`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`tool_choice="auto"` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `qwen_tool_stream` | 工具调用 | 验证 SSE 流式响应、结束标记和返回文本能够完整解析。<br>来源：`qwen_dynamic_aggregator`、`qwen_openai_compat` | `stream=true`<br>`enable_thinking=false`<br>`tool_stream=true`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`tool_choice="auto"` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |

## 去哪里看结果

- Web 控制台会展示当前 model/route/API Form 的 profile 状态和最近一次结果。
- 文字参数结果：`reports/param_tests/<provider>/<model>/verdict.json` 或 Web Job 目录。
- 图片参数结果：`reports/jobs/<job_id>/summary.json`、`plan.json` 和逐 case 文件。
- 总体解释方法见 [参数测试说明](../parameter_testing.md)，不要只看顶层 `pass`。
