# 图片模型参数测试接入 Web 控制台计划

状态：已完成（2026-07-20 完成控制台接入；2026-07-29 增加 Google Gemini
Interactions 原生图片 transport、认证与最终图片解码）

目标：把现有独立 CLI `scripts/image_param_test.py` 的图片参数/分辨率测试接入
`scripts/web_console.py` 控制台，作为一个新的测试 tab（子页面），支持在浏览器中配置、
运行、查看进度，并直接展示返回图片以人工核验分辨率。

## 1. 现状架构结论

### 1.1 前端

- 单页控制台：`scripts/templates/web_console.html` + `scripts/static/web_console.js`
  （原生 JS，约 2400 行）+ `scripts/static/web_console.css`。
- 信息架构为「测试类型优先」：顶部 `.test-tab` 按钮现切换四个
  `<section class="test-view">`（`paramView` / `imageView` / `loadView` / `cacheView`），
  tab 切换逻辑按 `data-tab` 通用处理。
- 每个视图包含 Setup 控件、进度条、metric grid、结果表、Report Files、Log Tail；
  通过轮询 `/api/jobs/<id>` 等接口刷新。

### 1.2 后端

- Flask 应用 `scripts/web_console.py`：`Job` dataclass + `JobManager`
  （创建/启动/停止/恢复子进程）。
- 任务类型枚举：`lib/job_spec.py:13`
  `SUPPORTED_JOB_TYPES = {"param_test", "quick_load", "cache_suite", "staircase", "soak"}`。
- 每种类型由 `_command_for_job()` 映射到一个子进程 CLI；密钥通过
  `lib/credential_security.build_provider_child_env()` 只注入子进程环境变量，
  不进 payload、日志和 job_spec。
- 每个 job 写入 `reports/jobs/<job_id>/`；已有路由
  `GET /reports/<path>`（`web_console.py:769`）可直接服务报告目录下的任意文件，
  **图片展示不需要新增静态路由**。

### 1.3 图片测试现状

- 独立 CLI `scripts/image_param_test.py` + `lib/image_validation.py`，未接入控制台。
- 配置走环境变量 `IMAGE_TEST_BASE_URL` / `IMAGE_TEST_API_KEY` / `IMAGE_TEST_MODEL`，
  脚本刻意不接受明文 `--api-key` 参数。
- 产出：`plan.json`、`model_check.json`、`case_results.json`（每 case 增量写盘）、
  `summary.json`（含 `resolution_correspondence` 与 `postprocess_inference`）、
  `images/<case>_<index>.<ext>`。`case_results.json` 的 `artifacts[]` 保存相对
  report_dir 的图片路径。
- 已支持 `--output-dir`，可把整个报告直接写进 job 目录。

## 2. 页面形态决策

新增独立 tab「图片参数测试」（当前导航第 2 个已更名为「图片（多模态）参数测试」，同一 SPA 内的子页面），不开独立路由。理由：

- 复用现有 job 轮询、进度条、全局 Current Job 栏、停止按钮和报告文件服务；
- 与现有「参数测试 / 压测 / Cache 测试」信息架构一致；
- 图片核验需要的缩略图网格 + 放大查看可在该视图内完成。

## 3. 配置方案（已定：provider 下挂 image capability）

图片能力属于 provider 的一个可选 capability，不新增 `type: image` provider，也不复制
provider、base URL 或密钥。这样同一个 provider 可以同时提供 Chat、Responses 和 Images
接口，并继续复用现有 `api_key_env` / 本地 `api_key` 机制。当前
`providers.local.yaml` 中已有的 `providers.<name>.image` 结构作为兼容基线：

```yaml
providers:
  some_provider:
    label: Some Provider
    base_url: https://provider.example/v1
    api_key_env: SOME_PROVIDER_KEY
    # 现有 Chat 配置保持不变。
    backend: openai_compatible
    default_transport: chat_completions
    models:
      default: chat-model
      candidates: [chat-model]
      families: {chat-model: openai}
    api_interfaces:
      chat_completions:
        path: /chat/completions
        auth: bearer
      images_generations:
        path: /images/generations
        auth: bearer
      gemini_interactions:
        base_url: https://generativelanguage.googleapis.com
        path: /v1beta/interactions
        auth: google_api_key
    image:
      enabled: true
      default: gpt-image-2
      models:
        - id: gpt-image-2
          family: gpt-image-2       # gpt-image-2 | banana | grok-imagine
          transport: images-generations
        - id: grok-imagine-image
          family: grok-imagine
          transport: images-generations
        - id: gemini-3.1-flash-image
          family: banana
          transport: gemini-interactions
          # 可选；缺省时只允许 transport 字段指定的单一协议。
          allowed_transports: [gemini-interactions]
```

