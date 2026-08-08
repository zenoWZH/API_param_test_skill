---
name: llm-api-test
description: LLM 供应商准入与测试。对任意供应商+模型执行参数合规、缓存、API 溯源（上游来源）、图片参数与并发压测，全部通过 CLI 脚本完成并可总结报告；内置供应商准入工作流，可从头或中途推进。Web 控制台（含登录与公网隧道）为可选人工界面。
author: wangzhouhao
version: 1.0.0
triggers:
  - "参数测试"
  - "缓存测试"
  - "压测"
  - "并发测试"
  - "溯源"
  - "param test"
  - "cache test"
  - "测试供应商"
  - "测试模型"
  - "新供应商接入"
  - "继续测试"
  - "接着测"
metadata: {"clawdbot":{"emoji":"🧪","requires":{"bins":["bash","curl"]},"config":{"env":{"LLM_API_TEST_DATA_DIR":{"description":"数据目录（密钥/配置/报告/工作流状态）","default":"~/.config/llm-api-test","required":false},"WEB_CONSOLE_PORT":{"description":"Web 控制台端口","default":"8090","required":false}}}}}
---

# LLM API Test（供应商准入与测试）

多供应商 LLM 测试工具包：CLI 测试脚本 + 供应商准入工作流 + 可选 Web 控制台。
**前端完全可选**：所有测试（参数/缓存/溯源/图片/并发）、供应商准入工作流、结果总结、任务停止都能仅靠本文件的 CLI 脚本完成，无需打开浏览器；前端只是给用户的人工操作界面。
所有测试都会**真实调用付费 API**：执行前必须向用户确认 provider、model 与测试类型。

能力一览（全部 CLI 可达）：

| 需求 | 命令入口 |
|---|---|
| 初始化/配置检查 | `scripts/setup.sh`、`run_test.py --list-providers` |
| 发起各类测试 | `scripts/run_test.py`（7 种类型） |
| 查看任务状态 | `scripts/jobs.py` |
| 停止任务 | `scripts/jobs.py --stop <job_id>` |
| 读取并总结结果 | `scripts/result.py` |
| 供应商准入流程 | `scripts/workflow.py`（状态机，可从头/中途接手） |
| 新模型登记 | `scripts/register_model.py` |
| 前端/密码/公网（可选） | `scripts/console.sh` |

路径约定：`{baseDir}` = skill 根目录。Python 环境由 uv 管理（`{baseDir}/.venv`，若不存在先跑 setup）；**所有 Python 命令一律通过 uv 执行**：`uv run --python {baseDir}/.venv/bin/python <script> ...`。若 `uv` 不在 PATH，用 `~/.local/bin/uv` 替代。
数据目录默认 `~/.config/llm-api-test/`（下称 `$DATA`），存放 `.env`、`providers.local.yaml`、注册表覆盖层、溯源语料库、报告与工作流状态。

## 安装/初始化

skill 完全自包含，不依赖外部仓库；Python 环境由 uv 管理（setup 自动检测或安装 uv，优先用系统 Python 3.11+，缺失时自动下载受管 3.12）：

```bash
bash {baseDir}/scripts/setup.sh   # uv 建 venv、装依赖、初始化数据目录（含 providers.local.yaml 模板）
```

初始化后引导用户编辑 `$DATA/.env`（API key）与 `$DATA/providers.local.yaml`（供应商定义，模板见 `{baseDir}/app/providers.local.example.yaml`）。

## Web 控制台（前端，可选）

> 仅供用户在浏览器人工操作；openclaw 执行任务**不需要**启动它。CLI 发起的任务也会出现在控制台（运行中与历史），所以前端可作为用户的观察窗口按需开启。

```bash
bash {baseDir}/scripts/console.sh start     # 后台启动，默认 0.0.0.0:8090
bash {baseDir}/scripts/console.sh status    # 状态 + URL
bash {baseDir}/scripts/console.sh stop
bash {baseDir}/scripts/console.sh logs
```

**登录认证（可选，默认开启）**：首次启动自动生成 `admin` + 随机密码。openclaw 可用以下命令把密码告诉用户或修改：

```bash
bash {baseDir}/scripts/console.sh passwd            # 查看当前用户名/密码（转述给用户）
bash {baseDir}/scripts/console.sh passwd --set <新密码>   # 用户要求改密码时（立即生效，无需重启）
bash {baseDir}/scripts/console.sh passwd --reset    # 重置为新的随机密码
```

