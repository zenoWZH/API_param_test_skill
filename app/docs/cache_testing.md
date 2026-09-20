# 缓存测试说明

返回[测试总指南](testing_guide.md)。

## 2026-09-09 当前执行口径

源仓库的 `docs/cache_remaining_20260909.md` 按账号、来源响应、遥测、接口绑定及样例准备列明历史未完成原因；本 skill 的当前状态见[矩阵审查](../../MATRIX_REVIEW_20260919.md)。

按用户最新指令，缓存测试按 **云厂商来源 × 模型系列** 组织；当前以MPDB的family为系列，
必要时保留不同缓存机制的子组。Pro标签型号及Pro运行模式退出缓存测试范围，参数与token核对范围不因此改变。
此处Pro标签按模型名中的独立pro片段识别（例如GPT Pro、Gemini Pro、DeepSeek Pro）；这是测试范围排除，
不表示模型在技术上不支持缓存。现有历史记录保留。

不再要求官方最小前缀、命中比例或其他参考数字才能开始、验收新缓存实验。
默认 `thresholds.cache.control_evaluation=hit_expectations`：先用合成输入隔离首次请求，再完全重复请求作为
应命中正例，替换输入最开头的可控前缀作为不应命中负例。各次保持来源、模型、接口及相关设置一致。
首次/负例应为零读取，正例应有大于零的有效读取；未达到预期保留为未命中/控制不符，不直接推断模型永远不支持缓存。
输入长度是有界实验设计选择，不是官方验收门槛。缺失usage须标为未知；合法入参、来源身份及原始读数仍然需要核对。

默认 `cache_test.exclude_pro=true` 在缓存入口排除Pro型号，并在请求构造时排除Pro模式。
旧数字比例配置只供显式 `legacy_ratios` 历史兼容口径使用，新默认不要求达到50%或任何固定官方比例。


缓存测试验证三件事：真实增长会话是否产生可复用前缀、供应商是否真的上报缓存使用、上报数字是否经得起正负控制和结构上限审计。

“第二次更快”不是缓存证据；只有官方响应 usage 中的 cache token 字段才能产生数值结论。

## 默认场景

正式默认场景是 `progressive_customer_session`：

- 短固定 system。
- 每个独立会话有唯一首部，避免跨会话污染。
- 会话按轮次 append-only 增长。
- assistant、变化 user、真实工具调用和唯一 tool result 都进入历史。
- 所有会话先完成 seed，统一等待后再交错推进。
- 每次运行从首个可缓存语义 token 注入 run nonce，隔离历史运行。

默认基础配置是 10 个会话 × 4 轮，工具在第 3 轮增加 follow-up；再加结构探针、3 组正向控制和 3 个负向请求，约 60 个请求。控制台会在启动前展示精确预算。

客户会话的五个阶段分别是 `seed`、`direct_growth`、`tool_initial`、`tool_followup`、`final_growth`。报告会按阶段拆分理论上限、实际命中率和效率；某一阶段失败不能被其他阶段的高命中率掩盖。

![Cache 测试设置、流程、核心指标和可信度控制界面示意](assets/ui/cache-testing-console.svg)

图中编号对应：① 客户场景与预算设置；② 五个客户会话阶段；③ usage 覆盖、结构上限、实际命中率和效率；④ 正负控制及逐请求 telemetry 审计。示意数值不代表当前供应商实测结果。

`kilocode_agent_session`、`growing_conversation`、`shared_prefix` 是诊断场景，不应与默认客户场景混用口径：

- `kilocode_agent_session`：大 system + 工具 schema + 多步 agent/tool result，观察长轨迹复用。
- `growing_conversation`：逐轮增长对照，便于定位命中单调性。
- `shared_prefix`：固定长前缀的简化实验。

## 四组流量必须分开

| 分组 | 作用 | 是否进入客户命中率 |
|---|---|---|
| 客户会话 | 模拟实际增长对话 | 是 |
| 结构探针 | 独立估算每轮理论可复用前缀 | 否 |
| 正向控制 | 同长前缀 cold→warm，证明缓存机制存在 | 否 |
| 负向控制 | 唯一随机首部，验证不应命中时不会虚报 | 否 |

缺少正向或负向控制时，不能给出“缓存统计可信”的结论。正向控制未命中时记录预期未达成；负向控制命中时核对前缀隔离、入口与统计口径。单组结果不能直接证明模型不支持缓存或供应商统计造假。

## 命中率算法与结构诊断

```text
structural_hit_rate_ceiling
= Σ structurally_cacheable_prefix_tokens / Σ input_tokens

actual_cache_hit_rate
= Σ provider_cached_input_tokens / Σ input_tokens

cache_efficiency
= actual_cache_hit_rate / structural_hit_rate_ceiling
```

分子和分母必须使用同一批成功、且输入与缓存读数完整有效的客户请求：

