# 供应商准入工作流

本文件是 `app/workflow.yaml` 的人类可读版。状态机以 **provider + model** 为实例粒度，
实例状态存于 `$DATA/workflows/<provider>__<model>.json`，因此可以随时中断、随时由
agent 中途接手（`workflow.py status`）。

## 流程图

```mermaid
flowchart TD
    START([开始]) --> KEY([① 拿到供应商 Key])
    KEY --> TESTED{② 以前测试过?}
    TESTED -->|没有| PARAM[③ 参数合规测试]
    TESTED -->|有的| SUP_QUOTE[拿到供应商报价]
    ORIGION[原厂发布参数、模型变动] --> MANUAL[人工：参数规格维护]
    MANUAL -->|未接入| PARAM
    MANUAL -->|已接入| ONBOARD
    PARAM --> PARAM_OK{④ 是否通过?}
    PARAM_OK -->|不通过| TRACE[⑤ API 溯源测试]
    PARAM_OK -->|通过| PRICE[⑦ 人工价格核对]
    TRACE --> TRACE_OK{⑥ 上游是否符合?}
    TRACE_OK -->|符合| PRICE
    TRACE_OK -->|不符合| NEGO[人工：与供应商交涉]
    PRICE --> PRICE_OK{⑧ 是否通过?}
    PRICE_OK -->|不通过| NEGO
    PRICE_OK -->|通过| CONC[⑨ 并发测试]
    SUP_QUOTE --> CONC
    CONC --> CONC_OK{⑩ 是否通过?}
    CONC_OK -->|不通过| FIX_PRICE[⑪ 核实性能要求]
    FIX_PRICE --> NEGO
    CONC_OK -->|通过| ONBOARD([⑫ 提交 MPDB 审核并验证已同步 binding])
    NEGO --> KEY
    ONBOARD --> VERIFY[验真测试·人工后续·出范围]
```

## 节点 → 实现映射

| 节点 | workflow.yaml id | 类型 | 实现 |
|---|---|---|---|
| ① 拿 Key | `acquire_key` | human_gate | 协助写 `$DATA/.env` + `providers.local.yaml`（需用户批准） |
| ② 以前测试过? | `check_history` | decision(auto) | 查 workflows 历史 + reports 中该 provider+model 的 verdict |
| ③ 参数合规 | `param_test` | auto_test | `bin/llm-api-test run --type param_test` |
| ④ 判定 | `param_decision` | decision(auto) | verdict.pass |
| ⑤ API 溯源 | `trace_test` | auto_test | `bin/llm-api-test run --type trace_test --expect <宣称上游>`；指纹仅是来源信号，不单独证明真实上游 |
| ⑥ 判定 | `trace_decision` | decision(auto) | verdict.match_expected |
| ⑦ 价格核对 | `price_check` | human_gate | 用户核对报价后给 pass/fail |
| 报价（已测过路径） | `supplier_quote` | human_gate | 记录报价后进并发 |
| ⑨ 并发 | `concurrency_test` | auto_test | `bin/llm-api-test run --type staircase` |
| ⑩ 判定 | `concurrency_decision` | decision(auto) | verdict.pass |
| ⑪ 核实性能要求 | `verify_perf_requirements` | human_gate | 记录结论 → 交涉 |
| 交涉 | `negotiate` | human_gate | 回到 ① |
| 参数规格维护 | `profile_maintenance` | human_gate | `--entry profile_maintenance` 进入；not_onboarded→③ / onboarded→⑫ |
| ⑫ MPDB 审核/验证 | `onboard` | onboard | `onboard-propose` 生成只读 JSON 证据；独立审核并同步 MPDB 源码快照后，`onboard-apply --review-ref <ref> --yes` 只验证 exact binding 并记录完成，绝不写本地模型事实 |
| 验真 | — | 出范围 | 人工后续事项，本 skill 不自动化 |

## 中途接手示例

1. `bin/llm-api-test workflow list` 找到进行中的实例；
2. `bin/llm-api-test workflow status --provider P --model M` 看当前节点、历史、待办；
3. 按节点类型执行（见 SKILL.md），`advance` 推进。

## 溯源语料库

trace_test 需要 `$DATA/upstream_fingerprints.json` 里有已知上游参考指纹
（用官方/云厂商直连 key 通过 trace collect 流程采集）。
语料为空时 compare 直接报错——这是刻意设计，避免“空库误判上游”。

## MPDB 审核边界

Provider 配置只描述“向哪里、用什么私有凭据调用”，不能登记 Source、Profile、Interface、Contract 或 Test Binding。`onboard-propose` 输出的 `llm-api-test.mpdb-review-proposal.v1` 包含本次 job 证据、已解析执行目标和所需实体清单，但 `mutates_database` 恒为 `false`。

如果 proposal 中没有 exact executable binding，流程停在 `onboard`：在 api_pressure 模型档案治理流程核验官方来源，再把审核过的完整 MPDB 源码快照与 app consumer 一起同步。本流程不依赖独立 registry 发布。只有当前安装能解析 exact binding，且操作者提供外部 review/commit 标识时，`onboard-apply` 才将 workflow 标记为 done。历史/不完整参数结果不会自动推进通过或失败分支；专用研究 workflow 的通过也不等于完整参数准入证明。升级与回滚见 [model-profile-database.md](model-profile-database.md)。
