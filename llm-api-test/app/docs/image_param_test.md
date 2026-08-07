# 图片参数与分辨率测试手册

本手册对应 [`scripts/image_param_test.py`](../scripts/image_param_test.py) 和
[`lib/image_validation.py`](../lib/image_validation.py)，是 GPT Image 2、Banana 与 Grok Imagine
图片参数测试的完整操作说明。主 README 只保留快速入口。

## 1. 测试范围与边界

图片测试已接入 Web Console 的第 2 个「图片（多模态）参数测试」tab，同时保留独立 CLI 作为
等价复现入口。它仍与通用 Chat 参数矩阵隔离，也不进入 Locust、Staircase、Soak 或
Cache 指标；Web 与 CLI 明确区分两种 OpenAI-compatible 图片传输和 Google 原生传输：

```text
GET  /v1/models                   # GPT Image 2 / Banana provider model discovery
GET  /v1/image-generation-models  # Grok Imagine 官方模型发现
POST /v1/images/generations       # 默认，GPT Image 2、Grok Imagine 或 provider image route
POST /v1/chat/completions         # Banana only，返回 message 图片
GET  /v1beta/models               # Google Gemini 原生模型发现
POST /v1beta/interactions         # Gemini 3.1 Flash Image 原生 Interactions API
```

当前覆盖：

- 请求的 `size` 是否被接受，以及返回图片的实际宽高是否逐项对应。
- `quality`、`output_format`、`output_compression`、`background`、`moderation`
  与 `n` 的透传、负向边界及响应格式。
- PNG、JPEG、WebP 的文件结构和宽高；PNG 额外验证 chunk CRC、IHDR、IDAT 解压和 IEND。
- GPT Image 2 本地契约的合法尺寸和非法边界。
- Grok Imagine 官方 `aspect_ratio`、`resolution=1k/2k`、`n=1..10` 和
  `response_format=url/b64_json`，并解码核验实际数量、格式与像素尺寸。
- Banana provider alias 与 `size` 冲突时，实际由哪一个控制输出像素。
- 固定 Banana 模型通过 `extra_body.google.image_config.image_size` 请求 1K/2K/4K，
  并从 Chat `message.content` / `message.images` 的 data URL 解码实际图片。
- `gemini-3.1-flash-image` 通过官方 Interactions API 测 512/1K/2K/4K、
  `response_format.aspect_ratio/image_size/mime_type`、非法小写 `1k` 和非法比例，
  从最终 `model_output` step 解码图片；thought image 不冒充最终产物。
- 输出 token、延迟、字节密度和图像残差是否呈现疑似后置超分信号。

不覆盖：

- 文生图质量、美学、提示词遵循度和内容安全策略排名。
- Vertex 原生端点。Google AI Studio 的 Interactions API 已覆盖；供应商 Chat
  兼容模型或 alias 仍不根据名称推断真实上游身份。
- 对“原生分辨率生成”“latent refinement”或“生成后超分”的确定性归因。
- 图片编辑、mask、variation 或批量并发性能。

## 2. 安装与快速开始

需要 Python 3.11+；脚本使用 `datetime.UTC`，不能由 Python 3.10 直接运行。
基础结构和尺寸验证使用项目主依赖。半尺寸重采样残差等视觉启发式需要 Pillow：

```bash
pip install -r requirements.txt
pip install -r requirements-image.txt  # 可选
```

Web 入口使用 `providers.<name>.image` capability；图片模型与 Chat 模型可以挂在同一
provider 下，并复用该 provider 的 `base_url`、`api_interfaces` 与 `api_key_env`。配置好
`image.enabled/default/models` 及 `api_interfaces.images_generations`（Banana Chat 则使用
`chat_completions`，Google 原生则使用 `gemini_interactions`）后，启动控制台并选择
「图片参数测试」：

```bash
python scripts/web_console.py
# http://127.0.0.1:8090/
```

页面按 provider → model → route profile → API Form 选择图片测试组合；内部 transport 由 API Form 映射，启动前显示最终 case 数和 2K/4K/负向用例
计费提醒；运行中展示逐 case 进度，完成后展示尺寸/格式判定、缩略图与放大预览、
`resolution_correspondence`、`postprocess_inference` 和模型列表检查。控制台报告写入
`reports/jobs/<job_id>/`，重启后可恢复，并按 provider/model 显示最近一次完成或失败结果。
密钥仅通过 `LOADTEST_SELECTED_API_KEY` 注入图片子进程，不进入 job spec、命令参数或报告。

