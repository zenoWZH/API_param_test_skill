# llm-api-test（openclaw skill）

多供应商 LLM 测试工具包：参数合规 / 缓存 / API 溯源 / 图片参数 / 并发压测 + 供应商准入工作流 + Web 控制台。

日常使用方式：把需求用中文说给 openclaw（如"帮我接入供应商 X 的模型 M"），由 agent 调用本 skill 的 CLI 脚本完成测试并总结结果；Web 控制台是可选的人工观察/操作界面，不开也能用。

## 目录结构

本仓库根目录即 skill 本体（完全自包含，不依赖外部仓库）：

```text
SKILL.md      agent 操作手册（命令细节、流程定义，openclaw 按它工作）
_meta.json    skill 元数据
scripts/      入口脚本：
                setup.sh        初始化（装 uv、建 .venv、初始化数据目录）
                console.sh      Web 控制台管理（start/stop/passwd/tunnel 等）
                run_test.py     发起测试（7 种类型 + --list-providers 发现）
                jobs.py         任务列表/状态/停止（--stop <job_id>）
                result.py       读取并精简测试结果
                workflow.py     供应商准入工作流状态机（start/next/advance/onboard-*）
                register_model.py 新模型能力 profile 登记助手
                skill_env.py    内部工具库（被以上脚本引用，非命令入口）
app/          vendored 测试引擎（源自 yibuapi-llm-loadtest，含 P1/P2/P4 补丁与新增 trace_test.py；详版文档见 app/docs/）
references/   专题文档：console-access.md（控制台登录与公网隧道）、
              supplier-onboarding-workflow.md（准入工作流完整说明与 mermaid 图）、
              testing-guide.md（测试判读指南）
```

数据目录（密钥/配置/报告/工作流状态，默认 `~/.config/llm-api-test/`，可用 `LLM_API_TEST_DATA_DIR` 改）：

```text
.env                            API key（权限 600）
providers.local.yaml            供应商定义（setup 从 app/providers.local.example.yaml 生成模板）
model_capability_profiles.local.yaml  模型能力注册表覆盖层
console_auth.json / console_password / console_secret_key   控制台登录凭据（哈希/明文 600/密钥）
upstream_fingerprints.json      API 溯源参考指纹语料库（初始为空，需先建库）
reports/jobs/<job_id>/          每次任务的产物：job_spec.json、run.json、job.log、
                                verdict.json / summary.json / load_result.json
workflows/<provider>__<model>.json  准入工作流状态
console.pid/.log、tunnel.pid/.log   控制台与隧道进程状态
```

## 快速上手（安装）

本仓库根目录就是 skill 本体，**完全自包含，不依赖任何外部仓库**。在本机执行（openclaw agent 可直接运行）：

```bash
# 1. 直接把仓库克隆为 skill（目标路径按实际调整）
git clone git@github.com:zenoWZH/API_param_test_skill.git ~/.openclaw/workspace/skills/llm-api-test
# 2. 初始化（自动安装 uv；Python 3.12 由 uv 受管下载并创建 .venv、安装依赖）
bash ~/.openclaw/workspace/skills/llm-api-test/scripts/setup.sh
# 3. 配置密钥与供应商：编辑数据目录下的 .env 和 providers.local.yaml
#    （setup 已从 app/providers.local.example.yaml 生成模板，按注释填入即可）
# 4. 启动 Web 控制台
bash ~/.openclaw/workspace/skills/llm-api-test/scripts/console.sh start
```

前置要求：仅需 `bash` + `curl`（用于自动安装 uv）；Python 3.12 解释器与虚拟环境全部由 uv 受管安装，无需任何系统 Python 包。

## 可直接粘贴给 openclaw 的安装提示词

复制下面整段发给 openclaw 即可：

