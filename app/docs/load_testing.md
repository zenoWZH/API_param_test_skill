# 压测说明

返回[测试总指南](testing_guide.md)。

压测用于回答容量和稳定性问题。运行前必须先确认模型、Route、API Form 与 model profile 已注册；压测请求会应用该组合的参数约束，但不会替代完整参数矩阵。

压测只适用于 `text` 模态。所有 `image` 和 `video` 模型永久排除在 Quick、Staircase、Soak、Locust 与 Cache 压力流量之外；Profile、Provider overlay、手工 Job Spec 或前端缺失字段都不能重新开启。图片参数矩阵仍可按用例顺序执行真实请求，但它不是并发、速率或持续时间压力测试，并会拒绝压力形状字段。

## 选择运行模式

| 模式 | 目的 | 典型时长 | 主要结论 |
|---|---|---:|---|
| Smoke 套件 | 遍历吞吐与兼容性 profile，并运行完整缓存套件 | 取决于模型和全部用例 | 各类请求及缓存控制是否通过 |
| Quick | 固定并发或 RPM/TPM cap 下快速观察 | 1–10 分钟 | 当前设置是否安全、配置是否合理 |
| Staircase | 逐阶提高并发直到达到目标或质量失败 | 每阶数分钟 | 最高合格阶梯和目标能否达到 |
| Streaming | 等长流式请求测 TTFT/TPOT/E2E | 视样本量 | 首 token 与持续生成体验 |
| Soak | 在已知安全并发长期运行 | 默认 1 小时 | 稳态成功率、限流、延迟和吞吐漂移 |

推荐顺序是 Smoke → Quick → Staircase → Soak。不要直接用高并发 Soak 探索未知容量。

![Quick、Staircase、Soak 设置和压测结果界面示意](assets/ui/load-testing-console.svg)

图中编号对应：① Quick 的速率上限；② Staircase 的并发阶梯与达标目标；③ Soak 的长期稳定性计划；④ 吞吐、质量、延迟和趋势结果。示意数值不代表当前供应商容量。

## Workload 预设

| Workload | 请求分布 | 适用问题 |
|---|---|---|
| `throughput_rpm` | 8 种固定请求体的短任务混合，要求 `fixed` | 验证固定工作负载的 RPM 与调度 |
| `throughput_balanced` | 短、中、约 8k 和较长上下文混合 | 综合业务基线 |
| `throughput_tpm` | 长上下文权重更高，超限项按模型上下文过滤 | 提高 TPM、观察长输入能力 |
| `throughput_streaming` | 三个近似等长流式请求 | TTFT、TPOT、E2E 延迟 |
| `throughput` | 项目历史混合比例 | 与旧结果连续比较 |
| `mixed_compat` | 多种兼容性 profile 混合 | 诊断接口行为，不是容量基准 |

Staircase 和 Soak 只接受确定性的 `throughput*`，拒绝 `mixed_compat`。`mixed_compat` 中不同 profile 成本差异大，不能把其 business RPM 当作稳定容量。

## 请求模式与缓存污染

直接 Locust 的默认模式是 `request_mode=unique`：在首个 user 内容前注入 nonce，降低 Provider prompt cache 对容量的影响。选用 `throughput_rpm` 时必须显式设置 `LOADTEST_REQUEST_MODE=fixed`；Web 选择该 workload 且未指定模式时会使用 `fixed`。

`fixed` 也是标准 RPM 工作负载的请求模式，不能只把它理解为缓存实验。报告须披露 workload 和请求模式；固定请求可能命中缓存，其吞吐不等同于变化请求的吞吐。

Staircase/Soak 的默认 warmup 使用 `throughput_rpm`，执行器只为这个预热子进程设置 `fixed`。测量阶段保留调用方的请求模式；若测量 workload 也是 `throughput_rpm`，仍须选择 `fixed`，不兼容的 `unique` 会被拒绝。

