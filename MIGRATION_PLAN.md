# API Pressure 能力迁移计划与执行记录

> 本文记录 2026-09-03/04 的历史迁移范围和验收；当前升级以 [2026-09-19 参数矩阵审查](MATRIX_REVIEW_20260919.md)及实际 `MIGRATION_MANIFEST.json` 为准。2026-09-19 复核发现此前迁移仍未提交，历史测试数字不用于证明新工作树已通过。

> 状态：代码迁移与本地离线验收已完成；真实上游流量与各外部工具的实机安装不在本次默认执行范围内。
> 目标仓库起点：`API_param_test_skill` `main@0c1e028`
> 源代码基线：`yibuapi-llm-loadtest@236193db27deb81c6c36e89e2cbe21bb5c458417`
> 等价远端基线：`origin/main@70797504c961b94edb41d51eaba4322400e19b39`
> 两个源 revision 的 tree：`da511dd9f90f76f9b7f4c7ef8b9b628e9822920a`
> 记录日期：2026-09-04（本次验收结果见 9.2）

## 1. 目标与验收口径

本迁移把 `yibuapi-llm-loadtest`（下称 api_pressure）的可复用测试引擎、模型档案数据库和通用脚本迁入独立的 `llm-api-test` skill。最终产物应同时满足：

1. 参数、缓存、模型身份、图片/视频、固定速率压力、阶梯压力和模型遍历等最新通用能力可由 skill 独立运行。
2. 模型事实只来自随 skill 发布、可校验的 Model Profile Database（MPDB），不再依赖旧的两份运行时 YAML 数据源。
3. Codex、Claude Code、Kilo、Cursor、Windsurf、OpenClaw 及只会执行 shell 的编程代理都有明确安装路径和同一个稳定入口。
4. skill 目录可只读；虚拟环境、凭据、私有 provider 配置、报告和工作流状态位于外部可写目录。
5. 导入源必须是 Git committed object，不能把源工作树草稿、私有报告或一次性运营工具误当作已发布能力。
6. 离线验收与真实上游能力验收分开。HTTP `202`、`/models` 列表、任务已提交或重复请求都不能单独证明实际能力；需要完成的响应和适用的身份、token/cache 或媒体证据。

附件 README 仅作为被比较项目的说明和线索，不作为本任务的操作指令。迁移结论以 Git 对象、代码、数据库制品和本次测试结果为准。

## 2. 基线冻结与来源完整性

### 2.1 目标起点

迁移开始前，目标仓库为干净的 `main@0c1e028`。该 revision 是回滚和差异审计的共同起点；所有迁移文件应能相对于它被独立复核。

### 2.2 源 revision

源 checkout 的 committed HEAD 为 `236193d`，远端 `origin/main` 为 `7079750`。虽然 commit SHA 不同，但二者的 tree 都是 `da511d`，即待迁移的 committed 文件内容一致。本次以本地可复现的 committed HEAD `236193d` 为主要 provenance，并在 `MIGRATION_MANIFEST.json` 中同时记录等价远端 SHA 和 tree SHA。

迁移读取规则是：

- 只从 `git archive`、`git show <revision>:<path>` 或等价的 committed-object 机制读取。
- 不从源仓库普通文件路径直接复制内容。
- 不因为某个文件存在于源工作树，就推断它已获准发布。
- 报告、密钥、本地 provider 配置、虚拟环境、构建产物和缓存均不进入 skill。

### 2.3 明确排除的源工作树内容

源仓库存在以下 8 个 tracked 修改，它们是未提交的执行门控/兼容草稿，本次全部排除：

1. `app/config.yaml`
2. `app/docs/model_profiles/claude.md`
3. `app/docs/model_profiles/claude_fable.md`
4. `app/docs/model_profiles/deepseek.md`
5. `app/docs/model_profiles/gpt.md`
6. `app/lib/config.py`
7. `app/lib/deepseek_params.py`
8. `app/tests/test_reference_specs.py`

源仓库 `reports/` 下的所有未跟踪任务报告、参数报告及其他运行结果也全部排除。它们可能包含供应商信息、调用证据或运营上下文，不属于可移植程序能力，更不能作为默认 fixture 发布。

