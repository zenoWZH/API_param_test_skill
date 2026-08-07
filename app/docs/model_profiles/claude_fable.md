# Claude Fable 模型家族 Profile 说明

<!-- 由 scripts/generate_test_docs.py 从 schema v4 生成，请勿手工维护表格。 -->

说明 Fable 的 Native Messages、云路由、动态路由和兼容接口 profile。

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
| `claude-fable-5` | `fable5` |
| `claude-opus-5` | — |

## Route 与 API Form

| Route Profile | API Form | 内部 transport | 已注册模型数 | Reference Source |
|---|---|---|---:|---|
| `cloud_adapter` | `anthropic_messages` | `claude_messages` | 2 | `claude_fable_cloud_adapter_messages` |
| `aws_bedrock` | `anthropic_messages` | `claude_messages` | 2 | `claude_fable_aws_bedrock_messages` |
| `google_vertex` | `anthropic_messages` | `claude_messages` | 2 | `claude_fable_google_vertex_messages` |
| `dynamic_aggregator` | `anthropic_messages` | `claude_messages` | 2 | `claude_fable_dynamic_messages` |
| `vendor_direct` | `anthropic_messages` | `claude_messages` | 2 | `claude_fable_native_messages` |
| `vendor_compat` | `openai_chat_completions` | `chat_completions` | 2 | `claude_fable_openai_compat` |

## Reference Source

