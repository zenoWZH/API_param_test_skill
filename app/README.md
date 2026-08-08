# Multi-provider LLM Loadtest Console

这个目录是一个多供应商 LLM 压测项目。默认仍使用一步 API 上的 `deepseek-v4-pro`，同时支持在配置里加入其它供应商和模型，例如 `glm-5.2`、Claude 或 Gemini。主入口是自研 Web 控制台，Locust 只作为 headless 压测执行后端。

它覆盖六类流程：

1. Web Console：选择供应商和模型，启动文本/图片参数测试、快速压测、cache suite、staircase、soak。
2. Param Test：按“模型家族 → route profile → API Form → 模型 profile”检测参数兼容性、响应语义、token accuracy 与 returned-model identity。
3. Cache Suite：默认模拟“短固定 system + 变化 user + 真实工具调用/结果”，仅用官方 usage 计算缓存指标。
4. Staircase：阶梯并发，目标由 `thresholds.staircase.target_business_rpm_min` 配置。
5. Soak：1 小时稳定性长跑。
6. [Image Param Test](docs/image_param_test.md)：对 GPT Image 2、Banana 与 Grok Imagine 执行分辨率、格式、数量、边界和超分后处理信号测试。

## 文档索引

- [测试总指南](docs/testing_guide.md)：先读这里；说明参数、缓存、压测分别解决什么问题以及推荐执行顺序。
- [参数测试说明](docs/parameter_testing.md)：Route-first 选择、profile、兼容性、响应语义、token 与 returned-model identity 判读。
- [缓存测试说明](docs/cache_testing.md)：客户会话、结构探针、正负控制、官方 usage 和三个核心缓存指标。
- [压测说明](docs/load_testing.md)：Smoke、Quick、Staircase、Streaming、Soak 的口径、运行方式和结果解读。
- [模型家族 Profile 手册](docs/model_profiles/README.md)：当前所有文字与图片模型家族的逐 profile 说明，由 schema 自动生成并检查。
- [图片参数与分辨率测试专项手册](docs/image_param_test.md)：完整 CLI、图片用例、报告 schema、判定阈值和安全边界。
- [参数测试覆盖审计](docs/param_test_audit.md)：各模型家族 Reference Source 与参数覆盖缺口。
- [参数测试架构](docs/param_test_architecture.md)：当前架构、执行流程、指标口径和验收记录。

## 安装

需要 Python 3.11+；图片测试使用的 `datetime.UTC` 不兼容 Python 3.10。

```bash
cd /home/wangzhouhao/projects/yibuapi-llm-loadtest
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# 可选：启用图像细节取证启发式
pip install -r requirements-image.txt
cp .env.example .env
```

在 `.env` 中设置 `YIBU_API_KEY`。私有供应商定义可以放在 `providers.local.yaml`，但密钥统一由 `api_key_env` 指向 `.env`；这两个文件都已被 `.gitignore` 排除。

## 项目结构

```text
.
├── config.yaml
├── providers.local.example.yaml
├── locustfile.py
├── fixtures/
├── docs/
│   ├── testing_guide.md
│   ├── parameter_testing.md
│   ├── cache_testing.md
│   ├── load_testing.md
│   ├── model_profiles/
│   ├── image_param_test.md
│   └── param_test_audit.md
├── lib/
│   ├── cache_suite.py
│   ├── adaptive_load.py
│   ├── client.py
│   ├── config.py
│   ├── deepseek_params.py
│   ├── image_validation.py
│   ├── metrics.py
│   └── threshold.py
├── scripts/
│   ├── param_test.py
│   ├── image_param_test.py
│   ├── generate_test_docs.py
│   ├── run_cache.py
│   ├── run_soak.py
│   ├── run_staircase.py
│   ├── smoke_test.py
│   └── web_console.py
└── reports/
```

## 运行

自研控制台：

```bash
python scripts/web_console.py
# open http://127.0.0.1:8090/
```

控制台支持：

- 选择 provider / model / workload
- 运行文字参数测试
- 运行图片（多模态）参数测试，实时查看 case 进度、实际尺寸/格式、缩略图、放大预览和启发式汇总
- 按 provider → model → route profile → API Form → Reference Source 选择测试组合，并只显示完全匹配 route/profile 的最后一次结果
- 启动/停止快速压测
- 运行 cache suite
- 运行 staircase
- 运行 soak
- 查看 job 状态、报告文件、日志尾部和实时 summary
- 按 `metrics.live_chart_interval_sec` 分别绘制“成功率 + RPM”和“成功率 + TPM”曲线

