# 参数测试说明

返回[测试总指南](testing_guide.md)。

参数测试验证的是一个精确组合：

```text
运行目标 + MPDB Profile + Interface + Reference Contract + Test Binding
```

它不只检查请求有没有返回 2xx，还检查参数实际效果、响应结构、工具调用、结构化输出、usage、returned-model identity 以及预期拒绝行为。

## 先确定测试对象

控制台选择顺序为：

```text
Provider → Model → Route Profile → API Form → Reference Contract
```

解析时先确定运行 Route，再列出允许的 API Form。随后必须在 MPDB 中唯一解析
`modality/source_id/family_id/model_slug` Profile、属于该 Profile 的 Interface、
source-scoped Reference Contract 和 Test Binding。任何身份或引用不匹配都应在任务
启动前报错，不能回退到家族级通用矩阵。

能力注册路径是：

```text
modality
└── source_id
    └── family_id
        └── model_slug                 # MPDB Profile
            └── interface_id           # API Form / routing mode
                └── contract_id
                    └── test_binding_id
```

MPDB Profile ID 只包含四级参考身份，例如：

```text
text/google_vertex/gemini/gemini-2.5-pro
```

API Form 只属于 Interface，例如上述 Profile 的
`text/google_vertex/gemini/gemini-2.5-pro#gemini-generate-content-default`。
运行 Route/Provider 是独立 execution target；不同 Source 或 Interface 的结果不得串用。
迁移前含 Route/API Form 的 `model_api_profile_id` 仅作为 legacy alias 或历史快照字段保留，
不是当前 MPDB Profile identity。

### Gemini API Form 与 Interactions 状态

App CLI 按当前模型/Route 的配置选择默认 API Form；测试原生 Gemini 时应显式指定
`LOADTEST_API_FORM=gemini_generate_content`，并使用同一 Source 的 Reference Contract。
root 的 Gemini 参数测试默认 resolver 与 App CLI 不完全相同。

Gemini 3.7 的普通文字 Interactions 入口当前为 `user_disabled_interactions`，
Interface 与 Binding 均不可执行。图片 `gemini-3.1-flash-image` 的 Interactions Binding
也为 `parameter_test_enabled=false`，对应 v1beta 仍标记 `live_unverified`。
显式指定 API Form 不能绕过门禁；历史实测和独立有界研究入口不授予普通任务执行权限。

![文字参数测试 Route-first 设置、矩阵、Token Audit 和身份审计界面示意](assets/ui/parameter-testing-console.svg)

图中编号对应：① Route-first 设置；② 官方参数与模型期望；③ 三轮参数矩阵；④ Token Audit；⑤ Model Identity Audit。示意值仅解释阅读顺序，不代表某个供应商的实测结果。

## 一个 Test Case 到底测什么

这里的旧 `profile` 名称表示 Test Binding 中的可复现 Test Case，不是 MPDB Profile。
例如：

- `basic_stream`：发送流式请求，校验 SSE chunk、结束标记和拼接文本。
- `stream_with_usage`：除流式结构外，还要求末块包含可用 usage。
- `json_output`：发送结构化输出参数，并确认内容能解析为 JSON。
- `tool_choice_required`：要求模型产生合法工具调用，不接受只返回普通文本。
- `stop_sequences`：确认停止序列实际控制结束位置，或按该模型合同被明确拒绝。
- `gemini_vertex_labels`：只用于 Vertex Route，验证 Vertex 专属字段和可观察指纹。

每个家族的所有现行 profile、请求设置、期望和响应检查都在[模型家族 Profile 手册](model_profiles/README.md)中逐项列出。

## 文字输出 allowance 硬下限

所有参数测试的**实际出站文字请求**都必须给模型至少 `256` 个 output tokens 的
allowance。这里约束的是请求允许的最大输出长度，不要求模型实际生成满 256 tokens。
下限由 `config.yaml` 的 `test_cases.minimum_output_tokens` 配置；省略时使用 256，配置为
非整数或小于 256 会在请求发往 Provider 前直接失败。可以按任务需要把该值调高，但不能调低。

执行器会在发送前抬高已有的 output-limit 字段，包括 `max_tokens`、
`max_completion_tokens`、`max_output_tokens`、`maxOutputTokens`，Gemini 的
`generationConfig.maxOutputTokens` / `generation_config.max_output_tokens`，以及 AWS 的
`inferenceConfig.maxTokens` / `body.max_tokens`；若当前文字 API Form 没有显式上限，则补入
该协议对应的字段。这条规则覆盖矩阵前的 identity probe、每个 profile 的 initial 请求、
真实 tool result follow-up，以及 `compatibility_profiles` 的 smoke 路径和有界的官方文字
smoke。矩阵结果中的 `minimum_output_tokens` 与 `output_token_limits` 记录本次实际生效值；
旧 profile 中保留的较小声明值不能当作出站请求值。

