# Grok Imagine 模型家族 Profile 说明

<!-- 由 scripts/generate_test_docs.py 从 schema v4 生成，请勿手工维护表格。 -->

覆盖 1K/2K、宽高比、批量数量、URL/b64 交付与越界拒绝。

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
| `grok-imagine-image` | `grok-imagine-image-2026-03-02` |
| `grok-imagine-image-quality` | — |

## Route 与 API Form

| Route Profile | API Form | 内部 transport | 已注册模型数 | Reference Source |
|---|---|---|---:|---|
| `vendor_direct` | `openai_images_generations` | `images-generations` | 2 | — |
| `dynamic_aggregator` | `openai_images_generations` | `images-generations` | 2 | — |

## 全部图片 Case / Profile

图片测试的 2xx 还必须解码输出，并核对数量、格式和实际像素；仅收到 HTTP 200 不算通过。

| Case / Profile | API Form | 具体测试目的 | 关键请求设置 | 期望 |
|---|---|---|---|---|
| `grok_1k_square_b64` | `openai_images_generations` | 基线：验证 1K 方图的 b64 解码与像素。 | `n=1`<br>`aspect_ratio="1:1"`<br>`resolution="1k"`<br>`response_format="b64_json"`<br>`expected_size=[1024,1024]` | 应支持 |
| `grok_1k_landscape_16_9` | `openai_images_generations` | 验证 1K、16:9 横图。 | `n=1`<br>`aspect_ratio="16:9"`<br>`resolution="1k"`<br>`response_format="b64_json"` | 应支持 |
| `grok_1k_portrait_9_16` | `openai_images_generations` | 验证 1K、9:16 竖图。 | `n=1`<br>`aspect_ratio="9:16"`<br>`resolution="1k"`<br>`response_format="b64_json"` | 应支持 |
| `grok_1k_batch_n2` | `openai_images_generations` | 验证 n=2 的批量生成和逐张解码。 | `n=2`<br>`aspect_ratio="1:1"`<br>`resolution="1k"`<br>`response_format="b64_json"`<br>`expected_size=[1024,1024]` | 应支持 |
| `grok_1k_square_url` | `openai_images_generations` | 验证临时 URL 交付可下载、可解码。 | `n=1`<br>`aspect_ratio="1:1"`<br>`resolution="1k"`<br>`response_format="url"`<br>`expected_size=[1024,1024]` | 应支持 |
| `grok_2k_square_b64` | `openai_images_generations` | 验证需计费确认的 2K 方图。 | `n=1`<br>`aspect_ratio="1:1"`<br>`resolution="2k"`<br>`response_format="b64_json"`<br>`expected_size=[2048,2048]` | 应支持 |
| `grok_2k_landscape_16_9` | `openai_images_generations` | 验证需计费确认的 2K、16:9 横图。 | `n=1`<br>`aspect_ratio="16:9"`<br>`resolution="2k"`<br>`response_format="b64_json"` | 应支持 |
| `grok_2k_portrait_9_16` | `openai_images_generations` | 验证需计费确认的 2K、9:16 竖图。 | `n=1`<br>`aspect_ratio="9:16"`<br>`resolution="2k"`<br>`response_format="b64_json"` | 应支持 |
| `grok_reject_aspect_ratio_7_5` | `openai_images_generations` | 负向：非法宽高比枚举应被拒绝。 | `n=1`<br>`aspect_ratio="7:5"`<br>`resolution="1k"`<br>`response_format="b64_json"` | 应拒绝 |
| `grok_reject_resolution_4k` | `openai_images_generations` | 负向：未支持的 4K 档应被拒绝。 | `n=1`<br>`aspect_ratio="1:1"`<br>`resolution="4k"`<br>`response_format="b64_json"` | 应拒绝 |
| `grok_reject_n11` | `openai_images_generations` | 负向：超过最大批量数量 10 应被拒绝。 | `n=11`<br>`aspect_ratio="1:1"`<br>`resolution="1k"`<br>`response_format="b64_json"` | 应拒绝 |

## 去哪里看结果

- Web 控制台会展示当前 model/route/API Form 的 profile 状态和最近一次结果。
- 文字参数结果：`reports/param_tests/<provider>/<model>/verdict.json` 或 Web Job 目录。
- 图片参数结果：`reports/jobs/<job_id>/summary.json`、`plan.json` 和逐 case 文件。
- 总体解释方法见 [参数测试说明](../parameter_testing.md)，不要只看顶层 `pass`。