```text
缓存token命中率 = Σ缓存读取token / Σ总输入token
命中请求比例 = 缓存读取token大于0的请求数 / 有效可观测请求数
测量覆盖率 = 有效可观测请求数 / 成功客户请求数
```

不能平均各请求百分比。例如两请求为90/100和90/900，token命中率是180/1000=18%，不是50%；
命中请求比例则为2/2=100%。冷启动控制、正负控制和结构探针不混入客户命中率，控制组单独统计。
缺失/异常读数不补零、不截断；从可用比例中排除并单列数量与覆盖率。分母为0时显示N/A。

对于原生Claude，完整输入为 `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`，
只有read进入命中分子；OpenAI/Responses的input已包含缓存读取，不能再加一次。

`shared_prefix`和增长会话以前以可复用前缀为分母的比例，现在独立保存为 `prefix_cache_hit_rate`，
其中增长会话使用请求发出前的可复用前缀（上一轮对应输入）。它是复用效率诊断，不能替代总输入token命中率。
没有可靠结构估算时仍能计算实际命中率和判断正负例；结构上限与效率显示N/A。

- `structural_hit_rate_ceiling`：按真实请求结构估算的理论上限，不是供应商上报值。
- `actual_cache_hit_rate`：客户请求中供应商官方上报的 cached input tokens 占比。
- `cache_efficiency`：实际命中相对理论可复用上限的实现程度。

`cache_efficiency > 100%` 不应被截成 100%；报告保留原值并标记 `exceeds_structure`，因为这可能意味着 usage 超报、代理注入污染或结构估算不一致。

另外要看：

- `cache_measurement_coverage`：成功客户请求中有官方 cache 字段的比例。
- `cache_hit_request_ratio`：报告命中的请求比例，不等于 token 命中率。
- `session_completion_ratio`：会话是否真的走完整条轨迹。
- `tool_flow_supported_session_ratio`：工具阶段是否被模型正常支持。
- `cache_usage_accuracy_status`：逐请求和控制组审计结果。

## 官方 usage 的归一化

不同 API 可能在不同位置报告缓存，例如：

- OpenAI 兼容：`usage.prompt_tokens_details.cached_tokens`。
- DeepSeek 风格：`usage.prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`。
- Claude：`cache_creation_input_tokens` / `cache_read_input_tokens`。
- Gemini：按当前 Route 和官方 schema 读取对应 cached content/token 字段。

执行器会统一为 cached/uncached input tokens，但仍保留原始 usage。每条记录检查：

- `0 ≤ cached ≤ input`。
- cached + uncached 与 input 的算术一致性。
- 可复用前缀估算用于独立诊断；不作为官方参考数字前置条件。
- 正控 warm 相比 cold 有增长，且不超过 cold 的输入上限。
- 新口径负控应无缓存读取，正控应有缓存读取；无需官方目标比例。

字段缺失时指标是 N/A，不允许从延迟反推数值。

## Observe 与 Gate

默认 `thresholds.cache.mode=observe`：普通命中率或性能没有达到目标时仍可完成流程，适合先建立基线。

但 observe 不会放过数据造假或自相矛盾。以下情况任何模式都应阻断：

- cached tokens 为负数或超过 input。
- hit/miss 算术不成立。
- 正负控制与各自是否应命中的预期不符。
- 强制控制组缺失。
- 控制组存在明确的 telemetry 矛盾。

如另行启用业务命中率目标，可显式设置 `gate` / `hard_fail`；它是用户自己的目标，不是官方参考数字。当前默认按正负例是否应命中验收，保留实际比例与测量覆盖率。

## 如何运行

推荐从 Web 控制台选择 Provider、Model、Route 和 API Form，再配置会话数、轮数、内容档位和工具阶段：

```bash
python scripts/web_console.py
```

启动前会把所选 Provider/Model/Route/API Form 解析为精确的 MPDB
Profile、Interface 和 `model_test_policy`，并冻结到 Job/运行快照。只有
`pressure_test_enabled=true` 的叶子可以执行；工具阶段还必须使用该叶子批准的
`pressure_profiles`。控制台把这两层分别投影为 `pressure_test_runnable` 和
`cache_tool_runnable`：缺少已批准工具 profile 的叶子仍可用于关闭工具阶段的
渐进会话以及 `growing_conversation` / `shared_prefix`，但不能选择 Kilo 或带工具
阶段；直接提交构造的 Job 也会在创建任务前按同一规则拒绝。执行期间不会重新
读取当前 catalog。正式套件支持
Chat Completions、OpenAI Responses、Claude Messages 与 Gemini GenerateContent；
未知 transport 会在发送请求前失败，不会回退到 Chat Completions。
OpenAI Responses 在 `store=false` 且启用 reasoning 时会请求
`reasoning.encrypted_content`，并在真实多轮场景中原样回放返回的 reasoning item。
通过 `LOADTEST_JOB_SPEC` 重放时只接受 schema 4 且带有效不可变 MPDB schema 1 快照的
`cache_suite` Job；旧 Job 仍可在历史中查看，但不会按当前环境静默重解释并执行。