参数请求保持用例声明的 prompt、system 和消息历史，不追加数字填充、测试编号或默认测试
system。普通短回复保留 256 下限；JSON/工具用例默认预留 1024，推理用例默认预留 4096，
可由 `test_cases.output_token_budgets` 调高。文字协议中明确请求图片输出时默认预留 8192，
独立 Images API 仍不强加不存在的输出上限字段。有显式 thinking budget 时另留可见输出余量。
直接测试输出上限的 case 保留其被测值（至少 256）；遇到截断仍判失败，不通过重试更大上限
覆盖原失败。工具续轮保留原输入、模型输出与真实 tool result，成功结果也保存实际请求与响应。

这不是全局负载参数：Quick、Staircase、Soak 等压力流量和 Cache Suite 继续使用各自的
输出预算，不受该下限改写。image-only 接口如果没有 output-token 上限字段，只有该请求字段为
N/A；图片输入输出的 token 数量审计仍然必做。

## 期望与结果状态

| Profile 期望 | 正常结果 | 异常结果 |
|---|---|---|
| `supported` | HTTP 2xx，且结构和语义校验通过，记为 `pass` | 400/422 为 `incompatible`；2xx 但语义不成立也失败 |
| `unsupported` | 明确的 400/422，记为 `expected_rejection`，计入兼容通过 | 仍返回 2xx 为 `unexpected_acceptance`；伪装 5xx 不算正确拒绝 |

429、502、连接超时等应归为上游瞬时故障或可用性问题。重放只针对失败 profile，低频重试后再区分“稳定不兼容”和“瞬时失败”，不能用整套重跑掩盖首轮故障。

### 重复轮次如何合并

普通 profile 默认要求每轮通过。`kimi_k3_preserved_thinking` 使用已登记的
`run_success_mode: any`：同一 Provider、模型、Route、API Form 和合同下，一轮真实的
语义成功且 token 校验通过，可以满足其它轮的 `preserved_thinking_mismatch` 语义检查。
接口、协议、token 或模型身份错误仍按各自门禁判定；所有轮都失败时不会升级为通过。

被合并的失败行保存 `pre_aggregation_outcome`，并标记
`satisfied_by_sibling_run=true`；失败明细仍保留原始结果。应写成“重复轮次中有一次满足
preserved-thinking 语义”，不能把聚合后的 PASS 数写成每轮原始请求都成功。
具体用例见 [Kimi 家族手册](model_profiles/kimi.md)。

## 四层判定

### 1. 参数与响应语义

`compatibility_pass` 要求：

- 请求字段在当前 API Form 的正确位置。
- 应支持项得到 2xx，应拒绝项得到明确的客户端错误。
- 文本不是空壳；JSON 可解析且满足 schema。
- `n`/candidate count、stop、logprobs、reasoning 等返回值与请求相符。
- 工具场景产生合法调用，并完成真实 tool result follow-up。
- 流式响应可完整拼接，要求 usage 时末块确实存在。

只看“输入字段已接受”和 HTTP 状态不够。模型吞掉参数、固定采样值、返回 Markdown 围栏而不是 JSON、声明工具却没有 tool call，都应在语义校验中暴露。

Claude 4.5 原厂原生接口还可明确选择三次独立固定对照，验证 assistant prefill、stop 和 content 类型负例；
通用 30 项矩阵保持独立。模型、选择方式及判读边界见 [Claude 4.5 固定前缀验证](anthropic_prefill_reference.md)。

九款已审 Anthropic 模型还可选择[缓存固定对照](anthropic_cache_reference.md)：两个官方输入估算前置，
再做冷 / 重复 / 负例三次生成。每个任务只创建两枚新前缀 nonce 并冻结五个请求体；计数不合格时不生成，
计数响应不进入生成 token 审计。该套件的报告保留一个月。

### 2. Returned-model identity

矩阵前会先发送低成本 identity probe，随后每个 initial/follow-up/candidate 都作为身份样本：

- 默认要求响应顶层 `model` 或 Gemini `modelVersion` 与请求精确一致。
- 官方快照名、命名空间名等合法差异必须在 `identity_aliases` 显式登记。
- 任务内返回身份漂移会被汇总。
- `mismatch` 阻断；`suspicious` 告警；没有字段时为 `unverifiable`。

同时记录响应 ID 前缀、request ID、headers 和上游指纹用于溯源。字段一致仍不能 100% 证明物理上游，因为网关可以伪造字段；因此报告会保留这一限制。同理，`model_identity_pass=true` 但 status 为 `unverifiable` 只表示没有确认 mismatch，不等于身份已证实。

