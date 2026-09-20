# 当前参数矩阵审查与 skill 升级

开始日期：2026-09-19；最新源快照：2026-09-20。任务：依据最新迁移后的参数矩阵审查、优化 standalone skill，并保留跨编程工具和 OpenClaw 的统一调用方式。

## 最终输入与验收

源工作树在开发期间继续增加真实照片、视频及原始响应脱敏。最终输入冻结于
`2026-09-20T08:02:38.503009+00:00`，包含 388 个有 SHA256 的源输入；后续源修改属于下一次同步。
该快照已完整三方合并，MPDB 为 **87 models / 155 Profiles / 222 Interfaces / 89 Contracts / 365 Test Bindings**，
其中 **94 个可选图像/视频/音频输入 workflow**。core digest 保持 `f7217d7595a6545a66a33e4d8599dda08706d290c885740d12d7561b5f9fc39a`，
最终 extension digest 为 `629ca0a5dfa6eceeeac73a6f5ee73546c43efb8dfe861032bba5a7a6b6658b51`；冻结源的 8 个制品校验通过。

完整 App 首轮的 7 个失败项已修复。最终相同的 3945 个测试节点按耗时分为 4 组，
全部退出 0：**3578 passed、367 skipped、1279 subtests passed、0 failed**。
已逐项核对 JUnit 收据与计划节点，无遗漏、无重复；节点清单 SHA256 为
`57cbc8c3312b32ecbfa675c657ab1e1aca4f899bc48070ffe174dffe1d4c4e1e`。
367 个 skip 中，366 个依赖未分发的私有/历史原始报告，1 个属于源仓库专用数据库重建工具。
这不是删减运行能力，也不代表这些历史实测已在本机重放。

独立 CLI **39 passed**；只读安装的 5 条调用链、MPDB 8 个制品、严格 doctor、生成文档与 skill 校验通过。

## 基线与输入

- 用户给出的 `~/projects/yibuapi_loadtest` 在本机不存在；实际使用此前对应的 `~/projects/yibuapi-llm-loadtest`，只读访问。
- 目标 HEAD 为 `0c1e028`；上一轮迁移仍在工作树，未形成 Git 提交。保留这些修改并基于它们升级。
- 上轮引擎基线为 `236193db27deb81c6c36e89e2cbe21bb5c458417`。
- 本轮引擎基线为 `2df5ca4efda00f3dfd1468dd80b1394ac20258ef`，tree 为 `0695bf6d10e7e340dbed2a771e714a9305f7db93`。
- “当前最新矩阵”包括源工作树中的 DeepSeek V4.1 Flash 和 94 个可选图像/视频/音频输入 workflow。按明确文件白名单冻结、逐文件记录 SHA256，与已提交引擎分开标注；不将其描述为已提交或已发布版本。
- 源项目在工作期间更新了测试扩展和配套 schema、工厂、CLI/UI；这些组件已整体同步。最终统计为 87 models / 155 Profiles / 222 Interfaces / 89 Contracts / 365 Test Bindings。
- 私有 `.env`、provider overlay、原始供应商报告及未进入所选矩阵的实验未导入。历史回放需要的公开事实/定义按 committed blob 导入；缺少私有原始报告的回放和不随 runtime 分发的 source-only 数据库重建测试明确 skip。

## 已确认差异及处理

| 审查项 | 原 skill 的缺口 | 处理 |
| --- | --- | --- |
| 执行引擎 | 只有旧参数 runner / JobSpec v4 | 同步统一功能 runner、JobSpec v6、依赖排序、响应引用、请求预算和自有资源清理 |
| 参数与图片矩阵 | 缺少后续 Fable、GPT Image 2.5、原生流式及固定功能套件 | 同步上游 portable app、完整 MPDB 与对应 consumer |
| DeepSeek V4.1 Flash | 无新版本参考身份及专用矩阵 | 同步 5 个 API form、61 个研究用例及 10 个 smoke 用例；通用参数/压测权限维持禁用 |
| 图像/视频/音频输入 | 新增的可选媒体 workflow、工厂、素材和 schema 尚不存在 | 同步 94 个精确来源/模型/API workflow；必须显式选择，保留普通参数和 Fable 默认路径，压力权限不变 |
| 离线选择 | 只能发现 provider、规划压力 sweep | 新增 `matrix list` / `matrix preview`，冻结完整参数、图片或固定功能计划 |
| 冻结执行 | 无跨工具统一的 v6 计划入口 | 新增 `matrix run --job-spec ... --yes`，执行同一计划；清理恢复只接受原作业目录和 run ID |
| 请求别名 | 新上游部分 capability 保存 canonical 名，旧校验按 request ID 判断 | 从相同数据库快照生成运行时 capability，分别保存 canonical identity 与 execution target |
| 结果与取消 | 新功能报告、cleanup 状态未投影到 standalone 工具 | 结果摘要保留执行/清理状态，前台矩阵只向自身 PID 发停止信号 |
| 可移植性 | 新上游测试借用根目录定义和私有历史报告 | 导入公开测试定义；报告不随 skill 分发；运行文件继续放在外部 data/runtime |
| 大矩阵加载 | 每次启动纯 Python 解析较大 YAML，单 Contract 查询重复复制全部矩阵 | 使用 C SafeLoader（无扩展时安全回退），精确查询只加载选中 Contract；保持安全构造与不可变返回值 |

三方合并保留上一轮的快照一致性、请求前检查、全新 sweep 报告目录和 Locust 退出码门禁；冲突以当前语义和回归测试核验，不盲目覆盖任一方。

