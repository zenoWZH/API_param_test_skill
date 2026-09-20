# GPT Image 2.5 参数矩阵与原厂验证

另建的[分辨率 × 质量参考矩阵](../../docs/gpt_grok_resolution_quality_reference.md)已完成两个型号的 Responses 各 108 项观察，并接入 MPDB。生成与编辑接口的组织验证限制仍单独保留；本页既有 310 项矩阵及其历史结论独立保存。完整范围见[新矩阵执行记录](../../docs/image_resolution_quality_execution_20260909.md)。

核对日期：2026-09-09（America/Los_Angeles）。矩阵共有 **310 个 source/model/API 绑定用例**：Sunburst、Flare 各 48 个生成、59 个编辑、48 个 Responses 图片工具用例。

## 当前判定与汇总修复

按用户于 2026-09-09 确认的口径，`size=auto` 成功接收并返回完成、可解码的图片即可；显式 `WIDTHxHEIGHT` 仍检查对应尺寸规则。两型号的 `input_fidelity=low/high` 改为要求明确拒绝，2xx 或与该字段无关的报错都不能通过。

[本轮零网络重判](../../references/gpt_image_25_parameter_policy_20260909.json)得到 **参数检查 96/96、图片输出 token 估算 62/62 通过**。附加已经完成的图像语义复核后，`overall_pass` 的动态结果为 **92/96**，仅四个遮罩样例因已确认的语义失败而不通过。缺失可选的图片后端 ID 或没有精确媒体输入计数，不再导致测试结果永久为 False。

此前普通 CLI 的 `summary.pass`、`overall_pass` 固定 False 是实现错误，混淆了结果和“尚未认证”的说明；现已改为按实际必需检查计算。有效正向请求可以返回通过，缺 usage、计数异常、错误主模型、缺失用例或已附加的真实语义失败会给出具体失败原因。仅用于记录参照的 candidate 模式仍单独使用 `diagnostic_pass`。

旧包、旧判定、旧事实及 310 个冻结用例定义原样保留；新政策通过显式 `expectation_policy` 和新 replay 记录生效。fidelity 用例沿用带 `_observation` 的历史 ID，但当前有效预期已经是 `rejection`。

## 官网应使用的参数

最新官方来源、请求内容类型和所有不确定项的决策依据已整理在[官方参数刷新记录](../../references/gpt_image_25_parameter_docs_refresh_20260909.md)。正常 Responses 请求省略 `input_fidelity`；官方关于“固定 high”的明确说明只针对旧 image-2，不能外推为 2.5 内部档位已获证实。

| 控制内容 | 官方列出的主要字段/值 |
|---|---|
| 图片模型 | Image API 的 `model`；Responses 的 `tools[].model` 选 Sunburst/Flare，顶层 `model` 仍为主模型 |
| 输出质量 | `quality`: low / medium / high / xhigh / max / auto |
| 尺寸 | `size`: auto 或合规 WIDTHxHEIGHT；自定义尺寸按最具体的接口规则检查 |
| 背景与编码 | `background`: auto / opaque / transparent；`output_format`: png / jpeg / webp；透明输出使用 PNG/WebP |
| 压缩与审核 | `output_compression`: 0–100，面向 JPEG/WebP；`moderation`: auto / low，须按具体接口和内容类型确认 |
| 流式 | `stream`；`partial_images`: 0–3，上限不代表至少交付该数量 |
| Image API | Generations 的 prompt / n / user；multipart Edits 的 image / mask；JSON Edits 使用 images 引用数组和 mask 引用对象 |
| Responses 编辑 | input 图片或 file ID；工具的 action=auto/generate/edit、input_image_mask；多轮使用 previous_response_id 或 image call ID |

JSON Edits 文档包含 moderation，而 Python 文件上传参数页未列出；这两种形态分开记录。PNG 压缩效果、mask 不同文档上限及型号页 streaming 描述差异均依具体接口官方资料处理，文档缺省不自动等于“不支持”。

## 历史执行结果

用户明确确认的原冻结包（原始证据归档路径 `reports/gpt_image_25_responses_reference_20260908/20260909T050000Z_d564f118/package.json`）已执行完毕：**96/96 用例、108/108 次 API 请求**，其中 96 次 Responses POST、6 次 Files 上传、6 次 DELETE。6 个上传文件均收到匹配 ID 的 `deleted=true`；没有重试或追加 API 请求。