CLI 入口仍适合脚本化复现。推荐用环境变量传入地址和密钥：

```bash
export IMAGE_TEST_BASE_URL='https://provider.example'
export IMAGE_TEST_API_KEY='<provider-key>'

# 不读取 key、不联网，先检查请求计划
python scripts/image_param_test.py \
  --family gpt-image-2 --suite resolution --dry-run

# GPT Image 2 分辨率和非法边界
python scripts/image_param_test.py \
  --family gpt-image-2 --suite resolution

# Banana 1K/2K aligned + crossed control
python scripts/image_param_test.py \
  --family banana --suite resolution \
  --model 'nano-banana-pro-{resolution_lower}'

# New API 风格的固定 Banana 模型，通过 Chat Completions 返回图片
python scripts/image_param_test.py \
  --family banana --transport chat-completions \
  --suite resolution --model gemini-3.1-flash-image \
  --no-cross-control

# Google 官方 Gemini 3.1 Flash Image Interactions API
IMAGE_TEST_BASE_URL='https://generativelanguage.googleapis.com' \
IMAGE_TEST_API_KEY='<gemini-key>' \
python scripts/image_param_test.py \
  --family banana --transport gemini-interactions \
  --suite resolution --model gemini-3.1-flash-image \
  --no-cross-control

# Grok Imagine 官方参数矩阵；full 需显式确认 2K 费用
python scripts/image_param_test.py \
  --family grok-imagine --model grok-imagine-image \
  --suite full --include-2k
```

不希望 key 进入进程环境时，可使用隐藏输入：

```bash
python scripts/image_param_test.py \
  --base-url https://provider.example \
  --family gpt-image-2 --suite smoke --api-key-stdin
```

脚本不接受明文 `--api-key` 参数，避免 key 进入 shell history 和进程列表。
使用 `python scripts/image_param_test.py --help` 可查看当前代码实际接受的参数。

## 3. Endpoint 与模型约定

`--base-url` 或 `IMAGE_TEST_BASE_URL` 接受 provider root、`/v1` 或所选 transport 的
完整 endpoint。默认 `--transport images-generations`：

| 输入 | 实际生成 endpoint |
|---|---|
| `https://provider.example` | `https://provider.example/v1/images/generations` |
| `https://provider.example/v1` | `https://provider.example/v1/images/generations` |
| `https://provider.example/v1/images/generations` | 原样使用 |

使用 `--transport chat-completions` 时，provider root、`/v1` 或完整
`/v1/chat/completions` endpoint 都归一化到
`https://provider.example/v1/chat/completions`。完整 endpoint 与 transport 不匹配时会直接拒绝，
不会在错误路径后继续拼接。该 transport 当前只允许 `--family banana`。
请求使用以下 wire format：

```json
{
  "model": "gemini-3.1-flash-image",
  "messages": [{"role": "user", "content": "<prompt>"}],
  "stream": false,
  "extra_body": {
    "google": {
      "image_config": {"aspect_ratio": "1:1", "image_size": "2K"}
    }
  }
}
```

这里必须保留 `extra_body.google.image_config` 层级和 snake_case。至少在 New API 网关中，
顶层 `image_config` 不属于通用 Chat request schema，可能被静默丢弃；Gemini 原生
`generationConfig.imageConfig` 也不是本 transport 的公开请求格式。

使用 `--transport gemini-interactions` 时，provider root、`/v1beta` 或完整
`/v1beta/interactions` 会归一化到 Google Interactions endpoint；默认认证为
`google_api_key`，即 `x-goog-api-key`，也可为明确使用 Bearer 的兼容网关显式传
`--auth-mode bearer`。请求格式为：

```json
{
  "model": "gemini-3.1-flash-image",
  "input": [{"type": "text", "text": "<prompt>"}],
  "response_format": {
    "type": "image",
    "mime_type": "image/jpeg",
    "aspect_ratio": "16:9",
    "image_size": "1K"
  }
}
```

REST 响应只从 `steps[].type=model_output` 的 `content[].type=image` 提取最终图片。
Interactions SDK 的 `output_image` convenience 字段也兼容，但 thought step 中的中间图
不会进入通过判定。当前官方 ImageResponseFormat 只接受 `image/jpeg`，CLI 与控制台会
固定该 MIME，并拒绝原生 transport 上的 PNG/WebP 请求。
`usage.total_input_tokens/total_output_tokens/total_thought_tokens` 会单独归一化，
且用官方 `total_tokens = input + output + thought` 算术核对避免重复计数。