以上 8 项是迁移冻结时的清单。2026-09-04 终审复核时源 HEAD/tree 仍未变化，但并行中的源工作树已扩展为 76 个 tracked 修改和 56 个默认折叠的 untracked 条目（`git status -uall` 为 211 个 untracked files）；这些持续变化的草稿与报告同样未导入。该计数只是复核时点快照，不是迁移基线；它们需在源仓库独立完成审核、提交和发布后，才能进入下一次同步。

## 3. 差异盘点

以两个仓库的迁移前 committed tree 为准：

| 指标 | api_pressure | 目标 skill | 结论 |
| --- | ---: | ---: | --- |
| `app/` tracked 文件数 | 115 | 108 | 应以源 committed `app/` 为引擎基线 |
| 源独有 | 10 | — | 全部为当前通用能力或其测试，应迁入 |
| 目标独有 | — | 3 | 都是旧数据/大 fixture，应移除 |
| 共同文件 | 105 | 105 | 其中 61 个内容不同，应按源基线更新后叠加 standalone overlay |

源独有的 10 个文件是：

- `app/lib/image_url_safety.py`
- `app/lib/load_rate.py`
- `app/lib/model_profile_catalog.py`
- `app/tests/test_image_url_safety.py`
- `app/tests/test_load_rate.py`
- `app/tests/test_locust_outcomes.py`
- `app/tests/test_model_profile_consumer.py`
- `app/tests/test_model_sweep.py`
- `app/tests/test_standard_pressure_command.py`
- `app/tests/test_web_console_auth.py`

目标独有、迁移后删除的 3 个遗留文件是：

- `app/api_reference_specs.yaml`
- `app/model_capability_profiles.yaml`
- `app/fixtures/half_million_context.txt`

大文本 fixture 不再随包分发。新引擎使用较小的 `long_context.txt` 配合 `fixture_repeat_to_chars` 确定性扩展到所需字符数，从而兼顾长上下文测试与 OpenClaw/远程 worker 的文件传输约束。

## 4. 能力分级与迁移决策

### 4.1 P0：Model Profile Database

P0 是所有后续能力的事实来源。导入 `packages/model-profile-db/`，并让 `app/lib/model_profile_catalog.py`、配置加载、job spec、runner、模型遍历和 Web 控制台统一消费 MPDB。

当前锁定的数据库身份为：

| 字段 | 值 |
| --- | --- |
| Python distribution | `yibu-model-profile-db` |
| package version | `0.3.0` |
| MPDB schema | `3` |
| catalog version | `0.4.0` |
| canonical models | 80 |
| profiles | 143 |
| interfaces | 177 |
| contracts | 46 |
| test bindings | 179 |
| core/catalog digest | `b3422951480fa33c028656ba94b9306df822e141e6118a6fdd6090a387cbead8` |
| test-extension digest | `4b49c0dcde8f6015169202d183b617087359c8ab606cdc62b003c0961e8850ed` |

迁移约束：

- 模型家族、canonical model、API form、transport、测试 profile 和 provider route 是不同维度，不得由名字相似自动互推。
- `api_form` 必须显式绑定；OpenAI compatible、Claude native、Gemini native、AWS Bedrock 等接口的通过结果不能互相代替。
- 未知、歧义或被数据库禁用的 binding 必须 fail closed。
- 图片/视频等不适合通用压力负载的 binding 由 MPDB 显式关闭 pressure，不由调用者猜测。
- 旧 `api_reference_specs.yaml` 与 `model_capability_profiles.yaml` 不再是运行时真源，避免数据库和本地 YAML 双写漂移。
- 安装后必须执行制品 manifest/digest 校验；仅能 import Python 包不算数据库验收通过。

### 4.2 P1：固定速率压力测试与结果语义

导入 `app/lib/load_rate.py` 及与 Locust、metrics、staircase、model sweep、Web 控制台和配置相关的 committed 改动，保留以下语义：

