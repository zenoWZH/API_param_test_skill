# 参数任务执行控制快照（2026-09-09）

> 当前入口已升级为 JobSpec v6：文字、图片和已接入的固定功能套件冻结完整执行计划，默认完整启用套件、1 轮。本文保留 v5 的设计与历史读取边界；当前行为见仓库 `docs/unified_test_runner_implementation_plan.md` 与 `lib/test_runner/README.md`。


新文字参数任务使用 JobSpec schema 5，保存创建时解析后的重复次数和工具校验模式：

```json
{
  "schema_version": 5,
  "type": "param_test",
  "parameter_execution": {
    "schema_version": 1,
    "runs": 7,
    "tool_validation_mode": "gemini_native"
  }
}
```

完整 JobSpec 仍要求原有 MPDB 身份及结果合同；以上只展示新增控制字段。
`runs` 必须是 1–1000 的整数，布尔值和字符串不能作为快照中的次数。
普通任务默认 3 次；App 的 beta、FIM、prefill、缓存固定套件保持 1 次，矛盾次数拒绝执行。
图片、压力、Cache Suite 和 trace 的 JobSpec 保持 schema 4。

Web 创建任务时写入控制值。启动器重新校验落盘的 JobSpec，从中构造子进程环境；
排队期间 Job 对象中的次数或模式变化不会改变执行选择。
runner 在构造 API 客户端前检查 `LOADTEST_PARAM_TEST_RUNS`、`LOADTEST_TOOL_VALIDATION_MODE`：
没有覆盖或覆盖值与快照相同则执行，矛盾值直接报错。运行上下文只属于当前配置对象，
后续参数校验和工具 follow-up 使用该上下文，不会再次读环境变量改变模式。
App 独立缓存 CLI 自建快照时也写入并绑定同样的控制值。

历史 JobSpec 1–4 按原格式读取，不写回文件或补造控制值。
结果分类新增 `parameter_execution_status`：`frozen`、`mismatch`、`invalid`、
`legacy_unfrozen` 或 `not_applicable`。新报告的次数或模式与快照不符时不能列为当前已验证；
历史报告原有 token/identity 判读保留，同时注明没有冻结执行控制值。
新 schema 5 需要本次更新后的消费者；旧版本程序不会把它当成 schema 4 执行。

本轮覆盖 Web POST 创建、落盘加载、排队字段变更、子进程环境、CLI 冲突前置拒绝、
执行途中环境变化、实际参数判读、历史读取和固定套件。
根目录控制/历史组合 69 passed、39 subtests，加一个实际 formal profile 原文回放通过；
App 控制/历史/固定套件/Gemini 组合 265 passed、20 subtests，加一个同类回放通过。
首次回归中各三项旧结果 fixture 没有填入新报告控制字段，补齐后两端历史套件通过；首次失败保留在验证记录中。

报告：`reports/approved_live_20260907/parameter_job_controls_20260909T130719Z`，3 个文件按一个 UTC 日历月保留。
本轮 API 请求新增 0，批准目标累计仍为 1468 次（不含其它并行任务）；MPDB core/extension 未变。

普通 profile 正文和输入样本仍从当前配置读取，本项不证明完整配置重放。
云账户配置、供应商权限/可用性、其它参数参照和发布目标仍按完整批准范围继续处理。
