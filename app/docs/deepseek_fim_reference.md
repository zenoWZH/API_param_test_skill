# DeepSeek Pro 0813 FIM 固定因果套件

选择官方 DeepSeek、`deepseek-v4-pro`、`vendor_direct`、`openai_fim_completions_beta` 与
`deepseek_v4_pro_0813_fim_beta` 后，可显式选择 `deepseek_fim_causal_20260908`。
通用参数矩阵保留原十五项；固定套件完整运行八项、仅一次，不额外发送身份／计数请求或自动重试。

| 固定案例 | 必须成立的观察 |
|---|---|
| suffix_42 / suffix_73 | 仅 suffix 改变，拼接后的 Python AST 分别返回且断言 42／73 |
| echo_42 | 非空 suffix 与 echo=true 的组合被明确拒绝；不表示 echo 单独不支持 |
| suffix_invalid_type | 错误明确归因 suffix 字符串类型，并与成功对照配对 |
| stop_baseline / stop_enabled | 基线含 CUT_HERE，添加 stop 后精确返回 LEAD_ |
| echo_false / echo_true | 两者均不发送 suffix；true 原文精确等于 prompt 加 false 原文 |

所有请求体和判读定义来自共享 MPDB Binding 的 `fixed_case_suites`，随任务快照冻结。
固定 endpoint 为 `https://api.deepseek.com/beta/completions`，max_tokens=512、stream=false。
仅解析生成代码的 AST，不执行代码。其协议依据为 [DeepSeek FIM 官方文档](https://api-docs.deepseek.com/api/create-completion/)；
echo/suffix 组合拒绝来自该来源已经保留的八条原厂实证，不扩大为未经验证的 null 或空 suffix 结论。

固定套件报告与通用矩阵按 suite ID 分别查询。新任务独立判读，历史通过不预定新结果。
原始响应保持原样；只有已验证的 suffix-free echo 正例在 token 审计视图中去除回显输入，避免把输入再次算作生成输出。
该视图不宣称完整 token 精确证明。原厂参考报告仍遵循既定 P1M 保留策略。

CLI 从 `app/` 使用以下选择变量调用既有 `scripts/param_test.py`（会发送上述八次请求）：

```bash
LOADTEST_PROVIDER=deepseek_official LOADTEST_MODEL=deepseek-v4-pro \
LOADTEST_API_FORM=openai_fim_completions_beta LOADTEST_ROUTE_PROFILE=vendor_direct \
LOADTEST_REFERENCE_SOURCE=deepseek_v4_pro_0813_fim_beta \
LOADTEST_PARAMETER_SUITE=deepseek_fim_causal_20260908 LOADTEST_PARAM_TEST_RUNS=1 \
uv run --offline --no-project --python ../.venv/bin/python python scripts/param_test.py
```

本次实现复用已有八条原厂证据，没有新增 API 外发。当前套件的语义、传输和 CLI／Web 验收结果按最新执行报告记录。
