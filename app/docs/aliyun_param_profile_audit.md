# Aliyun 风格文本参数 Profile 核对与实测

日期：2026-07-23

## 范围

本次以第三方 Aliyun 资源入口 `callxyq_k3` 的 `/v1/models` 为被测范围。该入口返回 9 个文本模型：

- `qwen3.7-max`
- `glm-5`、`glm-5.1`、`glm-5.2`
- `kimi-k2.6`、`kimi-k2.7-code`
- `deepseek-v3.2`、`deepseek-v4-flash`、`deepseek-v4-pro`

阿里云官方入口的 `/models` 返回 229 个混合资源 ID，包含文本、图片、语音、Realtime 及内部资源。参数 profile 不把这些非 Chat Completions 资源误算为“可用文本模型”，而是对第三方 9 个模型逐一检查官方同名模型，并按官方参数语义归并为 6 个系列。

所有凭据只保存在 gitignored 的 `providers.local.yaml`，未写入本报告、公开配置、请求报告或 Job Spec。

## 官方资料与系列划分

主要依据：

- [OpenAI Chat Completions 兼容接口](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)
- [文本生成模型能力表](https://help.aliyun.com/zh/model-studio/text-generation-model)
- [DeepSeek](https://help.aliyun.com/zh/model-studio/deepseek-api)
- [GLM](https://help.aliyun.com/zh/model-studio/glm)
- [Kimi](https://help.aliyun.com/zh/model-studio/kimi-api)

| 系列 | 模型 | Reference Source | 关键约束 |
|---|---|---|---|
| Qwen 3.7 | `qwen3.7-max` | `qwen_openai_compat` | 复用已有 Qwen 官方矩阵，包含 `n`、`logprobs` 等响应语义检查 |
| GLM 5.x | `glm-5`、`glm-5.1`、`glm-5.2` | `aliyun_glm5_openai_compat` | thinking、`reasoning_effort=high|max`、`top_k`、JSON、tools、`tool_stream` |
| Kimi K2.6 | `kimi-k2.6` | `aliyun_kimi_k2_6_openai_compat` | thinking 可开关、支持 `preserve_thinking`；不测未支持的 `top_k`、`n`、structured output |
| Kimi K2.7 Code | `kimi-k2.7-code` | `aliyun_kimi_k2_7_code_openai_compat` | thinking-only，因此不生成关闭 thinking 的用例 |
| DeepSeek V3.2 | `deepseek-v3.2` | `aliyun_deepseek_v3_2_openai_compat` | thinking 可开关；不加入 V4 专属 reasoning effort |
| DeepSeek V4 | `deepseek-v4-flash`、`deepseek-v4-pro` | `aliyun_deepseek_v4_openai_compat` | 在 V3.2 矩阵上增加 `reasoning_effort=high|max` |

## 可用性与参数实测

基线请求对 9 个第三方模型均返回 HTTP 200，响应 `model` 与请求模型名一致。官方入口对同名 Qwen、GLM、Kimi 共 6 个模型返回 HTTP 200；3 个 DeepSeek 均因当前官方 Key 的 API-Key restriction 返回 HTTP 403，因此不能把这三项解释为官方参数不兼容。

完整参数矩阵按参数语义系列选择代表模型执行；同系列其余模型已分别通过基线和模型身份检查。

| 被测端 | 系列代表 | 通过 / 总数 | 结论 |
|---|---|---:|---|
| 第三方 | Qwen `qwen3.7-max` | 23 / 25 | `n=2` 只返回 1 个 choice；请求 `logprobs` 后响应仍为 null |
| 第三方 | GLM `glm-5.2` | 16 / 17 | `json_object` 返回 Markdown fenced JSON，严格 JSON 解析失败 |
| 第三方 | Kimi `kimi-k2.6` | 12 / 12 | 通过 |
| 第三方 | Kimi Code `kimi-k2.7-code` | 11 / 11 | 通过 |
| 第三方 | DeepSeek `deepseek-v3.2` | 10 / 10 | 通过 |
| 第三方 | DeepSeek V4 `deepseek-v4-pro` | 12 / 12 | 通过 |
| 官方 | Qwen `qwen3.7-max` | 25 / 25 | 通过，证明第三方的 `n` / `logprobs` 差异不是该模型的官方限制 |
| 官方 | GLM `glm-5.2` | 17 / 17 | 通过，证明 fenced JSON 是第三方响应语义差异 |
| 官方 | Kimi `kimi-k2.6` | 12 / 12 | 通过 |
| 官方 | Kimi Code `kimi-k2.7-code` | 11 / 11 | 通过 |
| 官方 | DeepSeek 三个同名模型 | 0 / 3 baseline | 当前 Key 权限 403，未执行参数矩阵 |

第三方完整系列矩阵合计 **84 / 87 通过、3 项 incompatible、0 项请求失败**。官方当前可授权的四类矩阵合计 **65 / 65 通过**。所有成功矩阵的响应模型身份检查均为 `match`；该检查基于可观察响应字段，只能发现明显错路由或漂移，不能证明网关背后的物理模型。

### 2026-07-29 Qwen 3.7 语义补强

`qwen_openai_compat` 继续选择多个已配置供应商共有的最新模型 `qwen3.7-max` 作为公平样本，并新增以下判定：

- thinking 开启及 `thinking_budget` 用例必须返回非空 `reasoning_content`；
- thinking 关闭用例不得返回非空 `reasoning_content`；
- Preserved Thinking 使用带历史 assistant `reasoning_content` 的固定三轮输入，并验证最终答案确实恢复历史中未直接展示的两个数字；
- `mixed_compat` 压力请求会实际抽到 thinking 开启和 `thinking_budget`，而非只压 thinking 关闭路径。

现有同源历史报告已经能识别供应商差异：阿里云官方为 25 / 25；`callxyq_k3` 为 23 / 25，分别静默忽略 `n=2` 与 `logprobs` 响应语义；`xinglian` 的 `tool_stream` 返回未正确合并的分片 arguments；`oxoapi` 的 streaming/thinking/tool-stream 路径出现 chunked 传输中断。后两份历史报告中的 `qwen_response_format` 失败属于当时测试输入未包含 JSON 关键字的本地构建错误，不计作供应商参数差异。上述报告早于本次 reasoning/Preserved Thinking 响应判定，因此新矩阵分数需在获得明确的外部调用授权后重跑，不能用旧分数冒充新结论。

本地详细报告：

- `reports/param_tests/callxyq_k3/*-aliyun-profile/verdict.json`
- `reports/param_tests/aliyun_maas/qwen3.7-max-aliyun-profile/verdict.json`
- `reports/param_tests/aliyun_maas/glm-5.2-aliyun-profile/verdict.json`
- `reports/param_tests/aliyun_maas/kimi-k2.6/verdict.json`
- `reports/param_tests/aliyun_maas/kimi-k2.7-code/verdict.json`

## 实现结果

- Provider 可通过 `models.reference_sources.<model>` 选择模型级默认 Reference Source；CLI、控制台配置、参数规格、Job 创建与历史恢复使用同一解析逻辑。
- 新增 18 个可复用的 `aliyun_*` compatibility profiles，以及 GLM、两类 Kimi、两类 DeepSeek 共 5 个官方 Reference Sources；Qwen 复用已有官方来源。
- 参数对照元数据可明确返回 `official: unsupported`，不会再把 Kimi/DeepSeek 文档明确不支持的参数显示成 supported。
- 两个本地 Provider 都配置了全部 9 个候选模型及对应 Reference Source；私有端点和凭据继续只存在本地覆盖文件中。