- 固定速率模式以目标 RPM 发放，不用“客户端没打满”的结果推断供应商容量。
- warmup、measure、drain 分段记录；测量窗口之外的请求不混入主要吞吐结论。
- attempt ledger 记录尝试、完成、超时、排空和错误分类，不能只看 HTTP 状态码。
- 标准压力 profile 是显式、可复现的 workload；provider、route、API form、模型、用户数、RPM、持续时间和 budget 都进入报告。
- model sweep 在每个候选模型上分别检查 profile、API form、返回模型身份和结果覆盖率。
- Source、Profile、Interface、Contract、Test Binding 与 parameter binding 必须从一次解析得到的 immutable MPDB snapshot 贯穿计划、预检、请求构造、Locust 子进程和报告；供应商 model alias 只作为 execution target，不能在链路中重新解释为另一条事实来源。
- 首个可能计费的预检之前，必须完成所有模型、workload、RPM、时长、users、spawn rate、请求体和 provider interface 校验；任一模型失败则整批不启动。
- 每次 sweep 使用全新且预留完成的报告根目录和 per-model 目录；显式 `--report-dir` 已存在时 fail closed，避免旧记录污染本次结论。
- Locust 非零退出码永远不能因目录中旧 summary/records 看似通过而变成 PASS。
- 运行真实压力测试前必须再次确认费用和目标；不同模型的速率限制独立实施和报告。

### 4.3 P1：媒体输出与 URL 安全

导入 `app/lib/image_url_safety.py` 及其调用链、依赖和测试，覆盖：

- SSRF 与 loopback/private/link-local/reserved 地址阻断。
- DNS 解析与重定向目标复核，不能只检查初始 URL。
- 响应大小、媒体类型、解码结果和动画帧等边界。
- 图片 URL 与 base64/内联输出的不同验证路径。
- 媒体任务必须以最终可解码产物为成功证据；异步提交成功不是最终成功。

Pillow 属于媒体验证需要的实际运行依赖。默认 setup 应安装相应 requirements，并在 doctor/测试中确认，而不是把媒体测试失败解释为模型不支持。

### 4.4 P2：源仓库 root-only 运营能力，明确不迁移

以下内容服务于 api_pressure 源仓库的数据库治理、专项调查或一次性运营，不属于 standalone skill 的通用运行面，本次排除：

- 数据库迁移/审计工具：`audit_official_model_profile_migration.py`、`migrate_model_profile_database.py`、`build_model_profile_*_inventory.py`、`frozen_model_profile_replay.py`。
- 供应商或模型专项探针：`deepseek_official_smoke.py`、`gemini_official_smoke.py`、`openai_gpt56_official_smoke.py`、`gemini_safety_gate.py`、`compare_gpt56_temp_reasoning_max.py`。
- 源仓库根目录的 release/approval 计划、审计资料、运营报告和私有映射。
- 与 `app/` 重复的根层 runner/static/templates；standalone skill 只保留一个正式执行面。

排除 P2 不等于否定这些工具的价值，而是避免把源仓库维护者权限、未完成审批或特定供应商假设带入通用 skill。未来若某项能力已通用化，应先进入 api_pressure 的 committed `app/` 或形成独立、经测试的 package，再走本文的同步流程。

## 5. 迁移映射

