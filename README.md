# API_param_test_skill

将 [yibuapi-llm-loadtest](../yibuapi-llm-loadtest)（多供应商 LLM 参数/缓存/压测控制台）改造为 openclaw skill `llm-api-test`。

## 目标

- openclaw 可 host 起 web console，用户在浏览器中操作。
- 用户可用自然语言下令：对某 provider + model 跑参数测试 / 缓存测试 / 图片参数测试 / 压测。
- 内置供应商准入工作流（mermaid 状态机），可从头执行或中途接手。
- 测试产物（verdict/summary JSON）由 agent 读取并总结报告。

## 目录规划

```text
llm-api-test/          # skill 包（待构建）
├── SKILL.md           # agent 操作指令
├── app/               # vendored 原仓库代码 + trace_test.py + workflow.yaml
├── scripts/           # setup.sh / console.sh / run_test.py / jobs.py / result.py / workflow.py
└── references/        # 测试指南 + 供应商准入工作流文档
```

数据目录（运行时生成，不进 skill 包）：`~/.config/llm-api-test/`（密钥、报告、工作流实例状态）。

## 计划

完整实施计划见 [.kilo/plans/1785982927567-openclaw-llm-api-test-skill.md](.kilo/plans/1785982927567-openclaw-llm-api-test-skill.md)。