用户明确不要密码时：`LLM_API_TEST_DISABLE_AUTH=1 bash {baseDir}/scripts/console.sh start`（仅可信网络，重启生效）。

**公网访问（可选）**——两种方式，引导见 `{baseDir}/references/console-access.md`：

```bash
# 方式一：Cloudflare 免费快速隧道（无需账号；地址随机、每次重建会变、无 SLA）
bash {baseDir}/scripts/console.sh tunnel        # 打印 https://*.trycloudflare.com
bash {baseDir}/scripts/console.sh tunnel-url    # 再次查看
bash {baseDir}/scripts/console.sh tunnel-stop

# 方式二：Cloudflare 账户命名隧道（固定域名、需要用户在 dashboard 创建隧道拿 token）
bash {baseDir}/scripts/console.sh tunnel --token <CLOUDFLARE_TUNNEL_TOKEN>
# 或先 export CLOUDFLARE_TUNNEL_TOKEN=... 再 bash {baseDir}/scripts/console.sh tunnel
```

cloudflared 首次自动下载到 `~/.local/bin/`；均走 TCP 443（http2），封锁 UDP 的网络也能用。公网地址+登录密码要一起给用户。

用户可在浏览器里选择供应商/模型发起测试、查看实时进度与历史报告。
CLI 发起的任务也会出现在控制台（运行中与历史）。

## 供应商/模型发现

接到"测试某供应商某模型"的请求时，先确认 provider/model 已配置：

```bash
$PY {baseDir}/scripts/run_test.py --list-providers   # 所有 provider 及其模型清单（JSON）
```

未配置 → 走"配置与密钥"一节引导用户；未注册 profile → 用 `register_model.py` 登记。

## 模式一：供应商准入工作流（默认推荐）

流程：①拿 Key → ②是否测过 → ③参数合规测试 →（④不过→⑤API 溯源→⑥判定）→ ⑦人工价格核对 → ⑨并发测试(staircase) → ⑫注册能力 profile。
人工交涉/性能核实为人工节点，交涉后回到①。验真测试不属于本 skill。

状态机定义：`{baseDir}/app/workflow.yaml`；说明文档：`{baseDir}/references/supplier-onboarding-workflow.md`。

```bash
PY="uv run --python {baseDir}/.venv/bin/python"
# 开始新供应商准入（也可 --entry profile_maintenance 处理原厂模型变动；--expect-upstream 声明宣称上游）
$PY {baseDir}/scripts/workflow.py start --provider <P> --model <M> [--expect-upstream anthropic_official]
# 中途接手：先看状态
$PY {baseDir}/scripts/workflow.py status --provider <P> --model <M>
# 查看当前节点该做什么（auto_test 节点会给出可直接执行的命令）
$PY {baseDir}/scripts/workflow.py next --provider <P> --model <M>
# 推进节点：自动判定（decision 节点从 verdict 读取）或人工结论
$PY {baseDir}/scripts/workflow.py advance --provider <P> --model <M> --auto
$PY {baseDir}/scripts/workflow.py advance --provider <P> --model <M> --outcome pass --notes "价格已核对"
# 调整某个测试节点的运行参数（如缩短压测时长做探针）
$PY {baseDir}/scripts/workflow.py set-args --provider <P> --model <M> --node concurrency_test --args '--extra-json {"staircase_plan":{"steps":[10,30],"step_duration":"1m"}}'
# 所有实例
$PY {baseDir}/scripts/workflow.py list
```

节点类型处理方式：
- `human_gate`：把 `prompt` 转述给用户，得到结论后 `advance --outcome ...`（valid_outcomes 见 status 输出）。
- `auto_test`：两种方式——a) `next --execute` 直接由 workflow 发起并自动登记 job_id；b) 手动执行 status/next 输出中的 `command`（`--background` 发起），用 `jobs.py` 轮询，结束后 `result.py` 总结，再 `advance --auto --job-id <id>`。长任务（staircase/soak 默认数十分~1 小时）先用 `set-args` 缩短时长做探针，或与用户确认后跑完整时长。
- `decision`：优先 `advance --auto`；失败时手动读 result 后指定 `--outcome`。
- `onboard`（⑫注册）：**必须人工批准**——

