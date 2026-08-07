# 参数测试审查报告

- **日期**：2026-07-10
- **架构复审**：2026-08-03（模型家族、API Form、模型 profile 与 route profile 完整拆分）
- **范围**：参数兼容性测试子系统（reference source × compatibility profile × 构建/校验逻辑）
- **被审代码**：
  - `api_reference_specs.yaml` — OpenAI/Kimi K3/GLM/Qwen/Gemini/Claude/DeepSeek/Grok/Aliyun 等独立 reference source
  - `config.yaml → compatibility_profiles`（约 70 个 profile）
  - `lib/deepseek_params.py` — family 参数白名单、`build_request`、`_validate_body`、`_build_gemini_native_body`
  - `lib/profile_validation.py`、`lib/client.py`（`gemini_generate_content` transport）、`scripts/param_test.py`
- **对照的官方文档**：见文末 Sources
- **说明**：2026-07-10 的官方参数覆盖结论保留为历史审计；2026-08-03 已完成 schema v4 route-first 家族化架构更新。

> 说明：`lib/param_specs.py` 里的参数对照表是旧版 Locust `/yibu/params` 调试页专用，**不在**主参数测试路径上；本报告以 `api_reference_specs.yaml` 为准。

---

## 执行摘要

2026-08-03 后，参数能力由 `model_capability_profiles.yaml` schema v4 统一管理：

1. 顶层分 `text` 与 `image` 两种 modality。
2. 每个 family 可声明多个 API Form；OpenAI Chat Completions、Responses、Anthropic Messages 与 Gemini GenerateContent 只表示接口形态，不再充当模型家族。
3. 每个可运行模型必须在每个 `route profile + API Form` 可执行组合下拥有独立 model profile，Reference Source 必须精确匹配 family/route/form。
4. 运行时按 family → canonical model → route profile → API Form → model profile → provider-local override 展开 supported/unsupported 清单。未注册模型、route、当前 route 下的 form 或组合 profile 只允许只读诊断，不允许启动参数测试、图片测试或压力任务。
5. `mixed_compat` 选择该模型当前 API Form/route 对应的 Reference Source，再与模型 `pressure_profiles` 和显式权重取交集；吞吐与 Cache 构造器还会删除 `pressure_omit_params` 及模型明确不支持的参数。
6. `kimi-k3` 使用专属 `kimi_k3_openai_compat`：固定官方采样参数，验证三档 effort、Preserved Thinking、`prompt_cache_key` 和动态 tools，并保留官方错误参数拒绝控制。压力路径复用同一模型 profile 的参数 alias/override。
7. 图片族的最新模型与供应商差异证据见 [`image_latest_model_param_audit.md`](image_latest_model_param_audit.md)：Gemini 3.1 Flash Image 增加原生 Interactions transport，GPT Image 2 与 Grok Imagine 以解码后的数量、格式、像素和负例拒绝作为判定依据。

完整分层、展开规则与判定逻辑的权威说明及代码入口索引见 [`param_test_architecture.md`](param_test_architecture.md)。

下面的内容保留 2026-07-10 对 Reference Source 本身的字段覆盖审查；其中“多规格尚未建模”等判断属于当时快照，当前状态以上述 schema v4 摘要和架构文档为准。

整套参数集合已经相当扎实，**DeepSeek** 与 **Gemini 原生 generateContent** 两块几乎是逐字段覆盖官方文档。问题集中在三处：

1. **少量无官方依据的探针**：仅 `gemini_native_response_format` 一项属于"无据却未说明"；`gemini_chat_*` 全家桶是有意为之的非标准透传探针（coverage 已注明）。
2. **官方有、但没测**：最值得补的是 **Qwen 的 `n` / `logprobs` / `top_logprobs` / `response_format`**。
3. **同族多规格未建模**：AI Studio↔Vertex、DeepSeek 正式↔beta、Qwen 按模型/区域、GLM 国内↔国际的规格差，除 DeepSeek beta 外均未建模；更根本的是**被测目标是第三方转售网关，reference 却是官方一方口径**，二者规格差没有在结论里显式区分。

---

## Q1. 所测参数是否有官方文档依据

