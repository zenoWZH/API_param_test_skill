# GPT Image 模型家族 Profile 说明

<!-- 由 scripts/generate_test_docs.py 从 schema v4 生成，请勿手工维护表格。 -->

覆盖输出解码、格式、数量、任意尺寸、2K/4K 边界和无效参数拒绝。

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
| `gpt-image-2` | `gpt-image-2-2026-04-21`, `gpt-image-2-c`, `gpt-image-2-req` |
| `gpt-image-1.5` | — |
| `gpt-image-1` | — |
| `gpt-image-1-mini` | — |

## Route 与 API Form

| Route Profile | API Form | 内部 transport | 已注册模型数 | Reference Source |
|---|---|---|---:|---|
| `vendor_direct` | `openai_images_generations` | `images-generations` | 4 | — |
| `dynamic_aggregator` | `openai_images_generations` | `images-generations` | 4 | — |

## 全部图片 Case / Profile

图片测试的 2xx 还必须解码输出，并核对数量、格式和实际像素；仅收到 HTTP 200 不算通过。

| Case / Profile | API Form | 具体测试目的 | 关键请求设置 | 期望 |
|---|---|---|---|---|
| `baseline_1024_square` | `openai_images_generations` | 基线：确认返回内容可解码，并精确得到 1024×1024 图片。 | `n=1`<br>`quality="low"`<br>`size="1024x1024"`<br>`output_format="png"`<br>`expected_size=[1024,1024]` | 应支持 |
| `standard_portrait` | `openai_images_generations` | 验证官方标准竖图尺寸。 | `n=1`<br>`quality="low"`<br>`size="1024x1536"`<br>`output_format="png"`<br>`expected_size=[1024,1536]` | 应支持 |
| `arbitrary_landscape` | `openai_images_generations` | 验证边长为 16 倍数的自定义横图尺寸。 | `n=1`<br>`quality="low"`<br>`size="1536x864"`<br>`output_format="png"`<br>`expected_size=[1536,864]` | 应支持 |
| `square_2k` | `openai_images_generations` | 验证 2K 方图像素对应关系，并为疑似后处理分析提供对照。 | `n=1`<br>`quality="low"`<br>`size="2048x2048"`<br>`output_format="png"`<br>`expected_size=[2048,2048]` | 应支持 |
| `batch_n2_1024_square` | `openai_images_generations` | 验证 n=2 时返回两张都可解码且尺寸正确的图片。 | `n=2`<br>`quality="low"`<br>`size="1024x1024"`<br>`output_format="png"`<br>`expected_size=[1024,1024]` | 应支持 |
| `background_auto` | `openai_images_generations` | 验证自动背景参数。 | `n=1`<br>`quality="low"`<br>`size="1024x1024"`<br>`output_format="png"`<br>`background="auto"`<br>`expected_size=[1024,1024]` | 应支持 |
| `moderation_low` | `openai_images_generations` | 验证 low moderation 设置。 | `n=1`<br>`quality="low"`<br>`size="1024x1024"`<br>`output_format="png"`<br>`moderation="low"`<br>`expected_size=[1024,1024]` | 应支持 |
| `jpeg_compression_50` | `openai_images_generations` | 验证 JPEG 格式与显式压缩质量。 | `n=1`<br>`quality="low"`<br>`size="1024x1024"`<br>`output_format="jpeg"`<br>`output_compression=50`<br>`expected_size=[1024,1024]` | 应支持 |
| `landscape_4k` | `openai_images_generations` | 验证文档允许的 4K 横图上边界。 | `n=1`<br>`quality="low"`<br>`size="3840x2160"`<br>`output_format="png"`<br>`expected_size=[3840,2160]` | 应支持 |
| `reject_non_multiple_of_16` | `openai_images_generations` | 负向：非 16 倍数边长应被拒绝。 | `n=1`<br>`quality="low"`<br>`size="1537x864"`<br>`output_format="png"` | 应拒绝 |
| `reject_aspect_ratio_over_3_to_1` | `openai_images_generations` | 负向：超过 3:1 的宽高比应被拒绝。 | `n=1`<br>`quality="low"`<br>`size="3072x768"`<br>`output_format="png"` | 应拒绝 |
| `reject_below_minimum_pixels` | `openai_images_generations` | 负向：低于最小像素数应被拒绝。 | `n=1`<br>`quality="low"`<br>`size="512x512"`<br>`output_format="png"` | 应拒绝 |
| `reject_edge_over_3840` | `openai_images_generations` | 负向：单边超过 3840 应被拒绝。 | `n=1`<br>`quality="low"`<br>`size="4096x1920"`<br>`output_format="png"` | 应拒绝 |
| `reject_transparent_background` | `openai_images_generations` | 负向：不支持透明背景的模型应明确拒绝。 | `n=1`<br>`quality="low"`<br>`size="1024x1024"`<br>`output_format="png"`<br>`background="transparent"` | 应拒绝 |

## 去哪里看结果

- Web 控制台会展示当前 model/route/API Form 的 profile 状态和最近一次结果。
- 文字参数结果：`reports/param_tests/<provider>/<model>/verdict.json` 或 Web Job 目录。
- 图片参数结果：`reports/jobs/<job_id>/summary.json`、`plan.json` 和逐 case 文件。
- 总体解释方法见 [参数测试说明](../parameter_testing.md)，不要只看顶层 `pass`。
