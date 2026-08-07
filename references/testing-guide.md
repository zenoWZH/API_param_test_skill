# 测试类型与结果判读（速查）

详版文档在 `{baseDir}/app/docs/`（testing_guide.md、parameter_testing.md、cache_testing.md、load_testing.md、image_param_test.md）。本文件是给 agent 的速查。

## 类型选择

| 目的 | 类型 | 关键产物 |
|---|---|---|
| 参数兼容性/响应语义/token 准确性/模型身份 | `param_test` | verdict.json |
| 缓存命中（短固定 system + 变化 user + 工具调用） | `cache_suite` | verdict.json |
| token 真实上游来源 | `trace_test` | verdict.json |
| 图片分辨率/格式/边界 | `image_param_test` | summary.json + case_results.json |
| 阶梯并发上限 | `staircase` | verdict.json（max_qualified_business_rpm 等） |
| 快速压测 | `quick_load` | load_result.json + locust html |
| 1 小时稳定性 | `soak` | verdict/load_result |

## 判读要点

- `param_test` verdict：`pass` 总判定；`compatibility_pass`（参数兼容）、`token_accuracy_pass`（token 计数）、`model_identity_pass`（返回模型真实性）分项；`failures`/`incompatibilities` 列失败点。注意 `expected_rejections` 是“预期拒绝”不算失败。
- `cache_suite` verdict：`pass` + `summary`（含官方 usage 计算的缓存指标）；`latency_speedup_ratio` 仅作证据，不作判定。
- `staircase` verdict：`max_qualified_business_rpm`/`peak_business_rpm` 对照 `target_business_rpm_min`；`first_failing_step` 指示从哪一级开始不达标。
- `trace_test` verdict：`best_match` + `best_score`（≥0.6 才算有效匹配）；有 `--expect` 时看 `match_expected`。
- 未注册模型（能力注册表无 profile）时 param/压测会被拒绝启动——先注册最小 profile。

## 共同约定

- 所有生成式请求遵守 `config.yaml` 的 `test_cases.minimum_prompt_tokens`（默认 100）下限。
- 报告都在 `$DATA/reports/jobs/<job_id>/`；`job.log` 是运行日志。
