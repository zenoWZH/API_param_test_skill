# Banana / Gemini Image 模型家族 Profile 说明

<!-- 由 scripts/generate_test_docs.py 从 schema v4 生成，请勿手工维护表格。 -->

区分兼容 Chat 与 Gemini Interactions，并验证分辨率、宽高比和模型别名控制。

本文档回答三个问题：这个家族有哪些模型身份、在不同 route/API Form 下使用哪份测试契约、每个 profile 实际发送什么并检查什么。

## 在界面中使用本手册

![图片参数测试界面示意](../assets/ui/image-parameter-console.svg)

先在界面按 Provider → Model → Route Profile → API Form → Suite 选择组合，再用下文表格确认图片 case、费用确认和验收要求。

## 先理解判读规则

- `应支持`：期望 HTTP 2xx，且响应结构、内容语义、usage、returned-model 均通过校验。
- `应拒绝`：期望明确的 400/422；若仍返回 2xx，记为 `unexpected_acceptance`。
- `按模型/route 变化`：同一 profile 对家族内不同模型或 route 的期望不同，运行前以控制台展开的 model profile 为准。
- 动态聚合 route 即使全部通过，也只证明 adapter 兼容，不能证明物理上游或原厂合同。

## 模型与 alias

| 规范模型 | 显式 alias |
|---|---|
| `gemini-2.5-flash-image` | — |
| `gemini-3.1-flash-image` | `gemini-3.1-flash-image-preview` |
| `gemini-3-pro-image` | — |
| `nano-banana-pro` | `nano-banana-pro-{resolution_lower}`, `nano-banana-pro-{resolution}`, `nano-banana-pro-1k`, `nano-banana-pro-2k`, `nano-banana-pro-4k` |

## Route 与 API Form

| Route Profile | API Form | 内部 transport | 已注册模型数 | Reference Source |
|---|---|---|---:|---|
| `provider_compat` | `openai_chat_completions` | `chat-completions` | 4 | — |
| `provider_compat` | `gemini_interactions` | `gemini-interactions` | 4 | — |
| `google_ai_studio` | `gemini_interactions` | `gemini-interactions` | 4 | — |

## 全部图片 Case / Profile

图片测试的 2xx 还必须解码输出，并核对数量、格式和实际像素；仅收到 HTTP 200 不算通过。

| Case / Profile | API Form | 具体测试目的 | 关键请求设置 | 期望 |
|---|---|---|---|---|
| `banana_1k_aligned` | `gemini_interactions`<br>`openai_chat_completions` | 验证 1K 请求与模型/别名分辨率一致。 | `n=1`<br>`quality="low"`<br>`size="1024x1024"`<br>`output_format="png"`<br>`model_override="nano-banana-pro-1k"`<br>`expected_size=[1024,1024]` | 应支持 |
| `banana_2k_aligned` | `gemini_interactions`<br>`openai_chat_completions` | 验证 2K 请求与模型/别名分辨率一致。 | `n=1`<br>`quality="low"`<br>`size="2048x2048"`<br>`output_format="png"`<br>`model_override="nano-banana-pro-2k"`<br>`expected_size=[2048,2048]` | 应支持 |
| `banana_model_1k_request_2k` | `openai_chat_completions` | 交叉控制：1K 模型别名配 2K 请求，判断真正生效的控制来源。 | `n=1`<br>`quality="low"`<br>`size="2048x2048"`<br>`output_format="png"`<br>`model_override="nano-banana-pro-1k"` | 应支持 |
| `banana_model_2k_request_1k` | `openai_chat_completions` | 交叉控制：2K 模型别名配 1K 请求，判断真正生效的控制来源。 | `n=1`<br>`quality="low"`<br>`size="1024x1024"`<br>`output_format="png"`<br>`model_override="nano-banana-pro-2k"` | 应支持 |
| `banana_4k_aligned` | `gemini_interactions`<br>`openai_chat_completions` | 验证需显式计费确认的 4K 请求。 | `n=1`<br>`quality="low"`<br>`size="4096x4096"`<br>`output_format="png"`<br>`model_override="nano-banana-pro-4k"`<br>`expected_size=[4096,4096]` | 应支持 |
| `banana_512_square` | `gemini_interactions`<br>`openai_chat_completions` | 验证 Interactions 官方 512 分辨率档。 | `n=1`<br>`quality="low"`<br>`size="512x512"`<br>`output_format="png"`<br>`model_override="gemini-3.1-flash-image"`<br>`expected_size=[512,512]` | 应支持 |
| `banana_1k_landscape_16_9` | `gemini_interactions`<br>`openai_chat_completions` | 验证 Interactions 的 1K、16:9 组合。 | `n=1`<br>`quality="low"`<br>`size="1024x1024"`<br>`output_format="png"`<br>`model_override="gemini-3.1-flash-image"`<br>`expected_size=[1376,768]` | 应支持 |
| `banana_reject_lowercase_1k` | `gemini_interactions`<br>`openai_chat_completions` | 负向：错误的小写分辨率枚举应被拒绝。 | `n=1`<br>`quality="low"`<br>`size="1024x1024"`<br>`output_format="png"`<br>`model_override="gemini-3.1-flash-image"` | 应拒绝 |
| `banana_reject_aspect_ratio_7_5` | `gemini_interactions`<br>`openai_chat_completions` | 负向：未登记的 7:5 宽高比应被拒绝。 | `n=1`<br>`quality="low"`<br>`size="1024x1024"`<br>`output_format="png"`<br>`model_override="gemini-3.1-flash-image"` | 应拒绝 |

## 去哪里看结果

- Web 控制台会展示当前 model/route/API Form 的 profile 状态和最近一次结果。
- 文字参数结果：`reports/param_tests/<provider>/<model>/verdict.json` 或 Web Job 目录。
- 图片参数结果：`reports/jobs/<job_id>/summary.json`、`plan.json` 和逐 case 文件。
- 总体解释方法见 [参数测试说明](../parameter_testing.md)，不要只看顶层 `pass`。