| Reference Source | 说明 | Route / API Form | 认证范围 | Profile 数 | 官方资料 |
|---|---|---|---|---:|---|
| `claude_fable_aws_bedrock_messages` | Claude Fable Messages through Amazon Bedrock | `aws_bedrock` / `anthropic_messages` | `cloud_route_contract` | 12 | [资料1](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-messages-api.html) [资料2](https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards-anthropic.html) |
| `claude_fable_cloud_adapter_messages` | Claude Fable Messages through a cloud adapter | `cloud_adapter` / `anthropic_messages` | `raw_route_contract` | 12 | [资料1](https://platform.claude.com/docs/en/api/messages) [资料2](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking) [资料3](https://platform.claude.com/docs/en/build-with-claude/effort) |
| `claude_fable_dynamic_messages` | Claude Fable Messages through an unpinned aggregator | `dynamic_aggregator` / `anthropic_messages` | `adapter_only` | 12 | [资料1](https://platform.claude.com/docs/en/api/messages) [资料2](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking) [资料3](https://platform.claude.com/docs/en/build-with-claude/effort) |
| `claude_fable_google_vertex_messages` | Claude Fable Messages through Google Vertex AI | `google_vertex` / `anthropic_messages` | `cloud_route_contract` | 12 | [资料1](https://cloud.google.com/vertex-ai/generative-ai/docs/partner-models/use-claude) |
| `claude_fable_native_messages` | Claude Fable Native Messages API | `vendor_direct` / `anthropic_messages` | `raw_route_contract` | 12 | [资料1](https://platform.claude.com/docs/en/api/messages) [资料2](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking) [资料3](https://platform.claude.com/docs/en/build-with-claude/effort) |
| `claude_fable_openai_compat` | Claude Fable OpenAI-compatible Chat Completions | `vendor_compat` / `openai_chat_completions` | `raw_route_contract` | 7 | [资料1](https://platform.claude.com/docs/en/api/openai-sdk) |

## 全部参数 Profile

| Profile | 类别 | 具体测试目的 | 关键请求设置 | 期望 | 通过时还要检查 |
|---|---|---|---|---|---|
| `claude_native_max_tokens` | 采样 | 验证输出 token 上限字段 `max_tokens`、`messages` 使用当前 API Form 的正确名称和位置。<br>来源：`claude_fable_aws_bedrock_messages`、`claude_fable_cloud_adapter_messages`、`claude_fable_dynamic_messages`、`claude_fable_google_vertex_messages`、`claude_fable_native_messages` | `transport="claude_messages"`<br>`stream=false`<br>`max_tokens=64` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `claude_native_stream` | 流式 | 验证 SSE 流式响应、结束标记和返回文本能够完整解析。<br>来源：`claude_fable_aws_bedrock_messages`、`claude_fable_cloud_adapter_messages`、`claude_fable_dynamic_messages`、`claude_fable_google_vertex_messages`、`claude_fable_native_messages` | `transport="claude_messages"`<br>`stream=true`<br>`max_tokens=128` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `claude_native_system` | 采样 | 验证系统指令字段 `messages`、`system` 的协议位置和实际响应语义。<br>来源：`claude_fable_aws_bedrock_messages`、`claude_fable_cloud_adapter_messages`、`claude_fable_dynamic_messages`、`claude_fable_google_vertex_messages`、`claude_fable_native_messages` | `transport="claude_messages"`<br>`stream=false`<br>`system="You are a concise Claude Messages API compatibility test...`<br>`max_tokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `claude_native_temperature` | 采样 | 验证采样参数 `messages`、`temperature` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`claude_fable_aws_bedrock_messages`、`claude_fable_cloud_adapter_messages`、`claude_fable_dynamic_messages`、`claude_fable_google_vertex_messages`、`claude_fable_native_messages` | `transport="claude_messages"`<br>`stream=false`<br>`temperature=1`<br>`max_tokens=64` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `claude_native_stop_sequences` | 采样 | 验证停止序列参数 `messages`、`stop_sequences` 会影响结束位置或按契约被拒绝。<br>来源：`claude_fable_aws_bedrock_messages`、`claude_fable_cloud_adapter_messages`、`claude_fable_dynamic_messages`、`claude_fable_google_vertex_messages`、`claude_fable_native_messages` | `transport="claude_messages"`<br>`stream=false`<br>`stop_sequences=["END"]`<br>`max_tokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `claude_native_tools` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`claude_fable_aws_bedrock_messages`、`claude_fable_cloud_adapter_messages`、`claude_fable_dynamic_messages`、`claude_fable_google_vertex_messages`、`claude_fable_native_messages` | `transport="claude_messages"`<br>`stream=false`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true`<br>`max_tokens=128` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `claude_native_tool_choice_auto` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`claude_fable_aws_bedrock_messages`、`claude_fable_cloud_adapter_messages`、`claude_fable_dynamic_messages`、`claude_fable_google_vertex_messages`、`claude_fable_native_messages` | `transport="claude_messages"`<br>`stream=false`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true`<br>`max_tokens=128`<br>`tool_choice="auto"` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `claude_native_thinking_adaptive` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`thinking.type`。<br>来源：`claude_fable_aws_bedrock_messages`、`claude_fable_cloud_adapter_messages`、`claude_fable_dynamic_messages`、`claude_fable_google_vertex_messages`、`claude_fable_native_messages` | `transport="claude_messages"`<br>`stream=false`<br>`thinking.type="adaptive"`<br>`max_tokens=128` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `claude_fable_thinking_effort_low` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`output_config.effort`、`thinking.type`。<br>来源：`claude_fable_aws_bedrock_messages`、`claude_fable_cloud_adapter_messages`、`claude_fable_dynamic_messages`、`claude_fable_google_vertex_messages`、`claude_fable_native_messages` | `transport="claude_messages"`<br>`stream=false`<br>`thinking.type="adaptive"`<br>`output_config.effort="low"`<br>`max_tokens=256` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `claude_fable_thinking_effort_medium` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`output_config.effort`、`thinking.type`。<br>来源：`claude_fable_aws_bedrock_messages`、`claude_fable_cloud_adapter_messages`、`claude_fable_dynamic_messages`、`claude_fable_google_vertex_messages`、`claude_fable_native_messages` | `transport="claude_messages"`<br>`stream=false`<br>`thinking.type="adaptive"`<br>`output_config.effort="medium"`<br>`max_tokens=256` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `claude_fable_thinking_effort_high` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`output_config.effort`、`thinking.type`。<br>来源：`claude_fable_aws_bedrock_messages`、`claude_fable_cloud_adapter_messages`、`claude_fable_dynamic_messages`、`claude_fable_google_vertex_messages`、`claude_fable_native_messages` | `transport="claude_messages"`<br>`stream=false`<br>`thinking.type="adaptive"`<br>`output_config.effort="high"`<br>`max_tokens=512` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `claude_native_metadata` | 采样 | 验证 route 专属元数据 `messages`、`metadata` 放在正确的 body 或 header 位置。<br>来源：`claude_fable_aws_bedrock_messages`、`claude_fable_cloud_adapter_messages`、`claude_fable_dynamic_messages`、`claude_fable_google_vertex_messages`、`claude_fable_native_messages` | `transport="claude_messages"`<br>`stream=false`<br>`metadata.user_id="loadtest_claude_param_user"`<br>`max_tokens=64` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `basic_stream` | 流式 | 验证 SSE 流式响应、结束标记和返回文本能够完整解析。<br>来源：`claude_fable_openai_compat` | `stream=true`<br>`thinking.type="disabled"` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `stream_with_usage` | 流式 | 验证 SSE 分块可解析，并在结束前得到独立、算术一致的 usage 信息。<br>来源：`claude_fable_openai_compat` | `stream=true`<br>`thinking.type="disabled"`<br>`stream_options.include_usage=true` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `claude_max_tokens` | 基础能力 | 验证输出 token 上限字段 `max_completion_tokens`、`max_tokens`、`messages` 使用当前 API Form 的正确名称和位置。<br>来源：`claude_fable_openai_compat` | `stream=false`<br>`max_tokens=64` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `claude_sampling` | 基础能力 | 验证 `messages`、`temperature`、`top_p` 的请求兼容性以及对应响应字段是否正常。<br>来源：`claude_fable_openai_compat` | `stream=false`<br>`temperature=1`<br>`top_p=1`<br>`max_tokens=64` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `stop_sequences` | 基础能力 | 验证停止序列参数 `messages`、`stop` 会影响结束位置或按契约被拒绝。<br>来源：`claude_fable_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`stop=["\n\n","END"]` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `claude_tools` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`claude_fable_openai_compat` | `stream=false`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `claude_tool_choice_auto` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`claude_fable_openai_compat` | `stream=false`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true`<br>`tool_choice="auto"` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |

## 去哪里看结果

- Web 控制台会展示当前 model/route/API Form 的 profile 状态和最近一次结果。
- 文字参数结果：`reports/param_tests/<provider>/<model>/verdict.json` 或 Web Job 目录。
- 图片参数结果：`reports/jobs/<job_id>/summary.json`、`plan.json` 和逐 case 文件。
- 总体解释方法见 [参数测试说明](../parameter_testing.md)，不要只看顶层 `pass`。
