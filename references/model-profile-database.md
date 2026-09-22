# Model Profile Database 运行与维护

本文说明 standalone `llm-api-test` skill 如何读取、查询、升级、验收和回滚
Model Profile Database（MPDB）。所有示例都从 skill 根目录运行，并通过统一入口
`bin/llm-api-test mpdb` 调用数据库工具；不要依赖全局 `mpdb` 命令。

## 当前固定版本

本 skill 当前固定到以下一组不可拆分的数据库身份：

| 项目 | 固定值 |
|---|---|
| Python package | `yibu-model-profile-db==0.3.0` |
| MPDB schema | `3` |
| catalog version | `0.4.0` |
| canonical models | `87` |
| Profiles | `155` |
| Interfaces | `222` |
| Contracts | `89` |
| Test Bindings | `365` |
| core catalog digest | `f7217d7595a6545a66a33e4d8599dda08706d290c885740d12d7561b5f9fc39a` |
| test-extension digest | `9150c1f45e9388474c4f20deed12cb1f38c50eca809400767f5d6c16989d4820` |

core digest 仍是 2026-09-20 同步快照。test-extension digest 在 skill 2.1.1 随媒体工厂修订重算，catalog 版本未变。来源见
[本轮审查](../MATRIX_REVIEW_20260919.md)。MPDB 随同源码使用，不依赖独立 registry 发布。
package/catalog 版本号可能不变，升级必须比对两种 digest，不能仅比较版本号。

`package_version`、`mpdb_schema_version`、`catalog_version` 和两个 digest 的含义不同：

- package version 标识发布的 Python 包和代码 API。
- schema version 标识数据库结构与 ID 语义；变化时必须同步迁移 consumer。
- catalog version 标识经过审核的数据发布。
- core digest 标识精确的核心目录内容。
- test-extension digest 独立标识测试策略扩展。测试结果必须同时记录两个 digest，
  才能复现实验使用的模型事实和测试策略。

## 唯一事实源与边界

MPDB 是本 skill 中模型身份、官方来源、家族、API form、接口契约和测试 binding 的
唯一事实源。旧的 `api_reference_specs.yaml`、`model_capability_profiles.yaml` 或运行时
provider overlay 不能作为第二套模型事实库。

MPDB 内部仍有两个职责明确、独立摘要的源文件：

- `catalog.yaml` 是核心目录的可编辑源，包含官方来源、canonical model、Profile、
  Interface、route template、Contract 及其 provenance。
- `test_extensions.yaml` 是测试扩展源，包含可选 Test Binding、参数测试/压力策略及其
  独立 provenance。它不是核心目录的一部分。

运行时默认把编译后的 core 与 `test_extensions.yaml` 组合起来。`--core-only` 则只读
core，不加载任何 Test Binding。

数据库层级是：

```text
modality -> official source -> family -> canonical model -> Profile -> Interface
```

API form 和 routing mode 属于 Interface，不能通过修改 family 来表达兼容协议。第三方
供应商、聚合线路、账号路由和兼容标签是运行时执行信息，不能冒充 MPDB 的官方
`source_id`。

### Provider overlay 可以做什么

`providers.local.yaml` 只负责私有或环境相关的执行映射，例如：

- endpoint、环境变量名和鉴权方式；
- provider 下可请求的模型 ID；
- 将一条执行线路映射到已经存在的精确 MPDB Profile/Interface；
- 非模型事实的运行选项。

它不能：

- 新建 canonical model、Profile、Interface、Contract 或 Test Binding；
- 改写官方来源、模型家族、API form 或参数能力；
- 绕过 disabled、identity-only、not-certified 或缺少精确 binding 的 fail-closed 判定；
- 用一次供应商调用结果替代正式 MPDB 审核和发布。

## 存储形态

发布包中的数据库文件位于
`packages/model-profile-db/model_profile_db/data/`：

| 文件 | 内容与用途 |
|---|---|
| `catalog.yaml` | 核心目录的可编辑源 |
| `test_extensions.yaml` | 独立的测试扩展与 365 个 Test Bindings，包括 94 个可选媒体输入 workflow |
| `catalog.json` | 确定性编译的 core-only 运行时制品 |
| `catalog.sqlite3` | core-only 的关系查询制品 |
| `manifest.json` | 版本、计数、schema 身份、digest、文件 SHA256/大小 |

SQLite **只有 core**。它没有 Test Binding 表，365 个 Test Bindings 只来自
`test_extensions.yaml`，并在默认 `load_catalog()` 时组合进内存目录。不能因为 SQLite
查询不到 binding 就判定扩展缺失；应使用 CLI 的 `query --entity test-bindings` 或
`info` 检查组合后的视图。

## 安装与日常运行

首次安装或数据库 package 更新后运行：

```bash
bin/llm-api-test setup
bin/llm-api-test doctor --json --strict
```

`setup` 通过 `PYTHONPATH` 直接消费只读的 bundled checkout package，只把锁定的第三方
依赖安装到独立 runtime，并检查 Python 依赖、MPDB manifest 和 consumer pin。这样不会
在 skill 内产生 build/egg-info。运行时、凭据、provider overlay 和报告应放在
`LLM_API_TEST_RUNTIME_DIR` 与 `LLM_API_TEST_DATA_DIR` 指向的可写目录。