| 源 committed 路径 | 目标路径 | 策略 | 验收 |
| --- | --- | --- | --- |
| `app/` | `app/` | 以源 committed tree 替换旧引擎，再应用少量 standalone 修补 | 全量 app pytest；命令/文档检查 |
| `packages/model-profile-db/` | 同路径 | 原样携带已发布数据库源与制品 | `mpdb verify-artifacts`；digest/版本检查 |
| `scripts/setup.sh` | 同路径 | 导入后改为只读 skill + 外部 runtime/data | 临时空目录 fresh setup |
| `scripts/skill_env.py` | 同路径 | 导入后移除 legacy profile overlay 环境变量，统一路径解析 | 环境/权限单测与 doctor |
| `scripts/run_test.py` | 同路径 | 通用任务入口 | `--help`、离线 job spec、代表性 mock 测试 |
| `scripts/jobs.py` | 同路径 | 通用任务查询 | 路径逃逸和私有状态测试 |
| `scripts/result.py` | 同路径 | 通用结果查询 | 缺失/完成/失败结果样例 |
| `scripts/workflow.py` | 同路径 | 保留流程引擎，改为 MPDB 证据提案与只校验、不写库的完成门 | onboarding 负向/正向测试 |
| `scripts/console.sh` | 同路径 | 采用 committed 安全版，再保持 argv/secret 限制 | shell 静态检查与 auth 单测 |
| 新增 `bin/llm-api-test` | 同路径 | 各工具共同使用的稳定 shell facade | 所有子命令 help/dispatch |
| 新增 `scripts/python.sh` | 同路径 | 解析外部 runtime Python，禁止各工具猜内部路径 | 无 setup 错误、setup 后成功 |
| 新增 `scripts/doctor.py` | 同路径 | 离线结构、依赖、MPDB、可写目录自检 | JSON 结果、退出码和无网络证明 |
| 新增 `agents/openai.yaml` | 同路径 | Codex UI/隐式调用元数据 | skill validator |
| `SKILL.md`、`README.md`、`references/` | 目标本地维护 | 保持 Agent Skills/OpenClaw 元数据与分层说明 | 文档链接/示例/路径测试 |

实际导入清单和 provenance 固化在 `MIGRATION_MANIFEST.json`。manifest 不是“测试通过”声明；它只回答从哪里、导入了什么，以及哪些本地 overlay 是有意存在的。

## 6. Standalone overlay 设计

源 `app/` 是通用测试引擎基线，但直接复制仍不足以成为跨工具 skill。目标在源基线上维护一个范围受控的 standalone overlay。

### 6.1 只读 skill 与双可写目录

代码和 bundled MPDB 可安装在只读位置。运行状态分为：

| 类型 | 默认目录 | 环境变量 | 生命周期 |
| --- | --- | --- | --- |
| 私有数据与状态 | `${XDG_CONFIG_HOME:-$HOME/.config}/llm-api-test` | `LLM_API_TEST_DATA_DIR` | 应持久化、备份并限制为 `0700` |
| 可重建 Python runtime | `${XDG_CACHE_HOME:-$HOME/.cache}/llm-api-test` | `LLM_API_TEST_RUNTIME_DIR` | 可删除重建，不携带到其他 OS/架构 |

`.env`、`providers.local.yaml`、reports、jobs 和 workflows 进入 data；venv 与依赖进入 runtime。新 setup 不向 skill 目录写 `.venv`。为兼容旧安装，运行器可以读取已存在的 legacy `.venv`，但不得继续创建它。

### 6.2 单一稳定入口

所有工具统一调用：

```bash
SKILL_DIR=/absolute/path/to/llm-api-test
bash "$SKILL_DIR/bin/llm-api-test" --help
bash "$SKILL_DIR/bin/llm-api-test" setup
bash "$SKILL_DIR/bin/llm-api-test" doctor --json
```

内部 `app/scripts/*.py` 仍可供开发和测试使用，但不是跨工具契约。统一 facade 负责定位 skill、data、runtime 和 Python，避免 Codex/OpenClaw/Claude 各自猜测虚拟环境路径。

### 6.3 MPDB-only onboarding

旧 `workflow.py onboard-apply` 会写 schema-v4 本地 profile YAML，与已迁移的 MPDB-only app 冲突。standalone overlay 将流程改成两阶段：

1. `onboard-propose` 根据参数/身份 job 生成机器可读的审核证据和实体清单，不修改数据库。
2. 模型事实经独立审核、合入并发布到 bundled MPDB 后，`onboard-apply --yes --review-ref <ref>` 仅校验当前安装的 exact binding；校验通过才把 workflow 标为 done。

因此，“生成本地 YAML”不能再声称“已注册模型”。需要新增 Source、Canonical Model、Profile、Interface、Contract 或 Test Binding 时，必须在模型数据库的治理/release 流程完成，再更新本 skill 内的 package。

### 6.4 安全默认值

