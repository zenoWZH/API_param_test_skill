# DeepSeek Pro 0813 beta 固定参数套件

批准范围为 `R7-APP-PARITY-DEEPSEEK-0813`。选择 `deepseek_official` → `deepseek-v4-pro` → `vendor_direct` → `deepseek_beta_chat_prefix` → `deepseek_v4_pro_0813_chat_prefix_beta`，可执行五项固定验证。Web 固定一次，CLI 未指定次数时也使用一次；显式重复次数、压力或缓存任务会被拒绝。

请求体从共享 MPDB 的 Parameter Binding `case_definitions` 读取，并随任务快照冻结。接口为 `https://api.deepseek.com/beta/chat/completions`，输出上限 512，thinking disabled，stream false。执行器按顺序发送，不随机替换输入，不额外发送身份／计数请求，不自动重试或执行工具。已有执行账本的目录不能再次启动本套件。

| 用例 | 观察与判定 |
|---|---|
| `deepseek_beta_prefix_matrix_false_control` | prefix=false，完整 JSON 必须符合固定目标 |
| `deepseek_beta_prefix_matrix_true_json` | 分别记录前缀续写格式与严格尾串；格式成立而尾串不符仍为严格失败 |
| `deepseek_beta_prefix_matrix_invalid_type` | 结合正例和唯一字段变化，验证 messages 中 prefix 的布尔类型拒绝 |
| `deepseek_beta_prefix_stop_followup_baseline` | 完整 JSON 正确且实际内容含 stop 字面量 |
| `deepseek_beta_prefix_stop_followup_enabled` | 与上一条相同输入配对，返回内容必须精确截止于 stop 之前 |

旧严格尾串失败与旧 stop 证据不足仍保留在共享来源观察中。Flash beta 不属于本 App 批准型号范围。此套件只覆盖上述五项；它不代表所有 beta 参数已验证。

报告分别保存完整执行账本、`beta_observations.json`、普通参数结果与 token 审计。原生 usage 算术检查与 token 精确证明是不同结论，既有 App token 验证门继续生效。传输未完成，即使已接收到可解析 JSON，也不能算成功。

本次接入已通过无网络的固定载荷、源／地址／认证、输出下限、重复执行阻止、异常停止、CLI 和 Web 任务快照测试；没有为 App 接入新增 API 请求。已有原厂参照来自共享 Interface 的 `bounded_prefix_observations_20260908`，不读取到期原始报告，也不在 App 复制另一套参数数据库。
