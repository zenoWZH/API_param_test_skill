# 测试总指南

这份文档是项目的阅读入口。先根据问题选择测试，不要把“参数能用”“缓存有效”和“高并发稳定”混成一个结论。

## 三类测试分别回答什么

| 测试 | 回答的问题 | 不能单独证明 | 主要结果 |
|---|---|---|---|
| [参数测试](parameter_testing.md) | 这个模型经这条 Route、这种 API Form 调用时，参数是否按契约接受或拒绝，响应值是否正常，身份和 token 上报是否可信？ | 缓存命中率、容量上限、长时间稳定性 | `verdict.json`、`model_identity.json`、逐 profile 结果 |
| [缓存测试](cache_testing.md) | 真实增长会话中是否存在可复用前缀，上游是否如实上报 cached tokens，正负控制是否成立？ | 高并发吞吐、所有参数兼容 | `cache_results.json`、`verdict.json`、`request_records.jsonl` |
| [压测](load_testing.md) | 文本模型在指定并发、RPM/TPM 或持续时间下，成功率、吞吐、延迟、限流和稳定性怎样？ | 单个边界参数的合同完整性、缓存字段真实性；图片/视频容量 | Quick/Staircase/Soak summary、history、Locust 报告 |

推荐执行顺序：

1. 参数测试先确认调用合同、响应语义和模型身份。
2. 缓存测试确认官方 usage 和缓存控制组可信。
3. Quick 用低成本流量验证压测配置。
4. Staircase 寻找满足质量门槛的容量阶梯。
5. Soak 在已知安全并发上验证长期稳定性。

参数测试或身份门禁失败时，不应继续用该组合做容量结论；缓存测试失败不会自动否定普通无缓存吞吐，但不能再宣称缓存有效。
图片与视频只允许参数/身份类测试，不执行 Quick、Staircase、Soak、Locust 或 Cache 压力测试。

## 核心名词

项目采用 Route-first 模型能力结构，选择链路固定为：

```text
Provider → Model → Route Profile → API Form → Reference Contract
```

- **Provider**：提供 base URL、鉴权和可售模型的运行服务商配置，不是 MPDB Source。
- **Source**：官方参考事实的所有者，例如原厂、托管云或平台；由 `source_id` 标识。
- **Model Family**：模型家族，例如 GPT、Gemini、Claude、DeepSeek。`openai` 是 API 兼容标准，不是模型家族。
- **MPDB Profile**：只表示 `modality/source_id/family_id/model_slug` 四级参考身份，不包含 Route 或 API Form。
- **Interface**：属于一个 Profile，承载 API Form、routing mode 和 endpoint/operation 形状。
- **Reference Contract / Test Binding**：前者保存 source-scoped 参数合同，后者把 Test Case 确定性绑定到 Interface。
- **Route Profile**：独立的运行 execution target 规则；可适配 Interface，但不能改变或伪造参考 Source 身份。

同一个 Gemini 模型即使都使用 GenerateContent，AI Studio 和 Vertex 仍是两个 Source，
各自拥有独立 Profile、Interface、Contract 和 Binding，结果不能串用。`dynamic_aggregator`
只是运行 Route；返回的 `model` 字段相同不能把它升级成原厂 Source 证据。

## 五分钟开始