- 凭据只能进入私有 data 目录、进程环境或宿主 secret store，不进入参数、Git、日志和报告。
- console password/tunnel token 不允许通过 argv 传递。
- 私有状态使用原子写入和 `0600` 文件权限。
- live、media、cache、load 操作前保留人工确认；doctor、数据库校验和 unit tests 不发送真实流量。
- 未确定 provider、route、API form、model 或 profile 时不扩大探测范围。

## 7. 编程工具与 OpenClaw 策略

详细安装命令、路径和权限模型见 `references/tool-compatibility.md`。本计划只定义共同契约和必须分别验证的边界。

| 工具类别 | 发现路径策略 | 调用策略 | 关键边界 |
| --- | --- | --- | --- |
| Codex | 项目/用户 `.agents/skills/llm-api-test` | `$llm-api-test` 或 `/skills`；底层稳定入口不变 | 当前 Codex 支持 skill 自动发现，不再沿用旧 README 的“只能手动”结论 |
| Claude Code | `.claude/skills/llm-api-test` | `/llm-api-test` 或 description 触发 | Agent SDK 还需允许 Skill/Bash 和 project/user setting source |
| Kilo | `.agents/skills` 或 `.kilo/skills` | 描述触发或明确点名 | 用户级原生路径是 `~/.kilo/skills`，不是旧的 `~/.config/kilo/skills` |
| Cursor/Windsurf | `.agents/skills` 或各自原生 skills 目录 | 显式点名 skill | 云 worker 不继承本机 user skills/runtime/secrets |
| OpenClaw | workspace `skills/` 优先，也可 `.agents/skills`/用户目录 | 自然语言点名 + `openclaw skills info/check` | host 可见不等于 sandbox 可执行；必须在实际 sandbox 再跑 doctor |
| 通用 shell agent | 完整目录任意只读路径 | `bash <root>/bin/llm-api-test ...` | 无自动发现也能使用，但仍需 Bash/Python/网络权限 |

OpenClaw 特别约束：

1. 使用受信任且固定的 Git revision 或本地干净 release directory 安装；不要依赖开发机绝对路径。
2. 当前 bundled MPDB 单文件和完整 bundle 可能超过 managed library 上传限制，优先 workspace Git/local-directory 安装，不能为适配上传而静默删数据库。
3. host 侧 `skills info/check` 只验证发现和 eligibility；sandbox 内还要验证完整文件可见、Bash/Python 可执行、data/runtime 可写、secret 可用和目标域名 HTTPS 出站。
4. 推荐 skill 只读挂载，data/runtime 分别窄范围读写挂载。不要挂载整个 home、Docker socket、SSH 或云凭据目录。
5. setup 需要软件源或内部镜像；live 测试只开放目标 provider 域名；Web/Locust 绑定端口另行授权。

“不同编程工具可用”的完成定义不是文档里出现了工具名字，而是：工具能发现正确 revision，能运行统一 facade，能在其真实执行环境完成 doctor，并在获得单独授权后完成最小真实请求。每个工具的结果单独记录，不能用 Codex 的通过替代 OpenClaw。

## 8. 执行阶段与状态

| 阶段 | 工作 | 当前状态 | 退出条件 |
| --- | --- | --- | --- |
| 0. 基线冻结 | 记录目标起点、源 commit/tree、源 dirty 和排除项 | 已完成 | SHA/tree 可复核，dirty 清单完整 |
| 1. committed import | 从 `236193d` 导入 `app/`、MPDB package 和 wrapper allowlist | 已完成 | manifest 与目标文件一致，无源 dirty/reports |
| 2. P0 切换 | 移除两份 legacy YAML，统一 MPDB consumer 和安装依赖 | 已完成 | manifest/digest/schema/catalog 均通过 |
| 3. P1 能力 | 固定速率/结果语义、model sweep、媒体 URL 安全 | 已完成 | 对应 unit/integration 测试通过 |
| 4. standalone overlay | 外部 data/runtime、统一 launcher、doctor、MPDB-only workflow | 已完成 | 只读 skill 环境可运行，旧 overlay 不再产生 |
| 5. 工具元数据与文档 | 修正 SKILL frontmatter、Codex/OpenClaw/Kilo 等说明 | 已完成 | validator、链接和命令示例通过 |
| 6. 最终离线验收 | 执行第 9 节矩阵并记录精确结果 | 已完成 | 本地必选项通过；外部环境项明确隔离 |
| 7. 外部工具实机/真实 API | 在各工具和获授权 provider 上执行 | 未执行，需独立环境/授权 | 每个工具和模型分别留存证据 |
| 8. 提交与发布 | 审计 diff、secret scan、提交/tag/release | 2026-09-19 复核：迁移仍在工作树，未提交 | 以实际 Git 记录为准 |