远端地址必须使用 HTTPS，且 URL 不能携带 userinfo、query 或 fragment。`GET /v1/models`
使用同一 origin 和鉴权，只作为诊断：即使列表接口缺失、返回空列表或没有列出请求模型，
生成用例仍会继续运行，详情写入 `model_check.json`。

模型选择规则：

- 所有可运行图片模型必须先在 `model_capability_profiles.yaml` 的
  `modalities.image.families.<family>.models` 注册；family 的 `suite` 决定使用
  `gpt_image_2`、`banana` 或 `grok_imagine` 用例生成器。未注册模型会在任务创建阶段
  被拒绝，不能隐式落到其它图片家族。
- `gpt-image-2` 默认模型为 `gpt-image-2`；可用 `--model` 或
  `IMAGE_TEST_MODEL` 覆盖。
- `grok-imagine` 默认模型为 `grok-imagine-image`；也可显式选择
  `grok-imagine-image-quality`。profile 不使用已退役或渠道不可用的 `-pro` 作为默认模型。
- `banana` 默认模板为 `nano-banana-pro-{resolution_lower}`。
- `banana + gemini-interactions` 未显式指定模型时，默认
  `gemini-3.1-flash-image`。
- Banana alias 模板包含 `{resolution}` 或 `{resolution_lower}` 时，每个 case 会分别展开为
  `1K/2K/4K` 或 `1k/2k/4k`。
- 供应商只暴露固定图片模型 ID 时，可直接传 `--model gemini-3-pro-image`；resolution/full
  必须同时使用 `--no-cross-control`，只运行同一模型的 aligned 1K/2K/4K 探针。
- Banana alias 是供应商自定义路由。测试只验证该 alias 与请求参数的实际关系，不根据名称
  推断其上游一定是 Google AI Studio、Vertex 或某个特定生成管线。

## 4. Suite 与用例矩阵

### 4.1 GPT Image 2

以下约束是本仓库用于兼容性探测的本地契约：宽高为 16 的倍数、总像素在
`655360..8294400`、最大边不超过 `3840`、宽高比在 `1:3..3:1`。
如果供应商公布了不同契约，应把差异作为测试结论记录，而不是静默修改预期。

| Case | Suite | 请求尺寸 | 预期 |
|---|---|---:|---|
| `baseline_1024_square` | smoke/resolution/full | 1024×1024 | HTTP 2xx，实际 1024×1024 |
| `standard_portrait` | resolution/full | 1024×1536 | HTTP 2xx，实际 1024×1536 |
| `arbitrary_landscape` | resolution/full | 1536×864 | HTTP 2xx，实际 1536×864 |
| `square_2k` | resolution/full | 2048×2048 | HTTP 2xx，实际 2048×2048 |
| `batch_n2_1024_square` | resolution/full | 1024×1024, `n=2` | 两张图片均可解码且尺寸正确 |
| `background_auto` | resolution/full | `background=auto` | HTTP 2xx |
| `moderation_low` | resolution/full | `moderation=low` | HTTP 2xx |
| `jpeg_compression_50` | resolution/full | JPEG, `output_compression=50` | 实际 JPEG 可解码 |
| `landscape_4k` | resolution/full + `--include-4k` | 3840×2160 | HTTP 2xx，实际 3840×2160 |
| `reject_non_multiple_of_16` | resolution/full | 1537×864 | HTTP 400/422 |
| `reject_aspect_ratio_over_3_to_1` | resolution/full | 3072×768 | HTTP 400/422 |
| `reject_below_minimum_pixels` | resolution/full | 512×512 | HTTP 400/422 |
| `reject_edge_over_3840` | resolution/full | 4096×1920 | HTTP 400/422 |
| `reject_transparent_background` | resolution/full | `background=transparent` | HTTP 400/422 |

说明：

- `smoke` 固定只运行 `baseline_1024_square`；此时 `--include-4k` 不生效。
- `resolution` 默认 13 项；增加 `--include-4k` 后为 14 项。
- `full` 当前等于 resolution + 4K，并强制要求 `--include-4k`，用于显式确认高成本请求。
- `--no-negative` 会移除五个非法边界用例。
- 负向请求如果被供应商接受，仍可能实际生成图片并产生费用。

### 4.2 Banana provider aliases

Banana 的 1K/2K/4K 预期尺寸是本地探针矩阵，不表示这些 alias 属于 Google 官方命名。

