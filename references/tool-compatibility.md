# 多工具兼容、安装与验收

本文说明如何把本仓库作为 `llm-api-test` skill 安装到不同编程代理，以及如何验证“被发现、能执行、能访问真实上游”这三件彼此独立的事。文档中的路径与命令以 2026-09-03 的官方说明为准。

## 统一目录约定

无论仓库原名、下载包名或上级目录叫什么，最终 skill 的叶子目录都必须是：

```text
llm-api-test/
├── SKILL.md
├── bin/llm-api-test
├── app/
├── packages/model-profile-db/
├── scripts/
└── references/
```

`SKILL.md` 必须直接位于这个叶子目录中，且 frontmatter 的 `name` 为 `llm-api-test`。不要只复制 `SKILL.md`：测试引擎、模型档案数据库、脚本和参考文档都是运行时的一部分。

发布或安装时使用干净的 Git revision、release archive 或工具自带的 Git 安装能力。不要把开发机的 `.venv/`、`.env`、`providers.local.yaml`、报告、日志或工作流状态打进 skill。

## 目录与调用矩阵

| 工具 | 推荐的项目或 workspace 路径 | 用户级路径 | 明确调用 | 重新发现 |
| --- | --- | --- | --- | --- |
| Codex | `<repo>/.agents/skills/llm-api-test/` | `~/.agents/skills/llm-api-test/` | `$llm-api-test`；也可通过 `/skills` 选择 | 新会话；从 skill 所在仓库目录或其子目录启动 |
| Claude Code | `<repo>/.claude/skills/llm-api-test/` | `~/.claude/skills/llm-api-test/` | `/llm-api-test` | 通常实时检测；若会话启动时顶层 `skills` 目录不存在，则重启会话 |
| Kilo Code | `<repo>/.agents/skills/llm-api-test/` 或 `<repo>/.kilo/skills/llm-api-test/` | `~/.agents/skills/llm-api-test/` 或 `~/.kilo/skills/llm-api-test/` | 在提示中明确说“使用 llm-api-test skill” | `/reload` 或新会话 |
| Cursor | `<repo>/.agents/skills/llm-api-test/` 或 `<repo>/.cursor/skills/llm-api-test/` | `~/.agents/skills/llm-api-test/` 或 `~/.cursor/skills/llm-api-test/` | `/llm-api-test` 或 `@llm-api-test` | 新 Agent 会话；在 Customize → Skills 中复核 |
| Windsurf | `<repo>/.agents/skills/llm-api-test/` 或 `<repo>/.windsurf/skills/llm-api-test/` | `~/.agents/skills/llm-api-test/` 或 `~/.codeium/windsurf/skills/llm-api-test/` | `@llm-api-test` | 新 Cascade 会话；在 Customizations → Skills 中复核 |
| OpenClaw | `<workspace>/skills/llm-api-test/`（最高优先级）或 `<workspace>/.agents/skills/llm-api-test/` | `~/.agents/skills/llm-api-test/` 或 `~/.openclaw/skills/llm-api-test/` | 用自然语言明确要求使用 `llm-api-test`；先用 CLI 检查可见性 | 新会话后运行 `openclaw skills info/check` |
| 通用 shell/其他编程代理 | 任意完整且只读可见的 `llm-api-test/` | 不适用 | `bash <skill-root>/bin/llm-api-test ...` | 不依赖 skill 自动发现 |

Codex、Kilo、Cursor、Windsurf 与 OpenClaw 都能读取 `.agents/skills/`，因此项目内共享时优先使用这个开放目录。Claude Code 当前使用 `.claude/skills/`，需要单独放置一份。不要依赖符号链接实现跨工具共享；远程 worker、云代理和 OpenClaw 的 realpath 安全检查可能不接受或不会同步链接目标。

## 安装方式

以下 `<repo-url>`、`<owner>/<repo>` 与 `<tag-or-commit>` 都是占位符，必须替换为受信任且固定的来源。生产环境优先固定 tag 或 commit，不要无审核地跟随浮动分支。