先完成 [App 源码安装与配置](../README.md#安装)。在完整源码仓库根目录可执行以下离线校验，不发送模型请求：

```bash
PYTHONPATH=packages/model-profile-db python -m model_profile_db.cli verify-artifacts
PYTHONPATH=packages/model-profile-db python -m model_profile_db.cli info
```

预期第一条退出 0，第二条输出实际版本、摘要和数量；已有独立安装环境可省略 PYTHONPATH。确认所选 Provider 的 `api_key_env` 已配置，模型/Route/API Form 具备可执行的 MPDB 绑定。以下在 `app/` 目录启动，默认启用登录；配置与报告目录区别见 [App README](../README.md)。

```bash
WEB_CONSOLE_HOST=127.0.0.1 python scripts/web_console.py
```

打开 `http://127.0.0.1:8090/` 后：

1. 选择 Provider 和 Model。
2. 选择 Route Profile；切换 Route 后应重新选择 API Form 和 Reference Contract。
3. 先运行“文字参数测试”或“图片参数测试”。
4. 查看兼容性、token accuracy、returned-model identity 三个独立状态。
5. 参数合同成立后运行 Cache；确认正控 cold 读取为零、warm 出现读取，负控读取为零。
6. 先跑 Quick，再依据目标选择 Staircase 或 Soak。

![LLM Loadtest Console 测试入口与推荐执行顺序](assets/ui/testing-overview.svg)

图中编号对应：① 选择测试标签；② 先用参数测试确认合同和身份；③ 用独立控制组验证缓存；④ 再进入 Quick、Staircase 和 Soak。图为当前控件结构示意，不包含真实密钥和运行数据。

密钥只能放在 `.env` 或已忽略的 `providers.local.yaml` 所引用的环境变量中。不要把密钥写进命令、报告、Job Spec 或提交内容。

## 如何读结果，不被顶层 PASS 误导

### 参数测试

至少分别看：

- `compatibility_pass`：应支持的 profile 成功，应拒绝的 profile 明确拒绝，响应结构和语义校验通过。
- `token_validation_pass`（兼容字段 `token_accuracy_pass`）：所有成功 exchange 都有 input/output usage，且类型、非负、算术、宽松数量级检查和已有 exact 检查均通过。`validation_status=partial` 表示存在媒体、服务端上下文或隐藏思考，尚未发现强矛盾但不能完全本地复算。
- `token_audit_summary.status/coverage`：这是独立精确计数证据；`coverage=0` 仍不能写成 token 已精确证实，但也不会再掩盖缺 usage 或明显离谱的数量。
- `model_identity_pass` 及其 `status`：响应 `model` / `modelVersion` 与请求或显式 alias 一致；Boolean 为 true 但 status 为 `unverifiable` 时，身份仍未得到证明。
- `adapter_pass` 与 `certified_route_contract_pass`：动态聚合即使协议全过，也通常只有 adapter pass。

示例：`54/54 compatibility pass`、`token_validation_pass=true`、所有 returned model 精确一致，但本地 token exact coverage 为 0，应写成“参数兼容、usage 完整性/算术/数量级和可观察身份通过；独立精确 token 计数未证实”，不能简写成“token 精确一致”。

Kimi K3 preserved-thinking 的 `run_success_mode:any` 允许同一配置的一轮真实语义与 token 成功满足该项语义检查。应查看 `pre_aggregation_outcome`、`satisfied_by_sibling_run` 和原始失败记录；聚合通过不能表述为每轮原始请求均成功。协议/token 错误仍会阻断。

### 缓存测试

按以下顺序读：

1. 客户场景请求成功率与会话完成率。
2. `cache_measurement_coverage` 是否足够。
3. 正向控制 cold 的 cached read 是否为零、warm 是否出现读取。
4. 负向控制的 cached read 是否为零；任何正数都不满足当前验收规则。
5. `structural_hit_rate_ceiling`、`actual_cache_hit_rate`、`cache_efficiency` 是否相互合理。
6. `cache_usage_accuracy_status` 是否存在超报或算术矛盾。

延迟变快不能替代官方 cache usage 证据。

### 压测

至少同时看：

- `attempted_business_rpm` 与 `business_rpm`，后者只统计成功业务生成。
- 总 TPM、成功率、429/5xx 比例。
- E2E 延迟；流式 workload 还要看 TTFT/TPOT 覆盖率。
- Staircase 的 `highest_passing_step` 和 `first_failing_step`。
- Soak 首尾窗口吞吐漂移和时间序列。

Quick 中 RPM/TPM 是发送上限；Staircase 中是达标目标，两者含义不同。

## 文档导航

- [参数测试完整说明](parameter_testing.md)
- [缓存测试完整说明](cache_testing.md)
- [压测完整说明](load_testing.md)
- [全部模型家族 Profile 手册](model_profiles/README.md)
- [Route 与供应商目录](model_supplier_route_catalog.md)
- [参数测试架构与代码入口](param_test_architecture.md)
- [图片参数测试专项手册](image_param_test.md)
- [参数任务控制](parameter_job_controls_20260909.md)
- [源码仓库 Fable 5 / 5.1 专用研究矩阵](https://github.com/zenoWZH/api_pressure/blob/main/docs/fable_5_5_1_param_audit_20260912.md)

## 配置或新增模型后的维护

修改 MPDB 模型能力、Reference Contract 或 Test Binding 后运行：

```bash
python scripts/generate_test_docs.py
python scripts/generate_test_docs.py --check
```

第一条生成普通运行投影中的索引与非暂缓家族手册；第二条只检查这些生成结果。Claude、Claude Fable、DeepSeek、GPT 的 App 正文目前暂缓自动同步，需人工单独核对。

`identity_only`、禁用 Interface、研究绑定可能被过滤；检查通过不代表完整 catalog、研究矩阵、手写指南或历史实网证据均已覆盖。Fable 5.1 已登记，但普通参数入口尚未开放，专用研究入口见上方源码审计链接。