| Case | Suite | Alias 后缀 | `size` | 用途 |
|---|---|---:|---:|---|
| `banana_1k_aligned` | smoke/resolution/full | 1K | 1024×1024 | aligned 基线 |
| `banana_2k_aligned` | resolution/full | 2K | 2048×2048 | aligned 基线 |
| `banana_model_1k_request_2k` | resolution/full | 1K | 2048×2048 | crossed control |
| `banana_model_2k_request_1k` | resolution/full | 2K | 1024×1024 | crossed control |
| `banana_4k_aligned` | resolution/full + `--include-4k` | 4K | 4096×4096 | aligned 4K |

说明：

- `smoke` 固定只运行 1K aligned case。
- `resolution` 默认 4 项；增加 `--include-4k` 后为 5 项。
- `full` 强制要求 `--include-4k`，共 5 项。
- `--no-cross-control` 会移除两个 crossed case。
- 固定模型 ID 没有可交换的分辨率 alias，因此必须使用 `--no-cross-control`；此模式只能验证
  `size` 与实际像素是否对应，不生成 alias 控制关系结论。
- 在 `images-generations` transport 中，aligned case 使用 `size=1024x1024/2048x2048`；
  在 `chat-completions` transport 中，使用
  `extra_body.google.image_config.image_size=1K/2K` 和 `aspect_ratio=1:1`。
- crossed case 的 `status=observed` / `pass=true` 只表示请求得到可分析的响应、数量和格式正确，
  不表示尺寸匹配；最终控制关系必须查看 `resolution_correspondence`。

对于固定 `gemini-3.1-flash-image` 且 transport 为 `chat-completions` 或
`gemini-interactions`，resolution/full 额外加入当前官方能力探针：

| Case | 核心参数 | 预期 |
|---|---|---|
| `banana_512_square` | `image_size=512`, `aspect_ratio=1:1` | 512×512 |
| `banana_1k_landscape_16_9` | `image_size=1K`, `aspect_ratio=16:9` | 1376×768 |
| `banana_reject_lowercase_1k` | `image_size=1k` | HTTP 400/422 |
| `banana_reject_aspect_ratio_7_5` | `aspect_ratio=7:5` | HTTP 400/422 |

这里的 512、1376×768 和大小写规则来自 Gemini 3.1 Flash Image 官方矩阵。兼容供应商
如果不能接受其中某项，会在同一套模型 profile 下形成可比较差异；它不会被降级成通用
Banana alias 结论。`--no-negative` 可跳过两个负向项。

### 4.3 Grok Imagine

Grok profile 只发送 xAI 图片生成字段，不复用 GPT Image 的 `quality`、`output_format` 或
`size`。`response_format` 只控制 URL/base64 delivery，不预设最终文件一定是 JPEG 或 PNG；
每张返回图仍必须完成结构解码、数量和实际尺寸硬校验。

| Case | Suite | 核心请求 | 预期 |
|---|---|---|---|
| `grok_1k_square_b64` | smoke/resolution/full | `1:1`, `1k`, `b64_json`, `n=1` | 1024×1024 |
| `grok_1k_landscape_16_9` | resolution/full | `16:9`, `1k` | 实际像素宽高比严格为 16:9 |
| `grok_1k_portrait_9_16` | resolution/full | `9:16`, `1k` | 实际像素宽高比严格为 9:16 |
| `grok_1k_batch_n2` | resolution/full | `1:1`, `1k`, `n=2` | 两张 1024×1024 |
| `grok_1k_square_url` | resolution/full | `1:1`, `1k`, `url` | URL 可下载且解码为 1024×1024 |
| `grok_2k_square_b64` | resolution/full + `--include-2k` | `1:1`, `2k` | 2048×2048 |
| `grok_2k_landscape_16_9` | resolution/full + `--include-2k` | `16:9`, `2k` | 实际像素宽高比严格为 16:9 |
| `grok_2k_portrait_9_16` | resolution/full + `--include-2k` | `9:16`, `2k` | 实际像素宽高比严格为 9:16 |
| `grok_reject_aspect_ratio_7_5` | resolution/full | 非法 `7:5` | HTTP 400/422 |
| `grok_reject_resolution_4k` | resolution/full | 非法 `4k` | HTTP 400/422 |
| `grok_reject_n11` | full | 非法 `n=11` | HTTP 400/422 |

说明：

