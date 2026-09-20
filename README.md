# llm-api-test

面向编程代理与 OpenClaw 的自包含 LLM 供应商测试 skill。它将 `api_pressure` 的 portable 测试引擎、模型档案数据库和脚本放在同一安装目录，提供参数矩阵、冻结功能计划、返回模型身份、token/cache、图片参数、压力测试和供应商准入的统一 CLI。

当前 skill 版本为 `2.1.0`，迁移优化与本地离线验收已完成。引擎同步到 `yibuapi-llm-loadtest@2df5ca4`，另按文件白名单纳入冻结于 `2026-09-20 08:02:38 UTC` 的公开矩阵工作树快照（DeepSeek V4.1、可选图像/视频/音频输入 workflow）；后者不代表源项目已提交或已发布。计划、差异与验收见[本轮审查记录](MATRIX_REVIEW_20260919.md)，旧迁移过程保存在 [MIGRATION_PLAN.md](MIGRATION_PLAN.md)。

## 已迁入的最新能力

- MPDB-only 模型事实源：`yibu-model-profile-db==0.3.0`、schema 3、catalog 0.4.0，带 JSON/YAML/SQLite/manifest、制品哈希校验和严格 consumer pin。
- 完整当前数据库；数量和摘要通过 `mpdb info` / `doctor` 查询，family、route、API form、transport 和 source 分开解析。
- 统一功能 runner 与 JobSpec v6，支持用例依赖、响应引用、冻结请求预算、多轮和自有资源清理。使用 `matrix list/preview/run`；详见[参数矩阵指南](references/parameter-matrix.md)。
- Fable 5/5.1、GPT Image 2.5、固定来源功能套件和 DeepSeek V4.1 专用研究矩阵；研究登记与普通参数/压力权限分别判断。
- 94 个显式选择的图像/视频/音频输入 workflow，配套合成及真实素材、格式和正反控制；不改变原有普通参数默认套件或压力权限。
- 固定吞吐压力计划：错峰启动、warmup/measurement/drain 窗口、attempt 守恒账本、8 个标准业务 profile、目标 RPM/错误率/返回模型身份/场景覆盖门槛。
- 多模型 sweep 的安全计划入口；先枚举并校验所有模型的 route/API form/MPDB binding、请求体和可压测状态，执行时强制填写模型、RPM、时长、users、spawn rate 与 `--yes`。同一 immutable MPDB snapshot 会传入预检、Locust 和报告，报告目录必须全新，非零 Locust 退出码不能被旧记录误判为通过。
- 图片 URL 防护：协议、userinfo、端口、DNS/IP、重定向、代理继承、大小、解码尺寸、内容类型和动画图检查；image/video 的 pressure policy fail closed。
- Web console 认证与私有文件原子写；默认仅监听 `127.0.0.1`，启动、隧道和密码显示均为显式操作。
- Codex/Claude Code/Kilo/Cursor/Windsurf/OpenClaw/通用 shell 共用 `bin/llm-api-test`，runtime 与数据均在 skill 目录外。

旧的 `app/api_reference_specs.yaml`、`app/model_capability_profiles.yaml`、`scripts/register_model.py` 已删除；准入流程不能再用 local YAML 绕过 MPDB 审核。

## 目录结构

```text
SKILL.md                         agent 操作与安全边界
agents/openai.yaml               Codex 展示与默认提示元数据
bin/llm-api-test                 所有工具共用的稳定入口
scripts/                         setup/doctor/job/workflow/console 包装器
app/                             vendored portable 测试引擎
packages/model-profile-db/       版本化模型档案包与制品
references/                      数据库、工具兼容、准入、测试与控制台文档
MIGRATION_MANIFEST.json          精确上游 commit/tree 与数据库锁
MIGRATION_PLAN.md                详细迁移计划、执行记录、验收与回滚
```

私有状态不写入上述目录：

| 用途 | 默认值 | 覆盖变量 |
| --- | --- | --- |
| 密钥、provider overlay、报告、workflow | `${XDG_CONFIG_HOME:-$HOME/.config}/llm-api-test` | `LLM_API_TEST_DATA_DIR` |
| 可重建 Python runtime 与 uv cache | `${XDG_CACHE_HOME:-$HOME/.cache}/llm-api-test` | `LLM_API_TEST_RUNTIME_DIR` |