直接运行当前配置：

```bash
LOADTEST_PROVIDER=<provider> \
LOADTEST_MODEL=<model> \
LOADTEST_ROUTE_PROFILE=<route> \
LOADTEST_API_FORM=<api-form> \
python scripts/run_cache.py
```

复现 Web Job 的不可变配置快照时，输出必须使用新的目录；下面的 `<new-run-id>` 每次都应不同，不能指回原 Job：

先从来源 `job_spec.json` 原样取出 `provider`、`model`、`route_profile`、`api_form`，填入下面四个环境变量。直接执行器仍从环境/当前配置选择目标，Job 快照负责校验该选择；仅提供 Job 路径不会自动切换 Provider 或 Model。

```bash
LOADTEST_PROVIDER=<provider> \
LOADTEST_MODEL=<model> \
LOADTEST_ROUTE_PROFILE=<route> \
LOADTEST_API_FORM=<api-form> \
LOADTEST_JOB_SPEC="$HOME/.config/llm-api-test/reports/jobs/<job_id>/job_spec.json" \
LOADTEST_REPORT_DIR="$HOME/.config/llm-api-test/reports/cache-replays/<new-run-id>" \
python scripts/run_cache.py
```

Runner 会重写输出目录中的 `request_records.jsonl`、`cache_results.json`、进度和 verdict 文件。原 Job 保留为来源快照与历史证据；重放结果位于新目录，其中 verdict 保留冻结的 MPDB 身份/快照。该命令会重新发送真实请求，不是离线查看历史。

`run_cache.py` 没有 argparse `--help`；它读取环境变量、Job Spec 或 `config.yaml`。

## 结果阅读顺序

1. 来源 `job_spec.json`（Web Job/重放时）：确认 model/family/route/API Form/profile 与有效 cache plan；独立 CLI 则核对 `cache_results.json` 中的身份及 `effective_cache_plan`（存在时），并保留运行配置。
2. `cache_results.json`：其中 `summary` 保存客户、结构、正控、负控和分阶段指标。
3. `verdict.json`：observe/gate 状态、阈值失败、telemetry 阻断项和本次冻结的 MPDB 身份/快照。
4. `request_records.jsonl`：逐请求原始 usage、stage、控制组标记和 `cache_token_audit`。
5. 日志：查看工具流失败、等待阶段、路由错误或请求错误。

App 的默认报告根目录是 `~/.config/llm-api-test/reports/`：独立缓存运行写入其中的 `cache/`，Web 任务写入 `jobs/<job_id>/`。可用 `LLM_API_TEST_REPORTS_DIR` 指定报告根目录，或用 `LLM_API_TEST_DATA_DIR` 指定数据目录后取其 `reports/` 子目录。若修改过这些设置，重放示例中的来源路径应替换为实际 Job 路径；`LOADTEST_REPORT_DIR` 直接覆盖本次输出目录。

## 常见结果怎么解释

| 现象 | 优先解释 | 下一步 |
|---|---|---|
| 客户 actual 较高，正控有读取，cold/负控均为 0 | 正负控制符合预期 | 再看 coverage、usage 算术、结构效率和阶段分布 |
| 客户 actual 为零，正控也为零 | 缓存未启用、未达门槛或路由不稳定 | 核对Route、等待时间、缓存参数和前缀隔离；不等待官方门槛数字 |
| 客户 actual 很高，但负控也很高 | 统计污染、隐藏注入或路由聚合异常 | 暂停缓存结论，检查 run nonce、原始 usage 和上游指纹 |
| 正控 cold/warm 不单调 | 缓存未稳定命中或统计不可信 | 低频重放控制组，不要扩大客户请求量 |
| coverage 很低 | 官方字段大面积缺失 | 报告 N/A；不能用成功率或延迟代替 |
| efficiency 超过 100% | 读取量超过结构估算 | 保留 `exceeds_structure` 并核对估算/请求结构；新口径不会仅因此判 usage 造假或硬失败 |
| 工具阶段失败 | 模型工具兼容或 follow-up 结构有问题 | 先回到参数测试修复工具 profile，不伪造缓存 miss |

## 报告措辞模板

```text
客户场景完成率 100%，cache usage 覆盖 100%。
结构命中上限 0.908，实际 cached input 比例 0.564，效率 0.621。
正向控制 cold 为 0、warm 有有效读取，负向控制为 0。
逐请求 cached token 未超过完整 input，hit/miss 算术一致，控制组和 telemetry audit 通过。
```

如果任一控制或 coverage 不成立，应明确写“无法证实”或“统计不可信”，不要写“缓存测试完成，所以支持缓存”。