- `smoke` 固定 1 项；`resolution` 默认 7 项，增加 `--include-2k` 后为 10 项。
- `full` 强制要求 `--include-2k`，共 11 项；`--no-negative` 后为 8 项。
- URL 图片下载不转发 provider Authorization，并使用普通图片客户端 UA，兼容 xAI 临时 CDN。
- 负向请求若被非兼容网关接受，仍可能生成图片并计费；`n=11` 因潜在批量费用只进入 full。
- xAI 文档没有为非方形比例声明固定宽高，因此 profile 不把第三方渠道观察到的像素桶写成
  官方契约：方形 1K/2K 校验 1024²/2048²，非方形只硬校验比例并完整记录实际宽高。
- `size`、`quality`、`output_format` 不属于 Grok Imagine 官方生成参数，不进入正式 profile。

## 5. CLI 参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--base-url` | `IMAGE_TEST_BASE_URL` | Provider root、`/v1` 或所选 transport 的完整 endpoint |
| `--api-key-env` | `IMAGE_TEST_API_KEY` | 要读取的 key 环境变量名 |
| `--api-key-stdin` | false | 从隐藏交互提示读取 key |
| `--family` | `gpt-image-2` | `gpt-image-2`、`banana` 或 `grok-imagine` |
| `--transport` | `images-generations` | `images-generations`、Banana-only `chat-completions` 或 `gemini-interactions` |
| `--auth-mode` | 按 transport | `gemini-interactions` 默认 `google_api_key`，其他默认 `bearer` |
| `--model` | `IMAGE_TEST_MODEL` 或 family 默认值 | GPT/Grok 模型 ID、固定 Banana ID 或 Banana alias 模板 |
| `--suite` | `smoke` | `smoke`、`resolution`、`full` |
| `--include-2k` | false | Grok 专用；显式确认并加入 2K cases，Grok full 必需 |
| `--include-4k` | false | 显式确认并加入 4K case；smoke 中忽略 |
| `--no-negative` | false | GPT Image 2 / Banana / Grok 跳过非法边界请求 |
| `--no-cross-control` | false | Banana 跳过 alias/size 冲突请求 |
| `--case NAME` | 全部 suite cases | 只运行指定 case；可重复传入 |
| `--quality` | `low` | GPT/Banana Images 的 `low`、`medium`、`high`、`auto`；Grok 不发送 |
| `--output-format` | GPT/Banana Images 为 `png`；Gemini Interactions 为 `jpeg` | GPT/Banana Images 可选 `png`、`jpeg`、`webp`；Gemini Interactions 只接受 `jpeg`；Grok 不发送 |
| `--prompt` | 内置分辨率测试图提示词 | 覆盖测试提示词 |
| `--store-prompt` | false | 在 `plan.json` 保存明文 prompt；默认只保存 SHA-256 |
| `--timeout` | `300` | 单个生成请求超时秒数；models 请求最多等待 60 秒 |
| `--output-dir` | 自动时间戳目录 | 自定义报告目录；相对路径从项目根解析 |
| `--no-visual-forensics` | false | 禁用 Pillow 视觉残差，仅保留结构/像素硬校验 |
| `--dry-run` | false | 打印公共请求计划，不读取 key、不联网、不创建报告 |

`quality` 不用于 Chat 或 Gemini Interactions；Interactions 固定
`response_format.mime_type=image/jpeg`。Chat 图片协议不透传这两个字段；Grok 由 profile 自己发送
`n` 和 `response_format`，编码格式以实际响应为准，但仍会校验文件结构、数量和像素尺寸。

单 case 示例：

```bash
python scripts/image_param_test.py \
  --family gpt-image-2 --suite resolution \
  --case standard_portrait --quality medium --output-format webp
```

`--case` 只能选择当前 suite 已生成的名称。例如选择 `landscape_4k` 时还必须使用
`--suite resolution --include-4k` 或 `--suite full --include-4k`。

## 6. 单项判定与进程退出码

成功用例要求：

- HTTP 2xx。
- 返回 `data` 数量等于请求 `n`；Grok batch case 明确要求两张都通过。
- 每张图能从 Images `b64_json`、Chat Markdown/data URL 或 `message.images[].image_url`
  解码；远程图片 URL 使用不携带 provider 鉴权的新请求下载。
- 图片结构有效；GPT/Banana Images 的格式必须等于 `output_format`，Chat 与 Grok 记录并接受
  实际 PNG/JPEG/WebP 格式。
- 有明确预期尺寸的用例，实际宽高必须完全相等。

所有图片 family 的 case 都先经过模型 capability profile 展开；模型可以在家族默认之上
逐 case 覆盖 `supported` / `unsupported`。负向用例只把 HTTP `400` 或 `422` 视为 `expected_rejection`（兼容通过）。HTTP 2xx 记为
`unexpected_acceptance`（失败，对应 `invalid_parameter_accepted`），5xx/网络失败也不会被误判为正确拒绝。
GPT Image 2、Banana 与 Grok Imagine 都使用 `model_capability_profiles.yaml`，
与文字参数矩阵共享同一判定语义。