所有生成式测试请求都会遵守 `config.yaml` 中
`test_cases.minimum_prompt_tokens` 的输入长度下限；短 prompt 会自动追加可忽略的确定性数字上下文。默认下限为 100 tokens，吞吐、流式、参数兼容性、缓存和 model sweep preflight 共用这一规则。

Reference Param Test Matrix 以所选 Reference Source 的参数为行，并把关联的一个或多个测试 profile 聚合到每次运行状态中。切换 Reference Source 会立即清空旧矩阵并加载新参数；`n/t` 表示该参数需要专用端点或当前没有可执行 profile。

参数能力注册表 `model_capability_profiles.yaml` 使用
`modality -> model family -> route profile -> API form -> model profile`

未知代理保留为 `dynamic_aggregator`，不会因为使用兼容 API 或返回相同
`model` 字段就升级为厂商 route。此类结果可获得 `adapter_pass`，但
`certified_route_contract_pass` 保持为 false。当前供应商来源分类及每个家族的
route/API Form 组合见 [`docs/model_supplier_route_catalog.md`](docs/model_supplier_route_catalog.md)。
结构。OpenAI Chat Completions、Responses、Anthropic Messages 与 Gemini
GenerateContent 是 API Form，不是模型家族。每个实际模型（含显式 alias）必须在每个
可执行 route + API Form 组合下分别注册 profile；未注册模型、route、当前 route 下的 form 或组合 profile 只允许只读诊断，
文字/图片参数任务和压力任务都会拒绝启动。私有或临时模型差异可写入已忽略的
`model_capability_profiles.local.yaml`。

文字参数判定以该注册表展开后的 per-model 期望为准：模型 profile 会明确产出
`supported_profiles` / `unsupported_profiles` 和
`supported_parameters` / `unsupported_parameters`。`supported` 期望
2xx+校验通过（`pass`），若 400/422 则记 `incompatible`；`unsupported`
期望 400/422（`expected_rejection`，计入兼容通过），若仍 2xx 则记
`unexpected_acceptance`（兼容失败）。`incompatible` 表示“该支持却被拒”，
`expected_rejection` 表示“不该支持且拒对了”。文字与图片参数测试共用
同一套判定；完整架构与代码入口索引见
[参数测试架构](docs/param_test_architecture.md)。

`kimi-k3` 不再借用 GPT 的 `openai_chat_base`，而是自动选择
`kimi_k3_openai_compat`。该来源固定 `temperature=1.0` /
`top_p=0.95`，分别验证 `reasoning_effort=low|high|max`、响应
`reasoning_content`、历史轮完整 assistant 消息回传、`prompt_cache_key`
和 system 消息内动态 `tools`；同时用官方负向约束检查错误采样值、penalty
和 `n=2` 是否被拒绝。K3 的普通吞吐/Cache 请求也由模型 profile 把
`max_tokens` 改写为 `max_completion_tokens`，并应用同一采样/effort 参数，
避免参数测试与压力测试使用两套模型契约。

工具调用验证可在前端选择 `Auto (by Reference)`、OpenAI-compatible
`tool_calls` 或 Gemini Native `functionCall`。Auto 按 Reference
Source/传输协议选择。验证会检查调用 ID、函数名、声明匹配、arguments
结构，并完成一次工具结果回传；Gemini Native 回传会原样保留模型 parts
及 thought signature。`tools` 与 `tool_choice` 使用独立 profile，避免把两个参数合并判定。

每个文本参数 exchange（包括 SSE 末块、多个 candidate、tool initial/follow-up）和图片参数请求都会写入 schema v2 `token_audit`。Usage 先统一为 `input/output/answer/thinking/image/cached/total`，再严格检查非负性、`input + output = total` 以及子项不重复累计。只有模型精确 tokenizer、tokenizer JSON 或显式配置的 count-token 接口才能产生 token accuracy PASS/FAIL；旧的 ±50% 字符估算只作为显示证据，缺少精确计数器时结论为 N/A。可选本地依赖通过 `pip install -r requirements-token-audit.txt` 安装，计数器配置位于 `providers.<name>.models.token_counters.<model>`；本地计数器必须用 `exact_dimensions` 显式声明其能精确覆盖的 input/output 维度，未声明不会参与门禁。

参数矩阵开始前会发送一个低成本、非流式、无工具的 identity probe，并把后续所有响应作为身份样本。默认只接受响应 `model` / Gemini `modelVersion` 与请求模型精确相同；合法版本名必须在 `providers.<name>.models.identity_aliases.<requested_model>` 显式登记。状态为 `match | mismatch | suspicious | unverifiable`：确认 `mismatch` 阻断任务，`suspicious` 仅告警，缺少响应身份信号时保持 `unverifiable`，不会把模型自述、回答风格或 `/models` 列表冒充成执行身份 PASS。