| Reference source | 结论 | 说明 |
|---|---|---|
| **kimi_k3_openai_compat** | ✅ 官方专属契约 | 依据 Moonshot K3 README、Kimi Chat API 与 Kimi Vendor Verifier：显式测试 `reasoning_effort=low|high|max` 和 `reasoning_content`；历史 assistant `reasoning_content` 原样回传；`prompt_cache_key`；空 content 的 system message 内 `tools`；并验证错误 temperature/top_p、penalty 与 `n=2` 被拒绝。 |
| **deepseek_chat** | ✅ 全部有据 | messages/model/thinking/reasoning_effort/max_tokens/response_format/stop/stream/stream_options/temperature/top_p/tools/tool_choice/logprobs/top_logprobs/user_id 与官方一致；`frequency_penalty`/`presence_penalty` 正确标注 deprecated；beta 项（`messages[].prefix`、`reasoning_content`、`tools[].function.strict`）标注 `beta_endpoint_only`。规范。 |
| **glm_openai_compat** | ✅ 基本全部有据 | `reasoning_effort`（官方确认为 GLM-5.2 参数，取值 max/high/medium/low/minimal/none）、`thinking.{type,clear_thinking}`、`do_sample`、`tool_stream`、`request_id`(6–64)、`user_id`(6–128)、`tool_choice: auto` 均吻合 bigmodel 官方文档。 |
| **qwen_openai_compat** | ✅ 有据 | thinking 系列、top_k/repetition_penalty/enable_search/search_options/parallel_tool_calls/tool_stream 等扩展与 DashScope OpenAI 兼容文档一致（文档也说明这些走 extra_body 扩展）。 |
| **gemini_native_generate_content** | ⚠️ 绝大部分有据，1 项存疑 | generationConfig 的 stopSequences/responseMimeType/responseSchema/responseJsonSchema/responseModalities/candidateCount/maxOutputTokens/temperature/topP/topK/seed/presencePenalty/frequencyPenalty/responseLogprobs/logprobs/enableEnhancedCivicAnswers/thinkingConfig/mediaResolution，以及顶层 contents/tools/toolConfig/systemInstruction/cachedContent/serviceTier/store/safetySettings——**均在官方 generateContent 文档中**。**唯一存疑：`gemini_native_response_format`**（`generationConfig.responseFormat.text.mimeType`）——当前官方 GenerationConfig **无 `responseFormat` 字段**，JSON 输出应走 `responseMimeType`/`responseSchema`。疑似误搬 OpenAI 的 `response_format` 或押注未公开字段。 |
| **gemini_openai_compat** | ⚠️ 一半标准、一半探针 | 有据：reasoning_effort、max_tokens、response_format、stop、stream、stream_options、temperature、top_p、n、tools、tool_choice、service_tier、`extra_body.*`。**无官方兼容层依据（按设计如此，属探针）**：`gemini_chat_*` 系列（把裸 `generationConfig.*` 塞进 chat body）+ 顶层 `safetySettings`——Google 官方要求 Gemini 专属能力走 `extra_body` 而非裸透传。coverage 已诚实标为 `chat alias probe` / `chat top-level extension`。 |

**结论**：真正"无据且未说明"的只有 `gemini_native_response_format`。`gemini_chat_*` 是有意探针，但建议在报告/UI 上把"文档参数"与"透传探针"显式分组，避免被误读为 Gemini 官方兼容层支持这些。

---

## Q2. 对照官方文档，是否有遗漏

| 家族 | 官方有、但**未测**的参数 | 严重度 |
|---|---|---|
| **DeepSeek** | 无遗漏 | — |
| **GLM** | `stream_options.include_usage`（兼容层是否支持存疑，可补探针） | 低 |
| **Qwen** | **`n`**（qwen3-thinking）、**`logprobs`/`top_logprobs`**（官方 [0,5]）、**`response_format`**（json_object）——均为通用能力且文档明确支持却未测。（`vl_high_resolution_images`/`translation_options`/`skill` 属 VL/翻译/专项模型，可豁免） | **中** |
| **Gemini OpenAI 兼容** | 顶层 `presence_penalty`/`frequency_penalty` 官方支持，但只经 `gemini_chat_*` generationConfig 探针间接测，无标准顶层 profile。（`logprobs`/`top_logprobs`/`seed` 兼容层不支持，当前正确未测 ✅） | 低–中 |
| **Gemini 原生** | `generationConfig.speechConfig`、`audioTimestamp` 未测（音频/TTS，可豁免）；`labels` 未测（Vertex 专属，见 Q3） | 低 |

**结论**：最值得补的是 **Qwen 的 `n` / `logprobs` / `response_format`**；其次是 Gemini OpenAI 兼容顶层 `presence_penalty`/`frequency_penalty` 标准 profile。

---

## Q3. 同一模型/家族是否存在不同规格（AI Studio vs 企业版等）

这是当前设计最大的盲点，分两层：

### (a) 被测目标是第三方聚合网关，reference 却是官方一方口径

reference source 建模的是官方或明确的接口契约，实际被测 target 可能是第三方网关。网关可能静默丢弃、忽略或拒绝参数，因此一个格子的结果仍表示“该 provider/model 接口相对所选契约的兼容性”，不能单凭它证明物理上游。当前 schema v4 为所有家族提供 per-route/per-API-form/per-model `supported`/`unsupported` 期望和 evidence 标签，把“该支持却拒”的 `incompatible` 与“不该支持且拒对了”的 `expected_rejection` 分开；`requested_model` / `response_model`、identity alias 与漂移审计继续独立记录模型身份。供应商实测差异应回填精确 route/form model profile 或本地覆盖，不改写官方 Reference Source。

### (b) 官方一方本身就多规格，而每个家族只有一份 reference