常见状态：

| `status` | `pass` | 含义 |
|---|---:|---|
| `pass` | true | 成功用例的状态、数量、格式和尺寸全部满足 |
| `expected_rejection` | true | 非法/不支持参数得到 400/422（拒对了） |
| `incompatible` | false | 期望支持的参数被 400/422 拒绝（该支持却拒） |
| `unexpected_acceptance` | false | 期望拒绝的参数仍返回 2xx（该拒却接） |
| `observed` | true | crossed case 得到可分析响应；控制结论另看汇总 |
| `fail` | false | 鉴权/限流/5xx/解码等非参数兼容失败 |

`verification_level` 进一步区分 `constraint_verified`、`expected_rejection`、
`diagnostic_observation`、`response_only` 和 `none`。只要任一 case 的 `pass=false`，
`summary.pass=false`，进程退出码为 `1`；全部通过时退出码为 `0`。

## 7. 分辨率对应关系

`summary.json -> resolution_correspondence` 只分析 Banana crossed cases：

| Verdict | 含义 |
|---|---|
| `request_parameter_controls` | 实际像素跟随 `size` |
| `model_alias_controls` | 实际像素跟随 alias 后缀 |
| `conflict_rejected` | Provider 用 400/422 拒绝冲突 |
| `mixed_behavior` | 两个 crossed case 没有一致规则 |
| `unreliable_baseline` | aligned case 失败，crossed 结论不可信 |
| `unknown` | 没有 crossed 样本或证据不足 |

这里的 `confirmed=true` 只表示当前 crossed samples 对“谁控制输出像素”给出一致结果；
它不确认上游身份、原生生成分辨率或后置超分。

## 8. 疑似超分后处理判定

`summary.json -> postprocess_inference` 是保守的黑盒启发式。只有同时满足以下条件的
case 才进入观察集：case 本身通过、`status=pass`、实际宽高严格等于请求宽高。
尺寸被供应商归一化的失败 case 和 Banana crossed observation 不参与评分。

算法以最小像素样本为 baseline，只比较像素数至少为 baseline `3.5` 倍的高分辨率样本：

| 信号 | 条件 | 分数 |
|---|---:|---:|
| 输出 token 基本不增长 | ratio ≤ 1.15 | +2 |
| 延迟基本不增长 | ratio ≤ 1.25 | +1 |
| 每百万像素字节数明显下降 | ratio ≤ 0.45 | +1 |
| 半尺寸重采样后的细节残差下降 | ratio ≤ 0.75 | +1 |

每一对样本独立评分，最终取最高分：

- `0..1`：`unknown`
- `2..3`：`suspected`
- `>=4`：`strongly_suspected`
- `confirmed`：始终为 `false`

Pillow 缺失或使用 `--no-visual-forensics` 时，残差信号为 N/A，其它信号仍可计算。
图片 token 优先读取 `completion_tokens_details.image_tokens`，然后读取正数
`output_tokens`，最后回退到 `completion_tokens`。这是为了兼容部分 Gemini Chat 网关把
顶层 `output_tokens` 固定写成 0、但在 completion details 中提供真实图片 token 的响应。

这些信号不能区分原生高分辨率生成、latent refinement 和后处理超分。文生图没有稳定 seed，
不同样本的内容复杂度也会影响 token、延迟、压缩率和残差。只有供应商阶段元数据，或受控的
原图/超分图配对，才足以支持确定性管线结论。

## 9. 报告目录与 schema

默认目录：

```text
reports/image_param/<UTC timestamp>-<model>/
├── plan.json
├── model_check.json
├── case_results.json
├── summary.json
└── images/
```

| 文件 | 内容 |
|---|---|
| `plan.json` | endpoint、transport、family、model、suite、prompt SHA-256 和展开后的 case 请求计划 |
| `model_check.json` | `/v1/models` 状态、模型列表、请求模型是否缺失；仅诊断，不是 gate |
| `case_results.json` | 增量写入的逐 case 结果、请求参数、实际图像、usage、错误和 artifacts |
| `summary.json` | 总体 pass、计数、失败 case、model check、两类 inference 和报告目录 |
| `images/` | 解码后的原始响应图像；文件名为 `<case>_<index>.<ext>` |