### 3. Token accuracy

Token 审计先统一 usage 为 input/output/answer/thinking/image/cached/total，再检查：

- identity、initial、follow-up 和每次重试都分别审计；HTTP 2xx 必须提供权威 input/output usage，非 2xx 若带 usage 或已生成输出也必须审计。只有未生成内容且没有 usage 的预期拒绝可免计数。
- 所有值非负。
- `input + output = total` 等算术关系成立。
- answer/thinking 等子项没有重复累计。
- cached tokens 没有超过 input 或可复用前缀。
- 有本地计数器时优先比较其计数；否则用可见语义内容估算。项目范围最多允许 2 倍波动，输入另留 32 tokens、输出另留 16 tokens 的协议余量；配置不能恢复旧的 32x/16x 范围。超出范围阻断，绝对请求输出上限仍生效。
- `output_completion` 要求每个 candidate/choice 都正常终止。`length`、`MAX_TOKENS`、`incomplete`、缺少终止证据、流中 error、终止后的额外数据均阻断；JSON/工具结果还必须通过各自结构校验。
- 发送前保存输入快照；发送或独立计数期间的请求变异会失败。多轮/媒体等不可见输入需要覆盖完整输入的独立计数，不能用 `partial` 冒充通过。

`token_validation_pass` 是 schema v4 的强制门禁：缺 usage、audit 被禁用/异常、算术错误、数量异常、未验证维度或输出未完成都阻断任务。旧 schema v3 PASS 不满足新规则，在历史结果中显示未验证。估算范围通过只说明数量合理，不产生 exact accuracy PASS；只有声明了对应协议完整模板的精确 tokenizer 才能给出精确比较。