### Codex 及 `.agents` 兼容工具

项目级：

```bash
git clone <repo-url> .agents/skills/llm-api-test
```

用户级：

```bash
git clone <repo-url> ~/.agents/skills/llm-api-test
```

同一路径可被 Codex、Kilo、Cursor、Windsurf 和 OpenClaw 读取，但远程或云端代理不会自动继承开发机的用户级目录。对 Cursor Cloud Agent、自托管 worker、远程 SSH 环境及同类远端执行器，应把项目级 skill 提交到目标仓库，或预装进实际 worker 镜像。

### Claude Code

项目级：

```bash
git clone <repo-url> .claude/skills/llm-api-test
```

用户级：

```bash
git clone <repo-url> ~/.claude/skills/llm-api-test
```

如果通过 Claude Agent SDK 使用，必须在 `setting_sources`/`settingSources` 中包含 `project` 或 `user`，并且不能用空的 `skills` 列表禁用 skill。允许 `Skill` 只代表允许加载说明，不代表自动允许 Bash、文件写入或网络；这些权限仍由 SDK 的 `allowed_tools`、`canUseTool` 或宿主策略决定。

### Kilo Code

优先使用项目级 `.agents/skills/llm-api-test/`；若只供 Kilo 使用，也可以放在 `.kilo/skills/llm-api-test/`。用户级原生目录是 `~/.kilo/skills/llm-api-test/`，不是旧文档中出现过的 `~/.config/kilo/skills/`。

Kilo 会根据 `description` 自动选择 skill，也支持在提示中明确点名。安装或更新后执行 `/reload`。如果启用了 `KILO_DISABLE_SKILL_SHELL`，skill 中的嵌入式 shell 上下文会被禁用；本 skill 的确定性入口仍需宿主允许 Bash 工具执行。

### Cursor

项目级优先使用 `.agents/skills/llm-api-test/` 或 `.cursor/skills/llm-api-test/`；用户级使用 `~/.agents/skills/llm-api-test/` 或 `~/.cursor/skills/llm-api-test/`。在 Agent 中用 `/llm-api-test` 或 `@llm-api-test` 明确调用。

本机用户目录不会自动复制到 Cursor Cloud Agents、远程 SSH 或自托管 worker。云端必须使用仓库中的项目 skill、Cursor 的受控同步能力，或把 skill 烘焙进 worker 镜像。同步了“说明文件”也不代表云端已获得本地 `.env`、Python 环境或任意网络权限。

### Windsurf

项目级优先使用 `.agents/skills/llm-api-test/` 或 `.windsurf/skills/llm-api-test/`；用户级使用 `~/.agents/skills/llm-api-test/` 或 `~/.codeium/windsurf/skills/llm-api-test/`。在 Cascade 中可让模型按描述自动选择，也可用 `@llm-api-test` 明确调用。

### OpenClaw

从受信任的 Git revision 安装到当前 agent workspace：

```bash
openclaw skills install git:<owner>/<repo>@<tag-or-commit> --as llm-api-test
```

从本地的干净 release 目录安装：

```bash
openclaw skills install /absolute/path/to/llm-api-test --as llm-api-test
```

如确实要供同一宿主机的所有 agent 使用，可在对应安装命令后添加 `--global`。安装后先做宿主机侧检查：

```bash
openclaw skills info llm-api-test --json
openclaw skills check --json
openclaw skills list --eligible
```

不要把 `$CODEX_HOME/skills` 当成 OpenClaw 的 skill root；OpenClaw 原生读取 `<workspace>/skills`、`<workspace>/.agents/skills`、`~/.agents/skills` 和 `~/.openclaw/skills`。同名 skill 的优先级依次为 workspace、workspace `.agents`、用户 `.agents`、OpenClaw managed/local、bundled、额外目录。

