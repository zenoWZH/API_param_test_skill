# 参数矩阵与冻结功能计划

所有编程工具和 OpenClaw 都通过完整 skill 目录中的 `bin/llm-api-test` 调用相同引擎。
以下示例的 `SKILL_DIR` 必须指向实际安装目录；报告和计划写在 skill 外。

```bash
SKILL_DIR=/absolute/path/to/llm-api-test
bash "$SKILL_DIR/bin/llm-api-test" matrix list --source deepseek --model deepseek-flash
```

列表来自 bundled MPDB，不读取 API key、不发送请求。它保留 reference identity、binding
类别、用例数和 disabled 原因；有矩阵记录不代表存在通用可执行 binding。

## 普通文字、图片与固定功能测试

先离线生成一份 JobSpec v6：

```bash
bash "$SKILL_DIR/bin/llm-api-test" matrix preview \
  --provider deepseek_official --model deepseek-v4-pro \
  --api-form openai_responses --case deepseek0813_responses_basic \
  --output-dir /tmp/llm-parameter-review
```

未指定 `--case` 时选择该模型/接口的完整启用套件，默认 1 轮。文字子集使用重复的
`--case`；图片加 `--type image_param_test`，可选 `--suite smoke|resolution|full`。
固定 Banana 模型按原矩阵要求显式加 `--no-cross-control`；可展开分辨率的模型模板默认
保留交叉控制，CLI 不会静默减少所选完整套件。
固定来源功能通过 `--parameter-suite` 选择；其完整单轮合同不可被普通子集参数改写。
研究 workflow 通过精确的 `--workflow-id` / `--workflow-binding-id` 选择。

预览输出包含计划摘要、实际依赖闭包、总请求上限、独立清理预算和原始 JobSpec 路径。
多轮数量、工具协议校验模式、随机抽样及计数助手请求均进入冻结计划。预览没有凭据
读取或 HTTP 行为；参数不匹配在生成报告之前报错。

用户授权覆盖该计划的供应商、模型、范围和费用后，执行原文件：

```bash
bash "$SKILL_DIR/bin/llm-api-test" matrix run \
  --job-spec /tmp/llm-parameter-review/job_spec.json --yes
```

执行会产生真实 API 请求。`--yes` 记录对既有计划的确认，不授予目录中其他任务权限。
源码、参考绑定或冻结内容漂移会被拒绝；已经进入执行的批次不能重发业务请求。
需要再次运行时重新预览并使用新目录。

默认未指定输出目录时，计划放在外部 data 目录的 `reports/jobs/<id>/` 下，可用：

```bash
bash "$SKILL_DIR/bin/llm-api-test" jobs --id JOB_ID
bash "$SKILL_DIR/bin/llm-api-test" result --id JOB_ID
bash "$SKILL_DIR/bin/llm-api-test" jobs --stop JOB_ID
```

显式选择 data 目录之外的输出路径时直接读取该目录的报告；`jobs/result` 只枚举当前
data 目录。摘要保留用例/轮次、请求和清理结果，完整响应与图片留在报告文件中。

清理恢复必须使用原作业目录和原报告给出的真实 `run_id`：

```bash
bash "$SKILL_DIR/bin/llm-api-test" matrix run \
  --job-spec /tmp/llm-parameter-review/job_spec.json \
  --cleanup-only ORIGINAL_RUN_ID --yes
```

该流程仅清理账本中确认属于该轮的资源；不重发业务请求、不猜测丢失的资源 ID，
清理证据与原始业务结果分别保存。清理失败或未确定的创建状态阻止整体通过。

## 图像、视频和音频输入（文字输出接口）

新增 94 个 source/model/API 精确绑定的媒体输入 workflow；这类测试仍属于文字输出
接口的 `param_test`，不同于生成图片的 `image_param_test`。它们不会成为普通参数默认值。
先用 `matrix list` 找到对应 workflow，再显式选择：

```bash
bash "$SKILL_DIR/bin/llm-api-test" matrix preview \
  --provider openai_official --model gpt-4o \
  --workflow-id media-input/openai_official/gpt-4o/openai_chat_completions \
  --case image_png --output-dir /tmp/media-input-review
```

省略 `--case` 才选择该 workflow 的完整用例集合。负向用例会补齐基线依赖；识别基线
不成立时阻断对应负例。内联素材已随目录分发并按 SHA256 校验，运行时无需重新生成。
使用外部视频 URL 的用例会在模型请求前后各取回一次公开素材，核对字节数与 SHA256；
两次读取都计入冻结预算，前置校验失败不发送模型请求，内容变化时保留未验证结论。
只有绑定声明支持的图片/视频/音频输入形式才生成对应测试；unknown、unsupported 和已有禁用
权限不会通过 family 推断被开启。没有本地 provider 映射的条目需要自行配置准确执行路径。

识别正确、HTTP 接受、usage 算术和媒体精确 token 计数分别报告；缺少独立计数证明时
仍可能得到 inconclusive / 未验证结果，不能将其改写为完整参数认证或“模型不支持”。

## DeepSeek V4.1 Flash 专用研究矩阵

本轮同步的工作树快照包含 `deepseek-flash` → `deepseek-v4.1-flash` 的 5 种 API form、
61 个研究用例和 10 个 smoke 用例。普通参数接口与压力入口保持 disabled；不要借用旧
Flash/Pro 的 binding 或修改 overlay 来开启。专用入口只接受其固定官方来源与模型：

```bash
bash "$SKILL_DIR/bin/llm-api-test" deepseek-v41 \
  --suite smoke --output-dir /tmp/deepseek-v41-review
```

默认仅生成计划并保存请求与源码哈希。完整研究矩阵用 `--suite matrix`，精确子集用
重复 `--case`。在获得该范围授权后，用相同参数加 `--execute` 执行；脚本拒绝来源漂移
和重复进入的批次，串行、无自动重试。它不读取其他研究任务或扩大到其他供应商。

该专用 runner 的响应语义/身份/usage 检查不等于所有参数效果或精确 token 计数认证。
当前目录与矩阵登记不能替代本次真实请求证据。

## 判读与升级

普通功能参数 JobSpec 使用 v6，压力调度保留 v4，历史 v1–v5 按其原有证据边界读取。
参数 token audit 已为 schema v4；完成性、usage 算术、合理数量与精确计数是不同维度。
普通 Gemini Interactions 的禁用状态、专用研究范围和 AWS 网关/原生来源区别均保持。

数据库、consumer、模型函数和脚本需作为整体同步。准确来源、工作树例外、验证结果见
[本轮审查记录](../MATRIX_REVIEW_20260919.md)和 [MIGRATION_MANIFEST.json](../MIGRATION_MANIFEST.json)。
