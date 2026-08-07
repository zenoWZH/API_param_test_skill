# 模型供应商来源与 Route Profile 目录

更新时间：2026-08-03

## 1. 四个维度必须分开

- `model_family`：模型是谁，例如 GPT、Claude、Gemini、Kimi。
- `route_profile`：采用哪一套上游来源规则，例如 AI Studio、Vertex、Bedrock、阿里云或未固定聚合路由。
- `api_form`：请求使用什么协议，例如 Chat Completions、Responses、Messages、GenerateContent。
- `provider`：本项目实际连接的销售商或网关。它不是模型家族，也不自动等于上游来源。

`observed_upstream_fingerprint` 是运行时证据，只能提示配置 route 可能错误；它不能自动改写 `configured route_profile`。确认错误后应显式修正 Provider 配置并重跑。

## 2. 官方资料确认的多来源类型

| 来源类型 | 官方依据 | 对参数测试的影响 |
|---|---|---|
| Gemini Developer API / AI Studio | [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)、[GenerateContent](https://ai.google.dev/api/generate-content) | AI Studio 的 OpenAI 兼容层与原生 GenerateContent 是不同 API Form。 |
| Google Vertex AI | [Developer API 与 Vertex 迁移](https://ai.google.dev/gemini-api/docs/migrate-to-cloud)、[Vertex GenerationConfig](https://cloud.google.com/vertex-ai/generative-ai/docs/reference/rest/v1beta1/GenerationConfig) | 即使同为 GenerateContent，端点、认证、元数据及部分参数规则也不同。 |
| Claude on Amazon Bedrock | [Bedrock Messages API](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-messages-api.html)、[Request/Response](https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages-request-response.html) | Bedrock 有独立版本字段、端点/认证和模型 ID 规则，不能并入 Anthropic 厂商 route。 |
| Claude on Vertex AI | [Claude on Vertex AI](https://cloud.google.com/vertex-ai/generative-ai/docs/partner-models/use-claude) | Vertex 使用独立模型版本名、端点、认证和云平台限制。 |
| Alibaba Cloud Model Studio | [Model Studio 介绍](https://www.alibabacloud.com/help/en/model-studio/what-is-model-studio)、[支持模型](https://www.alibabacloud.com/help/en/model-studio/models) | 同时供应 Qwen、DeepSeek、Kimi、GLM、MiniMax，并提供 OpenAI、Anthropic 和 DashScope 等多种 API Form；参数基线必须走 `aliyun_maas`。 |
| OpenRouter | [Provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)、[Router metadata](https://openrouter.ai/docs/guides/features/router-metadata) | 默认可在多个 physical provider 间负载、重试和回退；未固定 physical provider 时只能判 adapter 兼容，不能判原厂合同通过。 |
| Fireworks | [OpenAI compatibility](https://docs.fireworks.ai/tools-sdks/openai-compatibility)、[Anthropic compatibility](https://docs.fireworks.ai/tools-sdks/anthropic-compatibility) | 同一个模型可通过不同兼容 API Form 暴露，且 usage、模型 ID、必填参数等与原厂存在明确差异。只有出现显式 Fireworks Provider route 后才注册对应 profile。 |

## 3. 当前能力表覆盖

| 家族 | 当前显式 route | API Form |
|---|---|---|
| DeepSeek | `vendor_direct`、`aliyun_maas`、`dynamic_aggregator` | Chat Completions |
| GLM | `vendor_direct`、`aliyun_maas`、`dynamic_aggregator` | Chat Completions |
| Qwen | `aliyun_maas`、`dynamic_aggregator` | Chat Completions |
| Gemini | `google_ai_studio`、`google_vertex`、`dynamic_aggregator` | Chat Completions / GenerateContent |
| Claude | `vendor_direct`、`aws_bedrock`、`google_vertex`、`dynamic_aggregator`，以及兼容/旧迁移 route | Messages / Chat Completions |
| Claude Fable | `vendor_direct`、`aws_bedrock`、`google_vertex`、`dynamic_aggregator`，以及兼容/旧迁移 route | Messages / Chat Completions |
| GPT | `vendor_direct`、`dynamic_aggregator` | Chat Completions / Responses |
| Kimi | `vendor_direct`、`aliyun_maas`、`openrouter`、`dynamic_aggregator` | Chat Completions |
| MiniMax | `vendor_direct`、`dynamic_aggregator` | Chat Completions |
| Grok | `vendor_direct`、`dynamic_aggregator` | Chat Completions / Responses |
| GPT Image / Grok Imagine | `vendor_direct`、`dynamic_aggregator` | Images Generations |
| Banana / Gemini Image | `google_ai_studio`、`provider_compat` | Interactions / Chat Completions |

没有因互联网“存在某服务”就自动开放新组合。Azure、Fireworks、DeepInfra、CoreWeave、Modal、Novita、Parasail、Sail Research 等来源只有在项目出现明确 Provider route、API Form、模型 ID 和 Reference Source 后，才进入可执行能力表。

## 4. 当前配置迁移结果

脱敏盘点共覆盖 64 个 Provider、282 个文字模型声明和 26 个图片模型声明：

| 模态 | route | 模型声明数 |
|---|---|---:|
| text | `dynamic_aggregator` | 215 |
| text | `vendor_direct` | 31 |
| text | `aliyun_maas` | 18 |
| text | `google_ai_studio` | 8 |
| text | `google_vertex` | 6 |
| text | `aws_bedrock` | 3 |
| text | `openrouter` | 1 |
| image | `dynamic_aggregator` | 13 |
| image | `vendor_direct` | 7 |
| image | `provider_compat` | 5 |
| image | `google_ai_studio` | 1 |

此前大部分未知代理被错误压成 `vendor_direct`。新迁移规则为：Reference Source 的 route 优先；其次使用 Provider 显式 route；Bedrock、Vertex、OpenRouter 等明确旧标识只迁到对应具体 route；其余来源未知的代理保留为 `dynamic_aggregator`。不会再根据“模型属于某家族”猜成厂商直连。

## 5. 判定规则

- `vendor_direct`、具体云 route 和 `dynamic_aggregator` 的 profile ID、Reference Source、历史主键互不复用。
- `dynamic_aggregator` 和未固定 physical provider 的 `openrouter` Reference Source 使用 `certification_scope: adapter_only`，并要求 route 稳定性证据。
- 它们可以得到 `adapter_pass`，但 `certified_route_contract_pass` 必须为 `false`；不得写成“原厂参数合同通过”。
- Bedrock、Vertex、阿里云必须使用各自 route-specific Reference Source。
- 未知来源不能靠响应 `model` 字段一致升级为厂商 route；需要显式来源声明或经过人工确认的上游证据。
- 参数兼容、token accuracy、returned-model identity 继续独立判定；route provenance/certification scope 作为额外结论展示。

## 6. XinyunAI DeepSeek V4 Flash 0731 快照（2026-08-04）

- DeepSeek 官方 API 请求名仍为 `deepseek-v4-flash`，当前模型版本为 `DeepSeek-V4-Flash-0731`；官方 Chat Completions 支持 `reasoning_effort=low/high/max`，官方直连同时支持 Responses API。
- XinyunAI 新账号的 `/v1/models` 只声明 `deepseek-v4-flash-0731`。连续三次 Chat 请求的响应 `model` 均与该请求名一致，且 usage 同时包含 reasoning 与 cache 字段。
- 该账号只验证通过 `openai_chat_completions`；`/v1/responses` 返回 404，因此不能因为官方直连支持 Responses 就把这一 API Form 注册到 XinyunAI route。
- 供应商 route 保持 `dynamic_aggregator`，Reference Source 为 `deepseek_xinyunai_v4_flash_0731_openai_compat`，认证范围为 `adapter_only`。当前证据不能证明其物理上游是 DeepSeek 官方 API。
- 旧 XinyunAI 账号只声明裸名 `deepseek-v4-flash`，新旧账号模型权限互斥，因此分别保存为独立 Provider，避免更换密钥破坏既有 Kimi、GLM 和 DeepSeek 测试。
- 标准参数矩阵最终结果为 18 profile × 3 轮，即 54/54 兼容性通过；含工具 follow-up 在内共 67 个 exchange 的返回模型均为 `deepseek-v4-flash-0731`。
- usage 算术 67/67 通过，并观察到 `completion_tokens_details.reasoning_tokens`、`prompt_tokens_details.cached_tokens`、`prompt_cache_hit_tokens` 和 `prompt_cache_miss_tokens`。本地尚未配置该模型的精确 tokenizer/count-token 接口，因此 token accuracy 的独立计数覆盖率为 0，状态应解读为 partial/N/A，而不是“已证明计费 token 精确”。
- 通用 128-token JSON profile 在首次三轮中出现一次 `finish_reason=length` 截断；失败重放确认是输出上限波动后，Reference Source 改用 `deepseek_json_output_256`，三次定向重放及最终三轮矩阵均通过。
