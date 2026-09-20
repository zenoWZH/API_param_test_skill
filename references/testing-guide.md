# 测试类型与结果判读（速查）

详版文档在 `<skill-root>/app/docs/`（testing_guide.md、parameter_testing.md、cache_testing.md、load_testing.md、image_param_test.md）。本文件是给 agent 的速查；所有执行入口统一为 `bin/llm-api-test`。

## 类型选择

| 目的 | 类型 | 关键产物 |
|---|---|---|
| 参数兼容性/响应语义/token 准确性/模型身份 | `param_test` | verdict.json |
| 缓存命中（短固定 system + 变化 user + 工具调用） | `cache_suite` | verdict.json |
| 响应指纹来源对比（仅作信号） | `trace_test` | verdict.json |
| 图片分辨率/格式/边界 | `image_param_test` | summary.json + case_results.json |
| 阶梯并发上限 | `staircase` | verdict.json（max_qualified_business_rpm 等） |
| 快速压测 | `quick_load` | load_result.json + locust html |
| 1 小时稳定性 | `soak` | verdict/load_result |
| 多模型固定速率能力 | `sweep` 子命令 | sweep_results.json + 每模型 summary |
| 冻结功能矩阵与资源依赖 | `matrix preview/run` | JobSpec v6、workflow_result、逐轮账本 |
| DeepSeek V4.1 专用研究 | `deepseek-v41` | 冻结 package、逐用例响应语义与 usage 结果 |

## 判读要点

- `param_test` verdict：`pass` 总判定；`compatibility_pass`（参数兼容）、`token_accuracy_pass`（token 计数）、`model_identity_pass`（返回模型真实性）分项；`failures`/`incompatibilities` 列失败点。注意 `expected_rejections` 是“预期拒绝”不算失败。
- `cache_suite` verdict：`pass` + `summary`（含官方 usage 计算的缓存指标）；`latency_speedup_ratio` 仅作证据，不作判定。
- `staircase` verdict：`max_qualified_business_rpm`/`peak_business_rpm` 对照 `target_business_rpm_min`；`first_failing_step` 指示从哪一级开始不达标。
- `trace_test` verdict：`best_match` + `best_score`（≥0.6 才算有效匹配）；有 `--expect` 时看 `match_expected`。
- `trace_test` 是指纹相似度证据，不能单独证明 token 的真实上游；需结合直连来源、route telemetry、返回模型身份与供应商证据。
- 未注册或没有 exact executable MPDB binding 的模型会被普通入口拒绝。不得创建 local YAML 绕过；专用研究按其独立入口和范围执行，新增通用 binding 经独立审核后与 consumer 一起同步。
- sweep 必须先看离线 plan。只有明确 model、每模型 RPM、duration、users、spawn rate 且得到费用/容量批准后才可加 `--execute --yes`；当前候选逐模型顺序执行。

## 共同约定

- 压力/Cache 请求遵守 `test_cases.minimum_prompt_tokens`（默认 100）；参数请求保留声明输入，不添加填充或隐式 system，文字输出 allowance 至少 256。
- 参数 token audit 为 schema v4：完成性、usage 算术与合理数量检查不等于精确 tokenizer 证明；历史 v3 PASS 保持未验证。冻结功能流程还需检查每轮用例和清理状态。
- 报告都在 `$DATA/reports/jobs/<job_id>/`；`job.log` 是运行日志。
- HTTP 202、`/models` 列表或 profile 存在都不等于能力通过；必须有已完成请求和适用的 identity/usage/cache/media 证据。
