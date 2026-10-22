# Dashboard 使用标准 HTTPS 端口

在现有集成门户 Compose 文件及生产版本覆盖文件之后，追加 `-f docker-compose.dashboard-https443.yml`。只重新创建 `gateway` 和 `jusha` 服务，并继续使用已部署的镜像；Fleet 后端及下载节点不需要重启。

该覆盖文件同时设置 `DASHBOARD_PUBLIC_ORIGIN=https://${DASHBOARD_IP}`，更新 Caddy 配置挂载并发布 TCP 443。必须同步修改入口和登录来源校验，否则登录写请求会被拒绝。

访问地址为 `https://服务器IP`；旧 HTTPS 3000 入口以 308 跳转到新入口，保留路径和查询参数。HTTPS 8443 继续服务节点上报和 Fleet API，其旧网页入口跳转到标准 HTTPS 门户。证书继续使用原 IP 证书及续期任务。

切换前检查 443 没有其他服务占用，验证 Caddy 和合并后的 Compose 配置。切换后验证证书、登录、来源校验、只读权限、旧入口跳转以及 8443 节点上报。门户重新创建后原会话需重新登录。
