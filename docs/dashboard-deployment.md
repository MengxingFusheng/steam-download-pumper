# 可选 Fleet Dashboard

Dashboard 是独立的 HTTPS 控制台与 SQLite 账本服务，不承担下载流量。节点通过 `steam_pumper.dashboard_agent` 出站注册、发送心跳和统计并领取命令。未设置 `DASHBOARD_URL` 时不启用代理。

## 云端准备

复制 `.env.dashboard.example` 为 `.env.dashboard`，设置自己的域名；DNS 指向云端，并允许 HTTPS。创建仅用于本部署的 `dashboard-secrets/`，配置三个文件：

- `admin_password_hash`：通过 `python3 -m dashboard.security` 交互生成的管理员密码哈希。
- `enrollment_token`：至少 32 字符的随机注册令牌。
- `read_token`：至少 32 字符且与注册令牌不同的随机只读令牌。

将这些文件仅授权给容器用户 UID 10001 读取；不要将凭据加入 Git。使用 `docker compose --env-file .env.dashboard -f docker-compose.dashboard.yml up -d --build` 构建和启动云端服务，容器内 `/healthz` 用于健康检查。Caddy 根据域名提供 HTTPS。

## 节点接入

在节点 `.env.fleet` 中设置 HTTPS `DASHBOARD_URL`、唯一 `DASHBOARD_NODE_NAME` 和可选外部标识。将注册令牌保存到节点 `fleet-secrets/dashboard_enrollment_token`，通过 `docker-compose.fleet-dashboard.yml` overlay 只读挂载。节点必须使用本版本构建的 Fleet 镜像。已有节点应保留 `/data/dashboard-agent.sqlite3`，避免重新生成身份。

## 备份和运行

持久化并定期备份 Dashboard 的 SQLite 数据卷及节点身份库。需要在线备份时使用 `python3 -m dashboard.backup --help` 查看支持的参数。单实例运行 API 服务；反向代理和 SQLite 数据不得对公网直接开放。
