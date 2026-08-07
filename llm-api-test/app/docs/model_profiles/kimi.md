# Kimi 模型家族 Profile 说明

<!-- 由 scripts/generate_test_docs.py 从 schema v4 生成，请勿手工维护表格。 -->

区分 K2.x、K3、阿里云、OpenRouter 与动态聚合，并记录 K3 的固定采样与身份约束。

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
| `kimi-k2.5` | `Kimi-K2.5` |
| `kimi-k2.6` | — |
| `kimi-k2.7-code` | — |
| `kimi-k3` | `moonshotai/kimi-k3`, `Kimi-K3` |

## Route 与 API Form

| Route Profile | API Form | 内部 transport | 已注册模型数 | Reference Source |
|---|---|---|---:|---|
| `aliyun_maas` | `openai_chat_completions` | `chat_completions` | 2 | `aliyun_kimi_k2_6_openai_compat`<br>`aliyun_kimi_k2_7_code_openai_compat` |
| `vendor_direct` | `openai_chat_completions` | `chat_completions` | 4 | `kimi_k3_openai_compat`<br>`kimi_openai_compat` |
| `dynamic_aggregator` | `openai_chat_completions` | `chat_completions` | 4 | `kimi_dynamic_aggregator`<br>`kimi_k3_dynamic_aggregator` |
| `openrouter` | `openai_chat_completions` | `chat_completions` | 1 | `kimi_k3_openrouter` |

## Reference Source

