# Plan: README 多工具入口 —— 一套 skill 适配 openclaw / Claude Code / Kilo Code / Codex

## 审查结论（已确认）

**可以给其它编程软件使用，无需按工具开分支重构。** 理由：

- skill 的全部功能 = bash + uv + Python CLI 脚本（`scripts/*`），与具体 agent 产品零耦合；任何有 shell 的编程助手都能执行。
- 工具相关的只有文档层：README 的克隆路径（`~/.openclaw/workspace/skills/...`）与"openclaw"措辞；SKILL.md frontmatter 里的 openclaw 专用键（`triggers`、`metadata.clawdbot`）。
- 各工具加载方式均已核实：
  - **openclaw**：现状（`~/.openclaw/workspace/skills/llm-api-test`）。
  - **Claude Code**：`~/.claude/skills/<name>/SKILL.md`（本机 `~/.claude/skills/` 已有实例佐证），只要求 `name`/`description` frontmatter——已满足；额外键被忽略。
  - **Kilo Code**：从配置目录发现 `skills/<name>/SKILL.md`（全局 `~/.config/kilo/skills/`，亦支持 `~/.kilo/skills/` 与项目级 `.kilo/skills/`），同样只要求 `name`/`description`（kilo-config 文档核实）。
  - **Codex**：无 skill 加载机制；克隆到任意目录，提示词引导 agent 把 `SKILL.md` 当操作手册遵循（或在项目 AGENTS.md 中指路）。

## 决策（用户已确认）

1. README 按工具分四个子块，每块含「克隆命令 + 可粘贴提示词」；openclaw 块保持已验证内容。
2. 正文「openclaw」措辞泛化为中性（agent / 编程助手）。

## 现状关键事实（实施时注意）

- 现有提示词块（README.md:64-72）用占位符 `<你的 workspace skills 目录>`；改造后每块直接写该工具的具体路径，不再用占位符。
- 现有两个小节「快速上手（安装）」（命令，README.md:43-58）与「可直接粘贴给 openclaw 的安装提示词」（README.md:60-72）**合并为一个**「快速上手（安装）」节，下设四个 `####` 工具子块，每块依次含：①克隆+setup+console 命令块 ②可粘贴提示词块。
- 提示词第 2/3/6 步（setup.sh、数据目录、红线）四个工具完全一致；差异仅：收件人措辞、路径、Codex 块附加指路句。

## 任务清单

### 1. README.md 改造

- 标题 `# llm-api-test（openclaw skill）` → `# llm-api-test（AI 编程助手 skill）`。
- 开头定位段（README.md:5）："说给 openclaw"→"说给你的编程助手"，并加一句兼容性说明：适用于 openclaw / Claude Code / Kilo Code / Codex 及任何可执行 shell 命令的编程 agent。
- 「快速上手（安装）」重构为四个子块（顺序：openclaw → Claude Code → Kilo Code → Codex）：
  - `#### openclaw`：命令块与提示词块**逐字保留现有已验证内容**（路径 `~/.openclaw/workspace/skills/llm-api-test`，提示词内占位符 `<你的 workspace skills 目录>` 保持原样——该块已验证，不动）。
  - `#### Claude Code`：命令路径 `~/.claude/skills/llm-api-test`；提示词以 openclaw 版为模板，首句"发给 Claude Code"，占位符替换为具体路径 `~/.claude/skills`。
  - `#### Kilo Code`：命令路径 `~/.config/kilo/skills/llm-api-test`；提示词同上模板，首句"发给 Kilo Code"，路径 `~/.config/kilo/skills`；附注"项目级用法可克隆到 `<项目>/.kilo/skills/llm-api-test`"。
  - `#### Codex`：命令路径示例 `~/skills/llm-api-test`（任意目录）；提示词同上模板，首句"发给 Codex"，并附加一句："Codex 不会自动加载 skill，之后把 `<克隆路径>/llm-api-test/SKILL.md` 作为操作手册遵循（也可在你的 AGENTS.md 中加入指向该文件的说明）"。
  - 前置要求句（`bash`+`curl`、uv 受管 Python）上移到四个子块之前，只写一次。
- 「日常使用（对 openclaw 说什么）」→「日常使用（对编程助手说什么）」；表头"openclaw 做什么"→"助手做什么"；表格内容不动。
- 「结果在哪」"对 openclaw 说"→"对助手说"。
- 文档地图"openclaw 按它工作"→"agent 按它工作"（README.md:12 目录结构注释同款修改）。

### 2. SKILL.md 措辞泛化（轻量）

- 正文 3 处"openclaw"→"agent"（SKILL.md:68、77、198）。
- **保留不动**：frontmatter 的 `triggers` 与 `metadata.clawdbot`（openclaw 专用，Claude Code/Kilo 忽略未知键，删了反而丢 openclaw 能力）。

### 3. references 措辞泛化（轻量）

- `references/console-access.md` 3 处（:9、:34、:37）、`references/supplier-onboarding-workflow.md` 1 处（:5）"openclaw"→"agent/助手"。仅此措辞替换。

### 4. 版本与元数据

- `SKILL.md` frontmatter `version: 1.1.0` → `1.1.1`，`_meta.json` 同步。
- `_meta.json` 其余字段不动；`.kilo/plans/` 历史文件中的 openclaw 字样不改（历史记录）。

### 5. 校验与提交

- `grep -rn "openclaw" README.md SKILL.md references/` 确认残留仅在允许位置（SKILL.md frontmatter、openclaw 子块）。
- `cd app && ../.venv/bin/python -m pytest tests/test_documentation.py -q` 确认无回归。
- 一次 commit + push 到 main（沿用仓库 deploy key 配置）。

## 不做

- 不开 per-tool 分支、不做任何代码/脚本重构。
- 不动 `app/` vendored 引擎与文档。
- 不删 SKILL.md frontmatter 的 openclaw 专用键。
- 不为 Codex 在仓库根新增 AGENTS.md（提示词中说明即可，避免 skill 仓库根出现 agent 指令文件产生歧义）。

## 风险

- Claude Code / Kilo 对 SKILL.md 多余 frontmatter 键的容忍属"未知键忽略"惯例，低风险；如未来报错再裁剪。
- Codex 无自动触发机制，使用依赖用户粘贴提示词或 AGENTS.md 指路——已在 Codex 块注明，属预期限制。
- 四个提示词块内容高度重复（仅路径/措辞不同）：接受重复换取"照抄即用"，与已验证的 openclaw 块保持同构。
