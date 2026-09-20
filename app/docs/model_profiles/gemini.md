# Gemini 模型家族 Profile 说明

<!-- 由 scripts/generate_test_docs.py 从 Model Profile Database 与测试扩展生成，请勿手工维护表格。 -->

分别说明 AI Studio Chat 兼容、AI Studio GenerateContent、Vertex GenerateContent 与动态聚合。

本文档回答三个问题：这个家族有哪些模型身份、在不同 route/API Form 下使用哪份测试契约、每个 profile 实际发送什么并检查什么。

## 在界面中使用本手册

![文字参数测试界面示意](../assets/ui/parameter-testing-console.svg)

先在界面按 Provider → Model → Route Profile → API Form → Reference Contract 选择组合，再用下文表格确认本次会运行哪些 Test Case。

## 先理解判读规则

- `应支持`：期望 HTTP 2xx，且响应结构、内容语义、usage、returned-model 均通过校验。
- `应拒绝`：期望明确的 400/422；若仍返回 2xx，记为 `unexpected_acceptance`。
- `按模型/route 变化`：同一 profile 对家族内不同模型或 route 的期望不同，运行前以控制台展开的 model profile 为准。
- 动态聚合 route 即使全部通过，也只证明 adapter 兼容，不能证明物理上游或原厂合同。

## 模型与 alias

| 规范模型 | 显式 alias |
|---|---|
| `gemini-2.5-flash` | — |
| `gemini-2.5-flash-lite` | — |
| `gemini-2.5-pro` | — |
| `gemini-3-flash` | — |
| `gemini-3.1-flash-lite` | — |
| `gemini-3.1-pro-preview` | — |
| `gemini-3.5-flash` | — |
| `gemini-3.5-flash-lite` | — |
| `gemini-3.6-flash` | — |
| `gemini-3.7-flash` | — |

## Route 与 API Form

| Route Profile | API Form | 内部 transport | 已注册模型数 | Reference Contract |
|---|---|---|---:|---|
| `google_ai_studio` | `openai_chat_completions` | `chat_completions` | 9 | `gemini_openai_compat` |
| `google_ai_studio` | `gemini_generate_content` | `gemini_generate_content` | 5 | `gemini_3_7_flash_generate_content`<br>`gemini_native_generate_content` |

## Reference Contract