```bash
$PY {baseDir}/scripts/workflow.py onboard-propose --provider <P> --model <M>   # 生成 YAML 提案，展示给用户
$PY {baseDir}/scripts/workflow.py onboard-apply --provider <P> --model <M> --yes   # 仅用户明确批准后执行
```

## 模式二：单点测试（不进入工作流）

```bash
$PY {baseDir}/scripts/run_test.py --type param_test   --provider <P> --model <M> [--runs 2] [--reference-source <S>]
$PY {baseDir}/scripts/run_test.py --type cache_suite  --provider <P> --model <M> [--cache-measured-requests 50]
$PY {baseDir}/scripts/run_test.py --type trace_test   --provider <P> --model <M> [--expect <上游名>]
$PY {baseDir}/scripts/run_test.py --type staircase    --provider <P> --model <M>
$PY {baseDir}/scripts/run_test.py --type quick_load   --provider <P> --model <M> --users 10 --duration 2m
$PY {baseDir}/scripts/run_test.py --type soak         --provider <P> --model <M>
$PY {baseDir}/scripts/run_test.py --type image_param_test --provider <P> --model <M> [--extra-json '{"include_2k":true}']
```

加 `--background` 立即返回 job_id；前台模式会等结束并返回退出码。

```bash
$PY {baseDir}/scripts/jobs.py [--running] [--id <job_id>]   # 任务列表/状态（含运行中）
$PY {baseDir}/scripts/jobs.py --stop <job_id>               # 停止运行中的任务（SIGTERM 进程组）
$PY {baseDir}/scripts/result.py --id <job_id> [--full]      # 精简结果 JSON → 用中文向用户总结
```

总结要求：给出 pass/fail、关键指标（参数兼容性/缓存命中率/RPM/TPM/上游匹配度）、失败点，以及报告目录路径。

## API 溯源语料库（一次性准备）

溯源（trace_test）用响应指纹比对判定 token 的真实上游（如官方/AWS/Vertex）。语料库在 `$DATA/upstream_fingerprints.json`，初始为空，需先用**已知直连渠道**采集参考指纹：

```bash
$PY {baseDir}/app/scripts/trace_test.py collect --provider <官方直连provider> --model <M> --save-upstream anthropic_official
$PY {baseDir}/app/scripts/trace_test.py collect --provider <aws渠道provider>   --model <M> --save-upstream aws_bedrock
```

语料为空时 compare 会报错并提示建库——不要跳过这一步。

## 配置与密钥

- 新供应商：协助用户把 `api_key_env` 写入 `$DATA/.env`、provider 定义写入 `$DATA/providers.local.yaml`（参考 `{baseDir}/app/providers.local.example.yaml` 与 `{baseDir}/app/config.yaml`）。**写入前展示内容并征得用户明确同意**。
- 未注册模型的限制：参数/压测任务要求模型在能力注册表有 profile。用注册助手完成未验证登记（proposal 展示 → 用户批准 → `--yes`）：

```bash
$PY {baseDir}/scripts/register_model.py --provider <P> --model <M> --family <已有family>   # 打印提案
$PY {baseDir}/scripts/register_model.py --provider <P> --model <M> --family <F> --yes      # 批准后才写入
```

family 必须是注册表中已有的（如 deepseek/kimi/gpt/claude...）；全新 family 需人工维护注册表。

## 停止任务

- CLI 发起的任务：`$PY {baseDir}/scripts/jobs.py --stop <job_id>`（SIGTERM 到任务进程组，状态记为 stopped）。
- 前端发起的任务：用户在控制台点 stop 按钮；或让 openclaw 调 console API（`POST /api/jobs/<id>/stop`，需先登录）。

## 故障排查

- `console.sh logs` 看控制台日志；任务日志在 `$DATA/reports/jobs/<job_id>/job.log`。
- 报 “Missing registered text model/API/route profile” → 模型未注册 profile，用 `register_model.py`。
- 报 “Missing API key” → `$DATA/.env` 缺对应 `api_key_env` 变量，引导用户补 key。
- trace compare 报 “corpus is empty” → 先按"溯源语料库"一节建库。
- Python 环境由 uv 受管（3.12），环境问题一律重跑 `setup.sh`。
