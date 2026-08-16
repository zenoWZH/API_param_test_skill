# Plan: 提升 SKILL 可读性 —— README 重写为完整用户入口

## 背景与结论

已确认方案：**扩展 README.md** 为面向日常使用者的完整入口（不新建独立用户指南，避免重复维护）。SKILL.md 保持 agent 操作手册定位，仅做轻量一致性润色。

现状：README（49 行）偏安装；SKILL.md（194 行）面向 agent；references/ 三篇专题（console-access / supplier-onboarding-workflow / testing-guide）。日常使用者缺一份"怎么用"的说明。

## 目标读者

1. 用户本人/测试负责人：通过 openclaw 对话驱动测试、偶尔用 Web 控制台
2. 新接触者：需要知道这是什么、怎么装、结构是什么

## 任务清单

### 1. 重写 README.md（保留现有安装命令与提示词块，扩展为用户入口）

结构（自上而下）：

- **这是什么**：一句话定位 + 能力清单（参数合规/缓存/API 溯源/图片/并发压测 + 准入工作流 + 可选控制台）
- **目录结构**：当前"目录说明"扩写——顶层布局（SKILL.md=agent 手册、scripts/ 八个脚本各自一行注释、app/=vendored 引擎+补丁说明、references/、_meta.json）+ 数据目录 `~/.config/llm-api-test/` 布局（.env / providers.local.yaml / console_auth.json / upstream_fingerprints.json / reports/jobs/ / workflows/）
- **快速上手**：现有 4 步安装命令 + 提示词块原样保留（已验证可复制）
- **日常使用（对 openclaw 说什么）**：话术示例表——
  - "帮我接入新供应商 X 的模型 M"（走准入工作流）
  - "接着上次测 X 的 M"（中途接手）
  - "测一下 X 的 M 的缓存"（单点测试）
  - "停掉那个任务" / "结果怎么样，给我总结"
  - "把控制台地址和密码告诉我" / "我要改密码" / "我要公网访问"
- **准入流程图**：简版文字流程（①→⑫一句话版），链接 references/supplier-onboarding-workflow.md 看完整 mermaid
- **结果在哪**：报告目录布局、verdict 含义一句话、让 openclaw 总结即可
- **FAQ**：费用提醒（真实付费 API）、弱机压测可信度（1核/512MB–1GB 可用、高并发绝对值仅供参考）、Python 由 uv 受管无需装系统包、端口冲突改 WEB_CONSOLE_PORT、溯源语料库需先建库、测试基线 pytest 全绿
- **安全说明**：现有块保留（登录/隧道/密钥位置）
- **文档地图**：SKILL.md（agent 手册）、references/ 三篇、app/docs/（引擎详版文档）各一行说明

写作要求：全中文；用户话术部分用引用块/表格方便照抄；所有路径/命令与现有脚本实际行为一致（以 scripts/ 源码为准核对）；不引入未实现的功能描述。

### 2. SKILL.md 轻量润色（不动实质内容）

- 文件顶部加目录（TOC）锚点列表
- 修正遗留不一致：`version: 1.0.0` → `1.1.0`（frontmatter 与 _meta.json 同步 bump，反映 auth/tunnel/CLI stop 等增强）
- 检查 `{baseDir}` 用法与 `$PY` 定义全文一致

### 3. 校验

- 所有 README/SKILL 中引用的相对路径逐一核实存在（references/*、app/docs/*、scripts/*）
- `cd app && pytest tests/test_documentation.py -q` 通过（该测试校验 docs 链接；README 在根目录不受其约束，但顺手确认无回归）
- git add/commit/push 到 main

## 不做

- 不新建 guides/ 或 USER_GUIDE.md
- 不改 references/ 三篇正文内容（仅在 README 文档地图中链接）
- 不动 app/ 内 vendored 文档
- 不改任何脚本行为
