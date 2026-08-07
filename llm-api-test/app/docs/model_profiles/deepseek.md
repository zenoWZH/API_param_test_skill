# DeepSeek 模型家族 Profile 说明

<!-- 由 scripts/generate_test_docs.py 从 schema v4 生成，请勿手工维护表格。 -->

覆盖思考档位、采样、JSON、停止词、logprobs、工具调用以及阿里云/动态聚合差异。

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
| `deepseek-v4-flash` | — |
| `deepseek-v4-flash-0731` | — |
| `deepseek-v4-pro` | — |
| `deepseek-v3.2` | — |
| `deepseek-v3` | — |
| `deepseek-v3.1` | — |
| `deepseek-v4-flash-max` | — |
| `deepseek-v4-flash-none` | — |
| `deepseek-v4-pro-none` | — |
| `deepseek` | `deepseek-r1` |

## Route 与 API Form

| Route Profile | API Form | 内部 transport | 已注册模型数 | Reference Source |
|---|---|---|---:|---|
| `aliyun_maas` | `openai_chat_completions` | `chat_completions` | 3 | `aliyun_deepseek_v3_2_openai_compat`<br>`aliyun_deepseek_v4_openai_compat` |
| `vendor_direct` | `openai_chat_completions` | `chat_completions` | 9 | `deepseek_chat` |
| `dynamic_aggregator` | `openai_chat_completions` | `chat_completions` | 10 | `deepseek_dynamic_aggregator`<br>`deepseek_xinyunai_v4_flash_0731_openai_compat` |

## Reference Source

