# Claude 5 参数 Profile 与供应商对比

日期：2026-07-29

`claude-opus-5` 同时存在于 Anthropic 官方、Bedrock 兼容入口及多个代理供应商，是 Claude 家族当前适合做同模型比较的最新样本。官方 [Effort](https://platform.claude.com/docs/en/build-with-claude/effort) 与 [Adaptive thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking) 文档表明，Opus 5 支持 adaptive thinking 和 `low|medium|high|xhigh|max` 五档 effort；手动 `thinking.enabled + budget_tokens` 不再适用。Sonnet 5 的[模型变更说明](https://platform.claude.com/docs/en/about-claude/models/whats-new-sonnet-5)同时明确，非默认 sampling 与手动 thinking budget 返回 400。

本次模型合同更新：

- `claude_native_messages` 新增五档 `output_config.effort`；
- Opus 5 与 Sonnet 5 将 `claude_native_top_p`、`claude_native_thinking_budget` 标为 unsupported，HTTP 400 应判 expected rejection；
- 两个 Claude 5 模型的压力路径不再选择 temperature/top_p 或手动 budget，而选择 adaptive thinking、medium effort、tools、stream 与 system；
- 通用吞吐 profile 中遗留的 `temperature=0` 会被 Claude 5 模型 profile 精确移除。

现有同源报告已经区分“模型限制”和“供应商差异”：Bedrock 与官方入口上的 Sonnet 5 都是 10 / 12，且两项失败恰好是 top_p 与手动 budget；按新合同这两项应转为预期拒绝。Opus 5 Bedrock 同样仅有这两项拒绝，而 Opus 5 官方入口还额外在 `claude_native_stream` 返回 400，因此 stream 才是需要复测的通道差异。

这些报告早于五档 effort 矩阵，不能作为新矩阵成绩。本阶段未发起外部计费请求；获得明确授权后，应只重跑声明 `claude-opus-5` 的供应商，并固定使用 `claude_native_messages`。
