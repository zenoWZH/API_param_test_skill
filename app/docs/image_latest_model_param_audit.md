# 最新图片模型参数 Profile 与供应商对比

日期：2026-07-29

## 选择结论

图片参数测试继续按 `image` modality 分成三个模型家族，并各选一个当前主对比模型：

| 家族 | 主对比模型 | 官方 transport | 本次公平矩阵 |
|---|---|---|---|
| GPT Image | `gpt-image-2` | `POST /v1/images/generations` | 任意尺寸边界、质量、格式、压缩、背景、moderation、批量数 |
| Gemini Image / Banana | `gemini-3.1-flash-image` | `POST /v1beta/interactions` | 512/1K/2K、16:9、MIME，以及非法小写 `1k`/非法宽高比拒绝 |
| Grok Imagine | `grok-imagine-image` | `POST /v1/images/generations` | 1K/2K、宽高比、批量数、URL/base64，以及非法宽高比/分辨率拒绝 |

官方依据：

- [GPT Image 2 model](https://developers.openai.com/api/docs/models/gpt-image-2)
- [OpenAI image generation guide](https://developers.openai.com/api/docs/guides/image-generation)
- [Gemini image generation](https://ai.google.dev/gemini-api/docs/image-generation)
- [Gemini Interactions API](https://ai.google.dev/api/interactions-api)
- [Grok Imagine Image model](https://docs.x.ai/developers/models/grok-imagine-image)
- [xAI image generation](https://docs.x.ai/developers/model-capabilities/images/generation)

## 模型级能力合同

`model_capability_profiles.yaml` 现在把三套家族矩阵与具体模型 profile 分开：

- GPT Image 2 将 `size`、`quality`、`output_format`、`output_compression`、`background`、`moderation` 与 `n` 记为支持；透明背景及五类尺寸/背景负例记为不支持，正确拒绝才通过。
- Gemini 3.1 Flash Image 使用官方 Interactions transport、`x-goog-api-key` 和 `response_format`；原生输出按当前合同固定 `image/jpeg`，并只解析最终 `model_output` step 中的 image，避免把 thought image 当最终输出。兼容网关仍可选择 `chat-completions`，但必须与官方 native transport 分开比较。
- Grok Imagine 保留官方 `aspect_ratio`、`resolution`、`n`、`response_format` 合同，并注册日期别名 `grok-imagine-image-2026-03-02`。
- 所有成功结果必须实际解码图片，并检查数量、格式和像素；HTTP 200 但参数被静默忽略仍判失败。

控制台 Job Spec 固化 transport、auth mode 与模型 profile，命令行不会根据 endpoint 猜协议。原生 Gemini 的 quality/output format 控件被隐藏并固定 JPEG，负例开关仍可选择。

## 已有供应商证据

历史同源报告已经证明图片矩阵能区分供应商，而不是只检查 HTTP 状态：

| 家族 / 模型 | 被测端 | 历史通过 / 总数 | 可观察差异 |
|---|---|---:|---|
| GPT Image 2 | OpenAI Official | 8 / 8 | 四个有效尺寸像素完全匹配，四个非法尺寸均返回 400 |
| GPT Image 2 | Mayi EUR | 8 / 8 | 与官方旧矩阵一致 |
| GPT Image 2 | 4sAPI | 1 / 8 | 多个尺寸被改写；四个非法尺寸均被接受 |
| GPT Image 2 | Buaga retry2 | 0 / 8 | 全部为权限 403，只能判通道不可用，不能判参数差异 |
| Grok Imagine | xAI Official | 7 / 7 | 1:1、16:9、9:16、`n=2`、URL/base64 均验证，非法值返回 422 |
| Grok Imagine | Mayi EUR retry | 1 / 7 | 多个请求被统一输出为 1280×720，非法值也被接受 |
| Gemini 3.1 Flash Image | XinyunAI | 2 / 2 | 旧 Chat 兼容矩阵的 1K/2K 像素匹配 |
| Gemini 3.1 Flash Image | Mayi EUR | 2 / 2 | 旧 Chat 兼容矩阵的 1K/2K 像素匹配 |

对应证据：

- [OpenAI GPT Image 2 summary](../reports/image_param/openai_official-gpt-image-2-resolution/summary.json)
- [Mayi GPT Image 2 summary](../reports/image_param/mayi_eur_gpt-image-2_resolution/summary.json)
- [4sAPI GPT Image 2 results](../reports/image_param/4sapi_gpt-image-2_resolution/case_results.json)
- [Buaga GPT Image 2 retry2 results](../reports/image_param/buaga-gpt-image-2-resolution-retry2/case_results.json)
- [xAI Grok Imagine results](../reports/jobs/20260729T222945Z_image_param_xai_official_grok-imagine-image_resolution/case_results.json)
- [Mayi Grok Imagine retry results](../reports/jobs/20260729T223133Z_image_param_mayi_eur_grok_grok-imagine-image_resolution_retry/case_results.json)
- [XinyunAI Gemini image results](../reports/image_param/20260720T144840Z-gemini-3.1-flash-image/case_results.json)
- [Mayi Gemini image results](../reports/image_param/mayi_eur_gemini-3.1-flash-image_resolution/case_results.json)

旧 GPT 矩阵只有 8 项，旧 Gemini 矩阵只有两个尺寸项；它们证明体系能够发现像素改写、参数静默忽略、错误拒绝语义与通道权限差异，但不能冒充本次扩展矩阵的成绩。本阶段没有新增外部计费调用。若获得明确授权，应只重跑声明上述同名模型的供应商，并固定同一 family、model、transport 与 suite；Gemini native 与 Chat-compatible 结果不得混排。
