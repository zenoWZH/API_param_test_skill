# Gemini 3.6 Flash 参数 Profile 与供应商对比

日期：2026-07-29

## 选择结论

Google 于 2026-07-21 发布稳定版 `gemini-3.6-flash`。本地配置中有两个供应商声明该模型，因此它是 Gemini 家族当前适合做同模型、同参照源公平对比的最新样本。

官方依据：

- [Gemini 3.6 Flash 模型页](https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash)
- [最新 Gemini 模型与迁移要求](https://ai.google.dev/gemini-api/docs/latest-model)
- [OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)
- [Gemini thinking](https://ai.google.dev/gemini-api/docs/thinking)
- [GenerateContent thinking](https://ai.google.dev/gemini-api/docs/generate-content/thinking)

## 模型级能力合同

`gemini_openai_compat` 保持为 Gemini 家族的跨供应商 comparison source，`gemini-3.6-flash` 模型 profile 负责收窄最新模型的实际合同：

- 支持 `reasoning_effort=minimal|low|medium|high`，分别映射 Gemini 3 Thinking Level；
- `n=2` / `generationConfig.candidateCount=2` 明确作为“不支持多候选”的 negative contract；`n=1` 只是单候选默认形态，不足以证明该能力；
- `temperature`、`top_p`、`top_k` 已被官方弃用并可能被静默忽略，因此不进入压力请求；
- Chat 压力混合流实际选择 medium/high reasoning、JSON、tools 与 stream/usage，不再只跑普通 sampling；
- Native `generateContent` 覆盖 MINIMAL/LOW/MEDIUM/HIGH 四档；medium/high 在请求 `includeThoughts=true` 后必须返回至少一个非空且标记 `thought: true` 的思考摘要。

压力参数过滤与参数探针分开：压力路径移除不应发送的字段；参数测试仍用 family source 加 model expectation 判断 supported / expected rejection，避免为了“压得通”而丢失兼容性证据。

## 已有供应商证据

现有 `mayi_eur / gemini-3.6-flash` 报告为 0 / 24，全部是 HTTP 503。这只能证明当时该通道不可用，不能解释为 24 个参数均不兼容。另一个已配置 3.6 供应商尚无同源历史报告。

较早的 `gemini-3.5-flash` 同源报告中，`mayi_eur` 与 `apihaishi` 均为 24 / 24，证明同一矩阵可跨供应商执行；但这些旧结果不包含 3.6 的四档 reasoning、candidate-count negative contract、弃用 sampling 压力过滤和 Native thought-summary 判定，不能替代新矩阵复测。

本阶段未发起新的外部计费请求。若获得明确授权，应只重跑两个声明 `gemini-3.6-flash` 的供应商，并用同一个 `gemini_openai_compat` source 比较；Native source 仅在双方都暴露 `generateContent` transport 时另做同协议比较。