## 9. 测试与验收矩阵

以下矩阵按层执行。测试数据目录和 runtime 均使用临时路径，避免读取开发机已有配置，也避免把本机旧 `.venv` 当成新安装证明。

| 层 | 测试内容 | 典型命令/方法 | 必须证据 | 状态 |
| --- | --- | --- | --- | --- |
| Provenance | commit/tree、dirty 排除、导入 allowlist | `git rev-parse`、blob 对照、manifest | SHA/tree；冻结时 8 个、终审时 76 个 tracked dirty 与 56 个默认 untracked 条目（211 files）均不在目标 | 通过；24 个 app intentional overrides + 2 个 standalone additions 已列入 manifest |
| Skill schema | `SKILL.md` frontmatter、名称、description、metadata | skill-creator `quick_validate.py` | validator 退出 0 | 通过 |
| UI metadata | `agents/openai.yaml` 字段与 prompt | YAML 解析/规范检查 | `$llm-api-test` 在 default prompt 中 | 通过 |
| Package | MPDB checkout、schema、catalog、digest、manifest | `mpdb verify-artifacts`；Python API 查询 | 版本和两个 digest 与第 4 节一致 | 通过；8/8 artifacts |
| Doctor | 文件、依赖、data/runtime 可写、数据库、app import | `bin/llm-api-test doctor --json --strict` | JSON 总结、退出 0、不访问 provider | 通过；`ready: true` |
| App unit | 参数、cache、identity、media、fixed-rate、metrics、web auth | `scripts/python.sh -m pytest -q app/tests` | 精确 pass/subtest/skip/fail 数 | 通过；349 passed、714 subtests |
| Wrapper | setup/run/jobs/result/workflow/console/python facade | help、dispatch、错误路径、shell 静态检查 | 无内部路径猜测；错误码稳定；不泄露 secret | 通过；21 个 standalone tests + shell/Python syntax |
| Fresh setup | 空 data/runtime；完整依赖；只读 skill | 带空格临时路径执行 `setup --from`、doctor、`uv pip check` | runtime 不写入 skill；Pillow/jsonschema 等齐全 | 通过；45 个锁定第三方包，目录 0700/私有文件 0600 |
| Workflow | propose 不写 DB；apply 对未知 binding 拒绝；已发布 exact binding 才完成 | mock 正负样例与 legacy path scan | 无 legacy local profile 文件；review ref 被记录 | 通过 |
| File/package | 无不必要超大 fixture、无 report/key/cache | 文件大小扫描、secret scan、Git status | 只有必需 MPDB 制品可能较大；无敏感内容 | 通过；3 个 >1 MiB 文件均为必需 MPDB 制品，managed bundle 明确 false |
| Web/Locust local | loopback server、auth、job 生命周期、固定速率调度 | 允许 socket 的本地/CI 环境 | 端口/auth/任务结果；不能用受限 sandbox 的 socket 拒绝误判产品失败 | app 集成测试通过；未手工启动浏览器/真实 Locust 流量 |
| Tool discovery | Codex/Claude/Kilo/Cursor/Windsurf/OpenClaw 实际选中正确路径 | 各工具自身 info/reload/new session | 每工具 revision、路径、facade/doctor 结果 | Codex 当前会话离线调用链通过；独立新进程发现及其余工具仍需对应环境 |
| Live smoke | 最小完成请求、返回模型身份、token/cache/media 证据 | 获授权后按 provider/API form 单独执行 | 完成响应而非 202/listing；报告已脱敏 | 未执行，需凭据/费用授权 |
| Live load | 每模型独立固定 RPM 与并发，warmup/measure/drain | 明确 RPM/持续时间/budget 后执行 | 每模型实际 achieved RPM、错误和 identity/profile coverage | 未执行，需容量/费用授权 |