日常只读健康检查：

```bash
bin/llm-api-test mpdb verify-artifacts
bin/llm-api-test mpdb info
bin/llm-api-test mpdb info --core-only
```

预期行为：

- `verify-artifacts` 校验 manifest schema、每个文件的 SHA256/大小/schema 身份、从源到
  core 的重编译一致性、extension digest、SQLite integrity 和关系数据等价性。
- 默认 `info` 报告 87/155/222/89/365，并且 `test_extensions_loaded` 为 `true`。
- `info --core-only` 保留 87/155/222/89 个 core 实体，把 `test_bindings` 报告为 `0`，
  `test_extension_digest` 为 `null`，`test_extensions_loaded` 为 `false`。

## 查询

先列官方来源，再按来源、家族、模型和 API form 逐层缩小范围：

```bash
bin/llm-api-test mpdb query --entity sources --modality text

bin/llm-api-test mpdb query \
  --entity models \
  --modality text \
  --source openai \
  --family gpt

bin/llm-api-test mpdb query \
  --entity interfaces \
  --modality text \
  --source openai \
  --family gpt \
  --model gpt/gpt-5.3-codex \
  --api-form openai_responses \
  --routing-mode vendor_direct \
  --enabled true
```

查询接口对应的核心 Contract 和测试扩展：

```bash
bin/llm-api-test mpdb query \
  --entity contracts \
  --family gpt \
  --api-form openai_responses

bin/llm-api-test mpdb query \
  --entity test-bindings \
  --interface-id \
  'text/openai/gpt/gpt-5.3-codex#openai-responses-default'
```

精确解析一条 source-scoped 请求模型：

```bash
bin/llm-api-test mpdb resolve \
  openai gpt-5.3-codex \
  --modality text \
  --api-form openai_responses \
  --routing-mode vendor_direct
```

查询测试工具实际使用的 source-first 参数配置：

```bash
bin/llm-api-test mpdb parameter-configs \
  --source deepseek \
  --modality text \
  --family deepseek \
  --model deepseek-v4-flash-0731 \
  --view

bin/llm-api-test mpdb resolve-parameter-config \
  --source deepseek \
  --modality text \
  --family deepseek \
  --model deepseek-v4-flash-0731 \
  --interface-id \
  'text/deepseek/deepseek/deepseek-v4-flash-0731#openai-chat-default' \
  --api-form openai_chat_completions
```

解析结果必须同时满足 source、family、model、Interface、API form 与 Test Binding 的精确
约束。不要在查询无结果时自动换 family、source 或 API form。

## 供应商 onboarding 的新语义

工作流不直接修改 MPDB。到达 `onboard` 节点后：

```bash
bin/llm-api-test workflow onboard-propose \
  --provider example_provider \
  --model example-model
```

`onboard-propose`：

- 验证工作流已经具备参数、trace、并发、价格或人工门禁所要求的证据；
- 输出 `llm-api-test.mpdb-review-proposal.v1` 审核证据；
- 列出需要审核的 Source、CanonicalModel、Profile、Interface、Contract 和 TestBinding；
- 明确返回 `mutates_database: false`，不会创建本地模型 overlay，也不会发布数据库。

如果不存在精确可执行 binding，应把 proposal 和官方资料提交给独立的 MPDB 审核流程。
审核通过后，必须把新的完整 MPDB 源码快照与对应 consumer 一起迁入本 skill，
重新运行 setup 和本页验收，再执行：

```bash
bin/llm-api-test workflow onboard-apply \
  --provider example_provider \
  --model example-model \
  --yes \
  --review-ref APPROVED-REVIEW-ID
```

`onboard-apply` 的名字是工作流兼容接口，不代表“写入数据库”。它只会：

1. 重新检查 onboarding 前置证据；
2. 在当前已安装 MPDB 中解析精确、可执行的 Profile/Interface binding；
3. 记录不可为空的外部 `--review-ref`；
4. 把工作流推进到 `done`。

成功输出仍应包含 `applied: false`、`database_mutated: false` 和 `verified: true`。若新
binding 尚未审核同步或不能精确解析，命令必须失败，不能用 `--yes` 绕过；不要求独立 registry 发布。

## 升级：package 与 consumer 原子同步

MPDB 升级不是复制一个 YAML、JSON 或 SQLite 文件。以下内容构成一个原子发布单元：

- `packages/model-profile-db/` 的 package 代码、source、extension、schemas 和全部编译制品；
- `app/lib/model_profile_catalog.py` 的预期 package/schema/catalog/digest pin；
- `app/requirements.txt` 的精确 package version pin；
- 所有读取 Profile、Interface、Contract、Test Binding 和 job snapshot 的 consumer 变更；
- 对应测试、迁移清单和发布文档。

只更新其中一部分会制造“新数据库 + 旧 consumer”或“旧数据库 + 新 pin”的混合状态，
必须 fail closed。

### 1. 在 authoring checkout 构建候选制品