## 快速开始

前置：Linux/WSL、`bash`、`python3` 和已审核安装的 `uv`。`setup` 不执行 `curl | sh`，也不会改写 skill checkout。

```bash
SKILL_DIR=/absolute/path/to/llm-api-test
bash "$SKILL_DIR/bin/llm-api-test" setup
bash "$SKILL_DIR/bin/llm-api-test" doctor --json
bash "$SKILL_DIR/bin/llm-api-test" providers
```

然后在 doctor 输出的 data 目录中编辑：

- `.env`：API key；不得放入聊天、argv、Git 或报告。
- `providers.local.yaml`：私有 provider、endpoint、模型与 route/API form 映射；不得覆盖模型能力事实。

`setup --from <old-data-dir>` 会在创建默认文件之前迁移旧 `.env`、provider overlay 和溯源 corpus。若运行在 container/OpenClaw sandbox/只读 home，显式设置并挂载 data 与 runtime 两个可写目录。

已有完整 uv cache 的隔离环境可加 `setup --offline`；需要本地 tiktoken/tokenizers 精确计数器时显式加 `--with-token-counters`。核心测试在缺少可用精确计数器时报告 N/A，不把字符估算伪装成 token accuracy PASS。

## 各工具安装位置

安装后的叶目录必须叫 `llm-api-test`，且必须复制整个目录，不能只复制 `SKILL.md`。

| 工具 | 推荐位置 | 明确调用 |
| --- | --- | --- |
| Codex | 项目/用户 `.agents/skills/llm-api-test` | `$llm-api-test` 或 `/skills` |
| Claude Code | `.claude/skills/llm-api-test` 或 `~/.claude/skills/llm-api-test` | `/llm-api-test` |
| Kilo | `.agents/skills/llm-api-test`、`.kilo/skills/...` 或 `~/.kilo/skills/...` | 明确说使用该 skill |
| Cursor | `.agents/skills/llm-api-test` 或 `.cursor/skills/...` | `/llm-api-test` / `@llm-api-test` |
| Windsurf | `.agents/skills/llm-api-test` 或 `.windsurf/skills/...` | `@llm-api-test` |
| OpenClaw | `<workspace>/skills/llm-api-test` 或 `<workspace>/.agents/skills/...` | 先 `skills check`，再让 agent 使用该 skill |
| 其他 shell agent | 任意完整只读目录 | `bash "$SKILL_DIR/bin/llm-api-test" ...` |

OpenClaw 推荐固定 revision 的 Git/local-directory workspace 安装：

```bash
openclaw skills install git:zenoWZH/API_param_test_skill@<tag-or-commit> --as llm-api-test
openclaw skills info llm-api-test --json
openclaw skills check --json
```

本包的 MPDB 有 3 个必要制品超过 OpenClaw managed bundle 的单文件上限，整体也超过其 managed upload 上限，所以当前支持 workspace Git/local-directory 安装，不宣称 managed ZIP/library upload 可用。Host 可发现也不等于 sandbox 已获得 Bash、可写 bind、依赖、网络和密钥；逐层配置与验收见 [多工具兼容文档](references/tool-compatibility.md)。

## 统一 CLI

```bash
# 帮助与离线检查
bash "$SKILL_DIR/bin/llm-api-test" --help
bash "$SKILL_DIR/bin/llm-api-test" doctor --json

# 数据库
bash "$SKILL_DIR/bin/llm-api-test" mpdb info
bash "$SKILL_DIR/bin/llm-api-test" mpdb verify-artifacts
bash "$SKILL_DIR/bin/llm-api-test" mpdb resolve openai gpt-5.3-codex \
  --modality text --api-form openai_responses --routing-mode vendor_direct

# 单项测试；真实流量前先获得明确批准
bash "$SKILL_DIR/bin/llm-api-test" run \
  --type param_test --provider <provider> --model <model>

# 离线枚举 sweep candidates
bash "$SKILL_DIR/bin/llm-api-test" sweep \
  --provider <provider> --models <model-1>,<model-2>

# 经批准后执行固定速率 sweep（当前逐模型顺序执行）
bash "$SKILL_DIR/bin/llm-api-test" sweep \
  --provider <provider> --models <model-1>,<model-2> \
  --target-rpm 100 --duration 10m --users 60 --spawn-rate 30 \
  --execute --yes

# 异步任务与结果
bash "$SKILL_DIR/bin/llm-api-test" jobs --running
bash "$SKILL_DIR/bin/llm-api-test" result --id <job-id>
bash "$SKILL_DIR/bin/llm-api-test" jobs --stop <job-id>
```