本版本携带的 MPDB 数据库包含超过 1 MiB 的单文件；2026-09-20 的完整目录约 45 MB，实际数量以 `doctor --json` 为准。按本文记录的 OpenClaw managed library/bundle 上限（每文件 1 MiB、每 bundle 8 MiB），当前不要使用 managed ZIP/library upload 交付本 skill。使用 workspace 的 Git/local-directory 安装，并在实际 host 和 sandbox 中执行下文的验收。若未来需要 library upload，必须先设计可验证的数据库拆分或运行时制品下载机制，不能静默删减数据库格式。

### 通用 shell 与没有原生 Agent Skills 的工具

只要工具能够读取完整目录并执行 Bash，就可以绕过自动发现，直接调用稳定入口：

```bash
SKILL_DIR=/absolute/path/to/llm-api-test
bash "$SKILL_DIR/bin/llm-api-test" --help
bash "$SKILL_DIR/bin/llm-api-test" setup
bash "$SKILL_DIR/bin/llm-api-test" doctor --json
```

在这类工具的项目说明文件中记录上述入口，并要求代理先读 `SKILL.md`，再按需读 `references/`。不要让代理自行猜测 `app/scripts/*.py` 的内部调用方式；`bin/llm-api-test` 才是跨工具稳定接口。

## 可写目录与运行环境

skill 安装目录只需可读，不应存储虚拟环境、密钥、运行状态或报告。两个可写根目录彼此独立：

| 用途 | 默认目录 | 覆盖变量 | 内容 |
| --- | --- | --- | --- |
| 数据与状态 | `${XDG_CONFIG_HOME:-$HOME/.config}/llm-api-test` | `LLM_API_TEST_DATA_DIR` | `.env`、私有 provider 配置、报告、任务与工作流状态 |
| 可重建运行时 | `${XDG_CACHE_HOME:-$HOME/.cache}/llm-api-test` | `LLM_API_TEST_RUNTIME_DIR` | Python/uv 虚拟环境和可重新安装的缓存 |

在容器、CI、远程 worker 或只读 home 中显式设置两者：

```bash
LLM_API_TEST_DATA_DIR=/secure/writable/llm-api-test-data \
LLM_API_TEST_RUNTIME_DIR=/cache/writable/llm-api-test-runtime \
bash /read-only/skills/llm-api-test/bin/llm-api-test setup
```

数据目录应持久化且权限为 `0700`，其中的密钥与私有配置应为 `0600`。运行时目录可以删除后重建，但必须与执行它的操作系统、CPU 架构和容器镜像匹配；不要把一个宿主机创建的 `.venv` 复制给另一种 OS、架构或 sandbox。

`uv` 必须由宿主按其受控安装流程预先提供；本 skill 不执行远程安装脚本。首次 `setup` 可能需要由 uv 下载受管 Python 或锁定依赖，因此需要软件源网络访问。无网络环境应提前在同一运行环境中准备完整 uv cache/镜像并使用 `setup --offline`；“宿主机已有依赖”不等于隔离 sandbox 内已有依赖。

## OpenClaw host 与 sandbox 是两层能力

OpenClaw 在 host 上发现 skill，只能证明名称、frontmatter、eligibility 和 allowlist 通过；不能证明 sandbox 内能读取全部制品、执行 Bash、写数据、解析 DNS、访问上游或读取密钥。

### Host 侧

1. 使用固定 revision 安装到目标 agent 的 workspace。
2. 用 `openclaw skills info llm-api-test --json` 确认实际胜出的路径。
3. 用 `openclaw skills check --json` 确认所需 binary 和配置未被 gating。
4. 检查 `agents.defaults.skills` 与目标 agent 的 skills allowlist；非空 agent allowlist 是最终集合，不会自动并入默认集合。

### Sandbox 侧

OpenClaw 的 `workspaceAccess` 常见模式为：

