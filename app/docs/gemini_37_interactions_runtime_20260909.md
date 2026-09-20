# Gemini 3.7 Interactions App 运行时同步

2026-09-09，批准项17（`R7-APP-PARITY-GEMINI-3.7`）已完成运行时同步及19请求官方App验收。
实际Web→JobSpec→CLI任务completed、退出0；身份和18个参数case全部通过，其中minimal/labels为预期400。
共享MPDB按`bounded_18_profile_runtime_acceptance`启用这18项，压力测试保持关闭。

App 现在具备 `gemini-3.7-flash` 的 18 个既有 Interactions 参数 profile，以及对应的
请求构造、JSON/SSE 解析、身份探针和参数分发。输入保持纯文本，始终发送 `store=false`；
拒绝历史 interaction 引用、后台执行和非文本输出。函数工具 profile 只观察首个响应，
不会执行工具、提交工具结果或建立状态链。参数输出限制仍经过统一出口处理，最低 256，
更大的既有预算继续保留。

无`id`响应通过共享validator判断，精确约束实际AI Studio官方v1beta URL、合同和型号。
无状态文本或受支持的首个函数调用可省略顶层资源ID，函数调用自身ID仍必需；
普通纯文本流只有生命周期ID键均存在且恰为空字符串、帧/文本/usage完整时才能验收输出，
不补造资源身份或resume结论。其它来源、状态、媒体或不完整帧不能借此例外通过。
详见[正式判读边界](../../docs/gemini_interactions_runtime_followup_20260909.md)。

当前默认 Interface
`text/google_ai_studio/gemini/gemini-3.7-flash#gemini-interactions-default`
已在限定参数范围内可执行。App可显式选择Interactions，GenerateContent仍为默认接口。
共享Contract、Interface与两条Test Binding保留原能力及五层研究观察，
只有此精确来源/型号/版本的执行状态、限定认证范围及两条负例预期更新。

保留的 App 差异包括 GenerateContent JSON 预算最低 2048、拒绝 `OFF` 安全设置、
固定参数 suite、不可变 Job 绑定及 App 报告目录。图片 Interactions、Vertex 状态资源和
后台任务没有随本次同步开放。

离线测试覆盖原生请求形状、JSON/SSE 解析、流完成标记、usage、工具观察、输出预算、
实际URL对无状态判读的约束，以及关闭策略/错来源无发送。官方验收使用冻结源码和请求包，
19次请求无重试，输出上限256–4096，P1M报告；原判保留。
完整参数因果效果、其它来源及普通任务完整配置重放不由这18项运行验收替代。

初始运行时同步阶段相关回归 **198 passed**，包含新增运行时测试、输出限制、停止词、已交付 Gemini
Web/Job 路径、既有参数同步、Gemini 3.7 Schema/拒绝预期和不可变参数执行控制。
最新19请求证据、冻结包及验证范围见[App验收记录](../../docs/gemini_interactions_app_acceptance_20260909.md)。