独立 Provider count 接口标为 `official_count`，覆盖完整输入时允许 10% 加 8 tokens 浮动；这与精确模板计数分开。官方 Gemini GenerateContent 的 `token_count` 配置使用 `generateContentRequest`，包含 system 和 tools，不能只数用户文本。原厂文档也展示了 countTokens 与实际 prompt usage 存在少量差值的情况。[Google countTokens](https://ai.google.dev/api/tokens)

图像输出按明确绑定的官方模型、实际解码尺寸、质量与图像张数计算范围，允许 10% 或至少 8 tokens 的浮动。GPT Image 2 使用官方计算器，Gemini 使用型号各自的分辨率表；伴随文字和 thinking 分开核对，不能用 image 子项掩盖总数中的额外残差。无规则、无明确模型映射或输入媒体缺独立完整计数时记为未验证并阻断，不再把任意正数 output 视为通过。依据见 [OpenAI 图片计数](https://developers.openai.com/api/docs/guides/image-generation)、[Google 图片计数](https://ai.google.dev/gemini-api/docs/image-generation)和 [Flash Lite 图片计数](https://ai.google.dev/gemini-api/docs/pricing)。

这些检查能发现本地请求变异、异常 usage 和已暴露的输出截断。若网关同时伪造响应内容与 usage，单靠黑盒接口无法证明其物理上游完全没有额外上下文；报告保留计数来源与未验证范围。

### 4. Route 认证范围

- `raw_route_contract`：运行 Route 明确绑定到同一 MPDB Source 的 Reference Contract，测试可以判定该 Route 合同。
- `adapter_only`：动态聚合或物理上游未固定，只能说明当前适配器接受这些请求。

因此需要同时读 `adapter_pass` 和 `certified_route_contract_pass`。动态聚合结果全部通过时，后者仍应为 false。

## 运行方法

推荐从 Web 控制台运行，因为它会按 Route-first 顺序限制可选项并保存无密钥 Job Spec：

```bash
python scripts/web_console.py
```

直接 CLI 适合复现当前配置中的组合：

```bash
LOADTEST_PROVIDER='<provider>' \
LOADTEST_MODEL='<model>' \
LOADTEST_ROUTE_PROFILE='<route>' \
LOADTEST_API_FORM='<api-form>' \
LOADTEST_REFERENCE_SOURCE='<contract-id>' \
python scripts/param_test.py
```

这里的 `LOADTEST_REFERENCE_SOURCE` 接收 **Reference Contract ID**，例如
`gemini_native_generate_content`；不能填 `google_ai_studio` 等 MPDB Source ID。
App CLI 当前沿用这个旧变量名，不读取 root 的 `LOADTEST_REFERENCE_CONTRACT_ID`。
以下命令从 `app/` 执行，使用已安装 App 依赖的 Python。

默认执行所选精确模型/API 的完整启用套件，每个文字 profile 运行 1 轮。不要向脚本传 `--help`：该脚本是环境变量驱动的执行器，不是 argparse 命令。图片模型使用独立执行器，具体命令见[图片参数测试专项手册](image_param_test.md)。

### 复测次数、范围与独立报告

`LOADTEST_PARAM_TEST_RUNS` 设置普通矩阵重复次数（1–1000）。App 的 beta、FIM、prefill
和缓存固定套件默认一次，具体限制见 [参数任务执行控制](parameter_job_controls_20260909.md)。
App 普通参数 CLI 默认运行所选合同的完整矩阵；`LOADTEST_PARAM_PROFILES` 可指定逗号分隔
的 profile ID 子集，任何不属于当前精确合同的 ID 都会拒绝。固定套件保留完整单轮范围。

下面示例对一个合同运行一轮完整复测，会发送真实 API 请求；先根据家族手册确认用例数，
还需计入 identity probe、工具 follow-up 与可能配置的独立 token count 请求。
默认普通报告目录按 Provider/模型固定；已经开始执行的目录拒绝重复发送。
每次创建新报告目录，保留原结果：

```bash
mkdir -p reports/param_tests
param_recheck_dir="$(mktemp -d "$PWD/reports/param_tests/recheck-XXXXXX")"
LOADTEST_PROVIDER=gemini \
LOADTEST_MODEL=gemini-3.7-flash \
LOADTEST_ROUTE_PROFILE=google_ai_studio \
LOADTEST_API_FORM=gemini_generate_content \
LOADTEST_REFERENCE_SOURCE=gemini_3_7_flash_generate_content \
LOADTEST_PARAM_TEST_RUNS=1 \
LOADTEST_REPORT_DIR="$param_recheck_dir" \
python scripts/param_test.py
```

读取现有 `LOADTEST_JOB_SPEC` 时须保持冻结次数与模式一致；不要修改旧 JobSpec 来减少轮次。
独立输出目录只防止覆盖，不能替代对原合同和请求配置的核对。

## 结果阅读顺序

App 默认报告根目录为 `~/.config/llm-api-test/reports/`，可由
`LLM_API_TEST_REPORTS_DIR` 覆盖；显式 `LOADTEST_REPORT_DIR` 优先。先查看当前 Job
或该根目录下的 `param_tests/<provider>/<model>/`：

1. `verdict.json`：总体和四层门禁、来源元数据、失败分类。
2. `model_identity.json`：requested/returned/allowed identity、漂移和指纹证据。
3. 逐 profile 结果：每轮状态、HTTP、语义校验、usage、token audit。
4. 原始请求/响应摘要：只用于定位，敏感 header 会被脱敏。

报告建议写成：

```text
兼容性：18/18 profiles × 3 轮通过。
身份：所有 67 个 exchange 与请求模型精确一致；未发现任务内漂移。
Token：usage 算术 67/67 一致；独立 tokenizer 覆盖 0%，准确性结论为 partial。
Route：dynamic_aggregator，因此 adapter_pass=true，route contract 未认证。
```

不要只写“参数测试通过”。

## 文字与图片的差异

文字测试关注流式、JSON、stop、sampling、reasoning、工具、多轮和 usage。图片测试还必须：

- 解码每个输出，而不是只接受 URL/base64 字符串。
- 核对返回数量、实际格式和实际像素。
- 区分模型 alias 控制分辨率和 body 参数控制分辨率。
- 对非法尺寸、比例、数量或分辨率验证明确拒绝。

图片接口返回 HTTP 200 但图片无法解码、数量不符或像素不符，均为失败。

![图片参数测试设置和实际图片验收界面示意](assets/ui/image-parameter-console.svg)

图片界面先选择 Route、API Form 和 Suite，再显式确认可能产生额外费用的 2K/4K 或负向用例；结果表必须继续核对解码、数量、格式、实际像素、usage 和 identity。

## 新增模型或调整 Profile

1. 在 MPDB source catalog 的正确 `source_id/family_id` 下登记 canonical model 与四级 Profile。
2. 在该 Profile 下登记精确 Interface；API Form 只能出现在 Interface/Contract，不能写入 Profile identity。
3. 用同一 Source 的官方证据登记 Reference Contract，并在测试扩展中建立唯一 Test Binding。
4. 在 `config.yaml` 仅登记独立的 runtime provider/model/Route execution target，不把 Provider 冒充 Source。
5. 添加严格解析、请求构造、响应校验及 wrong-source/Interface/Contract/Binding 负向测试。
6. 重建并验证 MPDB 制品，再重新生成和检查家族手册：

```bash
python scripts/generate_test_docs.py
python scripts/generate_test_docs.py --check
```

## 模型家族手册

当前全部文字和图片家族见[模型家族 Profile 手册索引](model_profiles/README.md)。这些文件从 schema 生成，表格不应手工修改；如果说明不对，应修正 schema、生成逻辑或公共中文释义后重新生成。