- `none`：工具使用隔离 workspace；eligible skills 会被物化到 sandbox。当前 skill 的完整制品规模可能触发 worker/bundle 交付上限，不能只凭 host 可见性假定可用。
- `ro`：agent workspace 以只读形式挂载；适合把 skill 代码保持只读，再为 data/runtime 提供两个专用可写 bind mount。
- `rw`：agent workspace 可读写；兼容性较直接，但扩大了可写范围。除非工作流确实需要修改 workspace，否则优先 `ro` 加窄范围可写挂载。

推荐把 `llm-api-test` 放在目标 OpenClaw workspace 的 `skills/` 下，以只读方式暴露；把 data/runtime 分别映射为独立的读写目录，并在 sandbox 进程里设置 `LLM_API_TEST_DATA_DIR` 与 `LLM_API_TEST_RUNTIME_DIR`。skill 指令引用自身文件时应使用 OpenClaw 的 `{baseDir}`，不要写宿主机绝对路径。

然后在实际 sandbox 中运行，而不是在 host 上代跑：

```bash
bash "<sandbox-skill-path>/bin/llm-api-test" setup
bash "<sandbox-skill-path>/bin/llm-api-test" doctor --json
```

需要真实 API 测试时，再单独开放目标域名的 DNS/TLS/HTTPS 出站。需要 Web 控制台或 Locust 时，还需允许绑定 loopback/指定端口；是否把端口暴露出 sandbox 是另一项显式决定。不要挂载 Docker socket、整个 home、SSH 目录或云厂商凭证目录来“解决权限问题”。

OpenClaw 的 `skills.entries.*.env`/`apiKey` 注入发生在 host agent turn；启用 sandbox 时不要假设这些值自动出现在 sandbox 命令中。应使用 OpenClaw 当前版本支持的 sandbox secret/env 注入或最小范围的只读 secret mount，并在不打印值的情况下验证变量是否存在。

## 权限与网络最小集

| 阶段 | 文件权限 | 进程/端口 | 网络 |
| --- | --- | --- | --- |
| 发现 skill | 只读 `SKILL.md` 与目录元数据 | 无 | 无 |
| 离线 doctor | 只读 skill；读写 data/runtime | Bash、Python | 已预装依赖时可无网络 |
| 首次 setup | 只读 skill；读写 data/runtime | Bash、uv/Python | 软件源或内部镜像 |
| 单模型 smoke/参数测试 | 上述权限；报告写入 data | HTTPS client | 仅目标 provider/gateway 域名 |
| Web 控制台 | 上述权限 | loopback 或显式监听端口 | 本机访问；隧道需额外出站 |
| 固定速率/并发压力测试 | 上述权限；足够的文件描述符和进程资源 | Locust/worker/本地端口 | 目标 provider；需计费与容量授权 |

不要为了方便给代理整个 home 的写权限或不受限网络。离线 doctor、数据库校验和文档检查不需要 provider 凭据，也不应触发真实请求。

## 密钥与真实流量边界

- API key、console password、tunnel token 只能进入私有数据目录、受控环境变量或宿主 secret store；不得写入 `SKILL.md`、命令行参数、聊天提示、Git、测试报告或日志。
- 不要在 shell 命令中展开或回显密钥。若密钥曾出现在聊天、命令、日志或报告中，按已泄露处理并轮换。
- `providers.local.yaml` 可记录 provider/endpoint/model 映射，但凭据优先引用环境变量，不内联明文。
- 真实请求可能产生费用或改变上游配额。执行 smoke、媒体生成、缓存测试或压力测试前，必须确认 provider、route、API form、模型、请求数/RPM、持续时间和预算。
- HTTP `202`、模型列表、provider 元数据或任务已提交都不是成功证据。验收真实能力需要已完成的请求，以及适用的返回模型身份、token/cache、媒体或任务结果证据。
- 控制台如需非 loopback 访问，必须启用认证并明确隧道/反向代理边界；不得因为“只用于测试”而裸露到公网。

## 分层验收

每个工具都要单独记录结果，不得用一种工具的通过结果替代另一种工具。

### A. 结构与发现