Cache Suite 默认使用 `progressive_customer_session`：每次运行从第一个可缓存语义 token 注入不可预测 run nonce；每个独立会话从短固定 system 和首部唯一 user 开始，所有会话完成 seed 并统一等待后，再按轮次交错追加真实 assistant、变化 user、真实工具调用和唯一 tool result。客户流量、自动结构探针、长前缀正向控制和随机首部负向控制分别统计，探针/控制样本不会混入客户命中率。

Cache 基础界面只保留会话数、每会话轮数、内容档位和工具阶段。默认 `10 sessions × 4 rounds`，工具在第 3 轮增加一次 follow-up，另有 1 个自动结构探针、3 组 positive pair 和 3 个 negative request，共 60 个请求。字符范围、控制模式、运行预算和 seed 位于高级设置；超过 100 个请求时才要求大规模确认，超过 1000 个请求直接拒绝。`kilocode_agent_session`、`growing_conversation` 与 `shared_prefix` 位于折叠诊断模式；结果分别标记为 v11、v10、v9、v8 或 legacy，不混用不同语义。

`kilocode_agent_session`（v11）模拟 Kilo Code Agent 的真实请求形态：固定大系统提示词（`fixtures/kilocode_system_prompt.txt`）+ 10 个工具 Schema（`fixtures/kilocode_tools.json`）+ append-only 多步会话，每步注入脚本化的 assistant tool_call 与大文件 tool result（`fixtures/long_context.txt` 切片）。默认 1 次 warmup + 20 步主会话（`trajectory_mode: scripted`，可选 `random` 加权随机、seed 固定）+ 3 组 positive pair + 3 个 negative request，共 30 个请求。gate 判定要求实测 `cached_input_token_ratio ≥ 0.90`、measurement coverage ≥ 0.90、positive warm ≥ 0.50、negative ≤ 0.10，四键在 `cache_test.diagnostic_defaults.kilocode_agent_session.thresholds` 显式配置。旧的 `customer_tool_flow` 场景已删除：携带 `scenario: customer_tool_flow` 或 v8 `cases` 结构的 job_spec 会被 `resolve_cache_plan` 明确拒绝。

快速压测中的 RPM/TPM cap 是速率上限；TPM 会先按请求内容估算并预留，响应后再用实际 usage 校正。Staircase 中两者只作为达标目标，不会限制各阶梯的实际吞吐。

普通压测的 `request_mode` 默认为 `unique`，会从第一个 user 内容开头插入 nonce，避免 Provider prompt cache 虚增容量；只有明确测试固定请求缓存效果时才选择 `fixed`。Staircase 与 Soak 要求确定性的 `throughput*` workload，拒绝 `mixed_compat`。所有压力路径在构造请求前都要求模型 profile 已注册：`mixed_compat` 只从该模型 Reference Source 的 `pressure_profiles` 中选取用例，模型标记为不支持或列入 `pressure_omit_params` 的参数会从吞吐/Cache 请求中剔除；`pressure_parameter_aliases` 与 `pressure_overrides` 可按模型改写压力参数。参数兼容性探针会绕过这些压力保护，保留待测或错误参数，用于验证供应商是否正确接受/拒绝。

Web 创建 Staircase、Cache 或 Soak 任务时会在 `reports/jobs/<job_id>/job_spec.json` 写入无密钥参数快照。Runner 只执行该快照，API、页面与报告同时返回 `effective_staircase_plan`、`effective_cache_plan` 或 `effective_soak_plan`。Staircase 不再接收顶层 `users/spawn_rate/duration`，必须使用专用 `staircase_plan`。

当 RPM 和 TPM 都大于 0 时，系统自动启用自适应请求长度，平均总 token 目标为 `TPM / RPM`。请求按 0.5x / 1.0x / 1.5x 三档以 25% / 50% / 25% 混合，使用真实 `usage` 校准输入估算和预期输出。Quick 与 Staircase 使用同一长度分布；Staircase 的子进程不做速率限流。上下文长度优先读取 `providers.<name>.models.context_windows.<model>`，缺失时回退到 131072，并按 95% 安全边界截断。静态 workload 同样会在启动时过滤估算后超过该安全边界的 profile，并在 Job 日志中列出跳过项。该模式仅支持 `throughput*` workload。