- image transport 到现有接口 registry 的映射固定为：
  - `images-generations` -> `api_interfaces.images_generations`；
  - `chat-completions` -> `api_interfaces.chat_completions`；
  - `gemini-interactions` -> `api_interfaces.gemini_interactions`。
- endpoint 由对应 interface 的 `base_url`（缺省回退 provider `base_url`）和 `path`
  组成，再交给 `normalize_image_endpoint()` 做最终校验；不假定所有 provider 都使用
  `<base_url>/v1/images/generations`。
- `images-generations` / `chat-completions` 使用 bearer；`gemini-interactions`
  接受官方 `google_api_key`（`x-goog-api-key`）或明确声明的 Bearer 兼容网关。
- `lib/config.py` 新增只读 helper：`list_image_providers(config)`、
  `get_image_provider_config(config, provider)`、`get_image_model_config(...)`、
  `image_provider_has_api_key(...)` 和 image interface 解析 helper。
- `validate_provider_config()` 增加可选 `image` capability 校验：`enabled` 必须为布尔、
  model ID 唯一、default 必须存在于 models、family/transport/allowed_transports 必须合法、
  `chat-completions` / `gemini-interactions` 仅允许 Banana，且所有所需 interface
  必须存在并使用该 transport 允许的 auth。
- 不含 `image` 的 provider、现有 `models` Chat schema、`list_public_providers()` 及
  `/api/config.providers` 的输出契约完全不变。
- `/api/config` 另增 `image_providers` 段，只下发 provider 名称/标签、`has_key`、默认模型、
  image models 及其 family/transport/allowed_transports，不下发 key 或本地私有字段。
- `providers.local.yaml` 继续作为本地密钥和私有 image capability 覆盖入口，不入库。

## 4. 后端改动

### 4.1 `lib/job_spec.py`

- `SUPPORTED_JOB_TYPES` 增加 `image_param_test`。
- `image_param_test` 不进入 Chat workload 解析；`validate_workload()` 对该类型直接返回仅作
  防御性兼容。
- 新增 `resolve_image_plan(payload, image_provider_cfg)`，校验并归一化：
  - `model` 必须存在于所选 provider 的 `image.models`；
  - `family` 只取服务端 model 配置，不接受 payload 任意覆盖；
  - `transport` 默认取 model 配置；payload 覆盖值必须出现在该 model 的
    `allowed_transports` 中，未配置 allowed_transports 时只允许默认 transport；
  - `transport` ∈ {images-generations, chat-completions, gemini-interactions}，且后两者仅允许 Banana；
  - 根据 transport 解析并保存校验后的完整 endpoint；
  - `suite` ∈ {smoke, resolution, full}；GPT/Banana `full` 必须 `include_4k=true`，
    Grok `full` 必须 `include_2k=true`；
  - `include_2k`（默认 false，仅 Grok，前端需显式勾选确认计费）；
  - `include_4k`（默认 false，前端需显式勾选确认计费）；
  - `quality` ∈ {low, medium, high, auto}，`output_format` ∈ {png, jpeg, webp}
    （GPT/Banana Images 生效；Gemini Interactions 按官方合同固定为 JPEG；
    chat-completions 与 Grok 不传 output format）；
  - `no_negative` / `no_cross_control` / `visual_forensics` 必须为布尔，并按 family
    归一化无关选项；
  - `cases` 是可选、去重后的 case 名称子集，必须属于最终 family/suite/4K/control
    组合实际生成的矩阵；不允许用重复 case 放大计费请求；
  - 固定 Banana 模型 ID 必须 `no_cross_control=true`；
  - `timeout_sec` 为正整数，并随 image plan 快照保存；
  - 计算 `estimated_case_count`，供前端在启动前展示成本规模。
