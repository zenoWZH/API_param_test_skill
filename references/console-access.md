# 控制台访问与网络暴露边界

Web 控制台是可选人工界面，CLI 测试不依赖它。默认监听 `127.0.0.1:8090` 并启用认证；启动控制台、显示密码、监听非 loopback 和创建公网隧道都是彼此独立的显式操作。

下文 `<skill-root>` 是包含 `SKILL.md` 的目录。数据默认位于 `${XDG_CONFIG_HOME:-$HOME/.config}/llm-api-test`，可用 `LLM_API_TEST_DATA_DIR` 覆盖。

## 本地启动

```bash
bash <skill-root>/bin/llm-api-test console start
bash <skill-root>/bin/llm-api-test console status
bash <skill-root>/bin/llm-api-test console stop
```

如需改端口，只为该进程设置 `WEB_CONSOLE_PORT`。不要为了远程访问直接把 `WEB_CONSOLE_HOST=0.0.0.0` 当默认做法；应先明确网络边界、认证和反向代理策略。

## 密码管理

- 首次启动会生成 `admin` 的随机密码；哈希与可恢复副本都在私有 data 目录，权限为 `0600`。
- agent 默认不得读取或打印密码。确有需要时，由人在可信终端本地运行 `console passwd --reveal`。
- 交互改密：`console passwd --set`。脚本会隐藏输入并二次确认，不接受 argv 中的明文密码。
- 受控自动化可把密码通过 stdin 传给 `console passwd --password-stdin`，但不得把值写进命令、日志或聊天。
- `console passwd --reset` 只生成并保存新密码，不在输出中显示；之后仍由人在本地选择是否 `--reveal`。
- `WEB_CONSOLE_PASSWORD` 可由宿主 secret store 注入；脚本不会把该值打印出来。

关闭认证只适用于明确隔离的可信 loopback 场景：

```bash
LLM_API_TEST_DISABLE_AUTH=1 \
bash <skill-root>/bin/llm-api-test console start
```

不得把关闭认证的控制台绑定到非 loopback、反向代理或公网隧道。

## Cloudflare 快速隧道

隧道不是 setup 的一部分。先从 Cloudflare 官方渠道安装并审核固定版本的 `cloudflared`；本脚本不会自动下载 latest 二进制。获得明确公网暴露批准后：

```bash
bash <skill-root>/bin/llm-api-test console tunnel
bash <skill-root>/bin/llm-api-test console tunnel-url
bash <skill-root>/bin/llm-api-test console tunnel-stop
```

快速隧道生成随机 `trycloudflare.com` 地址，无 SLA。URL 可以分享，但密码仍不得由 agent 从文件读取并转发到聊天。

## Cloudflare 命名隧道

先由账户管理员在 Cloudflare Zero Trust 中创建 tunnel 与 Public Hostname，并把 service 指向 `http://127.0.0.1:8090`。Tunnel token 等同控制权凭据，禁止粘贴进聊天或 argv。

推荐由 secret store 注入：

```bash
CLOUDFLARE_TUNNEL_TOKEN='<secret-store-injected>' \
bash <skill-root>/bin/llm-api-test console tunnel
```

或使用仅当前用户可读的普通文件：

```bash
chmod 600 /secure/path/cloudflared.token
bash <skill-root>/bin/llm-api-test console tunnel \
  --token-file /secure/path/cloudflared.token
```

`tunnel --token <value>` 会被脚本拒绝，因为进程 argv 可能被 shell history、进程列表或 agent 日志记录。

## Container 与 OpenClaw sandbox

Host 侧发现 skill 不表示 sandbox 已能启动服务。需分别确认：

- skill 目录在 sandbox 中完整可读；
- data/runtime 是窄范围可写 bind，且环境变量在 sandbox 内实际生效；
- loopback socket/端口绑定被允许；
- 如需外部访问，端口发布、反向代理和 HTTPS 出站分别获得批准；
- 密钥通过 sandbox 支持的 secret/env 注入或只读 secret mount 提供，不假设 host 的 skill env 自动传入。

不要为解决权限问题挂载整个 home、Docker socket、SSH 目录或云厂商凭据目录。OpenClaw 的完整配置与验收见 [tool-compatibility.md](tool-compatibility.md)。