- **Gemini：AI Studio（Developer API, `generativelanguage.googleapis.com`）vs Vertex AI（企业版）** —— 最典型，且差异恰落在被测参数上：
  - **`serviceTier`**：AI Studio 从 **JSON body** 读；Vertex 从 **HTTP header**（`X-Vertex-AI-LLM-Request-Type`）读，body 里的 `serviceTier` 在 Vertex 被**静默忽略**。当前 `gemini_native_service_tier`/`gemini_service_tier` 只对 AI Studio 有效。
  - **`labels`**（计费元数据）：**Vertex 专属**顶层字段，AI Studio 无——当前单一 reference 无法测。
  - **`logprobs`/`responseLogprobs`**：Vertex 支持且 GA；AI Studio 兼容层不支持顶层 logprobs（原生 generateContent 才有 `responseLogprobs`）；社区报告 **Gemini 3/3.1 在 Vertex 上 logprobs 被禁**——即同族**不同模型版本**支持度也不同。
  - **`seed`**：现为 Gemini 2.5 家族 GA，版本间不一。
  - 还有 auth（API key vs OAuth/服务账号）、endpoint、模型命名（`publishers/google/models/...`、`-001` 后缀）差异。
  - 当前 `gemini_openai_compat` + `gemini_native_generate_content` 实质都是 **AI Studio 口径，无 Vertex 变体**。
- **DeepSeek**：`api.deepseek.com` vs `/beta`（prefix/reasoning_content/strict tools/FIM）——**已建模**（`beta_endpoint_only`），正面案例。
- **Qwen**：北京 vs 国际（`dashscope-intl`）区域端点不同；参数支持**按模型分档**——`n` 仅 qwen3-thinking、`enable_thinking`/`thinking_budget` 仅思考模型、`enable_search` 仅部分模型。单一 reference 不分支，拿 qwen-max 测思考类 profile 会得"假不兼容"。
- **GLM**：`open.bigmodel.cn`（国内）vs `z.ai`（国际）双端点；`reasoning_effort`/`tool_stream` 仅 GLM-5.2/部分新版；vision/audio 模型参数集不同。

**结论**：家族内多规格真实存在且落在被测参数上，但除 DeepSeek beta 外均未建模；更根本的是被测目标（转售网关）与 reference（官方一方口径）之间的规格差没有在结论里显式区分。

---

## 附：影响"结论可信度"的实现层观察

1. **`reasoning_effort` 被强制归一到 {high, max}**（`REASONING_EFFORT_ALIASES` 把 low/medium→high）。但 GLM-5.2 支持 max/high/medium/low/minimal/none、Gemini 支持 minimal/low/medium/high/none——**当前无法测这些档位**，会给出"只支持 high/max"的失真画面。
2. **`_validate_body` 取值域是 DeepSeek 口径**（temperature 0–2、top_p 0–1），GLM 实际 [0,1]/[0.01,1]、Qwen [0,2)——校验器不区分 family。当前测试值（0.7/0.9）没踩线，但换值可能本地校验先报错、测不到上游真实行为。
3. `gemini_native_response_format` body 结构（`responseFormat.text.mimeType`）与官方 generateContent 不符（见 Q1）。

---

## 建议（按优先级）

| 优先级 | 建议 |
|---|---|
| 高 | **补 Qwen** `n` / `logprobs` / `top_logprobs` / `response_format` 四个 profile |
| 高 | **建模家族内多规格**：为 Gemini 增加 Vertex 变体 source（标注 `serviceTier` 走 header、`labels` 顶层、logprobs 按版本），并在结果里标出被测 target 是"官方一方"还是"转售网关" |
| 中 | **拆分 `gemini_openai_compat`**：把"文档参数"与"generationConfig/safety 透传探针"在 source 层或 UI 层显式分组 |
| 中 | **修 `gemini_native_response_format`**：改用 `responseMimeType`+`responseSchema`，或明确标为推测性探针 |
| 中 | **放开 `reasoning_effort` 归一**（或加显式 low/medium/minimal/none profile），让档位差异可测 |
| 低 | `_validate_body` 取值域按 family 区分；补 GLM `stream_options` 探针 |

---

## Sources

- [Kimi K3 model usage](https://github.com/MoonshotAI/Kimi-K3#6-model-usage)
- [Kimi Create Chat Completion](https://platform.kimi.ai/docs/api/chat)
- [Kimi Vendor Verifier](https://github.com/MoonshotAI/Kimi-Vendor-Verifier)
- [DeepSeek Create Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion)
- [Gemini API generateContent / GenerationConfig](https://ai.google.dev/api/generate-content)
- [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)
- [Gemini Developer API vs Vertex (migrate-to-cloud)](https://ai.google.dev/gemini-api/docs/migrate-to-cloud)
- [Vertex serviceTier read from headers (js-genai #1468)](https://github.com/googleapis/js-genai/issues/1468)
- [Vertex custom metadata labels](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/add-labels-to-api-calls)
- [Logprobs on Vertex AI](https://developers.googleblog.com/unlock-gemini-reasoning-with-logprobs-on-vertex-ai/)
- [Logprobs disabled for Gemini 3/3.1 on Vertex (forum)](https://discuss.ai.google.dev/t/were-logprobs-disabled-for-gemini-3-3-1-in-vertex-api/132426)
- [GLM 对话补全 API](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)
- [Qwen via OpenAI Chat Completions (DashScope)](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)