`actual_images[]` 包含 `format`、`width`、`height`、`pixel_count`、`byte_length`、
`sha256`、`has_alpha` 和 `visual_metrics`。上游 HTTP 错误体最多保存 1000 字符；响应
header 只保存 content type、request ID、限流和处理时长等白名单字段。

报告目录已被 git 忽略，但图片和可选明文 prompt 仍可能包含敏感业务内容。不要因为“不入库”
就把报告视为公开数据。

## 10. 密钥、重定向与下载边界

- Key 只从指定环境变量或隐藏输入读取，不进入请求 body、计划、报告或文件名。
- Provider key 封装为 origin-bound credential，只对 endpoint 的相同 scheme + host + port
  生成 `Authorization: Bearer ...`。
- `/v1/models`、`/v1/images/generations` 和 `/v1/chat/completions` 均关闭自动重定向，
  避免鉴权被带到非预期地址。
- 如果响应使用签名图片 URL，脚本新建不带 Authorization 的下载请求；URL 可以位于其它 host。
- 输出 JSON 在落盘前递归脱敏。Endpoint 会写入报告，因此禁止把 key 放进 URL query 或 userinfo。

## 11. 成本控制

- 先用 `--dry-run` 检查请求数量、模型展开和尺寸。
- 默认 `quality=low`、`n=1`；提升 quality 可能显著增加时延或费用。
- 4K 必须显式使用 `--include-4k`；`full` 还会拒绝缺少该确认的运行。
- Banana crossed cases 和 GPT 非法边界 case 都可能在宽松供应商上实际生成图片并计费。
- 可用重复的 `--case` 缩小范围，用 `--no-negative` / `--no-cross-control` 跳过诊断请求。
- 脚本会额外调用一次 `/v1/models`，但不会为缺失模型自动终止或切换模型。

## 12. 2026-07-20 nicolessss.com 快照

以下是时间点兼容性快照，不是长期供应商排名。Endpoint 为
`https://nicolessss.com/v1/images/generations`，默认测试 prompt SHA-256 为
`bfcc7ecad1f87797a0b41930c3edbae5138aa867f337ea6cd8ac77cc8005969b`。
以下命令假定 `IMAGE_TEST_API_KEY` 已在当前 shell 中安全设置。

GPT Image 2 的等价复现命令：

```bash
IMAGE_TEST_BASE_URL='https://nicolessss.com' \
python scripts/image_param_test.py \
  --family gpt-image-2 --model gpt-image-2 \
  --suite resolution --quality low --output-format png \
  --no-visual-forensics
```

本地报告：`reports/image_param/20260720T022839Z-gpt-image-2/`。

- `/v1/models` 返回 17 个模型，并包含 `gpt-image-2`。
- 8 项中 2 项通过：`1024x1536` 与 `2048x2048` 精确对应。
- `1024x1024` 实际为 `1254x1254`；`1536x864` 实际为 `1672x941`。
- 四个非法边界请求全部返回 HTTP 200 并生成图片，因此没有正确执行参数拒绝。
- 多数非 2K 输出落在约 157 万像素桶，表现为尺寸归一化，而非稳定透传任意像素尺寸。
- `postprocess_inference=unknown`：只有两个严格通过样本，像素倍率约 2.67，未达到 3.5
  的比较门槛。2K 的响应结构和 token accounting 差异只支持“可能存在不同管线”，不能确认超分。

Banana 的等价复现命令：

```bash
IMAGE_TEST_BASE_URL='https://nicolessss.com' \
python scripts/image_param_test.py \
  --family banana --suite smoke \
  --model 'nano-banana-pro-{resolution_lower}' \
  --quality low --output-format png --no-visual-forensics
```

本地报告：
`reports/image_param/20260720T023851Z-nano-banana-pro-_resolution_lower/`。

- `/v1/models` 包含 `nano-banana-pro-1k/2k/4k`。
- smoke 只覆盖 `nano-banana-pro-1k`，返回严格 `1024x1024` PNG，1/1 通过。
- 没有运行 2K、4K 或 crossed control，因此
  `resolution_correspondence=unknown`、`postprocess_inference=unknown`。
- 该结果不能外推到 Banana 2K/4K，也不能判断 alias 和 `size` 冲突时的控制关系。

## 13. 2026-07-20 api.xinyunai.cloud Banana 快照

以下是 `https://api.xinyunai.cloud/v1` 的时间点兼容性快照。密钥只通过隐藏输入传入，
未写入命令、报告或仓库。`GET /v1/models` 返回 6 个模型，其中图片模型为
`gemini-3.1-flash-image` 和 `gemini-3-pro-image`。