缓存机制本身应由[缓存测试](cache_testing.md)用正负控制验证，不要从压测延迟推断缓存。

## Quick 与 Staircase 的 RPM/TPM 含义不同

- **Quick**：`target_rpm` / `target_tpm` 是发送速率上限。成功和失败的尝试都会消耗 RPM 预算；TPM 先按请求估算预留，返回后再用实际 usage 校正。0 表示不限。
- **Staircase**：`target_rpm` / `target_tpm` 是达标目标，不会限制 Locust 子进程的发送速率。每阶用实际结果判断是否达到目标。

当 RPM 和 TPM 都大于 0 时，系统按 `TPM / RPM` 计算平均 token 目标，并用 0.5x / 1.0x / 1.5x 三档混合请求。实际 usage 会校正估算。请求仍受模型上下文窗口 95% 安全边界限制。

Quick 的 RPM 调度使用 Locust `constant_throughput`：每个 user 的目标速率为 `RPM / (60 × users)`。这是等待响应后继续发送的闭环调度；响应变慢或并发不足时，实际发送量可以低于 cap。

Web 中 RPM 大于 0 时，`duration` 表示测量窗口，启动时另加一轮预热（`60 × users / RPM` 秒），窗口结束后等待在途请求，最长为任务的 timeout。启动速率须满足 `spawn_rate ≥ RPM / 60`。CLI 若需要相同口径，必须同时设置 users、预热/测量窗口和 `--stop-timeout`，见下面的完整示例。

## Profile 如何影响压测请求

每条压力路径先解析：

```text
model → family → route → API Form → model profile → request
```

- 未注册模型、Route、当前 Route 下的 API Form 或 model profile 会在启动前失败。
- 只有 `modality=text` 且 runtime Profile 明确 `pressure_test_enabled=true` 时，压力任务才可运行；image/video 及其它状态全部 fail closed。
- `pressure_profiles` 决定 `mixed_compat` 可选择的场景。
- `pressure_omit_params` 删除模型不支持或高风险的参数。
- `pressure_parameter_aliases` 把通用字段改为该模型的字段名。
- `pressure_overrides` / `pressure_transport_overrides` 固定该模型或协议需要的值。

这些保护只保证压测请求符合已知合同，不会把参数测试的错误参数带进业务流量。完整参数边界仍需先跑[参数测试](parameter_testing.md)。

## 指标口径

### 吞吐

- `attempted_business_rpm`：所有业务请求尝试，包括失败。
- `business_rpm`：成功完成的业务生成请求；排除 `/models`、warmup、retry、cache suite 和兼容性控制流量。
- `total_tpm`：根据响应 usage 汇总的总 token 吞吐；usage 缺失时覆盖率必须同时报告。

目标容量应使用 business RPM，不要用 attempted business RPM 掩盖大量失败。

### 质量

- 请求成功率。
- 429 占比和 5xx 占比。
- P50/P90/P95/P99 E2E 延迟。
- 流式请求的 TTFT、TPOT 及各自 coverage。
- 响应结构、空内容、工具流等业务失败分类。

### 稳定性

Soak 使用 `history.jsonl` 的非 warmup bucket 比较首尾窗口：

```text
business_rpm_drift
= abs(last_window_avg - first_window_avg) / first_window_avg
```

同时观察成功率、429/5xx、延迟是否随时间恶化。只有总平均值正常而后半程退化，也应判为稳定性风险。

## Staircase 怎么判

每个阶梯同时检查：

1. 成功率、延迟、429/5xx 等质量门槛。
2. `business_rpm ≥ target_business_rpm_min`。
3. 若配置 TPM 目标，`total_tpm ≥ target_total_tpm_min`。

至少一个阶梯同时满足质量和目标时，整体可通过。之后更高阶发生饱和不会推翻已经证明的合格阶梯，但报告必须保留：

- `highest_passing_step`。
- `first_failing_step`。
- 最大合格 RPM/TPM。
- 峰值尝试 RPM/TPM。
- 每阶质量失败原因。