| Reference Source | 说明 | Route / API Form | 认证范围 | Profile 数 | 官方资料 |
|---|---|---|---|---:|---|
| `aliyun_kimi_k2_6_openai_compat` | Alibaba Cloud Model Studio Kimi K2.6 OpenAI-compatible Chat | `aliyun_maas` / `openai_chat_completions` | `raw_route_contract` | 12 | [资料1](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions) [资料2](https://help.aliyun.com/zh/model-studio/kimi-api) [资料3](https://help.aliyun.com/zh/model-studio/text-generation-model) |
| `aliyun_kimi_k2_7_code_openai_compat` | Alibaba Cloud Model Studio Kimi K2.7 Code OpenAI-compatible Chat | `aliyun_maas` / `openai_chat_completions` | `raw_route_contract` | 11 | [资料1](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions) [资料2](https://help.aliyun.com/zh/model-studio/kimi-api) [资料3](https://help.aliyun.com/zh/model-studio/text-generation-model) |
| `kimi_dynamic_aggregator` | Kimi through an unpinned aggregator | `dynamic_aggregator` / `openai_chat_completions` | `adapter_only` | 7 | [资料1](https://platform.moonshot.ai/docs/api/chat) |
| `kimi_k3_dynamic_aggregator` | Kimi K3 through an unpinned aggregator | `dynamic_aggregator` / `openai_chat_completions` | `adapter_only` | 13 | [资料1](https://github.com/MoonshotAI/Kimi-K3#6-model-usage) [资料2](https://platform.kimi.ai/docs/api/chat) [资料3](https://github.com/MoonshotAI/Kimi-Vendor-Verifier) [资料4](https://github.com/MoonshotAI/Kimi-Vendor-Verifier/tree/main/tests/k3_features) [资料5](https://github.com/MoonshotAI/Kimi-Vendor-Verifier/tree/main/tests/params) |
| `kimi_k3_openai_compat` | Kimi K3 OpenAI-compatible Chat Completions | `vendor_direct` / `openai_chat_completions` | `raw_route_contract` | 13 | [资料1](https://github.com/MoonshotAI/Kimi-K3#6-model-usage) [资料2](https://platform.kimi.ai/docs/api/chat) [资料3](https://github.com/MoonshotAI/Kimi-Vendor-Verifier) [资料4](https://github.com/MoonshotAI/Kimi-Vendor-Verifier/tree/main/tests/k3_features) [资料5](https://github.com/MoonshotAI/Kimi-Vendor-Verifier/tree/main/tests/params) |
| `kimi_k3_openrouter` | Kimi K3 through OpenRouter | `openrouter` / `openai_chat_completions` | `adapter_only` | 13 | [资料1](https://openrouter.ai/docs/guides/routing/provider-selection) [资料2](https://openrouter.ai/docs/guides/features/router-metadata) |
| `kimi_openai_compat` | Kimi OpenAI-compatible Chat Completions | `vendor_direct` / `openai_chat_completions` | `raw_route_contract` | 7 | [资料1](https://platform.moonshot.ai/docs/api/chat) |

## 全部参数 Profile

| Profile | 类别 | 具体测试目的 | 关键请求设置 | 期望 | 通过时还要检查 |
|---|---|---|---|---|---|
| `aliyun_stream` | 流式 | 验证 SSE 流式响应、结束标记和返回文本能够完整解析。<br>来源：`aliyun_kimi_k2_6_openai_compat`、`aliyun_kimi_k2_7_code_openai_compat` | `stream=true`<br>`max_completion_tokens=128` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `aliyun_stream_usage` | 流式 | 验证 SSE 分块可解析，并在结束前得到独立、算术一致的 usage 信息。<br>来源：`aliyun_kimi_k2_6_openai_compat`、`aliyun_kimi_k2_7_code_openai_compat` | `stream=true`<br>`max_completion_tokens=128`<br>`stream_options.include_usage=true` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `aliyun_max_completion_tokens` | 基础能力 | 验证 `max_completion_tokens`、`messages` 的请求兼容性以及对应响应字段是否正常。<br>来源：`aliyun_kimi_k2_6_openai_compat`、`aliyun_kimi_k2_7_code_openai_compat` | `stream=false`<br>`max_completion_tokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `aliyun_enable_thinking` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `enable_thinking`、`max_completion_tokens`、`messages`。<br>来源：`aliyun_kimi_k2_6_openai_compat`、`aliyun_kimi_k2_7_code_openai_compat` | `stream=false`<br>`enable_thinking=true`<br>`max_completion_tokens=256` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `aliyun_disable_thinking` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `enable_thinking`、`max_completion_tokens`、`messages`。<br>来源：`aliyun_kimi_k2_6_openai_compat` | `stream=false`<br>`enable_thinking=false`<br>`max_completion_tokens=128` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `aliyun_preserve_thinking` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `max_completion_tokens`、`messages`、`preserve_thinking`。<br>来源：`aliyun_kimi_k2_6_openai_compat`、`aliyun_kimi_k2_7_code_openai_compat` | `stream=false`<br>`enable_thinking=true`<br>`preserve_thinking=true`<br>`max_completion_tokens=256` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `aliyun_temperature` | 采样 | 验证采样参数 `messages`、`temperature` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`aliyun_kimi_k2_6_openai_compat`、`aliyun_kimi_k2_7_code_openai_compat` | `stream=false`<br>`temperature=0.7`<br>`max_completion_tokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `aliyun_top_p` | 采样 | 验证采样参数 `messages`、`top_p` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`aliyun_kimi_k2_6_openai_compat`、`aliyun_kimi_k2_7_code_openai_compat` | `stream=false`<br>`top_p=0.8`<br>`max_completion_tokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `aliyun_presence_penalty` | 采样 | 验证采样参数 `messages`、`presence_penalty` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`aliyun_kimi_k2_6_openai_compat`、`aliyun_kimi_k2_7_code_openai_compat` | `stream=false`<br>`presence_penalty=0.2`<br>`max_completion_tokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `aliyun_stop` | 基础能力 | 验证停止序列参数 `messages`、`stop` 会影响结束位置或按契约被拒绝。<br>来源：`aliyun_kimi_k2_6_openai_compat`、`aliyun_kimi_k2_7_code_openai_compat` | `stream=false`<br>`stop=["END"]`<br>`max_completion_tokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `aliyun_tools` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`aliyun_kimi_k2_6_openai_compat`、`aliyun_kimi_k2_7_code_openai_compat` | `stream=false`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true`<br>`max_completion_tokens=512` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `aliyun_tool_choice_auto` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`aliyun_kimi_k2_6_openai_compat`、`aliyun_kimi_k2_7_code_openai_compat` | `stream=false`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true`<br>`max_completion_tokens=512`<br>`tool_choice="auto"` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `basic_stream` | 流式 | 验证 SSE 流式响应、结束标记和返回文本能够完整解析。<br>来源：`kimi_dynamic_aggregator`、`kimi_openai_compat` | `stream=true`<br>`thinking.type="disabled"` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `stream_with_usage` | 流式 | 验证 SSE 分块可解析，并在结束前得到独立、算术一致的 usage 信息。<br>来源：`kimi_dynamic_aggregator`、`kimi_openai_compat` | `stream=true`<br>`thinking.type="disabled"`<br>`stream_options.include_usage=true` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `sampling_non_thinking` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`temperature`、`top_p`。<br>来源：`kimi_dynamic_aggregator`、`kimi_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`temperature=0.7`<br>`top_p=0.9` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `json_output` | 结构化输出 | 验证结构化输出参数 `max_tokens`、`messages`、`response_format`，并确认最终内容是可解析且符合约束的 JSON。<br>来源：`kimi_dynamic_aggregator`、`kimi_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`response_format.type="json_object"` | 按模型/route 变化 | 内容必须能解析为 JSON；有 schema 时还要满足 schema。 |
| `stop_sequences` | 基础能力 | 验证停止序列参数 `messages`、`stop` 会影响结束位置或按契约被拒绝。<br>来源：`kimi_dynamic_aggregator`、`kimi_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`stop=["\n\n","END"]` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `tool_calls` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`kimi_dynamic_aggregator`、`kimi_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `tool_choice_required` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`kimi_dynamic_aggregator`、`kimi_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true`<br>`tool_choice="required"` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `kimi_k3_stream` | 流式 | 验证 SSE 流式响应、结束标记和返回文本能够完整解析。<br>来源：`kimi_k3_dynamic_aggregator`、`kimi_k3_openai_compat`、`kimi_k3_openrouter` | `stream=true`<br>`temperature=1.0`<br>`top_p=0.95`<br>`max_completion_tokens=512`<br>`reasoning_effort="max"` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `kimi_k3_stream_usage` | 流式 | 验证 SSE 分块可解析，并在结束前得到独立、算术一致的 usage 信息。<br>来源：`kimi_k3_dynamic_aggregator`、`kimi_k3_openai_compat`、`kimi_k3_openrouter` | `stream=true`<br>`temperature=1.0`<br>`top_p=0.95`<br>`max_completion_tokens=512`<br>`reasoning_effort="max"`<br>`stream_options.include_usage=true` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `kimi_k3_reasoning_low` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `max_completion_tokens`、`messages`、`reasoning_effort`、`response.reasoning_content`、`temperature`、`top_p`。<br>来源：`kimi_k3_dynamic_aggregator`、`kimi_k3_openai_compat`、`kimi_k3_openrouter` | `stream=false`<br>`temperature=1.0`<br>`top_p=0.95`<br>`max_completion_tokens=512`<br>`reasoning_effort="low"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `kimi_k3_reasoning_high` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `max_completion_tokens`、`messages`、`reasoning_effort`、`response.reasoning_content`、`temperature`、`top_p`。<br>来源：`kimi_k3_dynamic_aggregator`、`kimi_k3_openai_compat`、`kimi_k3_openrouter` | `stream=false`<br>`temperature=1.0`<br>`top_p=0.95`<br>`max_completion_tokens=512`<br>`reasoning_effort="high"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `kimi_k3_reasoning_max` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `max_completion_tokens`、`messages`、`reasoning_effort`、`response.reasoning_content`、`temperature`、`top_p`。<br>来源：`kimi_k3_dynamic_aggregator`、`kimi_k3_openai_compat`、`kimi_k3_openrouter` | `stream=false`<br>`temperature=1.0`<br>`top_p=0.95`<br>`max_completion_tokens=512`<br>`reasoning_effort="max"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `kimi_k3_preserved_thinking` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `max_completion_tokens`、`messages`、`messages[].reasoning_content`、`reasoning_effort`、`response.reasoning_content`、`temperature`、`top_p`。<br>来源：`kimi_k3_dynamic_aggregator`、`kimi_k3_openai_compat`、`kimi_k3_openrouter` | `stream=false`<br>`temperature=1.0`<br>`top_p=0.95`<br>`max_completion_tokens=512`<br>`reasoning_effort="max"`<br>`messages=[3 items]` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `kimi_k3_prompt_cache_key` | 缓存参数 | 验证请求级缓存标识/缓存内容参数 `max_completion_tokens`、`messages`、`prompt_cache_key`、`reasoning_effort`、`response.reasoning_content`、`temperature`、`top_p` 能被正确接收和报告。<br>来源：`kimi_k3_dynamic_aggregator`、`kimi_k3_openai_compat`、`kimi_k3_openrouter` | `stream=false`<br>`temperature=1.0`<br>`top_p=0.95`<br>`max_completion_tokens=512`<br>`reasoning_effort="max"`<br>`prompt_cache_key="loadtest-kimi-k3-session-v1"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `kimi_k3_dynamic_tools` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`kimi_k3_dynamic_aggregator`、`kimi_k3_openai_compat`、`kimi_k3_openrouter` | `stream=false`<br>`temperature=1.0`<br>`top_p=0.95`<br>`max_completion_tokens=512`<br>`reasoning_effort="max"`<br>`tool_choice="required"`<br>`messages=[2 items]` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `kimi_k3_reject_temperature` | 负向/边界 | 负向探针：发送文档不允许的 `max_completion_tokens`、`messages`、`reasoning_effort`、`temperature`、`top_p`，确认网关明确拒绝而不是静默吞掉。<br>来源：`kimi_k3_dynamic_aggregator`、`kimi_k3_openai_compat`、`kimi_k3_openrouter` | `stream=false`<br>`temperature=1.1`<br>`top_p=0.95`<br>`max_completion_tokens=512`<br>`reasoning_effort="max"` | 应拒绝 | 应得到明确 400/422；若 2xx 则是 unexpected_acceptance。 |
| `kimi_k3_reject_top_p` | 负向/边界 | 负向探针：发送文档不允许的 `max_completion_tokens`、`messages`、`reasoning_effort`、`temperature`、`top_p`，确认网关明确拒绝而不是静默吞掉。<br>来源：`kimi_k3_dynamic_aggregator`、`kimi_k3_openai_compat`、`kimi_k3_openrouter` | `stream=false`<br>`temperature=1.0`<br>`top_p=0.8`<br>`max_completion_tokens=512`<br>`reasoning_effort="max"` | 应拒绝 | 应得到明确 400/422；若 2xx 则是 unexpected_acceptance。 |
| `kimi_k3_reject_presence_penalty` | 负向/边界 | 负向探针：发送文档不允许的 `max_completion_tokens`、`messages`、`presence_penalty`、`reasoning_effort`、`temperature`、`top_p`，确认网关明确拒绝而不是静默吞掉。<br>来源：`kimi_k3_dynamic_aggregator`、`kimi_k3_openai_compat`、`kimi_k3_openrouter` | `stream=false`<br>`temperature=1.0`<br>`top_p=0.95`<br>`max_completion_tokens=512`<br>`reasoning_effort="max"`<br>`presence_penalty=0.5` | 应拒绝 | 应得到明确 400/422；若 2xx 则是 unexpected_acceptance。 |
| `kimi_k3_reject_frequency_penalty` | 负向/边界 | 负向探针：发送文档不允许的 `frequency_penalty`、`max_completion_tokens`、`messages`、`reasoning_effort`、`temperature`、`top_p`，确认网关明确拒绝而不是静默吞掉。<br>来源：`kimi_k3_dynamic_aggregator`、`kimi_k3_openai_compat`、`kimi_k3_openrouter` | `stream=false`<br>`temperature=1.0`<br>`top_p=0.95`<br>`max_completion_tokens=512`<br>`reasoning_effort="max"`<br>`frequency_penalty=0.5`<br>`send_deprecated=true` | 应拒绝 | 应得到明确 400/422；若 2xx 则是 unexpected_acceptance。 |
| `kimi_k3_reject_n` | 负向/边界 | 负向探针：发送文档不允许的 `max_completion_tokens`、`messages`、`n`、`reasoning_effort`、`temperature`、`top_p`，确认网关明确拒绝而不是静默吞掉。<br>来源：`kimi_k3_dynamic_aggregator`、`kimi_k3_openai_compat`、`kimi_k3_openrouter` | `stream=false`<br>`temperature=1.0`<br>`top_p=0.95`<br>`max_completion_tokens=512`<br>`reasoning_effort="max"`<br>`n=2` | 应拒绝 | 应得到明确 400/422；若 2xx 则是 unexpected_acceptance。 |

## 去哪里看结果

- Web 控制台会展示当前 model/route/API Form 的 profile 状态和最近一次结果。
- 文字参数结果：`reports/param_tests/<provider>/<model>/verdict.json` 或 Web Job 目录。
- 图片参数结果：`reports/jobs/<job_id>/summary.json`、`plan.json` 和逐 case 文件。
- 总体解释方法见 [参数测试说明](../parameter_testing.md)，不要只看顶层 `pass`。