### 9.1 失败分类

验收失败必须归到以下一种，不能笼统写“测试失败”：

- **产品回归**：代码或数据库行为与契约冲突，必须修复。
- **源基线测试假设过时**：例如测试依赖源仓库 root facade，或选用了 MPDB 已禁用的默认 pressure binding；应把测试改成独立、显式 fixture，不能放宽 fail-closed 产品逻辑。
- **依赖缺失**：例如媒体验证环境没有 Pillow；修复 setup/requirements 后重跑。
- **sandbox 限制**：例如禁止创建 loopback socket；在获准的隔离环境复测，不能因此宣称网络能力通过或失败。
- **外部条件**：凭据、余额、上游配额、DNS/TLS 或 provider 故障；与离线迁移验收分开。

### 9.2 最终验收记录

本次最终记录如下；数字来自当前迁移 checkout，不使用旧 README 数字或源仓库历史结果：

- skill schema validator：`Skill is valid!`
- 本机 Codex 离线调用链：当前会话读取 `SKILL.md` 后执行 doctor、providers、MPDB info 与 `deepseek-v4-flash,deepseek-v4-pro` plan-only sweep，命令均退出 0；flash 为 eligible、pro fail closed；只创建隔离的空 data 目录，没有配置、report/job 或 provider 流量。隔离 `.agents/skills/llm-api-test` 链接布局已建立，但独立 `codex exec` 因未获准向外部服务披露未提交仓库内容而未运行，因此不宣称 fresh-process discovery 已通过。
- fresh setup / dependency check：带空格的全新 data/runtime 和 `--from` 迁移通过；45 个锁定第三方包 compatible；skill 内未生成 build/egg-info。
- MPDB artifact verification：8/8 artifacts；SQLite integrity、catalog `0.4.0`、两个 digest 均精确匹配。
- doctor：`ready: true`；runtime consumer 为 bundled checkout mode，路径与 identity 正确。
- app pytest：`349 passed, 714 subtests passed`；测试本身不访问真实 provider，localhost socket integration 在允许 socket 的隔离执行面通过。
- wrapper/workflow/standalone tests：`21 passed`；另有 Bash syntax、Python compile、generated-doc check 通过。
- secret/file-size/diff audit：常见 key/private-key pattern 无命中；源绝对路径未进入运行代码；旧 4.4 MiB fixture 已删除；3 个 >1 MiB 必需 MPDB 制品被 doctor 明确列出。
- 只读 skill：复制为全只读目录后，doctor strict 与 provider discovery 通过且文件 hash 未变化。
- 受限环境导致的独立未验证项：Codex fresh-process discovery、OpenClaw/Cursor/Windsurf 安装、Claude/Kilo 真实 skill 会话；OpenClaw managed upload 因官方大小限制明确不支持；所有真实 API 与压力流量均未执行。

## 10. 回滚方案

回滚以目标起点 `main@0c1e028` 和迁移提交为界，不操作 api_pressure 源仓库，也不删除用户 data 目录。

1. 发布前发现问题：先保存 `git diff --binary` 和验收日志，逐个修复；只有确认目标仓库没有用户并行修改时，才对明确迁移路径做恢复。
2. 发布后发现问题：优先对独立迁移 commit 执行 `git revert <migration-commit>`，生成可审计的反向提交，不使用 `git reset --hard`。
3. MPDB 回滚：代码和 bundled MPDB 必须作为同一发布单元回滚，不能只降 package 或只恢复 legacy YAML。
4. runtime 可删除并由旧 release 重新 setup；它是可重建缓存。删除前精确解析并核对 `LLM_API_TEST_RUNTIME_DIR`，禁止对 `$HOME`、`~` 或未解析变量做递归删除。
5. data 包含密钥、报告和工作流状态，默认保留。若 schema 发生变化，先备份并只对目标版本需要的文件做显式迁移；代码回滚不授权删除 data。
6. 已发送的真实请求无法回滚。任何 live/load 验收都必须在发送前完成费用、容量和目标确认。