Staircase/Soak 的 warmup 默认使用 `throughput_rpm` 短请求，不使用超长上下文。请求级失败由最终阈值判定，不再直接终止 Locust 子进程；启动、配置等进程级错误仍会使 Job 失败。

吞吐 workload 预设：

- `throughput_rpm`：只使用短、中 prompt，优先提高 RPM。
- `throughput_balanced`：混合短、中、约 8k token 和 128k 字符上下文。
- `throughput_tpm`：提高长上下文权重，配置包含 128k/512k 字符及超长上下文；运行时会按当前模型上下文安全边界过滤超限项。
- `throughput_streaming`：三个近似等长的流式请求，统计成功请求的 TTFT 与 End-to-End Latency P50/P90/P95/P99。
- `throughput`：保留原有混合比例。

命令行仍可直接运行：

```bash
# 1. 功能冒烟，含 models、throughput profiles、当前模型安全 compatibility profiles、cache suite
python scripts/smoke_test.py

# 2. 独立 cache 观测，默认 observe
python scripts/run_cache.py

# 3. 阶梯并发，自动扩展到 business RPM 达标或 max_users
python scripts/run_staircase.py

# 4. 稳定性长跑
python scripts/run_soak.py
```

也可以用环境变量选择供应商和模型：

```bash
LOADTEST_PROVIDER=yibu LOADTEST_MODEL=glm-5.2 python scripts/param_test.py
LOADTEST_PROVIDER=yibu LOADTEST_MODEL=glm-5.2 LOADTEST_WORKLOAD=throughput locust -f locustfile.py --headless -u 10 -r 2 -t 2m
```

### 图像参数与分辨率测试

完整说明见[图片参数与分辨率测试手册](docs/image_param_test.md)。控制台第 2 个「图片（多模态）参数测试」
tab 从 `providers.<name>.image` 读取图片模型，复用 provider 密钥，并把报告写入
`reports/jobs/<job_id>/`；它仍不进入 Chat 参数矩阵。独立 CLI 保留为等价复现手段，
使用独立环境变量时密钥也不会进入命令行、请求计划或报告：

```bash
export IMAGE_TEST_API_KEY='<provider-key>'
# 或在命令中增加 --api-key-stdin，通过隐藏提示交互输入密钥

# GPT Image 2：1K 冒烟；resolution 会增加横竖尺寸、2K 与非法边界
python scripts/image_param_test.py \
  --base-url https://provider.example \
  --family gpt-image-2 --suite smoke
python scripts/image_param_test.py \
  --base-url https://provider.example \
  --family gpt-image-2 --suite resolution

# Banana：模板会展开成 nano-banana-pro-1k / 2k；交叉用例判断由别名还是 size 控制像素
python scripts/image_param_test.py \
  --base-url https://provider.example \
  --family banana --suite resolution \
  --model 'nano-banana-pro-{resolution_lower}'

# Grok Imagine：使用官方 aspect_ratio / resolution / n / response_format
python scripts/image_param_test.py \
  --base-url https://provider.example \
  --family grok-imagine --model grok-imagine-image \
  --suite resolution

# 固定 Banana 模型走 Chat 图片协议；New API 参数使用 extra_body.google.image_config
python scripts/image_param_test.py \
  --base-url https://provider.example/v1 \
  --family banana --transport chat-completions \
  --suite resolution --model gemini-3.1-flash-image \
  --no-cross-control

# full 强制要求 4K 确认；resolution 也可用 --include-4k 主动加入 4K
# 先 dry-run 可查看所有请求而不读 key、不联网
python scripts/image_param_test.py \
  --base-url https://provider.example \
  --family banana --suite full --include-4k --dry-run

# Grok full 使用独立 2K 费用确认，不发送 GPT 的 quality/output_format/size
python scripts/image_param_test.py \
  --base-url https://provider.example \
  --family grok-imagine --suite full --include-2k --dry-run
```

