# llm-api-test（Claude Code skill）

多供应商 LLM 测试工具包：参数合规 / 缓存 / API 溯源 / 图片参数 / 并发压测 + 供应商准入工作流 + Web 控制台。

> 本分支（`for_claude`）是面向 **Claude Code** 的版本。面向 openclaw 的原版在 `main` 分支。

## 安装

本仓库根目录就是 skill 本体（`SKILL.md` 在根目录），**完全自包含，不依赖任何外部仓库**。
把仓库克隆到 Claude Code 的 skill 目录，目录名即命令名（`llm-api-test` → `/llm-api-test`）：

```bash
# 个人 skill（所有项目可用）
git clone git@github.com:zenoWZH/API_param_test_skill.git ~/.claude/skills/llm-api-test

# 或项目 skill（仅当前项目可用）
git clone git@github.com:zenoWZH/API_param_test_skill.git <项目根>/.claude/skills/llm-api-test
```

如果想把仓库放在别处（例如已有的工作目录），用软链接即可，Claude Code 会跟随软链接读取 `SKILL.md`：

```bash
ln -s /path/to/API_param_test_skill ~/.claude/skills/llm-api-test
```

安装后初始化（Claude Code 也可以直接执行这一步）：

```bash
bash ~/.claude/skills/llm-api-test/scripts/setup.sh
```

setup 会自动安装 uv、用 uv 下载受管 Python 3.12、创建 `.venv`、安装依赖，并在数据目录
（默认 `~/.config/llm-api-test/`）生成 `.env` 与 `providers.local.yaml` 模板。
之后编辑这两个文件填入 API key 与供应商定义即可。

前置要求：仅需 `bash` + `curl`（用于自动安装 uv）；Python 解释器与虚拟环境全部由 uv 受管安装，无需任何系统 Python 包。

## 使用

安装后在 Claude Code 里直接说需求即可（"测试 xx 供应商的 xx 模型"、"参数测试"、"压测"、"接着测"…），
Claude 会按 `SKILL.md` 的说明自动加载本 skill；也可以输入 `/llm-api-test` 手动调用。

- 说"测试某供应商某模型" → 走供应商准入工作流（`scripts/workflow.py`）
- 说单点测试 → 用 `scripts/run_test.py`
- 结果由 `scripts/result.py` 读取并中文总结

**红线（写在 SKILL.md 里，Claude 会遵守）**：写密钥配置（`.env` / `providers.local.yaml`）和注册模型 profile
（`onboard-apply`）前必须展示内容并征得明确同意；所有测试真实调用付费 API，执行前与用户确认 provider、model、测试类型。

### 减少权限确认（可选）

本 skill 的每条命令都会走 Claude Code 的权限确认。**发起测试的命令建议保持确认**（会真实花钱），
只把只读命令加入允许列表。在 `~/.claude/settings.json` 或项目 `.claude/settings.json` 中：

```json
{
  "permissions": {
    "allow": [
      "Bash(uv run --python ~/.claude/skills/llm-api-test/.venv/bin/python ~/.claude/skills/llm-api-test/scripts/jobs.py:*)",
      "Bash(uv run --python ~/.claude/skills/llm-api-test/.venv/bin/python ~/.claude/skills/llm-api-test/scripts/result.py:*)"
    ]
  }
}
```

路径按实际安装位置调整。不要把 `run_test.py`、`workflow.py ... onboard-apply` 加进来。

## 环境变量（可选，均有默认值）

| 变量 | 默认值 | 用途 |
|---|---|---|
| `LLM_API_TEST_DATA_DIR` | `~/.config/llm-api-test` | 数据目录（密钥/配置/报告/工作流状态） |
| `WEB_CONSOLE_PORT` | `8090` | Web 控制台端口 |
| `WEB_CONSOLE_USER` / `WEB_CONSOLE_PASSWORD` | 未设置 | 固定控制台登录凭据 |
| `LLM_API_TEST_DISABLE_AUTH` | 未设置 | 设为 `1` 关闭控制台登录（仅可信网络） |
| `CLOUDFLARE_TUNNEL_TOKEN` | 未设置 | Cloudflare 命名隧道 token |

## Web 控制台（可选）

```bash
bash ~/.claude/skills/llm-api-test/scripts/console.sh start   # 默认 0.0.0.0:8090
bash ~/.claude/skills/llm-api-test/scripts/console.sh passwd  # 查看登录密码
bash ~/.claude/skills/llm-api-test/scripts/console.sh tunnel  # 公网访问（Cloudflare 快速隧道）
```

CLI 发起的任务也会出现在控制台（运行中与历史），前端可作为观察窗口按需开启。

## 安全说明

- Web 控制台默认启用登录（可关闭，见上）：首次启动自动生成 `admin` + 随机密码；`console.sh passwd` 查看、
  `passwd --set` 修改、`passwd --reset` 重置；也可用 `WEB_CONSOLE_USER`/`WEB_CONSOLE_PASSWORD` 固定。
- 公网访问（可选）：`console.sh tunnel` 用 Cloudflare 免费快速隧道（随机地址）；
  `console.sh tunnel --token <TOKEN>` 用 Cloudflare 账户命名隧道（固定域名）。分步引导见 `references/console-access.md`。
- 密钥与私有供应商配置只存放在数据目录（默认 `~/.config/llm-api-test/`），权限 600，不随 skill 分发。
- 所有测试真实调用付费 API，执行前应与用户确认。

## 目录说明

```text
SKILL.md    skill 本体（Claude Code 读取的入口）
app/        vendored 测试引擎（源自 yibuapi-llm-loadtest，含 P1/P2/P4 补丁与新增 trace_test.py）
scripts/    setup.sh / console.sh / run_test.py / jobs.py / result.py / workflow.py / skill_env.py
references/ 测试判读指南与供应商准入工作流说明（按需加载）
```
