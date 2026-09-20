# Claude 4.5 固定前缀验证

App 从共享 MPDB 的 `parameter/claude_native_messages.fixed_case_suites` 读取三个独立套件。
在参数测试页选择 `anthropic_official`、下列精确日期模型、原生 `anthropic_messages` 和 `vendor_direct`，
再明确选择“Claude 4.5 prefill / stop / 类型固定对照”。默认仍是原来的 30 项通用参数矩阵。

| 模型 | 固定套件 ID |
| --- | --- |
| `claude-haiku-4-5-20251001` | `anthropic_prefill_claude_haiku_4_5_20251001_20260908` |
| `claude-opus-4-5-20251101` | `anthropic_prefill_claude_opus_4_5_20251101_20260908` |
| `claude-sonnet-4-5-20250929` | `anthropic_prefill_claude_sonnet_4_5_20250929_20260908` |

每次完整套件为三次顺序请求：assistant 前缀 JSON 正例、只增加 `stop_sequences` 的对照、
只把最后 assistant `content` 改为整数的类型负例。固定 `max_tokens=2048`、`thinking.type=disabled`、
`stream=false`、一次运行、无工具、无重试、无额外身份或计数请求。
CLI 可使用相同环境选择，例如在 App 目录中：

```bash
LOADTEST_PROVIDER=anthropic_official \
LOADTEST_MODEL=claude-haiku-4-5-20251001 \
LOADTEST_API_FORM=anthropic_messages \
LOADTEST_ROUTE_PROFILE=vendor_direct \
LOADTEST_PARAMETER_SUITE=anthropic_prefill_claude_haiku_4_5_20251001_20260908 \
LOADTEST_PARAM_TEST_RUNS=1 \
python scripts/param_test.py
```

凭据沿用现有安全配置。唯一外发地址为 `https://api.anthropic.com/v1/messages`，认证为原生 Anthropic，
API 版本 `2023-06-01`。当前配置的输出下限若高于 2048，会在任何请求前拒绝，避免改变固定载荷。
任务快照保存精确来源、模型、接口、Contract、Binding、套件及请求哈希；执行消费该不可变快照。
已有发送记录的目录不能自动继续或重跑。鉴权、额度、限流、重定向、服务器及传输异常会停止本批次。

每次新结果独立判读。正例需要精确返回模型、原生消息格式和用量，以及前缀拼装后的预期 JSON；
stop 需要本次成功正例、精确 `LEAD_` 续写和对应停止元数据；负例需要本次成功正例和归因于
`messages[1].content` 的原生类型错误。HTTP 200 或历史通过本身均不足以判定成功。
语义兼容、现有 token 审计和最终结果分别保留，原始续写不会替换成拼装后的 JSON。

共享参照来自已保存的九次原厂请求。原始参考报告沿用批准的 UTC 日历月 `P1M` 保留策略；
App 消费永久套件及哈希，不依赖读取到期原始报告。该套件不证明完整参数矩阵、独立计费精确性、
缓存因果关系或 thinking 开关效果，也不适用于模型别名、第三方线路、Claude 4.6+ 或 Fable。
官方边界见 [Claude 提示最佳实践](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)。