| 型号 | HTTP 200 | HTTP 400 | 原冻结判定通过 | 按项目既定容忍复核后通过 |
|---|---:|---:|---:|---:|
| `gpt-image-2.5-sunburst` | 31 | 17 | 44/48 | 45/48 |
| `gpt-image-2.5-flare` | 31 | 17 | 45/48 | 45/48 |
| 合计 | 62 | 34 | 89/96 | 90/96 |

34 个 HTTP 400 中，30 个是与目标字段明确关联的预期负向拒绝；另外 4 个是 `input_fidelity=low/high` 的观察用例。这里的 90/96 是旧判定；auto 和 fidelity 的当前口径见上文。通过数表示自动协议、图片和计数检查；下文的遮罩语义失败独立记录，不能被自动检查通过覆盖。

[执行状态与预算](../../references/gpt_image_25_execution_status_20260908.json)、[完整实测事实](../../references/gpt_image_25_responses_matrix_facts_20260909.json)、[视觉复核](../../references/gpt_image_25_responses_matrix_visual_review_20260909.json)分别保存执行、原判/派生结果与图像内容证据。原始包及 case 记录保持不变。

## 新功能与已观察行为

- 两个精确型号及各自的 `-2026-09-08` 固定版本均有成功基线。Responses 顶层主模型为 `gpt-6-astra`，图片型号在 `tools[].model` 指定；工具配置回显不能冒充返回的图片后端身份。
- 两型号的 `low/medium/high/xhigh/max/auto` 均成功出图。1024×1024 的五个明确质量档位分别报告 **196、439、1756、3122、7024** image-output tokens，与新版官方计算器一致。
- 显式横竖尺寸、自定义尺寸、最小像素边界、3:1 比例边界和 3840×2160 实验性 4K 均验证了真实解码尺寸。
- 透明 PNG/WebP 核对了实际透明像素和可见内容；JPEG/WebP 的压缩选项、文件格式及相关负向参数均有独立记录。
- base64 图片输入、file ID、mask 参数、`previous_response_id` 与 image-generation-call ID 多轮均收到了完成的图片响应。图像内容是否完成编辑还需以下语义判定。

