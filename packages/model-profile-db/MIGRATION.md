# MPDB 接入与迁移指南

本指南说明如何把其他项目接到独立的 Model Profile DB，以及如何升级本仓库的消费者。
它不要求运行压测控制台。包的 API 示例见 [README](README.md)，本仓库的历史迁移记录见
[迁移验收](../../migration/model_profile_migration_acceptance_20260903.md)。历史验收只证明当时的快照。
本指南随包源码目录提供；当前 wheel 包含运行数据、schema 与 Python API，不包含本 Markdown 文件。

## 1. 先分清三个边界

| 层 | 保存什么 | 不负责什么 |
| --- | --- | --- |
| MPDB core | 官方来源、模型身份、接口、参数合同、来源证据 | 供应商账户、密钥、测试调度 |
| MPDB test extensions | 用例选择、接口绑定、模型期望、测试开关、workflow 定义 | HTTP 客户端和具体 runner 实现 |
| 消费者 | Provider 配置、协议适配器、执行、结果校验、Web/Skill | 创建另一份模型能力真值表 |

业务层级固定为：

```text
modality → source → family → model/Profile → Interface
                                              ├─ Contract
                                              └─ Test Binding（可选扩展）
```

`profile_id = <modality>/<source_id>/<family_id>/<model_slug>`；
`interface_id = <profile_id>#<interface_slug>`。
API Form 属于 Interface；模型家族、官方参考来源、运行供应商是三个不同概念。
例如 Gemini 的 GenerateContent 矩阵不能直接作为 Chat Completions 请求的参数依据。

## 2. 包结构与作者源

```text
model-profile-db/
├── pyproject.toml              # Python 包版本与依赖
├── README.md                   # 查询 API 和 CLI
├── MIGRATION.md                # 本指南
└── model_profile_db/
    ├── catalog.py              # 查询、解析和 core/extension 组合
    ├── compiler.py             # 编译、校验、制品一致性
    ├── cli.py                  # mpdb 命令
    ├── paths.py                # 包内资源路径
    ├── recorded_reference.py   # 冻结参照读取
    ├── schemas/               # source/core/extension/manifest schema
    └── data/
        ├── catalog.yaml       # 当前 core 作者数据
        ├── test_extensions.yaml # 当前测试扩展作者数据
        ├── catalog.json       # 编译后的 core
        ├── catalog.sqlite3    # core 的关系型查询制品
        └── manifest.json      # 制品 hash、格式版本和身份
```

JSON 和 SQLite 都是 core-only；Test Binding、workflow 及测试策略在扩展 YAML 中。
`load_catalog()` 默认组合扩展，`load_catalog(include_test_extensions=False)` 只读取 core。
不能因为 SQLite 中没有测试表就另行维护一份参数矩阵。

本仓库还保留一条**历史来源重放链**：

```text
migration/frozen_legacy_inputs/20260902/（字节冻结）
  + official_model_profile_additions.yaml
  + scripts/ 下的来源增量与 workflow 投影逻辑
  → migrate_model_profile_database.py --check（只读比较）
```

这条链是仓库维护与历史审计约束，不是下游运行依赖。不要把冻结 YAML 复制回旧路径，
不要修改冻结输入以消除差异，也不要把 `migrate_model_profile_database.py` 当作升级写入器。
它已没有写入分支。日常制品构建由 `mpdb build` 负责；作者数据变更还必须满足仓库的重放校验。

## 3. 新项目接入

在选定并固定的源码 checkout 内，使用项目自己的虚拟环境安装：

```bash
python -m pip install ./packages/model-profile-db
python -m model_profile_db.cli verify-artifacts
python -m model_profile_db.cli info
```

独立 MPDB 包要求 Python 3.10+；完整控制台/Skill 按仓库安装说明使用 Python 3.12。
不需要先发布到 PyPI。开发时也可在仓库根目录使用
`PYTHONPATH=packages/model-profile-db python -m model_profile_db.cli ...`。

只读取模型/接口事实的消费者：

```python
from model_profile_db import load_catalog

catalog = load_catalog(include_test_extensions=False)
interfaces = catalog.list_interfaces(
    modality="text",
    source="google_ai_studio",
    family="gemini",
    model="gemini-2.5-flash",
    api_form="gemini_generate_content",
    enabled=True,
)
assert len(interfaces) == 1
interface = interfaces[0]
assert interface["api_form"] == "gemini_generate_content"
```

非 Python 项目可读取 `catalog.json`，或以只读连接查询 `catalog.sqlite3`。
保留 `manifest.json` 与相应 schemas 并验证制品身份；目录中的具体模型集合会随版本改变。
消费者不能从“查到接口”推导“允许执行压力测试”。测试消费者还要组合扩展并检查所选 Binding。

## 4. 旧消费者迁移步骤

1. **冻结旧解释结果。** 记录每个调用点的 modality、source、family、官方模型、运行模型、
   route、API Form、参数合同及测试策略。保留旧报告与任务快照，区分公开事实和私有账户配置。
2. **建立精确映射。** 用查询 API 找现存 Profile/Interface，保存稳定 ID。别名只在同一来源的
   证据约束下使用；模型名或兼容协议相同不代表同一来源。缺项进入迁移清单并阻止执行。
3. **替换能力读取。** 把旧 YAML、家族硬编码表、provider 推测替换成 Contract/Test Binding 查询。
   可以保留协议序列化与响应校验代码；它们不能覆盖 MPDB 对具体模型的支持/禁用决定。