- `make_job_spec()` 序列化 `image_plan`（含 endpoint、模型和生效参数，但不含 key）。
  该字段为可选字段，旧 job spec 保持可读；无需仅因增加可选字段破坏已有 schema v1。

### 4.2 `scripts/web_console.py`

- `Job` 增加字段：`image_plan: dict | None`。
- `JobManager.create()` 必须先解析并校验 `job_type`，随后明确分流：
  - `image_param_test`：读取 provider 的 `image` capability、解析 image model/plan/key；
    跳过 Chat 专用的 `get_model_family()` 推断、Reference Source、workload/request mode、
    context window、`_validate_model()` 和 `_preflight_job()`；为兼容现有 `Job` 公共字段，
    `model_family` 使用 image model 配置、`workload="image_param"`、
    `request_mode="fixed"`、reference 字段为 `None`；
  - 其他 job type：保持现有创建流程和行为不变。
- report_dir、单任务互斥、job spec 落盘、启动/停止生命周期继续复用。
- `_command_for_job()`（或独立 `_image_command_for_job()`）必须生成完整且可测试的 argv：

  ```text
  <python> scripts/image_param_test.py
    --base-url <resolved full image endpoint>
    --api-key-env LOADTEST_SELECTED_API_KEY
    --model <selected image model>
    --family <configured family>
    --transport <effective transport>
    --auth-mode <effective interface auth>
    --suite <suite>
    --timeout <timeout_sec>
    --output-dir <report_dir>
  ```

  `images-generations` 时追加 `--quality`、`--output-format`；
  `gemini-interactions` 由 CLI 固定 JPEG；布尔项按需追加
  `--include-4k` / `--no-negative` / `--no-cross-control` /
  `--no-visual-forensics`；每个已校验 case 追加一组 `--case <name>`。
- 子进程 env 继续调用 `build_provider_child_env()`，图片 CLI 固定通过
  `--api-key-env LOADTEST_SELECTED_API_KEY` 读取密钥；不尝试注入任意 provider key
  环境变量，也不设置 `IMAGE_TEST_*`，避免与交互式 CLI 语义混淆。
- `JobManager.public()`：
  - 运行中：读 `report_dir/case_results.json`（增量）计算
    `completed_cases / total_cases`（total 优先取 `plan.json`，文件尚未出现时回退
    `image_plan.estimated_case_count`），驱动进度条；当前 case 取
    `plan.cases[completed_cases]`，不依赖解析日志；
  - 完成后：读 `summary.json` 暴露 `pass`、`failed_cases`、
    `resolution_correspondence`、`postprocess_inference`、`model_check`；
  - `case_results.json` 只在 `include_detail=True` 的详情接口下发，避免
    `/api/jobs` 列表随图片 case 数膨胀；
  - 后端验证每个 artifact 为 report_dir 内、不含绝对路径或 `..`、且后缀为受支持图片
    格式的文件，再返回 `artifact_url`；前端不自行信任并拼接原始路径；
  - `case_results.json` 短暂处于写入中导致 JSON 不完整时返回上一次/空进度，轮询不能 500。
- `_job_type_from_report_dir()` 先读取 `job_spec.json -> type`，目录名匹配仅作为旧报告
  兼容回退，并增加 `_image_param_test_` 名称识别。
- `_load_finished_jobs()` 对图片任务读取 `job_spec.json`、`plan.json`、
  `case_results.json`、`summary.json`：
  - provider/model/family/image plan 从 job spec/plan 恢复，不回退成当前活动 Chat 模型；
  - `summary.pass=true` 恢复为 completed/returncode 0，`false` 恢复为
    failed/returncode 1；
  - 进程已不存在且没有 summary 的部分报告恢复为 failed/incomplete，并保留部分 case；
  - 恢复后仍能展示缩略图、汇总和日志。
- 增加 `JobManager.latest_image_result(provider, model)` 与
  `GET /api/image-results/latest?provider=...&model=...`，前端在切换图片 provider/model
  时加载最近一个 completed/failed 结果。这里的“历史查看”明确指所选 provider/model 的
  最近结果；不在本次实现通用的全量 job 历史浏览器。
