# llm-api-test（openclaw skill）

多供应商 LLM 测试工具包：参数合规 / 缓存 / API 溯源 / 图片参数 / 并发压测 + 供应商准入工作流 + Web 控制台。

## 给 openclaw 的安装命令

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

## 安全说明

- Web 控制台默认启用登录（可关闭，见下）：首次启动自动生成 `admin` + 随机密码；`bash scripts/console.sh passwd` 查看、`passwd --set` 修改、`passwd --reset` 重置；也可用 `WEB_CONSOLE_USER`/`WEB_CONSOLE_PASSWORD` 固定。关闭认证：`LLM_API_TEST_DISABLE_AUTH=1 bash scripts/console.sh start`（仅限可信网络）。
- 公网访问（可选）：`bash scripts/console.sh tunnel` 用 Cloudflare 免费快速隧道（随机地址）；`bash scripts/console.sh tunnel --token <TOKEN>` 用 Cloudflare 账户命名隧道（固定域名）。分步引导见 `references/console-access.md`。
- 密钥与私有供应商配置只存放在数据目录（默认 `~/.config/llm-api-test/`），权限 600，不随 skill 分发。
- 所有测试真实调用付费 API，agent 执行前应与用户确认。

## 目录说明

```text
app/        vendored 测试引擎（源自 yibuapi-llm-loadtest，含 P1/P2/P4 补丁与新增 trace_test.py）
scripts/    setup.sh / console.sh / run_test.py / jobs.py / result.py / workflow.py / skill_env.py
references/ 测试判读指南与供应商准入工作流说明
```