4. **统一三类测试的执行选择。** 参数、缓存、压力测试应使用同一个已解析 Interface 的 API Form。
   存在多接口时显式选定；缺少该接口或其测试策略被禁用时拒绝任务，不换用另一种协议继续执行。
   显式切换 API Form 必须重新解析其自身矩阵和 Binding，不能借用原接口的能力。
5. **冻结执行身份。** 将 provider/runtime model/route/API Form、Profile/Interface/Contract/Binding
   及 core/extension digest 与任务一起保存；发送前核对最终 endpoint、body 与适配器。
   历史任务按原快照解释，无法恢复的任务标记 unresolved，不查询当前默认值补造过去。
6. **验证后移除旧读取。** 同时覆盖 CLI、Web、Skill 和压力工具附带的参数展示入口。
   在旧表没有实际消费者之后再删除它，避免仅改主页面就宣称完成迁移。

本仓库可复用 `lib/model_profile_catalog.py` 的 runtime binding/snapshot 适配思路；
其他项目直接依赖独立包，不要为查询数据而导入整套 `lib`。
本仓库三类测试默认选择及 App 的已知差异见
[结构审查](../../docs/repository_structure_review_20260920.md)，上述步骤是迁移目标，不能当成当前全量实现声明。

## 5. 查询与故障定位

以下命令只查询目录，不发送模型请求：

```bash
mpdb query --entity interfaces --modality text --source google_ai_studio \
  --family gemini --model gemini-2.5-flash --api-form gemini_generate_content
mpdb resolve google_ai_studio gemini-2.5-flash --modality text \
  --api-form gemini_generate_content --routing-mode google_ai_studio
mpdb query --entity test-bindings --interface-id \
  'text/google_ai_studio/gemini/gemini-2.5-flash#gemini-generate-content-default'
mpdb parameter-configs --source google_ai_studio --modality text \
  --family gemini --model gemini-2.5-flash --api-form gemini_generate_content
```

若 `parameter-configs` 返回 `[]`，表示所选组合未解析出可执行参数配置；
不能仅根据接口查询成功就启动任务。当前示例模型可查询到接口，但普通参数配置查询为空。

| 现象 | 应检查的边界 |
| --- | --- |
| 查到模型但不能 resolve | identity-only、disabled、缺接口、多接口未消歧、API Form 不匹配 |
| 参数可运行但缓存/压力不可运行 | 对应 Binding 的测试开关与执行合同；不自动开启 pressure |
| App 报 catalog pin mismatch | 安装来源、package 版本、core/extension digest、manifest 是否为同一套 |
| verify-artifacts 失败 | 是否混用了 YAML/JSON/SQLite/manifest 或改了产物未重建 |
| verify-artifacts 成功而 migration check 失败 | 制品内部一致，但仓库历史重放/增量作者链不同步 |
| 历史报告无法恢复 | 保留原文件与 unresolved 状态，检查原任务 snapshot |

## 6. 数据升级和消费者切换

在独立 checkout 或候选目录准备升级，保留旧版本的整套制品、消费者版本与 pin。
`package version`、`mpdb_schema_version`、`catalog_version`、物理格式版本和两种 digest
含义不同；包版本相同不代表目录内容相同。

1. 修改候选 `catalog.yaml` / `test_extensions.yaml`，保留同来源的具体型号、API Form、字段证据和
   provenance。稳定 ID 不得改派给另一种模型或协议。本仓库还应同步相关增量作者记录与重放逻辑。
2. 在候选环境执行 `mpdb validate`，再执行 `mpdb build` 和 `mpdb verify-artifacts`。
   `build` 会写 JSON/SQLite/manifest；先确认指向候选包，不要在已安装的生产包中试改。
3. 用 `mpdb diff old-catalog.json new-catalog.json` 检查 core 差异；另外比较
   `test_extensions.yaml`、`test_extension_digest` 和测试开关。core diff 不覆盖独立扩展的全部内容。
4. 本仓库另运行以下只读门禁：

   ```bash
   python scripts/migrate_model_profile_database.py --check --report
   PYTHONPATH=packages/model-profile-db python -m model_profile_db.cli verify-artifacts
   python scripts/generate_test_docs.py --check
   ```

5. 同步 `app/lib/model_profile_catalog.py::EXPECTED_MODEL_PROFILE_DATABASE`；包版本变化时同时检查
   `app/requirements.txt`。Root 从同 checkout 包读取，App 还验证显式 pin 与制品 hash。
   Skill 调用 App，因此也要验证 Skill 的安装包来源和执行入口。
   在候选环境的 pin 更新后运行受影响的消费者离线测试，通过后再切换部署。
6. 同一批切换消费者代码、制品与 pin，重启消费者，再验证目录身份、任务冻结值和只读 Web API。

App 与 Root 的 runner 同步是另一个边界；`scripts/sync_test_runner_app.py --check`
只检查该脚本列出的文件，不证明所有复制代码或 UI 一致。
生成文档检查也只检查生成范围，手写架构文档仍需人工核对。

## 7. 验收与回退

至少验证：精确模型/来源/API Form 解析；错误组合在请求前失败；参数、缓存、压力三条路径的
最终 URL、body、transport 和快照身份一致；disabled/identity-only 不可执行；旧任务不按新目录重解释；
Root/App/Skill 所读目录身份一致。Fake client 或离线测试不能证明上游真实兼容性。

回退时恢复旧消费者版本、完整制品与匹配 pin，保留升级期间生成的任务和报告，不用旧目录覆盖它们。
不要回写或删除私有 Provider/密钥文件。历史旧 YAML 的恢复属于另一种运行时回退，见
[冻结输入回退合同](../../migration/frozen_legacy_inputs/20260902/REMOVAL_AND_ROLLBACK.md)，
不应作为普通 MPDB 数据升级失败的处理方式。