硬校验包括 HTTP 状态、输出数量、PNG/JPEG/WebP 结构、实际宽高与请求参数逐项对应。GPT Image 2 负向用例覆盖 16 像素整除、像素总量、最大边长和 1:3–3:1 宽高比。Banana 的 crossed probes 会让供应商 alias 的 1K/2K 与 `size` 故意冲突。Grok Imagine profile 只校验官方 `aspect_ratio`、`resolution=1k/2k`、`n` 与 URL/base64 delivery；方形分辨率校验精确像素，非方形校验官方宽高比并记录实际尺寸。完整 case 名称和 suite 展开数量见[用例矩阵](docs/image_param_test.md#4-suite-与用例矩阵)。

`postprocess_inference` 综合输出 token、延迟、每百万像素字节数与半尺寸重采样残差，只能输出 `unknown` / `suspected` / `strongly_suspected`，`confirmed` 永远为 `false`。黑盒结果不能单独证明供应商执行了超分；只有供应商阶段元数据或受控的原图/超分图对才能确认。具体评分阈值见[疑似超分判定](docs/image_param_test.md#8-疑似超分后处理判定)。报告和图像保存到 `reports/image_param/<timestamp>-<model>/`，文件含义见[报告 schema](docs/image_param_test.md#9-报告目录与-schema)，默认只保存 prompt SHA-256。

#### 2026-07-20 nicolessss.com 实测快照

该 OpenAI-compatible 供应商的 `gpt-image-2` resolution suite 共 8 项，仅 2 项通过。`1024x1536` 与 `2048x2048` 精确对应；`1024x1024` 被映射为 `1254x1254`，`1536x864` 被映射为 `1672x941`。4 个非法边界请求全部返回 HTTP 200 并生成图像，没有按 GPT Image 2 参数约束拒绝。多数输出落在约 157 万像素的固定像素桶，说明该代理会归一化尺寸，而不是稳定透传精确像素参数。

同一供应商的 `nano-banana-pro-1k` 单项冒烟返回严格 `1024x1024` PNG，通过基础对应检查。该结果只覆盖 1K，不能外推到 Banana 2K/4K。GPT Image 2 的 2K 响应字段和 token accounting 与其它尺寸明显不同，可以支持“按尺寸路由到不同生成管线”的判断，但仍不能确认是否存在后置超分。完整命令、prompt hash、报告目录和证据限制见[供应商快照](docs/image_param_test.md#12-2026-07-20-nicolesssscom-快照)。此处是时间点快照，不作为长期供应商排名；完整原图和 JSON 仅保存在本地 gitignored `reports/image_param/`。

#### 2026-07-20 api.xinyunai.cloud Banana 实测快照

该供应商列出的 `gemini-3.1-flash-image` 与 `gemini-3-pro-image` 都不能走
`/v1/images/generations`，但可通过 `/v1/chat/completions` 生成图片。使用
`extra_body.google.image_config` 后，两者 1K/2K 均严格返回 `1024x1024` / `2048x2048`；
把 `image_config` 错放在顶层会被静默忽略并返回 `1408x768`。Flash 未出现疑似后置超分信号，
Pro 得到 `suspected` 启发式结果但不能确认。完整数据、报告目录、未测试边界见
[供应商快照](docs/image_param_test.md#13-2026-07-20-apixinyunaicloud-banana-快照)。

Locust 原生 UI 只保留为调试用途：

```bash
LOADTEST_WORKLOAD=throughput locust -f locustfile.py
```

可选 workload：

```bash
LOADTEST_WORKLOAD=throughput     # 只跑 throughput_profiles，进入 500 RPM/soak 门禁
LOADTEST_WORKLOAD=throughput_streaming  # 等长流式请求，观测 TTFT / E2E 延迟百分位
LOADTEST_WORKLOAD=mixed_compat   # 跑 compatibility_profiles + control:list_models
LOADTEST_WORKLOAD=cache_suite    # 只加载 cache_profiles，正式 cache 观测优先用 scripts/run_cache.py
```

Locust Web UI 运行时仍提供一个兼容指标页面：

```text
http://127.0.0.1:8089/yibu
http://127.0.0.1:8089/yibu/summary
```

这里会显示业务 RPM、成功率、P95、旧 Locust cache 指标、profile 请求数和上游返回的模型名统计。
同页也会显示当前支持参数与 DeepSeek 官方 Chat Completion 参数的对比表。
正式 Cache Job 的 v8/v9/v10 客户指标不经过 `metrics.cache_min_prompt_tokens` 过滤；该 4000-token 配置只用于旧场景与历史兼容。

## 多供应商配置

公开配置在 `config.yaml`：

```yaml
active_provider: yibu

providers:
  yibu:
    label: "YibuAPI"
    base_url: "https://yibuapi.com/v1"
    api_key_env: "YIBU_API_KEY"
    backend: "openai_compatible"
    default_transport: "chat_completions"
    api_interfaces:
      chat_completions:
        path: "/chat/completions"
        auth: "bearer"
    models:
      default: "deepseek-v4-pro"
      candidates: ["deepseek-v4-flash", "deepseek-v4-pro", "glm-5.2"]
      families:
        deepseek-v4-pro: deepseek
        glm-5.2: glm
      context_windows:
        deepseek-v4-pro: 131072
```

本地私有配置在 `providers.local.yaml`：

```yaml
providers:
  other_provider:
    label: "Other Provider"
    base_url: "https://example.com/v1"
    api_key_env: "OTHER_PROVIDER_API_KEY"
    backend: "proxy_unknown"
    default_transport: "chat_completions"
    api_interfaces:
      chat_completions:
        path: "/chat/completions"
        auth: "bearer"
    models:
      default: "glm-5.2"
      candidates: ["glm-5.2"]
      families:
        glm-5.2: glm
      context_windows:
        glm-5.2: 131072
```

并在 `.env` 中设置 `OTHER_PROVIDER_API_KEY`。正常解析顺序是：绑定当前 provider 的子进程临时凭据 → provider 的 `api_key_env`。旧环境变量仅用于未声明 `api_key_env` 的 `yibu` 配置；旧版 `providers.local.yaml` 内联 `api_key` 只保留迁移兼容，优先级最低，而且加载时会从配置对象中剥离。

### 密钥边界

- 配置、Job spec、API 返回、日志和报告都不保存明文 key；公共配置只暴露 `api_key_env` 与 `has_key`。
- Web Job 启动子进程时不复制整个父进程环境，只传当前 provider 的一个临时 key，并通过 provider 标记绑定；子进程不会再次读取 `.env`。
- Client 只在配置的 HTTP(S) origin 上生成 `Authorization`、`x-api-key` 或 `x-goog-api-key`，且关闭自动重定向，避免鉴权头被带到其它 origin。
- profile 的附加请求 header 使用大小写不敏感白名单，目前只允许两项 Vertex 流量探测 header，不能覆盖鉴权、Host、Cookie 或 Content-Type。
- 响应与落盘边界进行递归脱敏；测试用 canary 会验证嵌套错误文本和 JSON 文件中不出现 key。

## 业务 RPM 口径

业务 RPM 只统计成功完成的 `POST /v1/chat/completions` 业务请求：

- request name 以 `chat:` 开头
- profile group 为 `throughput_profiles`
- 非 warmup
- 非 retry
- HTTP 成功且未触发失败 finish_reason

明确排除：

- `GET /v1/models`
- `control:*`
- `warmup:*`
- `compatibility_profiles`
- `cache_profiles`
- cache suite 与失败重试

每个 Locust run 会写：

- `request_records.jsonl`：逐请求明细
- `history.jsonl`：按 `metrics.history_interval_sec` 聚合的 RPM、TPM、成功率和延迟指标
- Locust CSV/HTML：Locust 自带报告
- `verdict.json`：阈值判定结果

## 参数覆盖

所有请求体都通过 `lib/deepseek_params.py` 生成。任务代码只选择 profile，不直接拼模型参数。构建器按真实模型家族过滤参数：DeepSeek / GLM / Qwen / Gemini / Claude / Claude Fable / GPT / Kimi / MiniMax / Grok 各自使用独立参数集；未知 family 返回 `unknown`，不能启动任务。运行时先按“任务显式 route → 模型默认 route → 唯一 route”确定上游规则，再只在当前 route 下按“任务显式 form → route 默认 form → 唯一 form”选择协议，最后映射到内部 transport；不会回退到 `dynamic_aggregator`。Client、Locust、Cache 与 Smoke 共用 `api_interfaces` 中的 URL、鉴权和 usage 解析约定；同一模型在不同 route 或 API Form 下生成独立 profile/result identity。

| 参数 | 覆盖方式 |
| --- | --- |
| `model` | `models.default` 或 profile 覆盖 |
| `messages` | profile 消息、prompt、fixture 或 prompt_key 生成 |
| `thinking` | throughput 默认 disabled，compatibility 覆盖 enabled/disabled |
| `reasoning_effort` | 按模型族保留官方语义：DeepSeek 使用 `high|max` 及别名映射；GLM-5.2 原样测试 `none|minimal|low|medium|high|xhigh|max`；Kimi K3 使用其专属 low/high/max 矩阵 |
| `max_tokens` | `deepseek_params.default` 或 profile 覆盖 |
| `response_format` | `json_output` profile |
| `stop` | `stop_sequences` profile |
| `stream` / `stream_options` | `basic_stream`、`stream_with_usage` |
| `temperature` / `top_p` | `sampling_non_thinking` |
| `tools` / `tool_choice` | `tool_calls` 使用 `required`；`tool_calls_thinking` 保持默认 auto，因为一步 API 当前拒绝 thinking + required |
| `logprobs` / `top_logprobs` | `logprobs` profile |
| `user_id` | cache profiles |

`throughput_profiles.long_context` 是长文本业务请求案例，使用 `fixtures/long_context.txt` 构造长 prompt，默认以 5% 权重混入 throughput 压测。

废弃参数 `frequency_penalty`、`presence_penalty` 默认剥离；若把 `deepseek_params.allow_deprecated` 改成 `true`，会保留发送并记录 warning，便于兼容性探测。

`scripts/param_test.py` 会按当前 `provider + model` 运行参数检测。`glm_openai_compat` 是 GLM-5.2 专属矩阵：覆盖 Thinking 开关、七档 `reasoning_effort`、`clear_thinking=false` 的保留式思考、Thinking+Tools 历史 `reasoning_content` 回传、`tool_stream`、JSON、`request_id` 与 `user_id`。旧 GLM 模型由各自 model profile 标记不支持的 effort 组合。

## 阈值配置

所有门禁都在 `config.yaml -> thresholds` 调整：

- `thresholds.smoke.success_rate_min`
- `thresholds.cache.mode`
- `thresholds.cache.cached_input_token_ratio_min`（启用 gate 时必须显式配置）
- `thresholds.cache.measurement_coverage_min`
- `thresholds.cache.positive_control_cached_ratio_min`
- `thresholds.cache.negative_control_cached_ratio_max`
- `thresholds.staircase.target_business_rpm_min`
- `thresholds.staircase.target_total_tpm_min`
- `thresholds.staircase.success_rate_min`
- `thresholds.staircase.p95_latency_max_ms`
- `thresholds.staircase.error_429_max_ratio`
- `thresholds.staircase.error_5xx_max_ratio`
- `thresholds.soak_1h.success_rate_min`
- `thresholds.soak_1h.p95_latency_max_ms`
- `thresholds.soak_1h.rpm_drift_max_ratio`

`p95_latency_max_ms: null` 表示只记录，不参与该项门禁。

阈值按 `Job plan.thresholds → Provider models.thresholds.<model>.<stage> → Provider thresholds.<stage> → Global thresholds.<stage>` 覆盖；Job 创建后把最终值写入对应 effective plan，Runner 不再重新解释实时配置。Cache 没有通用命中率默认值。

## Cache 指标

Cache suite 默认执行四个互相隔离的部分：

1. 客户场景：一组按轮次增长的 `progressive_customer_session` 会话。
2. Structure probe：客户会话结束后，用相同 system/工具定义和 1 字符 user 获取官方 prompt token 数，估算 seed 的静态前缀；不预热客户请求。
3. Positive control：每组使用隔离的长前缀，先冷请求、统一等待、再精确复用。
4. Negative control：从 system/user 输入首部开始随机化，用来发现代理缓存污染或 usage 误报。

上述正、负控制对 `progressive_customer_session`、`kilocode_agent_session`、`growing_conversation` 和 `shared_prefix` 都是强制项；`controls.mode: off` 或任一数量为 0 会在计划解析时被拒绝，默认值为 3 个 positive pair 与 3 个 negative request。每个 `request_records.jsonl` 行保存原始 usage 和 `cache_token_audit`，验证 `0 ≤ cached_tokens ≤ input_tokens`、hit/miss 算术、`cached_tokens ≤ structurally_reusable_tokens`，并校验正控 cold/warm 增长和负控上限。汇总字段 `cache_usage_accuracy_status/pass`、coverage、excess tokens 与 failure reasons 参与门禁；缺少官方字段返回 N/A，不用延迟反推 token。

默认四轮会话依次为 `seed → direct_growth → tool_initial + tool_followup → final_growth`。工具定义在会话内保持稳定，后续请求的消息历史必须严格扩展上一请求。工具调用缺失或格式错误时记录 `tool_flow_unsupported` 并停止该会话，不发送合成 follow-up，也不计为 cache miss。

v10 的三层核心指标：

```text
structural_hit_rate_ceiling
= Σ structurally_cacheable_prefix_tokens / Σ input_tokens

actual_cache_hit_rate
= Σ provider_cached_input_tokens / Σ input_tokens

cache_efficiency
= actual_cache_hit_rate / structural_hit_rate_ceiling
```

- seed 的结构前缀来自独立 structure probe；全部严格增长请求的结构前缀使用上一请求的官方 input tokens。结构探针失败或结构覆盖不完整时，上限和效率返回 N/A。
- `actual_cache_hit_rate` 是面向客户的主指标；`cached_input_token_ratio` 和兼容字段 `cache_hit_rate` 与它完全相等。
- `cache_efficiency` 回答“理论上能缓存的部分实际利用了多少”。若超过 100%，不截断并标记 `exceeds_structure`，用于发现结构估算偏差、usage 误报或代理污染。
- `progressive_prefix_reuse_rate` 继续统计严格增长请求内的前缀复用效率；`tool_followup_reuse_rate` 保留为工具阶段专项诊断。
- 结果按 `seed`、`direct_growth`、`tool_initial`、`tool_followup`、`final_growth` 分组，并报告会话完成率及工具能力覆盖。
- 缺少官方缓存 token 字段时返回 `N/A`；header 与延迟只作为旁证，绝不生成 token 命中率。
- 延迟提升只计算 positive control 冷/热配对的中位数。
- v9 progressive、v8 `customer_tool_flow` 和 v7/旧历史仍可读取，但会明确标记并与 v10 分离。`customer_tool_flow` 场景本身已移除（由 v11 `kilocode_agent_session` 替换），旧 v8 报告只读。

基础计划示例：

```yaml
cache_plan:
  scenario: progressive_customer_session
  sessions: 10
  rounds_per_session: 4
  content_profile: realistic       # small | realistic | large
  tool_stage: {enabled: true, round: 3}
  structure_probe: {enabled: true} # 自动且必须启用，不在客户/控制指标中
  controls: {mode: auto}           # 解析为 3 positive pairs + 3 negative
  wait_after_seed_sec: 5
  max_tokens: 128
  max_run_seconds: 1800
  consecutive_failure_limit: 3
  seed: 20260715
  evidence_mode: official_usage
```

高级自定义只需覆盖内容范围和控制数量：

```yaml
cache_plan:
  scenario: progressive_customer_session
  content_profile: custom
  content_ranges:
    user_chars: {min: 300, max: 1200}
    tool_result_chars: {min: 800, max: 3000}
  controls:
    mode: custom
    positive_long_prefix_pairs: 2
    negative_unique_prefix_requests: 2
```

最大计划请求数按 `sessions × (rounds_per_session + tool follow-up) + 1 个结构探针 + positive_pairs × 2 + negative_requests` 计算；实际工具不支持时不会补造缺失的 follow-up 请求。

判定优先级：

1. DeepSeek/OpenAI-compatible 官方 cache token 字段
2. Claude `cache_read_input_tokens` / `cache_creation_input_tokens`
3. Gemini `usageMetadata.cachedContentTokenCount`
4. 缺字段即 `N/A`；代理头和延迟仅保存在 evidence 中

`thresholds.cache.mode`：

- `observe`：默认，未达标也退出 0。
- `gate` / `hard_fail`：必须显式提供客户命中、测量覆盖率、正向控制和负向控制阈值，未达标退出 1。

无论 `mode` 是 `observe` 还是 `gate`，确认的 `cached_tokens` 算术矛盾、超过 input 或超过真实可复用前缀都会失败；observe 只放宽性能/命中阈值，不放宽 telemetry 真实性。

## 报告位置

```bash
ls reports/jobs/
cat reports/smoke/verdict.json
cat reports/cache/verdict.json
cat reports/staircase/verdict.json
cat reports/soak_1h/verdict.json
```

失败时重点看：

- `failures`
- `failure_classification_counts`
- `finish_reason_counts`
- `error_429_ratio`
- `error_5xx_ratio`
- `p95_latency_ms`
- `ttft_p95_ms`
- `cache_hit_rate`
- `cached_input_token_ratio`
- `structural_hit_rate_ceiling`
- `actual_cache_hit_rate`
- `cache_efficiency`
- `progressive_prefix_reuse_rate`
- `tool_followup_reuse_rate`
- `cache_hit_request_ratio`
- `cache_measurement_coverage`
- `session_completion_ratio`
- `tool_flow_supported_session_ratio`
- `cache_stage_metrics`

## 调参建议

- business RPM 不到 500：提高 `staircase.steps` 或开启/增大 `auto_extend`。
- 429 偏高：降低 users、降低 spawn_rate，或把 `max_tokens` 控短。
- P95 偏高：先用 throughput profiles 关 thinking、固定输出长度，再单独用 compatibility profiles 观察 thinking 开销。
- JSON 输出偶发空内容：smoke 对 `json_output` 内置一次重试，压测仍按失败率计入。
- Tool Calls `auto` 不稳定：smoke 默认使用 `tool_choice: required`。
- cache 波动：先检查测量覆盖率、正向控制与负向控制；保持 `observe`，稳定后再显式配置完整 gate。