```text
请安装并使用 llm-api-test skill，步骤如下：
1. 执行 git clone git@github.com:zenoWZH/API_param_test_skill.git <你的 workspace skills 目录>/llm-api-test
2. 运行 bash <skills 目录>/llm-api-test/scripts/setup.sh（Python 环境由 uv 打包：setup 会自动安装 uv，用 uv 下载受管 Python 3.12、创建 .venv 并安装依赖；如失败把报错发给我）
3. 提醒我编辑数据目录 ~/.config/llm-api-test/ 下的 .env（填 API key）和 providers.local.yaml（填供应商，模板已由 setup 生成）
4. 配置完成后运行 bash <skills 目录>/llm-api-test/scripts/console.sh start，用 bash <skills 目录>/llm-api-test/scripts/console.sh passwd 查到登录密码，把访问 URL 和密码一起告诉我；如需公网访问，运行 bash <skills 目录>/llm-api-test/scripts/console.sh tunnel（免费随机域名）并把公网地址告诉我；如果我有 Cloudflare 账户要固定域名，引导我按 references/console-access.md 创建命名隧道拿 token，再用 tunnel --token 启动
5. 之后按该 skill 的 SKILL.md 工作：我说“测试某供应商某模型”时走供应商准入工作流（workflow.py）；我说单点测试时用 run_test.py；所有 Python 命令都用 uv run --python <skills 目录>/llm-api-test/.venv/bin/python 执行（uv 不在 PATH 时用 ~/.local/bin/uv）；测试结果用 result.py 读取并向我中文总结
6. 红线：写密钥配置（.env / providers.local.yaml）和注册模型 profile（onboard-apply）前，必须把内容展示给我并征得明确同意；所有测试会真实调用付费 API，执行前与我确认 provider、model、测试类型
```

## 日常使用（对 openclaw 说什么）

装好后不需要自己敲命令，直接用中文提需求即可。常用话术：

| 你说 | openclaw 做什么 |
|---|---|
| "帮我接入新供应商 X 的模型 M" | 走供应商准入工作流：`workflow.py start` → 逐节点推进（参数测试→价格核对→并发→注册 profile），人工节点会向你转述并等结论 |
| "接着上次测 X 的 M" / "上次测到哪了" | `workflow.py status` 看当前节点，`next` 给出该做的事，中途接手继续推进 |
| "测一下 X 的 M 的缓存" | 单点测试：`run_test.py --type cache_suite`（同理可说参数/溯源/图片/压测，对应 param_test/trace_test/image_param_test/staircase 等 7 种类型） |
| "X 有哪些模型配好了" | `run_test.py --list-providers` 列出所有 provider 及模型 |
| "现在有哪些任务在跑" | `jobs.py --running` 列出运行中任务 |
| "停掉那个任务" | `jobs.py --stop <job_id>`（SIGTERM 到任务进程组） |
| "结果怎么样，给我总结" | `result.py --id <job_id>` 读取结果，中文总结 pass/fail、关键指标与失败点 |
| "把控制台地址和密码告诉我" | `console.sh status` + `console.sh passwd` |
| "我要改密码" / "重置密码" | `console.sh passwd --set <新密码>` / `passwd --reset` |
| "我要公网访问控制台" | `console.sh tunnel`（免费随机域名）；有 Cloudflare 账户要固定域名则用 `tunnel --token`，见 `references/console-access.md` |

红线（agent 已被告知，仍需你知晓）：写密钥配置（.env / providers.local.yaml）和注册模型 profile（onboard-apply）前，agent 必须把内容展示给你并征得明确同意；所有测试真实调用付费 API，执行前应与你确认 provider、model、测试类型。

## 供应商准入流程（简版）

```
①拿 Key → ②是否测过 ──没测过──→ ③参数合规测试
              │                     │
            测过→报价→⑨          ④不过 → ⑤API 溯源 → ⑥判定
              │                     ├─ 符合宣称上游 → ⑦人工价格核对
              │                     └─ 不符 → 人工交涉 → 回①
              └──→ ⑦通过 → ⑨并发测试(staircase) → ⑩通过 → ⑫注册能力 profile
                            └ ⑩不过 → ⑪人工核实性能 → 交涉
```

