# llm-api-test（openclaw skill）

多供应商 LLM 测试工具包：参数合规 / 缓存 / API 溯源 / 图片参数 / 并发压测 + 供应商准入工作流 + Web 控制台。

## 给 openclaw 的安装命令

本仓库根目录就是 skill 本体，**完全自包含，不依赖任何外部仓库**。在本机执行（openclaw agent 可直接运行）：

```bash
# 1. 直接把仓库克隆为 skill（目标路径按实际调整）
git clone git@github.com:zenoWZH/API_param_test_skill.git ~/.openclaw/workspace/skills/llm-api-test
# 2. 初始化（自动检测/安装 uv，由 uv 创建 venv 并装依赖；无 Python 3.11+ 时 uv 会自动下载受管 Python 3.12）
bash ~/.openclaw/workspace/skills/llm-api-test/scripts/setup.sh
# 3. 配置密钥与供应商：编辑数据目录下的 .env 和 providers.local.yaml
#    （setup 已从 app/providers.local.example.yaml 生成模板，按注释填入即可）
# 4. 启动 Web 控制台
bash ~/.openclaw/workspace/skills/llm-api-test/scripts/console.sh start
```

前置要求：仅需 `bash` + `curl`（用于自动安装 uv）；Python 由 uv 提供（优先系统 3.11+，缺失时自动下载受管 3.12），不再需要预装 python3-venv。

## 可直接粘贴给 openclaw 的安装提示词

> 请安装并使用 llm-api-test skill：执行 `git clone git@github.com:zenoWZH/API_param_test_skill <你的 workspace skills 目录>/llm-api-test`，运行其中的 `scripts/setup.sh`（如报 Python/venv 缺失请按提示安装系统包后重跑），然后提醒我编辑数据目录 `~/.config/llm-api-test/` 下的 `.env`（填 API key）和 `providers.local.yaml`（填供应商），配置好后用 `scripts/console.sh start` 启动 Web 控制台并把访问 URL 告诉我。之后按照该 skill 的 SKILL.md 工作：我说“测试某供应商某模型”时走供应商准入工作流（workflow.py），我说单点测试时用 run_test.py；测试结果用 result.py 读取并向我中文总结。注意：写密钥配置和注册 profile 前必须征得我同意。

## 安全说明

- Web 控制台默认监听 `0.0.0.0:8090` 且**无鉴权**，局域网内任何人都可发起付费 API 测试。仅需本机访问时：`WEB_CONSOLE_HOST=127.0.0.1 bash scripts/console.sh start`。
- 密钥与私有供应商配置只存放在数据目录（默认 `~/.config/llm-api-test/`），权限 600，不随 skill 分发。
- 所有测试真实调用付费 API，agent 执行前应与用户确认。

## 目录说明

```text
app/        vendored 测试引擎（源自 yibuapi-llm-loadtest，含 P1/P2/P4 补丁与新增 trace_test.py）
scripts/    setup.sh / console.sh / run_test.py / jobs.py / result.py / workflow.py / skill_env.py
references/ 测试判读指南与供应商准入工作流说明
```
