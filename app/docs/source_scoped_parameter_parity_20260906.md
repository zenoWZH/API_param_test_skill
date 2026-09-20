# 已验证来源的参数测试同步（2026-09-06）

> 2026-09-10 收尾状态：精确 `google_ai_studio / gemini-3.7-flash / gemini_interactions` 已完成身份与 18 项参数的 19 请求 App 验收，参数入口按 `bounded_18_profile_runtime_acceptance` 启用；GenerateContent 仍为默认，图片 Interactions、其它来源及压力能力不由此开放。见[正式验收与制品记录](../../docs/gemini_interactions_app_acceptance_20260909.md)。
> 普通任务完整配置冻结属于先前扩出的工作，不列为本次批准范围的收尾阻碍；已有 MPDB 身份与 JobSpec 控制值的历史快照验证继续保留。缓存证据缺口与云认证按用户最新指示排除，账号或服务拒绝另行记录。下文保留各检查点当时的结果。

## 2026-09-09 参数执行控制补齐

新文字参数任务以 JobSpec schema 5 保存重复次数和工具校验模式；启动、实际 runner 与历史判读均消费冻结值。
App beta/FIM/prefill/cache 固定套件继续只运行一次。控制/历史/固定套件/Gemini 组合 265 项、20 subtests 通过，
另一个实际 formal profile 回放确认环境变化不会改写校验模式。本轮没有新增 API。
该检查点的普通 profile 正文及输入样本仍从当前配置读取，未宣称完整配置可重放；当前收尾范围见页首说明。
见[版本与兼容边界](parameter_job_controls_20260909.md)。


> 下文为 9 月 6 日的历史验收快照，其中 `v1` 只标记当时的请求版本。当前 AI Studio 入口为 `v1beta`。
> 9 月 8 日已补 [Pro0813 beta 五项固定套件](deepseek_beta_reference.md)，Flash beta 保持不可执行；
> 后续 FIM 固定因果套件与 Gemini 3.7 Schema 实际请求判读见[当前执行报告](../../APPROVED_EXECUTION_PROGRESS_20260906.md)。
> 第 20 项现已接入 [Anthropic 九模型缓存固定套件](anthropic_cache_reference.md)：每任务两次输入估算和三次缓存生成，完整请求在创建时冻结。

## 2026-09-09 当前 Web 与请求验收

本次新增 API 请求为零。两个消费者各用自己的公开配置，分别以普通模式和
`parameter_test=True` 对照同一组 124 个已绑定文字用例；两种模式的 body/transport 均一致。
参数模式使用包含 JSON 的固定短提示，以实际经过 JSON Output 输入检查；不包含压力输入填充。

App 的 Gemini 3.7 GenerateContent `/api/param-specs` 返回 26 个 profile，创建任务保存
精确 AI Studio source/Interface/Contract 和 `v1beta` 快照，后续配置修改不改变已保存快照。
该入口验收当时仅通过 Job 对象核验运行次数；后续控制快照修复见本页顶部。未选择的 Chat 接口拒绝入队。
Lite Image 的 GenerateContent 任务保存独立 image Interface 与 `v1beta`，Interactions 仍被拒绝。

验收发现并修复 root/App 的图片任务接口选择问题：过去只读取 `image_plan` 中的声明，
会忽略顶层 `api_form`/`route_profile`，让指定 Interactions 的任务静默使用默认 GenerateContent。
现在两种请求形状都读取显式声明，两处同时出现时必须一致；空值、错误类型、未登记路由及关闭接口
均在任务执行前拒绝。解析不修改原始 payload，固定 Banana 套件仍要求 `no_cross_control`。

本次专项结果：root **37 passed / 154 subtests**；App **66 passed / 102 subtests**。
覆盖 `test_image_plan_explicit_selection.py`、`test_job_spec.py`，以及 App 的
`test_selected_gemini_app_delivery.py`、`test_approved_parameter_parity.py`。
这是选定入口、请求与快照的离线验收，不表示第 16–21 项所有来源或参数已完成 live 认证。

## 2026-09-06 历史验收

本次落实审批第 16/17/18 项中已有精确原厂参数证据的 app 差量。
以下来源与 API Form 各自使用独立 MPDB Contract；本地回归没有发送新 API 请求。