一旦某阶质量失败，执行器停止继续扩阶。若所有已配置阶梯质量正常但尚未达目标，`auto_extend` 可按增量扩到 `max_users`。

## 如何运行

### Web 控制台

```bash
python scripts/web_console.py
```

Web 会把 Provider、Model、Route、API Form、workload、门槛和 Quick/Staircase/Soak plan 写入无密钥 `job_spec.json`。Runner 只执行该快照，便于恢复和复现。

### CLI

完整冒烟套件：

```bash
LOADTEST_PROVIDER=<provider> LOADTEST_MODEL=<model> python scripts/smoke_test.py
```

这不是单请求 preflight。当前公共配置会遍历 18 个 `throughput_profiles`，再遍历所选模型批准的兼容性 profile（工具用例可能有 follow-up），最后执行完整缓存套件。默认缓存部分的计划为 60 次请求；总执行量还包含模型列表请求、前面的生成及可能的 token 核对请求，失败或预算中断时实际数量会变化。运行前应确认这些范围都符合本次计划。

缓存子套件仍会应用 Pro 排除、工具 profile 和控制组门槛，因此普通生成可用不代表整个 Smoke 通过。Smoke 主结果在默认报告根目录的 `smoke/` 下（可由 `LOADTEST_REPORT_DIR` 修改），其缓存部分另写该根目录的 `cache/`，不随 Smoke 的输出覆盖项移动。

直接 Locust Quick：

```bash
LOADTEST_PROVIDER=<provider> \
LOADTEST_MODEL=<model> \
LOADTEST_ROUTE_PROFILE=<route> \
LOADTEST_API_FORM=<api-form> \
LOADTEST_WORKLOAD=throughput_balanced \
LOADTEST_REQUEST_MODE=unique \
LOADTEST_REPORT_DIR=reports/quick/balanced-example \
locust -f locustfile.py --headless -u 10 -r 2 -t 2m \
  --csv=reports/quick/balanced-example/locust \
  --html=reports/quick/balanced-example/report.html
```

带 RPM cap 和明确测量窗口的 Quick（每次运行使用新的输出目录）：

```bash
LOADTEST_PROVIDER=<provider> \
LOADTEST_MODEL=<model> \
LOADTEST_ROUTE_PROFILE=<route> \
LOADTEST_API_FORM=<api-form> \
LOADTEST_WORKLOAD=throughput_rpm \
LOADTEST_REQUEST_MODE=fixed \
LOADTEST_USERS=10 \
LOADTEST_TARGET_RPM=60 \
LOADTEST_TARGET_TPM=0 \
LOADTEST_WARMUP_SEC=10 \
LOADTEST_MEASURE_DURATION_SEC=120 \
LOADTEST_REPORT_DIR=reports/quick/rpm-60-example \
locust -f locustfile.py --headless -u 10 -r 2 -t 130s \
  --stop-timeout=120 \
  --csv=reports/quick/rpm-60-example/locust \
  --html=reports/quick/rpm-60-example/report.html
```

这里计划在 120 秒测量窗口内发送 120 次请求；预热另计 10 秒，窗口关闭后最多等待 120 秒完成在途请求。设置正 RPM 时，`LOADTEST_USERS` 必须为正且与 `-u` 一致，仅写 `-u` 不够。该例关闭 TPM cap；若另设 TPM，请同时披露自适应请求长度。

Staircase 与 Soak：

```bash
LOADTEST_PROVIDER=<provider> LOADTEST_MODEL=<model> python scripts/run_staircase.py
LOADTEST_PROVIDER=<provider> LOADTEST_MODEL=<model> python scripts/run_soak.py
```

这些 runner 读取 `config.yaml`、环境变量或 `LOADTEST_JOB_SPEC`，没有 argparse `--help`。

## 结果文件