| Reference Contract | 说明 | Route / API Form | 认证范围 | Test Case 数 | 官方资料 |
|---|---|---|---|---:|---|
| `gemini_3_7_flash_generate_content` | Gemini 3.7 Flash GenerateContent | `google_ai_studio` / `gemini_generate_content` | `official_documented_v1beta_live_unverified` | 26 | [资料1](https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash) [资料2](https://ai.google.dev/gemini-api/docs/latest-model) [资料3](https://ai.google.dev/api/generate-content) [资料4](https://ai.google.dev/gemini-api/docs/generate-content/thinking) |
| `gemini_native_generate_content` | Gemini Native GenerateContent | `google_ai_studio` / `gemini_generate_content` | `official_documented_v1beta_live_unverified` | 27 | [资料1](https://ai.google.dev/api/generate-content) [资料2](https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash) [资料3](https://ai.google.dev/gemini-api/docs/generate-content/latest-model) [资料4](https://ai.google.dev/gemini-api/docs/generate-content/thinking) [资料5](https://ai.google.dev/api/caching) [资料6](https://ai.google.dev/gemini-api/docs/safety-settings) |
| `gemini_openai_compat` | Gemini OpenAI-compatible Chat Completions | `google_ai_studio` / `openai_chat_completions` | `raw_route_contract` | 26 | [资料1](https://ai.google.dev/gemini-api/docs/openai) [资料2](https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash) [资料3](https://ai.google.dev/gemini-api/docs/latest-model) [资料4](https://ai.google.dev/gemini-api/docs/thinking) [资料5](https://ai.google.dev/api/generate-content) [资料6](https://ai.google.dev/gemini-api/docs/safety-settings) |

## 全部参数 Profile

| Profile | 类别 | 具体测试目的 | 关键请求设置 | 期望 | 通过时还要检查 |
|---|---|---|---|---|---|
| `gemini_native_tools` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`multi_turn=true`<br>`native_tools=[1 items]` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `gemini_native_tool_config` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`multi_turn=true`<br>`native_tools=[1 items]`<br>`native_tool_config.functionCallingConfig.mode="AUTO"` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `gemini_native_system_instruction` | 采样 | 验证系统指令字段 `contents`、`response.modelVersion`、`response.usageMetadata`、`systemInstruction` 的协议位置和实际响应语义。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_system_instruction="You are a concise API compatibility test assistant."` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_native_service_tier` | 采样 | 验证 route 专属元数据 `contents`、`response.modelVersion`、`response.usageMetadata`、`serviceTier` 放在正确的 body 或 header 位置。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_service_tier="standard"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_native_store` | 采样 | 验证 `contents`、`response.modelVersion`、`response.usageMetadata`、`store` 的请求兼容性以及对应响应字段是否正常。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_store=false` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_native_stop_sequences` | 采样 | 验证停止序列参数 `contents`、`generationConfig.stopSequences`、`response.modelVersion`、`response.usageMetadata` 会影响结束位置或按契约被拒绝。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.stopSequences=["END"]`<br>`native_generation_config.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_native_response_mime_type` | 采样 | 验证 `contents`、`generationConfig.responseMimeType`、`response.modelVersion`、`response.usageMetadata` 的请求兼容性以及对应响应字段是否正常。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.responseMimeType="application/json"`<br>`native_generation_config.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_native_response_schema` | 结构化输出 | 验证结构化输出参数 `contents`、`generationConfig.responseSchema`、`response.modelVersion`、`response.usageMetadata`，并确认最终内容是可解析且符合约束的 JSON。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.responseMimeType="application/json"`<br>`native_generation_config.responseSchema.type="OBJECT"`<br>`native_generation_config.responseSchema.properties.summary.type="STRING"`<br>`native_generation_config.responseSchema.properties.items.type="ARRAY"`<br>`native_generation_config.responseSchema.properties.items.items.type="STRING"`<br>`native_generation_config.responseSchema.required=["summary","items"]`<br>`native_generation_config.maxOutputTokens=128` | 应支持 | 内容必须能解析为 JSON；有 schema 时还要满足 schema。 |
| `gemini_native_response_json_schema` | 结构化输出 | 验证结构化输出参数 `contents`、`generationConfig.responseJsonSchema`、`response.modelVersion`、`response.usageMetadata`，并确认最终内容是可解析且符合约束的 JSON。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.responseMimeType="application/json"`<br>`native_generation_config.responseJsonSchema.type="object"`<br>`native_generation_config.responseJsonSchema.properties.summary.type="string"`<br>`native_generation_config.responseJsonSchema.properties.items.type="array"`<br>`native_generation_config.responseJsonSchema.properties.items.items.type="string"`<br>`native_generation_config.responseJsonSchema.required=["summary","items"]`<br>`native_generation_config.responseJsonSchema.additionalProperties=false`<br>`native_generation_config.maxOutputTokens=128` | 应支持 | 内容必须能解析为 JSON；有 schema 时还要满足 schema。 |
| `gemini_native_response_modalities` | 结构化输出 | 验证 `contents`、`generationConfig.responseModalities`、`response.modelVersion`、`response.usageMetadata` 的请求兼容性以及对应响应字段是否正常。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.responseModalities=["TEXT"]`<br>`native_generation_config.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_native_candidate_count` | 采样 | 验证候选数量 `contents`、`generationConfig.candidateCount`、`response.modelVersion`、`response.usageMetadata`，并核对响应实际返回的候选数。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.candidateCount=2` | 按模型/route 变化 | 核对响应候选数量，不以第一个候选成功代替整体成功。 |
| `gemini_native_max_output_tokens` | 采样 | 验证输出 token 上限字段 `contents`、`generationConfig.maxOutputTokens`、`response.modelVersion`、`response.usageMetadata` 使用当前 API Form 的正确名称和位置。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_native_temperature` | 采样 | 验证采样参数 `contents`、`generationConfig.temperature`、`response.modelVersion`、`response.usageMetadata` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.temperature=0.7`<br>`native_generation_config.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_native_top_p` | 采样 | 验证采样参数 `contents`、`generationConfig.topP`、`response.modelVersion`、`response.usageMetadata` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.topP=0.9`<br>`native_generation_config.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_native_top_k` | 采样 | 验证采样参数 `contents`、`generationConfig.topK`、`response.modelVersion`、`response.usageMetadata` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.topK=20`<br>`native_generation_config.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_native_seed` | 采样 | 验证采样参数 `contents`、`generationConfig.seed`、`response.modelVersion`、`response.usageMetadata` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.seed=123456789`<br>`native_generation_config.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_native_presence_penalty` | 采样 | 验证采样参数 `contents`、`generationConfig.presencePenalty`、`response.modelVersion`、`response.usageMetadata` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.presencePenalty=0.2`<br>`native_generation_config.maxOutputTokens=128` | 按模型/route 变化 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_native_frequency_penalty` | 采样 | 验证采样参数 `contents`、`generationConfig.frequencyPenalty`、`response.modelVersion`、`response.usageMetadata` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.frequencyPenalty=0.2`<br>`native_generation_config.maxOutputTokens=128` | 按模型/route 变化 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_native_logprobs` | 采样 | 验证 `contents`、`generationConfig.logprobs`、`generationConfig.responseLogprobs`、`response.modelVersion`、`response.usageMetadata` 的请求兼容性以及对应响应字段是否正常。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.responseLogprobs=true`<br>`native_generation_config.logprobs=5`<br>`native_generation_config.maxOutputTokens=128` | 按模型/route 变化 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_native_civic_answers` | 采样 | 验证 `contents`、`generationConfig.enableEnhancedCivicAnswers`、`response.modelVersion`、`response.usageMetadata` 的请求兼容性以及对应响应字段是否正常。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.enableEnhancedCivicAnswers=false`<br>`native_generation_config.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_native_thinking_minimal` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `contents`、`generationConfig.thinkingConfig`、`generationConfig.thinkingConfig.thinkingLevel.MINIMAL`、`response.modelVersion`、`response.usageMetadata`。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.thinkingConfig.thinkingLevel="MINIMAL"`<br>`native_generation_config.thinkingConfig.includeThoughts=true`<br>`native_generation_config.maxOutputTokens=256` | 按模型/route 变化 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `gemini_native_thinking_low` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `contents`、`generationConfig.thinkingConfig`、`generationConfig.thinkingConfig.thinkingLevel.LOW`、`response.modelVersion`、`response.usageMetadata`。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.thinkingConfig.thinkingLevel="LOW"`<br>`native_generation_config.thinkingConfig.includeThoughts=true`<br>`native_generation_config.maxOutputTokens=128` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `gemini_native_thinking_medium` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `contents`、`generationConfig.thinkingConfig`、`generationConfig.thinkingConfig.thinkingLevel.MEDIUM`、`response.candidates[].content.parts[].thought`、`response.modelVersion`、`response.usageMetadata`。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.thinkingConfig.thinkingLevel="MEDIUM"`<br>`native_generation_config.thinkingConfig.includeThoughts=true`<br>`native_generation_config.maxOutputTokens=512` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `gemini_native_thinking_high` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `contents`、`generationConfig.thinkingConfig`、`generationConfig.thinkingConfig.thinkingLevel.HIGH`、`response.candidates[].content.parts[].thought`、`response.modelVersion`、`response.usageMetadata`。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.thinkingConfig.thinkingLevel="HIGH"`<br>`native_generation_config.thinkingConfig.includeThoughts=true`<br>`native_generation_config.maxOutputTokens=512` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `gemini_native_media_resolution` | 采样 | 验证 `contents`、`generationConfig.mediaResolution`、`response.modelVersion`、`response.usageMetadata` 的请求兼容性以及对应响应字段是否正常。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.mediaResolution="MEDIA_RESOLUTION_LOW"`<br>`native_generation_config.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_native_response_format` | 结构化输出 | 验证结构化输出参数 `contents`、`generationConfig.responseFormat`、`response.modelVersion`、`response.usageMetadata`，并确认最终内容是可解析且符合约束的 JSON。<br>来源：`gemini_3_7_flash_generate_content`、`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_generation_config.responseFormat.text.mimeType="APPLICATION_JSON"`<br>`native_generation_config.maxOutputTokens=128` | 应支持 | 内容必须能解析为 JSON；有 schema 时还要满足 schema。 |
| `gemini_native_cached_content` | 缓存参数 | 验证请求级缓存标识/缓存内容参数 `cachedContent`、`contents` 能被正确接收和报告。<br>来源：`gemini_native_generate_content` | `transport="gemini_generate_content"`<br>`native_cached_content="cachedContents/loadtest-compatibility-probe"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `basic_stream` | 流式 | 验证 SSE 流式响应、结束标记和返回文本能够完整解析。<br>来源：`gemini_openai_compat` | `stream=true`<br>`stream_options.include_usage=true`<br>`thinking.type="disabled"` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `stream_with_usage` | 流式 | 验证 SSE 分块可解析，并在结束前得到独立、算术一致的 usage 信息。<br>来源：`gemini_openai_compat` | `stream=true`<br>`thinking.type="disabled"`<br>`stream_options.include_usage=true` | 应支持 | 检查 chunk 结构、结束标记、文本拼接与 usage 末块。 |
| `gemini_reasoning_minimal` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`reasoning_effort`。<br>来源：`gemini_openai_compat` | `stream=false`<br>`reasoning_effort="minimal"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `gemini_reasoning_low` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`reasoning_effort`。<br>来源：`gemini_openai_compat` | `stream=false`<br>`reasoning_effort="low"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `gemini_reasoning_medium` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`reasoning_effort`。<br>来源：`gemini_openai_compat` | `stream=false`<br>`reasoning_effort="medium"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `gemini_reasoning_high` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`reasoning_effort`。<br>来源：`gemini_openai_compat` | `stream=false`<br>`reasoning_effort="high"` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `gemini_thinking_config` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `extra_body.google.thinking_config`、`messages`。<br>来源：`gemini_openai_compat` | `stream=false`<br>`extra_body.google.thinking_config.thinking_level="low"`<br>`extra_body.google.thinking_config.include_thoughts=true` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `gemini_max_tokens` | 基础能力 | 验证输出 token 上限字段 `max_tokens`、`messages` 使用当前 API Form 的正确名称和位置。<br>来源：`gemini_openai_compat` | `stream=false`<br>`max_tokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `sampling_non_thinking` | 推理 | 验证指定推理开关/档位，并检查响应中的 reasoning/thinking 语义；涉及 `messages`、`temperature`、`top_p`。<br>来源：`gemini_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`temperature=0.7`<br>`top_p=0.9` | 应支持 | 检查请求档位和响应 reasoning/thinking 字段语义，不以可见文本长度代替。 |
| `json_output` | 结构化输出 | 验证结构化输出参数 `messages`、`response_format`，并确认最终内容是可解析且符合约束的 JSON。<br>来源：`gemini_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`response_format.type="json_object"` | 应支持 | 内容必须能解析为 JSON；有 schema 时还要满足 schema。 |
| `stop_sequences` | 基础能力 | 验证停止序列参数 `messages`、`stop` 会影响结束位置或按契约被拒绝。<br>来源：`gemini_openai_compat` | `stream=false`<br>`thinking.type="disabled"`<br>`stop=["\n\n","END"]` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_n` | 采样 | 验证候选数量 `messages`、`n`，并核对响应实际返回的候选数。<br>来源：`gemini_openai_compat` | `stream=false`<br>`n=2` | 按模型/route 变化 | 核对响应候选数量，不以第一个候选成功代替整体成功。 |
| `gemini_service_tier` | Route 元数据 | 验证 route 专属元数据 `messages`、`service_tier` 放在正确的 body 或 header 位置。<br>来源：`gemini_openai_compat` | `stream=false`<br>`service_tier="default"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_tools` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`gemini_openai_compat` | `stream=false`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `gemini_tool_choice_auto` | 工具调用 | 验证工具声明、tool choice、结构化调用参数以及必要时的工具结果回传。<br>来源：`gemini_openai_compat` | `stream=false`<br>`tools_fixture="fixtures/tools_weather.json"`<br>`multi_turn=true`<br>`tool_choice="auto"` | 应支持 | 不能只看 HTTP 2xx；必须存在合法 tool/function call，follow-up 后还要有最终文本。 |
| `gemini_cached_content` | 缓存参数 | 验证请求级缓存标识/缓存内容参数 `extra_body.google.cached_content`、`messages` 能被正确接收和报告。<br>来源：`gemini_openai_compat` | `stream=false`<br>`max_tokens=64`<br>`extra_body.google.cached_content="cachedContents/loadtest-compatibility-probe"` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_chat_candidate_count` | 基础能力 | 验证候选数量 `generationConfig.candidateCount`、`messages`，并核对响应实际返回的候选数。<br>来源：`gemini_openai_compat` | `stream=false`<br>`generationConfig.candidateCount=2` | 按模型/route 变化 | 核对响应候选数量，不以第一个候选成功代替整体成功。 |
| `gemini_chat_max_output_tokens` | 基础能力 | 验证输出 token 上限字段 `generationConfig.maxOutputTokens`、`messages` 使用当前 API Form 的正确名称和位置。<br>来源：`gemini_openai_compat` | `stream=false`<br>`generationConfig.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_chat_stop_sequences` | 基础能力 | 验证停止序列参数 `generationConfig.stopSequences`、`messages` 会影响结束位置或按契约被拒绝。<br>来源：`gemini_openai_compat` | `stream=false`<br>`generationConfig.stopSequences=["END"]`<br>`generationConfig.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_chat_temperature` | 采样 | 验证采样参数 `generationConfig.temperature`、`messages` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`gemini_openai_compat` | `stream=false`<br>`generationConfig.temperature=0.7`<br>`generationConfig.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_chat_top_p` | 采样 | 验证采样参数 `generationConfig.topP`、`messages` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`gemini_openai_compat` | `stream=false`<br>`generationConfig.topP=0.9`<br>`generationConfig.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_chat_top_k` | 采样 | 验证采样参数 `generationConfig.topK`、`messages` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`gemini_openai_compat` | `stream=false`<br>`generationConfig.topK=20`<br>`generationConfig.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_chat_seed` | 采样 | 验证采样参数 `generationConfig.seed`、`messages` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`gemini_openai_compat` | `stream=false`<br>`generationConfig.seed=123456789`<br>`generationConfig.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_chat_presence_penalty` | 采样 | 验证采样参数 `generationConfig.presencePenalty`、`messages` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`gemini_openai_compat` | `stream=false`<br>`generationConfig.presencePenalty=0.2`<br>`generationConfig.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_chat_frequency_penalty` | 采样 | 验证采样参数 `generationConfig.frequencyPenalty`、`messages` 的接受度；非思考模式下还检查返回值不是空壳。<br>来源：`gemini_openai_compat` | `stream=false`<br>`generationConfig.frequencyPenalty=0.2`<br>`generationConfig.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |
| `gemini_chat_response_mime_type` | 基础能力 | 验证 `generationConfig.responseMimeType`、`messages` 的请求兼容性以及对应响应字段是否正常。<br>来源：`gemini_openai_compat` | `stream=false`<br>`generationConfig.responseMimeType="application/json"`<br>`generationConfig.maxOutputTokens=128` | 应支持 | 2xx 后仍需通过响应结构、内容、usage 和 returned-model 校验。 |

## 去哪里看结果

- Web 控制台会展示当前 model/route/API Form 的 profile 状态和最近一次结果。
- 文字参数结果：`reports/param_tests/<provider>/<model>/verdict.json` 或 Web Job 目录。
- 图片参数结果：`reports/jobs/<job_id>/summary.json`、`plan.json` 和逐 case 文件。
- 总体解释方法见 [参数测试说明](../parameter_testing.md)，不要只看顶层 `pass`。
