# 控制台访问与公网发布指南

Web 控制台默认监听 `0.0.0.0:8090` 并启用登录认证。本文档说明密码管理与两种公网发布方式。
`{baseDir}` = skill 根目录；`$DATA` = 数据目录（默认 `~/.config/llm-api-test/`）。

## 1. 登录密码

- 首次 `console.sh start` 自动生成 `admin` + 随机密码，存于 `$DATA/console_auth.json`（PBKDF2 哈希）。
- openclaw 查看并告诉用户：`bash {baseDir}/scripts/console.sh passwd`
- 用户改密码：`bash {baseDir}/scripts/console.sh passwd --set <新密码>`（立即生效，已有会话保持有效）
- 重置随机密码：`bash {baseDir}/scripts/console.sh passwd --reset`
- 密码也可用环境变量固定：`WEB_CONSOLE_USER` / `WEB_CONSOLE_PASSWORD`（此时 `passwd` 会提示去环境变量处改）。
- 认证是**可选**的：`LLM_API_TEST_DISABLE_AUTH=1 bash {baseDir}/scripts/console.sh start` 关闭（仅限可信网络）。

## 2. 公网方式一：Cloudflare 快速隧道（零配置，推荐先试用）

```bash
bash {baseDir}/scripts/console.sh tunnel
# 输出 public url: https://xxxx.trycloudflare.com，把地址 + 密码一起发给用户
```

- 无需 Cloudflare 账号；cloudflared 自动下载到 `~/.local/bin/`。
- 地址随机、每次重建都会变化；无 SLA；适合临时共享。
- 停止：`bash {baseDir}/scripts/console.sh tunnel-stop`。

## 3. 公网方式二：Cloudflare 账户命名隧道（固定域名，长期使用）

前提：用户有 Cloudflare 账户，且有一个域名已接入 Cloudflare DNS。

**用户侧一次性操作（引导用户完成）：**

1. 登录 https://one.dash.cloudflare.com/ → 左侧 **Networks → Tunnels → Add a tunnel**。
2. 选 **Cloudflared**，命名（如 `llm-api-test`），保存。
3. 创建页会显示安装命令，其中 `--token` 后面那串就是隧道 token，复制发给 openclaw。
4. 在隧道的 **Public Hostname** 页添加：子域名如 `llm-test`、域名选自己的域名、`Service` 类型 `HTTP`、URL 填 `127.0.0.1:8090`（本项也可由隧道侧忽略，以 dashboard 为准）。

**openclaw 侧：**

```bash
bash {baseDir}/scripts/console.sh tunnel --token <用户给的 token>
# 或写入环境后常用：export CLOUDFLARE_TUNNEL_TOKEN=<token>
```

固定公网地址即为 dashboard 里配置的主机名（如 `https://llm-test.example.com`）。
token 等效于隧道控制权，等同密码保管；可写入 `$DATA/.env`（600 权限）持久化。

## 4. 网络兼容性

两种方式的 cloudflared 都以 `--protocol http2`（TCP 443）运行，封锁 UDP/QUIC 的网络也能出公网。如企业网络拦截 trycloudflare.com 域名本身，只能用方式二自有域名。

## 5. 生产化建议（超出本 skill 范围）

- 命名隧道 + 自有域名 + Cloudflare Access（再加一层 IdP 登录）。
- 或自建 frp/nginx 反代 + Basic Auth。