App 的默认报告根目录是 `~/.config/llm-api-test/reports/`；设置 `LLM_API_TEST_REPORTS_DIR` 可直接指定根目录，或用 `LLM_API_TEST_DATA_DIR` 指定数据目录后取其 `reports/` 子目录。直接运行 Locust 使用下表单独列出的默认路径。

| 模式 | 默认目录 | 首先查看 |
|---|---|---|
| Quick | 独立 CLI 默认 `reports/locust/`；可设 `LOADTEST_REPORT_DIR` | Web 的 `load_result.json`、`request_records.jsonl`、Locust HTML/CSV |
| Staircase | 默认报告根目录的 `staircase/` | `verdict.json`、`staircase_progress.json`、各 step 的 measure 目录 |
| Soak | 默认报告根目录的 `soak_1h/` | `verdict.json`、run summary、`history.jsonl`、Locust HTML |

Web Job 统一写在默认报告根目录的 `jobs/<job_id>/`，应先读 `job_spec.json` 确认本次实际配置，再读 verdict/summary，而不是根据页面记忆猜测。

带 RPM cap 且设置测量窗口时还应读取 `load_window.json` 和 `request_attempts.jsonl`。只有 HTML/CSV 改了路径时，项目自己的 JSONL 仍使用 `LOADTEST_REPORT_DIR` 或上述默认目录。

| 窗口字段 | 含义 |
|---|---|
| `planned_start_count` | RPM × 测量秒数 / 60 的计划发送量 |
| `started_request_count` | 实际进入发送的请求数；用于 attempted RPM |
| `completed_request_count` | 已完成的请求数，包含失败 |
| `successful_request_count` | 成功完成的请求数；用于 business RPM |
| `unfinished_after_drain_count` | 等待在途请求结束后仍未完成的数量 |
| `unscheduled_count` | 计划量减去实际发送量的正差 |

同时核对 `admission_conservation_ok` 和 `completion_conservation_ok`：已准入请求须能分解为已发送、发送前取消及未解决项，已发送请求须能分解为已完成及仍未完成项。进程结束本身不能证明计划量已发送或请求均已完成。

## 停止和安全边界

- 先用 `/v1/models` 和最小真实请求确认鉴权与模型可达，但 `/models` 声明本身不是可用性证明。
- 参数或 identity mismatch 未解决时停止容量结论。
- Staircase 某阶出现质量失败后停止扩阶。
- 大量 429/5xx 时先降低频率定位供应商限制，不用高频重试制造更多噪声。
- 超过模型上下文安全边界的静态 profile 会被过滤；若目标 TPM 依赖这些被过滤项，应调整计划。
- 密钥不进入 Job Spec、命令参数、报告或提交。

## 常见误读

| 误读 | 正确解释 |
|---|---|
| attempted business RPM 达标，所以容量达标 | 必须看成功的 business RPM 和质量门槛 |
| Quick 的 target RPM 是目标 | Quick 中它是 cap；Staircase 中才是达标目标 |
| 平均延迟不错，所以流式体验好 | 还要看 TTFT/TPOT 和 coverage |
| 最高阶失败，所以整个 Staircase 都失败 | 若较低阶已同时满足目标和质量，应报告最高合格阶及首个失败阶 |
| 运行 1 小时没有崩溃，所以 Soak 通过 | 还需检查成功率、429/5xx、P95 和首尾吞吐漂移 |
| fixed 请求更快，所以模型容量更大 | fixed 可能命中缓存，不能与 unique 业务容量直接比较 |

## 报告措辞模板

```text
在 throughput_balanced、unique 请求模式下，Staircase 的最高合格阶为 80 users：
business RPM 612，total TPM 184k，成功率 99.4%，429 为 0.3%，P95 E2E 为 4.2s。
100 users 首次质量失败，原因是 429 占比超过门槛；attempted business RPM 790 不作为业务容量。
随后在 80 users 运行 1h Soak，首尾 business RPM 漂移 3.1%，其余质量门槛通过。
```