完整定义（状态机）见 `app/workflow.yaml`；含 mermaid 图的详细说明见 `references/supplier-onboarding-workflow.md`。验真测试不属于本 skill。

## 结果在哪

- 每次测试一个目录：`~/.config/llm-api-test/reports/jobs/<job_id>/`，含 `verdict.json`（判定结论）、`summary.json` / `load_result.json`（指标明细）、`job.log`（日志）等。
- `verdict.json` 一句话：该次测试的通过/不通过结论及失败点，agent 总结时以它为准。
- 不需要自己翻文件：直接对 openclaw 说"把 <job_id> 的结果总结一下"即可。

## 常见问题（FAQ）

- **费用**：所有测试真实调用付费 API，压测/缓存套件消耗更明显；agent 执行前会与你确认，介意费用时先用小参数探针（如让 agent 用 `set-args` 缩短 staircase 时长）。
- **弱机能不能跑**：能。1 核 / 512MB–1GB 内存 / 2GB 磁盘可运行全部功能；但高并发压测的绝对数值（RPS、延迟）受弱机性能影响，仅供参考，趋势与报错率仍有意义。staircase 默认 10→150 users、每档 5 分钟，嫌久可让 agent 缩短。
- **要装 Python 吗**：不用。Python 3.12 与依赖由 uv 受管安装在 skill 目录 `.venv` 内，不碰系统环境；环境问题重跑 `scripts/setup.sh`。
- **端口冲突**：控制台默认 8090，用环境变量 `WEB_CONSOLE_PORT` 改，如 `WEB_CONSOLE_PORT=9090 bash scripts/console.sh start`。
- **溯源（trace_test）报错说语料库为空**：溯源靠响应指纹比对，需先用已知直连渠道（官方/AWS 等）采集参考指纹建库，让 agent 按 SKILL.md "API 溯源语料库" 一节操作。
- **测试基线**：引擎自带测试 `cd app && ../.venv/bin/python -m pytest tests/ -q` 当前 251 passed / 0 failed，全绿。

## 安全说明

- Web 控制台默认启用登录（可关闭，见下）：首次启动自动生成 `admin` + 随机密码；`bash scripts/console.sh passwd` 查看、`passwd --set` 修改、`passwd --reset` 重置；也可用 `WEB_CONSOLE_USER`/`WEB_CONSOLE_PASSWORD` 固定。关闭认证：`LLM_API_TEST_DISABLE_AUTH=1 bash scripts/console.sh start`（仅限可信网络）。
- 公网访问（可选）：`bash scripts/console.sh tunnel` 用 Cloudflare 免费快速隧道（随机地址）；`bash scripts/console.sh tunnel --token <TOKEN>` 用 Cloudflare 账户命名隧道（固定域名）。分步引导见 `references/console-access.md`。
- 密钥与私有供应商配置只存放在数据目录（默认 `~/.config/llm-api-test/`），权限 600，不随 skill 分发。
- 所有测试真实调用付费 API，agent 执行前应与用户确认。

## 文档地图

| 文档 | 内容 |
|---|---|
| `SKILL.md` | agent 操作手册：全部命令、工作流节点处理、故障排查（openclaw 按它工作，用户一般不需要读） |
| `references/console-access.md` | 控制台登录、改密、Cloudflare 隧道（免费快速/命名）分步引导 |
| `references/supplier-onboarding-workflow.md` | 供应商准入工作流完整说明（含权威 mermaid 流程图） |
| `references/testing-guide.md` | 各测试类型的判读指南（看什么指标、怎么算过） |
| `app/README.md` + `app/docs/` | vendored 测试引擎的详版技术文档（参数测试架构、缓存/压测方法等） |
