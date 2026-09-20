# Anthropic 缓存固定参数验证

参数测试页选择“Claude 缓存固定对照”，每个新任务发送 **cold、repeat、negative 三次**顺序请求。
按 2026-09-09 的用户口径判断是否命中，不再要求官方最小前缀长度、固定命中比例或冷请求写入达到某个数字。
同来源／系列已有合格代表场景时，不要求为每个型号重复执行。现有选择器支持九个已审原生 Claude 型号；Pro 不在本套件中。

新建任务保存 `schema_version=2` 与 `scenario_expectations_v1` 策略，访问官方 `/v1/messages`，输出上限 2048、
`thinking.type=disabled`、`cache_control.type=ephemeral`。没有 count_tokens 前置、重试、额外身份请求或工具调用。
CLI、Web 预览、预算、进度、结果及历史均以冻结计划的三条请求为准。

预览不生成随机值。任务创建时只生成两个不同的 128-bit 十六进制 nonce，替换共享正文的前 32 字符。
正 nonce 供 cold/repeat 共用，负 nonce 供 negative 使用；来源、模型、接口、其余正文及设置保持一致。
完整请求、哈希、策略和计划摘要冻结进 JobSpec；发送和重放不能重建 nonce 或静默更换策略。

三次生成必须返回准确模型、完整原生文本和有效 usage。cold/negative 缓存读取为 0，repeat 大于 0；
cold/repeat 完整输入总量相等，已报告的地区信息一致。原生完整输入为未缓存输入、缓存写入和缓存读取之和。
缺失读数保持未知，非法计数、拒答、残缺响应和身份不符不能通过。重复读取不再受“不得超过 cold 写入量”约束，
仍须不超过该请求完整输入。原有 16384 输入上界是有界请求的核验限制，不是官方缓存触发阈值。

三控只用于验收，不进入客户命中率。客户 token 命中率为同一批成功且遥测完整有效请求的 `Σ缓存读取 / Σ完整输入`；
命中请求比例为读取大于零的请求数除以有效可观测请求数，测量覆盖率为有效可观测请求数除以成功客户请求数。
缺失或异常读数不补零、不截断。

CLI 使用原选择器 `LOADTEST_PARAMETER_SUITE=anthropic_cache_<model中连字符替换为下划线>_20260908`；
同时指定 `LOADTEST_PROVIDER=anthropic_official`、准确模型、`LOADTEST_API_FORM=anthropic_messages`、
`LOADTEST_ROUTE_PROFILE=vendor_direct` 和 `LOADTEST_PARAM_TEST_RUNS=1`。选择器标识保留，执行策略由冻结计划版本区分。
提供 `LOADTEST_JOB_SPEC` 时必须含原冻结 `fixed_parameter_plan`；重跑须新建任务和报告目录。

报告位于受控 `reports/jobs`，保留一个 UTC 日历月（P1M），API 总费用不设上限。
本套件不证明计费精确性、物理区域、缓存 TTL 或全部参数矩阵完成。

旧 `schema_version=1` Job 仍按原五条请求与原判读重放：两次官方输入估算、cold/repeat/negative，
保留当时的最小输入、cold 写入及 repeat/cold 写入比较。它们属于历史冻结语义，不能被自动改成三请求任务。
改动前判读源码另存于 [历史档案](../lib/anthropic_cache_legacy_20260908.py)，SHA256
`3be354a308c322f346c296cec5d785f6f8b3cc0675407baa5ba092fd4a0604c6`。共享 MPDB 历史 suite 与原始报告保持原样。

2026-09-08 的实际 CLI 验证是 Sonnet 4.5 和 Opus 5 的两套五请求任务，共 10 次 HTTP 200。
缓存读取分别为 0/8115/0 和 0/878/0。原汇总曾误将 count 行纳入生成 token 审计；旧失败保留，
后续零 HTTP 重放修正汇总。见[历史实际验证](../../references/anthropic_app_cache_validation_20260908.json)。
这些历史结果不改写为 v2 实测。