- `/api/config` 下发 `image_providers` 与默认值（suite=smoke、quality=low、
  output_format=png、timeout）。

### 4.3 安全边界（保持 CLI 现有约束）

- key 只经子进程 env 传递，不进 URL、payload、日志、job_spec、报告。
- endpoint 通过配置的 interface 解析后复用 `normalize_image_endpoint()` 校验
  （远端 HTTPS、无 userinfo/query/fragment、transport 与路径一致）。
- 4K 计费 case 必须显式勾选；`full` suite 缺确认时后端拒绝。
- Setup 区必须展示最终 case 数；负向 case 若被 provider 接受也可能产生图片费用，UI
  需在“skip negative”旁给出提示。
- artifact URL 仅指向当前 job 报告目录内经验证的 PNG/JPEG/WebP 文件；所有文本仍使用
  `textContent` 或现有 `esc()` 输出，不能把报告字段直接注入 HTML。
- 报告与图片仍视为敏感数据，只绑定本机/受信访问。

## 5. 前端改动

### 5.1 `templates/web_console.html`

- `test-nav` 增加 `<button class="test-tab" data-tab="image">图片参数测试</button>`。
- 新增 `<section id="imageView" class="test-view">`，包含：
  1. **Setup**：图片 Provider、Model（按 provider 的 image models 过滤）、
     Transport（默认取 model 配置，仅在 allowed_transports 声明时可切换）、Suite、
     Quality、Output format、
     复选框：include 4K（计费确认）/ skip negative / skip cross control /
     visual forensics；展示预计 case 数和计费提示；每个 case 次数不适用
     （图片矩阵固定 n=1）。没有可用 image provider/model 时展示明确空状态并禁用启动。
     Chat transport 禁用 Quality/Output format；GPT Image 隐藏 cross-control，Banana
     隐藏 negative；固定 Banana 模型自动勾选并锁定 skip cross-control。
  2. **Progress**：复用进度条 + metric grid（pass/fail 计数、当前 case、延迟）。
  3. **Case 结果表**：case 名、请求 size、预期、HTTP、延迟、实际宽高、格式、
     状态；成功 case 行内嵌缩略图
     `<img src="<backend artifact_url>" loading="lazy">`，点击放大（lightbox）。
  4. **汇总卡**：`resolution_correspondence` verdict、`postprocess_inference`
     verdict + score + 各信号明细、视觉分析 unavailable 原因、`model_check` 缺失模型提示。
  5. Report Files 与 Log Tail（复用现有样式）。
  6. 结果来源 pill：区分 live/current 与所选 provider/model 的 latest historical result。

### 5.2 `static/web_console.js`

- `formsByTab` 增加独立 image 表单状态；新增 image provider/model/transport/suite
  控件同步和预计 case 数计算。
- 更新 `tabForJobType()`，使 `image_param_test -> image`；更新 `isLoadJob()`，明确排除
  image job，避免它触发压测结果和曲线刷新。
- 将 image start/stop/busy hint 纳入 `renderBusyState()`，并在 `bindEvents()` 绑定新增
  provider/model/transport/suite/checkbox/lightbox 事件。
- 新增：`loadImageConfig()`（填充 image provider/model 下拉）、
  `startImageJob()` / `renderImageResults(job)` / `imageParamMetrics(job)`、
  `loadLatestImageResult()`、缩略图 lightbox 处理。
- `jobPayload("image_param_test")` 只发送 provider/model 和可由用户配置的 image plan 字段，
  不发送 family、endpoint 或 key 等服务端权威字段。
- `renderAllResults()` 增加 image 分发；复用现有 job 轮询主循环，
  `job.type === "image_param_test"` 时只更新图片视图。
- lightbox 使用后端返回的 artifact URL，并通过 DOM 属性/textContent 或现有 `esc()`
  渲染说明字段；支持 Escape、点击遮罩和关闭按钮。

### 5.3 `static/web_console.css`

- 新增：响应式缩略图网格（`img.image-thumb`，固定高度、object-fit、边框标注
  pass/fail）、lightbox 遮罩/键盘焦点样式、汇总卡和空状态样式。

## 6. 测试与文档