只从已审核的 `catalog.yaml` 与 `test_extensions.yaml` 构建；不要把 provider 私有配置、
凭据或运行报告放进 package。

```bash
bin/llm-api-test mpdb validate \
  --source packages/model-profile-db/model_profile_db/data/catalog.yaml \
  --extensions packages/model-profile-db/model_profile_db/data/test_extensions.yaml

bin/llm-api-test mpdb build \
  --source packages/model-profile-db/model_profile_db/data/catalog.yaml \
  --extensions packages/model-profile-db/model_profile_db/data/test_extensions.yaml

bin/llm-api-test mpdb verify-artifacts
```

`build` 是 authoring 操作，会重建受版本控制的制品；不要在已部署 skill 或正在执行测试
的 runtime 中运行它。

### 2. 审核数据差异

保存上一个发布的 core JSON，然后对候选版本运行：

```bash
bin/llm-api-test mpdb diff \
  /path/to/previous/catalog.json \
  packages/model-profile-db/model_profile_db/data/catalog.json
```

逐项审核新增、删除、修改和 lifecycle 变化；特别检查 source 归属、canonical model ID、
Profile/Interface 稳定 ID、API form、Contract 与 Test Binding 引用，以及 provenance 是否
覆盖变更字段。schema 或 ID 语义变化时，consumer 迁移必须在同一发布中完成。

### 3. 用隔离 runtime 验证完整候选 skill

把完整候选 skill 放入新的 staging 目录，用独立 data/runtime 运行 setup、doctor、MPDB
校验和测试。不要复用生产 runtime 来“试装”候选 package。

```bash
export LLM_API_TEST_DATA_DIR=/path/to/staging-data
export LLM_API_TEST_RUNTIME_DIR=/path/to/staging-runtime

/path/to/candidate/bin/llm-api-test setup
/path/to/candidate/bin/llm-api-test doctor --json --strict
/path/to/candidate/bin/llm-api-test mpdb verify-artifacts
/path/to/candidate/bin/llm-api-test mpdb info
/path/to/candidate/bin/llm-api-test mpdb info --core-only
```

随后运行完整离线测试和至少一组明确授权的最小真实调用。真实调用用于验证 provider
执行线路，不能替代 manifest、consumer pin 和 MPDB 实体审核。

### 4. 原子切换

通过宿主支持的同文件系统目录 rename 或版本化目录 + `current` symlink，一次切换完整
skill release 及其对应的已验证 runtime。切换前暂停创建新 job；切换后立即重跑：

```bash
bin/llm-api-test doctor --json --strict
bin/llm-api-test mpdb verify-artifacts
bin/llm-api-test mpdb info
```

不要就地逐个覆盖 package、consumer 或数据库制品。外部
`LLM_API_TEST_DATA_DIR`（凭据、provider overlay、报告、workflow 状态）不属于模型数据库
发布，不应随代码切换而删除或覆盖。

## 发布验收清单

每次安装、升级或回滚后都必须满足：

- `setup` 完成，依赖检查通过；
- `doctor --json --strict` 返回 `ready: true`，runtime consumer 与 bundled manifest 身份一致；
- `mpdb verify-artifacts` 成功；
- `doctor` 报告的 package version，以及 `mpdb info` 报告的 schema/catalog 版本、两个
  digest 和实体计数符合批准清单；
- `mpdb info --core-only` 显示 0 个 Test Bindings；
- 对本次变更涉及的 source/model/API form 执行 `query`、`resolve` 和必要的
  `resolve-parameter-config`；
- app 测试覆盖 source 隔离、API-form 隔离、unknown/disabled fail-closed、job snapshot、
  media 非压力策略和 consumer pin；
- 运行产物记录完整 Profile/Interface snapshot、core digest 与 extension digest；
- provider overlay 没有新增模型事实，日志、manifest 和报告中没有凭据。

当前固定版本的验收值必须精确为本页首表所列值；不能用“版本号相同”代替 digest 和
计数核验。

## 回滚

回滚单位与升级单位相同：回到上一个**完整 skill release + 对应 runtime**，不能只回滚
`catalog.sqlite3`、`catalog.json`、`test_extensions.yaml`、package 或 consumer 中的任意一项。

1. 停止创建新 job，保留现有 `$LLM_API_TEST_DATA_DIR` 和报告。
2. 原子切回上一个已验收的版本化 skill 目录和它的 runtime。
3. 运行 `doctor --json --strict`、`mpdb verify-artifacts`、`mpdb info` 和关键精确解析。
4. 恢复创建 job，并在变更记录中写明回滚版本、两个 digest、原因和验证结果。

已有 job 的数据库身份和 Profile/Interface snapshot 是不可变证据；不要用回滚后的当前
目录重新解释旧结果。需要复核旧 job 时，优先使用创建该 job 的发布版本。回滚也不应
删除 provider 私有配置、workflow 证据或历史报告。

若没有经过验收的上一完整发布，或 rollback 后 package、consumer、manifest 任一身份
不一致，应保持 fail closed，不启动真实测试，并恢复到独立 staging 重新构建发布单元。