| 来源与模型 | app 参数入口 | 已有证据 |
|---|---|---|
| `deepseek` / `deepseek-v4-pro-0813`，请求 ID `deepseek-v4-pro` | Chat Completions、Responses、Anthropic Messages、FIM beta | `reports/param_tests/deepseek_official_0813_*_20260821/` 的各协议完整矩阵与精确 Binding 重试 |
| `google_ai_studio` / `gemini-3.7-flash` | GenerateContent `v1`，26 个安全文字 profile | `reports/param_tests/gemini_official_37_generate_content_safe_full_20260821/` 及 corrected/JSON 2048 重试 |
| `google_ai_studio` / `gemini-3.1-flash-lite-image` | GenerateContent `v1`，8 个逻辑 profile 展开 21 个顺序 case | `reports/image_param/gemini_official_lite_generate_content_full_20260824/` 与该目录离线 reassessment |

DeepSeek 四个接口现可在控制台分别选择，FIM 默认选择保留。Responses 参数不会因 HTTP 200 自动通过：
须校验 output、usage、固定兼容字段、reasoning、JSON 和工具语义；Anthropic Messages 校验 content、usage
与工具结构。Responses SSE 的 incomplete/failed 末事件保留身份、usage 与 output，避免误报字段缺失。
这些 DeepSeek 专门校验限制到 exact `deepseek_v4_pro_0813_*` Contract 和匹配的 transport。

Gemini 3.7 的四个 JSON profile 在原厂历史重试中需要 2048 token 才完整输出。
app 对 exact `gemini_3_7_flash_generate_content` 合同设置这一有效预算，并保留更大值；
公开共享 profile 中的 128 是其他模型仍可使用的声明值，不是 3.7 JSON 请求的实际发送值。
该安全文字合同只允许精确 3.7 型号及登记 profile，并继续拒绝图片输出、cachedContent 与 BLOCK_NONE。
[Google 3.7 型号页](https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash)仍列文字输出与 low/medium/high，minimal 不支持。

Lite Image 的 14 个图片比例经过 codec 对齐，历史原厂输出如 `1:8 → 352×2928`。
app 现实际应用既有 exact-model 5% 相对比例容差，普通图片用例仍使用 0.5%。
GenerateContent 图片 MIME 取自返回的 inlineData；负例仍须把 400/422 归因于被测字段。
历史小写 `1k` 被接受这一文档偏差继续独立保留。

控制台默认选择 Lite GenerateContent；被 gate 阻止的 Interactions 接口独立标为不可执行，
不会再让有可用 GenerateContent 接口的整个模型消失。
Gemini 3.7 Interactions、Lite Interactions、其他云来源、图片 tools/cache、Vertex 新离线图片矩阵
都没有因本次 app 同步获得运行或认证资格。

2026-09-06 查阅的
[Google AI Studio Lite 型号页](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite-image)
明确 Function calling 与 Caching 不支持。MPDB 已在唯一 AI Studio Lite Profile 与两个 source-local
Contract 中纠正为 unsupported，保留 `live_unverified`；原有两个 8-profile Binding 的 case 列表与执行状态不变，
不授权 tools runner 或工具实测，Vertex 与其他型号没有继承这一 AI Studio 结论。

验证方法：

```bash
cd app
uv run --no-project --python ../.venv/bin/python python -m pytest -q tests --tb=short
```

回归使用两个消费者各自的公开 config 对比 124 个已绑定文字请求，而不是让 app 读取 root config；
另检查坏 envelope、reasoning/工具语义、SSE 末事件、HTTP 错误、来源误绑、控制台接口隔离与历史图片尺寸。
离线结果证明代码行为及历史证据的解释，不能替代本次没有进行的官方 live 复测。

最终目录与 app pin 同步后，全量离线验收为 **495 passed，894 subtests passed**（84.97 秒）。
验收使用公开配置、`LOADTEST_SKIP_DOTENV=1` 和不存在的 `LLM_API_TEST_PROVIDERS_LOCAL` 路径，
移除供应商凭据环境变量并保留正常系统环境；本地 loopback fake API 同时通过。
日志位于仓库 `reports/approved_execution_20260906/app_full_offline_acceptance_verified_20260906.log`。
该历史验收修复了通用校验入口重构后的既有 FIM helper 引用；当时两条 DeepSeek Prefix 合同仅供目录识别，
App 尚无该 transport。此项历史限制已由上方 9 月 8 日 Pro 五项接入部分取代，压力入口仍关闭。