- `tests/` 新增/扩展可由现有 unittest/pytest 发现的测试：
  - config：无 image capability 的 Chat provider 输出/校验不变；同一 provider 可同时
    配置 Chat + Image；image default/model/interface/auth/transport 非法组合被拒绝；
  - `resolve_image_plan`：非法 transport 覆盖、固定模型缺 no-cross-control、full 缺 4K
    确认、重复/越界 case、Chat transport 参数归一化、case 数估算；
  - argv/env：完整断言 `--model`、quality/format/timeout/cases，且子进程只通过
    `LOADTEST_SELECTED_API_KEY` 获得密钥，job spec/响应/日志不含 key；
  - `JobManager.create`：image capability 校验、image 分支不调用 Chat reference/context/
    preflight、单任务互斥和 400/201 路径；
  - progress/public：plan 尚未生成、部分 case、临时损坏 JSON、完成 summary、artifact
    路径穿越/跨 job 拒绝，且列表接口不携带完整 case_results；
  - recovery/history：job spec 优先识别类型，completed/failed/incomplete 正确恢复，
    latest image result 按 provider/model 过滤；
  - 前端静态契约：image job 映射到 image tab、不属于 load job、新按钮进入 busy/stop
    管理、没有 provider 时禁用启动。
- 增加本地 fake Image API 集成测试：返回最小可验证图片与 models 响应，覆盖 Flask 创建
  job -> 子进程 -> 增量 case_results -> summary/artifact URL，全程不依赖真实密钥或外网。
  API/recovery 测试使用临时 jobs root 和隔离的 manager，不扫描工作区现有历史报告。
- 最终手动验证：对真实图片 provider 跑一次 smoke suite，确认进度条、case 表、
  缩略图与放大、summary 卡、重启恢复和 latest historical result；真实调用只作为最终
  验收，不替代本地自动化集成测试。
- 文档：
  - `docs/image_param_test.md` 第 1 节「不属于 Web Console」改为描述 Web 入口，
    CLI 保留为等价复现手段；
  - README 快速入口补一行；
  - `PLAN.md` 现有 `image-param-console-tab` 在实现和验证全部完成后才置为 completed。

## 7. 实施顺序

1. `lib/config.py` image capability schema、interface 映射与公开 helper（含隔离测试）。
2. `lib/job_spec.py` 新 job type + `resolve_image_plan` + secret-free snapshot（含测试）。
3. `web_console.py` create 前置分流、完整命令/env、进度/public、安全 artifact URL、恢复、
   latest result 与 `/api/config`（含 API/恢复测试）。
4. 模板 + JS + CSS 新 tab、条件化控件、历史结果与安全图片渲染。
5. 本地 fake Image API 端到端验证。
6. 真实 provider smoke + 控制台重启恢复验证 + 文档更新，最后将 `PLAN.md` 条目标为
   completed。

估计工作量：后端约 450–600 行，前端约 300–400 行，测试约 250–350 行；控制台接入
本身无新第三方依赖，视觉启发式继续使用现有可选 `requirements-image.txt`。

## 8. 实施结果

- `lib/config.py` 已实现 provider 下挂 image capability、接口/auth/transport 校验与公开列表。
- `lib/job_spec.py` 已实现 `image_param_test`、服务端权威 plan 归一化、case 预算与无密钥快照。
- Flask 已实现图片任务分流、子进程环境注入、增量进度、安全 artifact URL、最近结果与重启恢复。
- 控制台已增加独立图片 tab（当前第 2 个「图片（多模态）参数测试」）、条件控件、case 表、缩略图/lightbox 和汇总卡。
- 自动测试覆盖非法配置/计划、argv 与密钥边界、部分 JSON、路径穿越、恢复/历史、前端静态契约，
  并通过本地 fake Image API 完整验证 Flask -> 子进程 -> summary/artifact 链路。
- 真实 OpenAI Official `gpt-image-2` smoke 已通过 Web job 完成：1/1 case 通过，返回
  `1024x1024` PNG，artifact URL 返回 `200 image/png`；控制台重启后 latest result 正确恢复。
  报告目录为
  `reports/jobs/20260721T024334Z_image_param_test_openai_official_gpt-image-2_9322b9a5/`。