1. 最终路径以 `llm-api-test/` 结尾，且其下直接存在 `SKILL.md`。
2. 工具列出的 skill 名称是 `llm-api-test`，实际路径是预期副本，没有被更高优先级的旧版本遮蔽。
3. 让代理回答它准备调用的稳定入口；正确答案应是 `bin/llm-api-test`，不是猜测某个内部 Python 文件。

### B. 离线执行

在该工具真正使用的 host、container 或 sandbox 中运行：

```bash
SKILL_DIR=/actual/path/to/llm-api-test
bash "$SKILL_DIR/bin/llm-api-test" --help
bash "$SKILL_DIR/bin/llm-api-test" setup
bash "$SKILL_DIR/bin/llm-api-test" doctor --json
```

通过条件：命令退出码为 0；doctor 的 JSON 明确报告 skill、运行时、数据目录、MPDB 制品和核心导入可用；skill 目录保持无运行时写入。失败时保存去密后的错误、工具版本、OS/架构、实际路径及权限策略。

### C. 代理调用

给每个代理同一条测试提示：

> 使用 llm-api-test skill，只做离线检查。先说明实际加载的 skill 路径，再运行 doctor --json；不要访问任何 provider，不要读取或打印密钥。

通过条件：代理加载正确的 `SKILL.md`，通过统一 CLI 运行 doctor，且没有越权访问网络或密钥。

### D. 有界真实 smoke

只有在用户确认 provider、模型、API form、上限与费用后，才对一个已配置模型发送一条最小请求。通过条件不是单看 HTTP 状态，而是请求完成、响应可解析，并记录适用的返回模型身份和计量证据。每新增一种工具执行面，都重新做一次有界 smoke；不要直接进入并发或 100 RPM。

### E. 压力与媒体能力

在 smoke 成功后才运行媒体、缓存、固定速率或并发场景。固定速率测试必须报告目标 RPM、实际发起速率、warmup/measurement/drain 窗口、错误分类和身份/计量覆盖；未把客户端驱动到目标速率时，不能据此宣称上游容量。

## 当前验证状态

本仓库提供统一 CLI、外置 data/runtime 目录和 Agent Skills 兼容结构；这些是跨工具可移植性的必要条件，不等于所有宿主都已做完端到端验证。

- 当前开发环境已由当前 Codex 会话执行统一 facade 的离线 doctor、provider discovery、MPDB 与 plan-only sweep；独立新 Codex 进程发现仍需要明确的数据披露授权。最终结果以迁移验收报告中的实测命令为准。
- Claude Code 与 Kilo 的路径和调用约定已按当前官方文档对齐；具体安装环境仍应执行本页 A–C 验收。
- 本机未安装 OpenClaw、Cursor、Windsurf，因此没有完成这三者的“宿主发现 → 代理调用 → sandbox/worker 执行”端到端验证。本文对它们的兼容结论仅限官方目录协议、命令接口与静态结构，不把它表述为实机通过。
- OpenClaw managed library/bundle 因当前 MPDB 大文件不在支持范围；workspace Git/local-directory 安装是当前交付路径，仍需在目标 OpenClaw 版本和 sandbox 策略下按 A–D 验收。

## 官方依据

- [OpenAI：Build skills](https://learn.chatgpt.com/docs/build-skills)
- [Claude Code：Extend Claude with skills](https://code.claude.com/docs/en/slash-commands)
- [Kilo Code：Skills](https://kilo.ai/docs/customize/skills)
- [Cursor：Agent Skills](https://prod.cursor.com/docs/skills)
- [Windsurf：Cascade Skills](https://docs.windsurf.com/windsurf/cascade/skills)
- [OpenClaw：Skills system](https://github.com/openclaw/openclaw/blob/main/docs/tools/skills.md)
- [OpenClaw：Skills CLI](https://docs.openclaw.ai/cli/skills)
- [OpenClaw：Sandboxing](https://docs.openclaw.ai/sandboxing)