## 实施与验收计划

1. **冻结与盘点**：已完成。source commit/tree、388 个源输入哈希及 MPDB 制品一致性已核实，最终清单见 `MIGRATION_MANIFEST.json`。
2. **引擎和数据同步**：`app/`、MPDB、媒体素材和必要公开测试定义已同步，standalone overlay 保留；相对导入输入没有缺失文件。
3. **统一 CLI**：矩阵列表、离线预览、冻结执行、专用研究、结果读取及停止已实现并验证。历史 PASS、有限范围 workflow 和固定小套件不能自动升级为完整参数准入证明。
4. **防止权限或证据扩大**：已验证。研究/固定子集不能冒充完整准入证明，disabled Interactions 不自动开启；历史结果不代表当前运行；请求与清理结果分别保留。
5. **验证**：已完成。App 全量分片与独立 CLI、MPDB、doctor、生成文档和无凭据预览通过；只读安装与带空格路径验证通过。
6. **文档与完成审查**：README、SKILL、矩阵/数据库说明与 manifest 同步维护。Git 提交以仓库日志为准；本轮不执行 push、tag 或外部发布。

## 验证记录

- 最终冻结源 MPDB：`verify-artifacts` 成功，8 个制品一致；core digest `f7217d7595a6545a66a33e4d8599dda08706d290c885740d12d7561b5f9fc39a`，extension digest `629ca0a5dfa6eceeeac73a6f5ee73546c43efb8dfe861032bba5a7a6b6658b51`。
- standalone 最终 39 passed；覆盖统一入口、冻结执行、停止、结果判读、MPDB-only 准入和有限范围套件不得升级为完整参数证明。
- 专用 DeepSeek V4.1 smoke 离线计划生成成功：10 cases；未发送 HTTP 请求。
- 完整 DeepSeek V4.1 研究计划：61 cases；实际 Fable 依赖预览为 3 个用例/3 个请求；媒体输入 `image_png` 显式 workflow 预览为 1 个请求，均未读取凭据或发送请求。
- 媒体输入新增测试：276 passed、1 source-only authoring test skipped。
- 80 个媒体 binding 逐项用单个真实工厂用例做离线编译：77 个公开配置目标通过；3 个条目需要私有 provider 映射（两条 `aliyun_maas`、一条 `moonshot_official_k3`），无预览失败。审查补齐了源根配置已有、App 缺少的 Gemini 3.6 Flash / 3.5 Flash Lite Chat 映射；未开放它们的其他 API form。
- 最终 94-binding 媒体快照已再次逐项编译：91 个公开配置目标通过，仍只有上述 3 个私有映射需求，无预览失败；这是每个 binding 的单用例计划检查，不代表完整矩阵实测。
- 对比当前源 App 和 skill 的 5 个 Contract：普通和参数模式各 124 份请求体及 transport 完全一致，共 248 份。
- 只读、带空格安装目录中的 doctor、普通/Fable/研究预览通过，安装文件哈希未变化；最新整体快照交付前再复核。
- 回环 HTTPS 假图片服务的真实子进程/私有 overlay/结果恢复/图片读取/密钥隔离链路通过；这不是供应商实测。
- 本机同一预览的 cProfile 耗时由 38.600 秒降到 23.782 秒；仅描述该离线启动样本，不作上游性能结论。
- App 单进程首轮：`3571 passed, 367 skipped, 1279 subtests passed, 7 failed`（2677.35 秒）。7 项已全部通过定向复测。失败涉及历史作业 fixture 的 v4/v6 混用、研究 binding 与新增 media workflow 的区分、重构后的 resolver 测试入口、领先于冻结 App 事实的 MiniMax 字段假设，以及 v6 报告重复控制字段的一致性。
- 其中实际实现修复为：v6 报告若声明 `param_test_runs` / `tool_validation_mode`，必须与冻结计划一致；旧 fixture 按实际 schema 修正。冻结 App 资料未登记 `reasoning_split`，没有为了匹配领先的 root 测试而改写已注册工厂或加入未登记字段，最终答案仍独立于 reasoning 内容校验。
- 旧 `349 passed / 714 subtests` 属于 2026-09-04 历史快照，不用于本轮验收。

最终分组结果：

| 分组 | passed | skipped | subtests | 退出码 |
| --- | ---: | ---: | ---: | ---: |
| 0 | 888 | 99 | 282 | 0 |
| 1 | 900 | 86 | 193 | 0 |
| 2 | 896 | 90 | 717 | 0 |
| 3 | 894 | 92 | 87 | 0 |

直接重跑同一 App 集合的命令为 `scripts/python.sh -m pytest -q app/tests`；本轮分组仅改变进程分配，
不改变用例内容、参数和 skip 条件。单进程首轮用时 44 分 37 秒，最终四组最慢 13 分 1 秒。

## 交付边界

本轮不发送真实供应商流量。OpenClaw 与其他编程工具的真实发现/沙箱端到端仍需在对应环境执行；同一 Bash CLI 的离线通过只证明可执行入口，不等价于各宿主实机验收。Git commit、push、tag 和发布状态以实际 Git 记录为准。

旧引擎和本轮导入前的工作树文件保留在独立临时备份；本轮仅移除了上游已删除的 `app/docs/param_test_audit.md`，其内容可从旧 Git revision 或临时备份恢复。数据库、consumer 与引擎需要整体回滚，用户 data 目录不属于回滚范围。