`providers` 中的 `selected_by_default` 只表示当前配置选择，
`credential_available` 只表示进程能够解析到非空凭据；两者都不是上游可用性证明。
`live_readiness` 在完成真实探针前固定为 `unknown_until_completed_probe`。

单任务类型：`param_test`、`cache_suite`、`image_param_test`、`quick_load`、`staircase`、`soak`、`trace_test`。模型 sweep 是单独命令，因为它需要先显示完整 candidates 与费率计划。

## 准入与数据库边界

```bash
bash "$SKILL_DIR/bin/llm-api-test" workflow start --provider <P> --model <M>
bash "$SKILL_DIR/bin/llm-api-test" workflow next --provider <P> --model <M>
bash "$SKILL_DIR/bin/llm-api-test" workflow onboard-propose --provider <P> --model <M>
```

`onboard-propose` 只生成 `llm-api-test.mpdb-review-proposal.v1` JSON。若 exact Source/Profile/Interface/Contract/Test Binding 不存在，必须在独立的 api_pressure 模型档案审核/发布流程中处理。批准后的新 MPDB 原子迁入本 skill 并通过 digest 验证后，`onboard-apply --review-ref <ref> --yes` 只验证 binding 并记录 workflow 完成，不写本地模型事实。

详见 [MPDB 文档](references/model-profile-database.md) 与 [准入流程](references/supplier-onboarding-workflow.md)。

## 可选 Web 控制台

```bash
bash "$SKILL_DIR/bin/llm-api-test" console start
bash "$SKILL_DIR/bin/llm-api-test" console status
bash "$SKILL_DIR/bin/llm-api-test" console stop
```

默认只监听 loopback 并启用认证。agent 不应把密码输出到对话；需要时由人在本地执行 `console passwd --reveal`。公网隧道不是安装步骤，只有在明确批准网络暴露边界后才启用，详见 [控制台文档](references/console-access.md)。

## 验收状态

本轮已通过独立 CLI 测试 39 项、MPDB 8 项制品校验、严格 doctor 和 skill 结构校验。
完整只读目录在任意 cwd、带空格路径中通过 doctor 与四类计划预览，安装文件哈希不变。
94 个媒体 workflow 中，91 个公开配置目标可离线编译，3 个需要私有 provider 映射。
App 全量 3945 个节点分 4 组验收：3578 passed、367 skipped、1279 subtests passed、0 failed。
跳过项为未分发的历史原始报告回放及 1 项源仓库专用重建测试，详见[审查记录](MATRIX_REVIEW_20260919.md)。

本轮没有发送真实 provider 请求或压力流量。OpenClaw 自身的发现、agent 和 sandbox
端到端仍需在对应安装环境验证；这里的只读 Bash CLI 验收不替代宿主实机验收。
完整目录超出本文记录的 managed bundle 限制，应采用 workspace Git/local-directory 方式。

## 文档地图

| 文档 | 内容 |
| --- | --- |
| [SKILL.md](SKILL.md) | agent 的操作流程、安全门和证据规则 |
| [MIGRATION_PLAN.md](MIGRATION_PLAN.md) | 项目差异、阶段计划、执行记录、验收与回滚 |
| [MATRIX_REVIEW_20260919.md](MATRIX_REVIEW_20260919.md) | 当前冻结源、增量优化与验收记录 |
| [references/parameter-matrix.md](references/parameter-matrix.md) | 矩阵列表、冻结执行、媒体输入与专用研究 |
| [references/model-profile-database.md](references/model-profile-database.md) | MPDB 查询、唯一事实源、升级与回滚 |
| [references/tool-compatibility.md](references/tool-compatibility.md) | Codex/Claude/Kilo/Cursor/Windsurf/OpenClaw 安装与分层验收 |
| [references/supplier-onboarding-workflow.md](references/supplier-onboarding-workflow.md) | 供应商准入状态机与 MPDB review 边界 |
| [references/testing-guide.md](references/testing-guide.md) | 各类测试结果如何判读 |
| [app/README.md](app/README.md) | vendored engine 的技术细节 |
