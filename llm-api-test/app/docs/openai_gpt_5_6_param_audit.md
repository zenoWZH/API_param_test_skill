# GPT-5.6 Sol 参数 Profile 与供应商对比

日期：2026-07-29

OpenAI 官方将 [`gpt-5.6-sol`](https://developers.openai.com/api/docs/models/gpt-5.6-sol) 定义为 GPT-5.6 家族旗舰模型；[`gpt-5.6` 模型指南](https://developers.openai.com/api/docs/guides/model-guidance?model=gpt-5.6)列出 `none|low|medium|high|xhigh|max` 六档 reasoning，并建议 reasoning、tools 和多轮工作优先走 Responses API。GPT-5.6 默认 effort 为 `medium`，所以 Chat Completions function tools 必须显式使用 `reasoning_effort=none`，不能用“省略字段”代替官方基线。

本次新增两套模型专属参照源：

- `openai_gpt56_chat` 作为跨供应商主对比矩阵，覆盖六档 effort、stream/usage、JSON、tool follow-up，以及 non-default temperature/stop 的预期拒绝；
- `openai_gpt56_responses` 作为原生能力矩阵，额外覆盖 `reasoning.context`、Pro mode、显式 prompt caching 和 `text.verbosity`；
- `gpt-5.6-sol` 模型 profile 明确记录支持/不支持项，并分别为 Chat 与 Responses 选择压力安全参数；
- 通用吞吐、Cache 与 Locust 压力请求会按 transport 把 `max_tokens` 改写为 `max_completion_tokens` 或 `max_output_tokens`，同时使用 Chat `reasoning_effort=none` 或 Responses `reasoning.effort=low`。

现有旧矩阵已经显示供应商差异：Bigsnake 与 Nyue 的 Chat tools 为 200；Mayi、NuwaFlux 与 PowerAPI 在同一旧 `openai_gpt5_chat` tools profile 上返回 400。Mayi 的旧 Responses 基础矩阵为 8/8。OpenAI Official 的旧报告使用的是通用 `openai_chat_base`，其 sampling、stop 与 reasoning+tools 拒绝说明该基线不适合直接和代理成绩排名。

上述报告均早于本次专属矩阵：旧 Chat tools 省略了 effort，新矩阵改为官方要求的显式 `none`；旧 Responses 也未测试 GPT-5.6 新能力。因此历史结果只能证明测试体系可以发现通道差异，不能冒充新矩阵成绩。本阶段未发起外部计费请求；获得明确授权后，应只重跑声明 `gpt-5.6-sol` 的供应商，并分别固定 `openai_gpt56_chat` 与可用的 `openai_gpt56_responses`。