这些功能的文档来源见[官方研究记录](../../references/gpt_image_25_official_research_20260908.md)，包括 [Sunburst](https://developers.openai.com/api/docs/models/gpt-image-2.5-sunburst)、[Flare](https://developers.openai.com/api/docs/models/gpt-image-2.5-flare)、[图像指南](https://developers.openai.com/api/docs/guides/image-generation)与[Responses 图片工具指南](https://developers.openai.com/api/docs/guides/tools-image-generation)。没有创建裸 `gpt-image-2.5` 请求别名。

## 需要保留的异常与限制

### 遮罩编辑：四个样例语义失败

对两型号的 base64 编辑、两种多轮编辑和两种遮罩输入方式共 **10 个样例**做了实际看图复核：6 个普通改色/多轮样例完成蓝→红，并保留杯形和白背景；4 个遮罩样例出现黑色矩形遮挡杯体，未完成要求的编辑。

异常同时出现于 `input_image_mask.image_url` 和 `input_image_mask.file_id`，原因尚未确定。原始输入、遮罩、输出和 seed 的 SHA 均有记录；不归因为仅 base64 问题，也不宣称像素级锁定已经验证。示例：Sunburst 遮罩输出（原始证据归档路径 `reports/gpt_image_25_responses_reference_20260908/20260909T050000Z_d564f118/artifacts_025/image_00.png`）、Flare 遮罩输出（原始证据归档路径 `reports/gpt_image_25_responses_reference_20260908/20260909T050000Z_d564f118/artifacts_073/image_00.png`）。

### input_fidelity：两型号均明确拒绝

Responses 工具对 `low` 和 `high` 均返回 HTTP 400，错误码 `invalid_input_fidelity_model`，错误内容明确指出对应型号不支持该参数。因此本接口调用应省略 `input_fidelity`。这不证明默认内部 fidelity 档位，也不外推到未能访问的 Images Edits 端点。

### auto 尺寸：当前按成功接收与正常出图验收

Sunburst 返回并解码为 **1312×1199**，Flare 为 **1254×1254**，对应 image-output tokens 为 215 和 229。冻结校验器把自定义尺寸的 16 对齐约束用于 auto 输出，因此原判失败。

官方[图像指南](https://developers.openai.com/api/docs/guides/image-generation)把倍数、像素预算与最大边限制放在自定义 `WIDTHxHEIGHT` 段落；[API 参考](https://developers.openai.com/api/reference/python/resources/images/methods/generate)描述的是 requested size，没有明确保证 auto 返回值也满足 16 对齐。旧判定曾保留文档范围未决；当前按用户确认与官方 auto 自动选择语义验收，两个样例通过。原严格计算器不接受这两个尺寸的历史数量判定仍保留。

按用户确认的大致用量口径，共享估算器将这两张 auto 输出分别参考相邻有效网格
`1312×1200` 与 `1248×1248`，low 档名义用量为 215 和 228；实际 215 和 229 均在项目容差内。
估算只核对 token；auto 几何验收使用上文明确的新政策，显式尺寸检查没有放宽。

### 流式：完成性与预览交付分开

两型号 final-only 和带预览选项的流式请求均收到完整最终图片。Sunburst 在 `partial_images=2` 时实际收到 1 张预览；Flare 实际收到 0 张。该参数是上限，0 张不构成最少数量违约，但没有证明本次 Flare 预览图交付。

Sunburst 流式样本的 image-output 为 **273**，公开公式加一张预览的名义比较值为 **296**。原专用校验器使用严格相等而判失败；项目原有规则是 10% 或至少 8 tokens，此样本允许偏差 30，实际偏差 23（约 7.77%）。已修正专用校验器以引用共享容忍常量，并通过零网络 replay 派生通过结果。原始失败记录和 exact-match=false 均保留；不据此推导真实单张预览价格。

## token、完成性与认证范围

用户于 2026-09-09 明确了[图片 token 估算口径](../../docs/image_token_estimation_policy.md)：纯文字输入
沿用文字模型的参考方法，图片输出按官网对应尺寸、质量的大致用量判断，不要求精确等值。
以下实测数值和历史判定保留；新的估算通过不代表完整媒体输入、图片后端身份或编辑语义通过。

2.5 使用[新版官方计算器](https://developers.openai.com/_astro/GptImageTokenCalculator.react.yz8GdjDh.js)的独立质量表 16/24/48/64/96；源码哈希、公式与离线向量见官方研究记录和 `lib/gpt_image_25_tokens.py`。不能借用旧 image-2 的 medium/high 表。

本次 62 个出图结果均有 mainline usage 与独立 `tool_usage.image_gen`，两套算术分别成立。此前严格尺寸匹配下，60 个出图结果的图像输出数量通过既定容忍检查，两个 auto 尺寸未验证。按用户新口径的零网络重审（原始证据归档路径 `reports/gpt_image_25_responses_reference_20260908/20260909T050000Z_d564f118/replay_88ffceedce5b.json`），**62/62 个出图结果均通过官网参考值的数量估算**；报告明确 `validation_scope=official_image_token_estimate`、`exact_output_count_verified=false`。该次重审的历史参数判定为 90/96；本轮再按明确的 auto/fidelity 政策重判为 96/96，遮罩语义失败仍单独阻断整体结果，原始报告未修改。

完整输入 token 数量、物理图片后端身份及未审查的图像语义仍有证据范围限制，这些说明不再被无条件写成失败。所有图片策略保持压力测试禁用；当前参数检查通过，已确认的四个遮罩效果失败有独立原因和图像证据。

## Images API 与历史基线

此前使用同一项目官方凭据，Images Generations、Images Edits 对两型号都返回要求 Organization Verification 的 403。本轮仅执行已确认的 Responses 冻结包，没有新增 Images 请求；该历史访问限制未被解除或重新验证。详见[三接口基线](../../references/gpt_image_25_interface_access_20260908.json)。

此前的两条 low 基线、独立 token 复核和视觉证据仍分别保留于[基线事实](../../references/gpt_image_25_responses_facts_20260908.json)与[基线视觉记录](../../references/gpt_image_25_visual_baselines_20260908.json)。当前完整包的结果不覆盖它们。

## 复现与离线核验

本页命令从仓库根目录执行；若当前位于 `app/`，先运行 `cd ..`。公开事实位于根目录 `references/`。

### 干净克隆：只验证公开事实与参数定义

`references/gpt_image_25*.json` 是随代码交付的脱敏事实。它们保留原请求、图片、
冻结包和派生报告的路径与哈希；`reports/` 下的原始响应和图片不随 Git 克隆交付，
需要维护者另行提供完整原始证据归档。公开事实可以验证其固定哈希、模型/接口绑定、
参数定义与判定层之间的一致性，不能在没有原始图片时重新完成视觉核验。

按项目说明建立 `.venv` 并安装 `requirements-image.txt` 与测试依赖后，以下命令
不读取原始 `reports/`、不访问 API：

```bash
.venv/bin/python -c 'from scripts.gpt_image_25_catalog_addition import load_interface_baselines, load_responses_baseline_facts; from scripts.gpt_image_25_responses_matrix_observations import load_matrix_facts; from scripts.gpt_image_25_parameter_policy_addition import load_parameter_policy; load_interface_baselines(); load_responses_baseline_facts(); load_matrix_facts(); load_parameter_policy(); print("Public facts validated")'
.venv/bin/python -m pytest -q tests/test_gpt_image_25_catalog.py tests/test_gpt_image_25_matrix_observations.py tests/test_gpt_image_25_parameter_policy_addition.py
```

### 持有原始证据归档：重新解码与重放

历史批次重放需要完整原始证据。若要求用
`scripts/build_gpt_image_25_responses_matrix_facts.py --check` 逐字节重建旧 facts，
还必须按历史哈希恢复当时的源码与依赖，并保留原始记录中的绝对路径环境。
仅将报告归档恢复到相对目录并不足够：历史记录包含原机器路径，旧 facts 也记录了
当时校验器源码的 SHA-256。当前隔离候选的必要依赖实现不承诺与该旧源码逐字节相同。

普通干净克隆使用上文公开 facts-only 校验；它不需要原始报告或原机器路径。
不要为通过历史字节复建而改写旧 facts、原始记录或哈希，也不要为此混入无关源码。

本次完整包额度已经用完，不应再次发送它的已完成请求。原始汇总与派生 replay 都在该批次目录内。离线重放不会发出 API 请求：

```bash
.venv/bin/python scripts/run_gpt_image_25_responses_reference.py \
  --replay --batch reports/gpt_image_25_responses_reference_20260908/20260909T050000Z_d564f118
```

查看当前矩阵而不发送请求：

```bash
.venv/bin/python scripts/image_param_test.py \
  --provider openai_official --base-url https://api.openai.com/v1 \
  --model gpt-image-2.5-sunburst --route-profile vendor_direct \
  --transport openai-responses-image --suite full --include-4k --dry-run
```

模型可换为 Flare，transport 可换为 `images-generations` 或 `images-edits`。Web 预览与 CLI 使用同一矩阵工厂和 MPDB 快照；具名用例的参数不被全局质量/格式默认值覆盖。

原矩阵实现的[离线验收](../../references/gpt_image_25_offline_validation_20260908.json)与[范围核验](../../references/gpt_image_25_scope_verification_20260908.json)保留。全库旧 provenance 日期断言和其他模型作者观察的 replay 差异不属于本次实测通过声明；本次 token 校验修复、事实构建、source 观察及计划绑定检查合计 **51 passed / 33 subtests**；facts 离线重建检查、制品 manifest 和本模型 source replay 均通过。详见[本次验证记录](../../references/gpt_image_25_matrix_validation_20260909.json)与[8 个对应记录的安装审计](../../references/gpt_image_25_matrix_installation_20260909.json)。

本轮政策、汇总及文档修复验证：[66 passed / 58 subtests](../../references/gpt_image_25_parameter_policy_validation_20260909.json)，另含候选矩阵回归的组合检查为 89 passed / 52 subtests。root/app 运行代码一致，制品 manifest 与 GPT Image 2.5 source replay 一致；未新增图片 API 调用。