### 13.1 接口路由

两个模型都不能通过 `/v1/images/generations` 使用：

- `gemini-3-pro-image` 的 1K/2K 两项均返回 HTTP 500。
- `gemini-3.1-flash-image` 的 1K smoke 同样返回 HTTP 500。
- 错误均为 `new_api_error / convert_request_failed`，消息说明该 route 只支持 Imagen 模型。
- 这只证明 Images Generations route 与这两个 Gemini 图片模型不兼容，不表示模型本身不可用。

两个模型通过 `/v1/chat/completions` 均能生成图片。New API 风格参数必须使用：

```json
{
  "extra_body": {
    "google": {
      "image_config": {"aspect_ratio": "1:1", "image_size": "2K"}
    }
  }
}
```

把 `image_config` 直接放在 Chat 请求顶层时，请求虽然返回 HTTP 200，但 1K/2K 两项都得到
`1408x768` JPEG，说明该字段被静默忽略。这个对照很重要：不能只看 HTTP 200 判断参数生效，
必须解码并读取实际像素。

### 13.2 正确参数的 1K/2K 结果

复现命令，`MODEL` 分别替换为两个图片模型；不包含 4K：

```bash
IMAGE_TEST_BASE_URL='https://api.xinyunai.cloud/v1' \
IMAGE_TEST_MODEL='gemini-3.1-flash-image' \
python scripts/image_param_test.py \
  --api-key-stdin --family banana --transport chat-completions \
  --suite resolution --no-cross-control
```

| 模型 | 请求 | 实际 JPEG | 延迟 | 图片 token | 文件字节 | 结果 |
|---|---:|---:|---:|---:|---:|---|
| `gemini-3.1-flash-image` | 1K / 1:1 | 1024×1024 | 9.161s | 1120 | 756854 | pass |
| `gemini-3.1-flash-image` | 2K / 1:1 | 2048×2048 | 13.882s | 1680 | 3226400 | pass |
| `gemini-3-pro-image` | 1K / 1:1 | 1024×1024 | 15.339s | 1120 | 679290 | pass |
| `gemini-3-pro-image` | 2K / 1:1 | 2048×2048 | 22.384s | 1120 | 2742623 | pass |

本地报告：

- `reports/image_param/20260720T144840Z-gemini-3.1-flash-image/`
- `reports/image_param/20260720T144318Z-gemini-3-pro-image/`

结论边界：

- 两个模型的 `image_size=1K/2K` 都与实际像素严格对应；当前证据确认的是参数透传和输出
  尺寸，不确认上游身份。
- 固定模型没有 alias/size crossed probes，所以 `resolution_correspondence=unknown` 是预期结果，
  不是尺寸测试失败。
- Flash 的超分启发式为 `unknown` / score 0：token、延迟、字节密度和细节残差均未跨阈值。
- Pro 为 `suspected` / score 3：1K/2K 图片 token 都是 1120，且高分辨率细节残差明显下降；
  但 2K 延迟增加约 46%、文件字节约按像素增长，且每档只有一个随机样本，不能确认后置超分。
- 未测试 512、4K、非 1:1 比例、图片编辑、并发、输出格式控制或重复样本稳定性，不能外推。

参数层级依据可对照
[New API Gemini adaptor](https://github.com/QuantumNous/new-api/blob/v0.13.2/relay/channel/gemini/relay-gemini.go)
和 [Google Gemini 图片配置](https://ai.google.dev/gemini-api/docs/generate-content/image-generation)。
这里不假定供应商部署的 New API 精确版本；最终结论来自上述真实请求和返回像素。

## 14. 常见问题

### `/v1/models` 没有目标模型，是否立即失败？

不会。检查结果写入 `model_check.json`，生成请求仍会执行，以实际 endpoint 响应为准。

### 返回 HTTP 200，为什么 case 仍失败？

成功状态只是一项条件。输出数量、文件结构、格式或实际尺寸不匹配都会使 case 失败。

### `postprocess_inference=unknown` 是否表示没有超分？

不是。它只表示当前样本不足，或没有信号跨过保守阈值。

### `strongly_suspected` 是否能作为供应商作弊证据？

不能。它仍是多信号黑盒启发式，`confirmed` 永远为 `false`。

### 为什么 crossed case 自身显示通过，但总的控制关系可能 unknown？

crossed case 的通过只表示响应可分析。需要至少一个可靠 aligned 基线和一致的 crossed
输出，才能形成稳定的 `resolution_correspondence` 结论。