## 11. 未来从 api_pressure 同步的标准流程

每次同步都视为一次新的受控迁移，不能对源工作树做盲目 `rsync`。

1. **选择 revision**：更新源远端信息，明确选择 committed SHA；同时记录 tree SHA、branch/tag 和选择理由。
2. **审计工作树**：列出源 tracked dirty、untracked reports/private files；无论是否与 chosen revision 同名，都不从普通工作树复制。
3. **生成差异清单**：比较 `app/`、`packages/model-profile-db/` 和 wrapper allowlist 的 source-only、target-only、different；按 P0/P1/P2 重新分类。
4. **核对数据库 release**：确认 package/schema/catalog 版本、manifest 和 digests 一致；若 app 需要新 MPDB API，二者作为同一变更集迁移。
5. **临时目录导出**：用 `git archive <sha>` 或 committed blob 导出到临时目录。导入范围默认只有 `app/`、MPDB package 和本计划表中的 wrapper allowlist。
6. **应用 standalone overlay**：重新审查外部 data/runtime、facade、doctor、MPDB-only workflow、tool metadata 和文档。若上游已等价实现，应删除重复 overlay；不能静默覆盖。
7. **更新 manifest**：记录新 SHA/tree、导入组件、数据库版本/digest 和仍存在的 overlay。
8. **执行完整矩阵**：至少完成 schema、MPDB、doctor、fresh setup、app tests、workflow 负向测试、secret/file audit；比较的是本次 checkout 结果，不沿用历史数字。
9. **人工 live gate**：只有变更影响真实协议且已有 provider、route、API form、模型、预算和凭据授权时，才发送最小请求或压力流量。
10. **审计提交**：仅 stage 本次迁移路径，检查 binary/database 和大文件，扫描 secret；在独立 commit 中记录 source SHA/tree 与验收结果。

如果源的 root-only 工具想进入 skill，必须先说明通用用户场景、权限边界、稳定输入/输出、脱敏策略和测试；“源仓库有这个脚本”本身不是迁移理由。

## 12. 完成项与未验证项

### 已完成或已落位

- 已冻结目标 `0c1e028` 和源 `236193d`/等价 `7079750`/tree `da511d`。
- 已记录并排除冻结时 8 个源 tracked dirty 文件；2026-09-04 终审时的 76 个 tracked 修改、56 个默认 untracked 条目（211 files）也未导入，源 committed HEAD/tree 未变化。
- 已按 committed source 迁入 `app/`、MPDB package 和通用 wrapper allowlist。
- 已移除两份 legacy profile/reference YAML 和超大文本 fixture。
- 已落位固定速率压力、结果语义、模型遍历、媒体 URL 安全和 Web auth 等当前通用能力。
- 已建立只读 skill、外部 data/runtime、统一 facade、doctor 和 MPDB-only onboarding overlay。
- 已记录跨工具及 OpenClaw 的发现、调用、sandbox、secret 和网络策略。

### 仍需最终验证或独立授权

- 手工 Web 控制台浏览器流程与真实 Locust 进程/流量（localhost job integration 已由 app 测试覆盖）。
- Codex 之外每个编程工具的实际安装、发现优先级和其真实 worker/sandbox 内 doctor。
- OpenClaw host 与 sandbox 两层的端到端安装、完整 bundle 物化、可写挂载和出站策略。
- 任意真实 provider 的 smoke、媒体、cache 或 fixed-rate 压力测试；本迁移默认不读取凭据、不产生费用。
- Git commit、tag、release、push、分发或源仓库改动；2026-09-19 复核时目标迁移仍未提交。

只有第 9 节离线必选项得到本次 checkout 的明确结果后，才可声明“迁移代码验收完成”；只有某个具体工具在自己的真实执行环境通过发现、facade 和 doctor 后，才可声明“该工具可用”；只有获授权的真实完成请求具备所需证据后，才可声明对应 provider/model/API form 能力已验证。