| Reference Source | 说明 | Route / API Form | 认证范围 | Profile 数 | 官方资料 |
|---|---|---|---|---:|---|
| `aliyun_deepseek_v3_2_openai_compat` | Alibaba Cloud Model Studio DeepSeek V3.2 OpenAI-compatible Chat | `aliyun_maas` / `openai_chat_completions` | `raw_route_contract` | 10 | [资料1](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions) [资料2](https://help.aliyun.com/zh/model-studio/deepseek-api) [资料3](https://help.aliyun.com/zh/model-studio/text-generation-model) |
| `aliyun_deepseek_v4_openai_compat` | Alibaba Cloud Model Studio DeepSeek V4 OpenAI-compatible Chat | `aliyun_maas` / `openai_chat_completions` | `raw_route_contract` | 13 | [资料1](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions) [资料2](https://help.aliyun.com/zh/model-studio/deepseek-api) [资料3](https://help.aliyun.com/zh/model-studio/text-generation-model) |
| `deepseek_chat` | DeepSeek Chat Completions | `vendor_direct` / `openai_chat_completions` | `raw_route_contract` | 18 | [资料1](https://api-docs.deepseek.com/api/create-chat-completion) [资料2](https://api-docs.deepseek.com/guides/thinking_mode) |
| `deepseek_dynamic_aggregator` | DeepSeek through an unpinned aggregator | `dynamic_aggregator` / `openai_chat_completions` | `adapter_only` | 18 | [资料1](https://api-docs.deepseek.com/api/create-chat-completion) [资料2](https://api-docs.deepseek.com/guides/thinking_mode) |
| `deepseek_xinyunai_v4_flash_0731_openai_compat` | DeepSeek V4 Flash 0731 through XinyunAI Chat Completions | `dynamic_aggregator` / `openai_chat_completions` | `adapter_only` | 18 | [资料1](https://api-docs.deepseek.com/updates/) [资料2](https://api-docs.deepseek.com/quick_start/pricing/) [资料3](https://api-docs.deepseek.com/guides/thinking_mode/) [资料4](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731) |

## 全部参数 Profile

| Profile | 类别 | 具体测试目的 | 关键请求设置 | 期望 | 通过时还要检查 |
|---|---|---|---|---|---|
| `aliyun_stream` | 流式 | 验证 SSE 流式响应、结束标记和返回文本能够完整解析。<br>来源：`aliyun_deepseek_v3_2_openai_compat`、`aliyun_deepseek_v4_openai_compat` | `stream=true`<br>`max_completion_tokens=128` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `aliyun_stream_usage` | 流式 | 验证 SSE 分块可解析，并在结束前得到独立、算术一致的 usage 信息。<br>来源：`aliyun_deepseek_v3_2_openai_compat`、`aliyun_deepseek_v4_openai_compat` | `stream=true`<br>`max_completion_tokens=128`<br>`stream_options.include_usage=true` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `aliyun_max_completion_tokens` | 基础能力 | 验证 `max_completion_tokens`、`messages` 的请求兼容性以及对应响应字段是否正常。<br>来源：`aliyun_deepseek_v3_2_openai_compat`、`aliyun_deepseek_v4_openai_compat` | `stream=false`<br>`max_completion_tokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `aliyun_enable_thinking` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `enable_thinking`、`max_completion_tokens`、`messages`。<br>来源：`aliyun_deepseek_v3_2_openai_compat`、`aliyun_deepseek_v4_openai_compat` | `stream=false`<br>`enable_thinking=true`<br>`max_completion_tokens=256` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `aliyun_disable_thinking` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `enable_thinking`、`max_completion_tokens`、`messages`。<br>来源：`aliyun_deepseek_v3_2_openai_compat`、`aliyun_deepseek_v4_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`max_completion_tokens=128` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `aliyun_temperature` | 采样 | 验证采样参数 `messages`、`temperature` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`aliyun_deepseek_v3_2_openai_compat`、`aliyun_deepseek_v4_openai_compat` | `stream=false`<br>`temperature=0.7`<br>`max_completion_tokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `aliyun_top_p` | 采样 | 验证采样参数 `messages`、`top_p` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`aliyun_deepseek_v3_2_openai_compat`、`aliyun_deepseek_v4_openai_compat` | `stream=false`<br>`top_p=0.8`<br>`max_completion_tokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `aliyun_stop` | 基础能力 | 验证停止序列参数 `messages`、`stop` 会影响结束位置或按契约被拒绝。<br>来源：`aliyun_deepseek_v3_2_openai_compat`、`aliyun_deepseek_v4_openai_compat` | `stream=false`<br>`stop=["END"]`<br>`max_completion_tokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `aliyun_tools` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`aliyun_deepseek_v3_2_openai_compat`、`aliyun_deepseek_v4_openai_compat` | `stream=false`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true`<br>`max_completion_tokens=512` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `aliyun_tool_choice_auto` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`aliyun_deepseek_v3_2_openai_compat`、`aliyun_deepseek_v4_openai_compat` | `stream=false`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true`<br>`max_completion_tokens=512`<br>`tool_choice="auto"` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `aliyun_reasoning_high` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `max_completion_tokens`、`messages`、`reasoning_effort`。<br>来源：`aliyun_deepseek_v4_openai_compat` | `stream=false`<br>`reasoning_effort="high"`<br>`max_completion_tokens=256` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `aliyun_reasoning_max` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `max_completion_tokens`、`messages`、`reasoning_effort`。<br>来源：`aliyun_deepseek_v4_openai_compat` | `stream=false`<br>`reasoning_effort="max"`<br>`max_completion_tokens=256` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `aliyun_json_object` | 结构化输出 | 验证结构化输出参数 `messages`、`response_format`，并确认最终内容是可解析且符合约束的 JSON。<br>来源：`aliyun_deepseek_v4_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`response_format.type="json_object"`<br>`max_completion_tokens=256` | 应支持 | 内容必须能解析为 JSON；有 schema 时还要满足 schema。 |
| `basic_stream` | 流式 | 验证 SSE 流式响应、结束标记和返回文本能够完整解析。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator`、`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=true`<br>`thinking.type="disabled"` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `stream_with_usage` | 流式 | 验证 SSE 分块可解析，并在结束前得到独立、算术一致的 usage 信息。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator`、`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=true`<br>`thinking.type="disabled"`<br>`stream_options.include_usage=true` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `thinking_low` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`reasoning_effort`、`response.reasoning_content`、`thinking`。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator`、`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=false`<br>`thinking.type="enabled"`<br>`reasoning_effort="low"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `thinking_enabled` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`reasoning_effort`、`response.reasoning_content`、`thinking`。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator`、`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=false`<br>`thinking.type="enabled"`<br>`reasoning_effort="high"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `thinking_max` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`reasoning_effort`、`response.reasoning_content`。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator`、`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=false`<br>`thinking.type="enabled"`<br>`reasoning_effort="max"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `thinking_disabled` | 负向/边界 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`response.reasoning_content`、`thinking`。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator`、`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=false`<br>`thinking.type="disabled"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `deepseek_max_tokens` | 基础能力 | 验证输出 token 上限字段 `max_tokens`、`messages` 使用当前 API Form 的正确名称和位置。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator`、`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`max_tokens=64` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `deepseek_user_id` | 基础能力 | 验证 `messages`、`user_id` 的请求兼容性以及对应响应字段是否正常。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator`、`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`user_id="loadtest_deepseek_param_user"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `sampling_non_thinking` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`temperature`、`top_p`。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator`、`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`temperature=0.7`<br>`top_p=0.9` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `json_output` | 结构化输出 | 验证结构化输出参数 `messages`、`response_format`，并确认最终内容是可解析且符合约束的 JSON。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator` | `stream=false`<br>`thinking.type="disabled"`<br>`response_format.type="json_object"` | 应支持 | 内容必须能解析为 JSON；有 schema 时还要满足 schema。 |
| `stop_sequences` | 基础能力 | 验证停止序列参数 `messages`、`stop` 会影响结束位置或按契约被拒绝。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator`、`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`stop=["\n\n","END"]` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `logprobs` | 基础能力 | 验证 `logprobs`、`messages`、`top_logprobs` 的请求兼容性以及对应响应字段是否正常。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator`、`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`logprobs=true`<br>`top_logprobs=5` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `tool_calls` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator`、`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `tool_choice_required` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator`、`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true`<br>`tool_choice="required"` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `deepseek_tool_strict` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator`、`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`tools_fixture="fixtures/tools_weather_strict.json"`<br>`tool_choice="required"`<br>`multi_turn=true` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `tool_calls_thinking` | 工具调用 | 验证推理模式下的结构化工具调用，并确认历史推理字段在 follow-up 中原样保留。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator`、`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=false`<br>`thinking.type="enabled"`<br>`reasoning_effort="high"`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true`<br>`pass_reasoning_content=true` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `deepseek_frequency_penalty` | 采样 | 验证采样参数 `frequency_penalty`、`messages` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator`、`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`frequency_penalty=0.2`<br>`send_deprecated=true` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `deepseek_presence_penalty` | 采样 | 验证采样参数 `messages`、`presence_penalty` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`deepseek_chat`、`deepseek_dynamic_aggregator`、`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`presence_penalty=0.2`<br>`send_deprecated=true` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `deepseek_json_output_256` | 结构化输出 | 验证结构化输出参数 `messages`、`response_format`，并确认最终内容是可解析且符合约束的 JSON。<br>来源：`deepseek_xinyunai_v4_flash_0731_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`response_format.type="json_object"`<br>`max_tokens=256` | 应支持 | 内容必须能解析为 JSON；有 schema 时还要满足 schema。 |

## 去哪里看结果

- Web 控制台会展示当前 model/route/API Form 的 profile 状态和最近一次结果。
- 文字参数结果：`reports/param_tests/<provider>/<model>/verdict.json` 或 Web Job 目录。
- 图片参数结果：`reports/jobs/<job_id>/summary.json`、`plan.json` 和逐 case 文件。
- 总体解释方法见 [参数测试说明](../parameter_testing.md)，不要只看顶层 `pass`。
