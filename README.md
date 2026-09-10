# Broadband Download Pumper

面向爱快多 WAN 环境的公共 HTTP/HTTPS 下载流量工具。下载正文直接写入内存中的 `io.Discard`，不生成下载文件，不持续擦写硬盘，并优先使用 IPv4。

项目保留两个下载器版本，并增加一个不承担下载流量的源清单发布器：

| 版本 | Docker Hub | 网络模型 |
|---|---|---|
| 爱快单线路 | `traveler1314/ikuai-line-pumper:latest` | 一个容器、一个 IP、一条 WAN |
| 多 IP V2 | `traveler1314/multi-ip-pumper:oss-v2` | 一个容器，每条 WAN 对应一个源 IP，支持 OSS 签名源清单 |
| 源清单发布器 | `traveler1314/pumper-source-publisher:publisher-v1` | 无入站端口，每日验证并发布清单 |
| Fleet | `traveler1314/fleet-pumper:latest` | 100+ 源 IP、SNMP 上传账本、按需启动下载线路 |

GHCR 对应镜像位于 `ghcr.io/mengxingfusheng/`。OSS 远程清单仅用于标准 Docker 的多 IP V2，不作为爱快 Docker 插件的兼容功能。

## Fleet：100+ PPPoE 账号滚动调度

Fleet 用于“每个 PPPoE 账号都需要持续拉下载”的场景，部署在 **PCDN 服务器本机** 的 Docker 中。它不接管、不重启或修改现有 PCDN 进程；容器使用独立的 macvlan 二层接口和 LAN 源 IP，因此不会向 PCDN 宿主机添加 IP、路由或防火墙规则。每个账号仍由你在 iKuai 中静态分流到对应 PPPoE，程序只按时段启动 10–20 条下载线路，不会常驻启动全部账号。

账号清单格式见 `fleet-config/accounts.csv.example`：

```csv
account_id,bind_ip,snmp_interface,enabled
pppoe-001,192.168.2.11,pppoe-wan001,true
```

创建仅自己可读的 `fleet-secrets/ikuai_snmp_community`，再复制 `.env.fleet.example` 为 `.env.fleet`，设置 `SNMP_HOST`、LAN 网段和容器首个地址后，即可先在 PCDN 服务器启动空账号实例：

```bash
docker compose --env-file .env.fleet -f docker-compose.fleet.yml up -d
```

天津当前部署的 139 账号完整映射见 `fleet-config/accounts.139.csv`，使用
`192.168.2.11–192.168.2.149/24`，iKuai/Fleet 网关为 `192.168.2.1`。CSV
导入是整表替换，不能只上传新增账号。账号下载连续 90 秒无进度，或连续
180 秒平均低于 50 Mbps 时，会立即停止该线路、冷却 30 分钟并从其他账号
补位；健康线路仍遵守 30 分钟最短连续运行时间。

Fleet 固定按 `00:00–01:00` 2 Gbps、`01:00–07:00` 4 Gbps、
`07:00–18:00` 2 Gbps 调度。`18:00–24:00` 为 PCDN 上传专用时段，
不启动任何 Fleet 下载进程，但 SNMP 采样和滚动账本仍持续运行。

默认 `FLEET_AUTOSTART=false`：容器只进行低频 SNMP 采样和账本预热，**不会产生下载流量**。完成单账号方向校准、源 IP→PPPoE 分流和 iKuai 的每账号 200 Mbps / 总下载 4 Gbps 硬限速验证后，再通过控制台“启动”或将 `FLEET_AUTOSTART=true` 后重建容器。

账号映射可在部署后由同一 LAN 的管理设备通过 `http://CONTAINER_IP/` 上传 CSV；接口会整批校验，任一行有误不会修改当前账本或已运行线路。也可将 CSV 挂载到容器内并设置 `FLEET_ACCOUNTS_CSV` 作为首次启动的预置清单。

为尽量不影响原业务，Fleet 容器默认限制为 8 CPU、2 GiB 内存、512 个 PID（为 20 条 Go 下载进程及 SNMP 采集线程保留余量），设置低 CPU 权重，并以 `nice 19` 与 `ionice idle` 运行；根文件系统只读、临时目录为小型 tmpfs、Docker 日志上限 10 MiB，且没有 `host` 网络、特权模式或端口映射。`FLEET_DATA_DIR` 应放在与 PCDN 缓存低竞争的本地文件系统；它只写入小型 SQLite 账本，不写下载文件。

> macvlan 容器默认不能从宿主机直接访问。控制台请从同一 LAN 的其他管理设备访问 `http://CONTAINER_IP/`；或仅在需要本机管理时，按 Docker/macvlan 文档额外建立宿主机 shim 接口。该 shim 不属于默认部署，避免改变 PCDN 宿主机网络。

Fleet 的 HTTP API：

- `GET /api/fleet/summary`：滚动总量、倍率、预热和 SNMP 状态；
- `GET /api/fleet/accounts`：逐账号状态；
- `GET /api/fleet/report.csv`：滚动 15 天对账导出；
- `POST /api/fleet/accounts/import`：以 `Content-Type: text/csv` 原子替换账号清单。

下载源使用签名的 Speedtest-only 清单。候选 URL 由 `source-list/candidates.json` 维护，发布器每日两次 Range 探测，只有中位速度不低于 `MIN_PROBE_MBPS=100` 的健康源才会发布。候选至少应准备 3 个经授权的 Speedtest 下载 URL。

## 爱快单线路版

适用于爱快 3.x Docker 插件。一个容器对应一条外网线路，容器 IPv4 地址在爱快创建容器时设置，再按该源 IP 手动绑定指定 WAN。

镜像：

```text
traveler1314/ikuai-line-pumper:latest
```

爱快创建容器时建议：

- 内存上限填写 `128` 或 `256` MB，不要填写 `0`。
- 网络接口和固定 IPv4 均在爱快 Docker 插件中设置。
- 容器端口为 `80`，创建后直接访问 `http://容器IP/`。
- 需要长期保留 Web 配置时，将一个持久化目录挂载到 `/data`。
- 每条 WAN 创建一个容器，并在爱快策略中做源 IP 到 WAN 的固定绑定。

可选环境变量：

```text
TARGET_MBPS=400
CONNECTIONS_PER_LINE=8
MAX_CONNECTIONS_PER_LINE=12
START_TIME=00:00
END_TIME=18:00
SOURCE_POOL=http://公共源/file1,http://公共源/file2
```

容器 IP 不由镜像内部设置，也不需要 `NET_ADMIN` 权限。

## 多 IP 版

适用于普通 Docker 主机上的 macvlan 部署。容器挂载多个 IPv4，每个下载引擎绑定一个源 IP；用户在爱快中将这些源 IP 分别绑定到对应 WAN。

一键部署：

```bash
curl -fsSL https://raw.githubusercontent.com/MengxingFusheng/steam-download-pumper/main/install-multi-ip.sh | bash
```

默认部署两条线路、总目标 `800 Mbps`，从 `192.168.1.233` 开始探测可用地址。常用覆盖参数：

```bash
LINE_COUNT=4 \
LAN_IPS=192.168.1.233,192.168.1.234,192.168.1.235,192.168.1.236 \
TARGET_MBPS=1600 \
LAN_PARENT=ens18 \
bash install-multi-ip.sh
```

安装器会将首个 `LAN_IPS` 地址写为 Compose 的 `CONTAINER_IP`，其余地址由容器启动后添加到 `eth0`。`LAN_IPS` 数量必须等于 `LINE_COUNT`，地址必须是唯一 IPv4。

### OSS 远程源清单

多 IP V2 可在启动时和每日自动拉取阿里云 OSS 中的 Ed25519 签名清单：

```text
REMOTE_SOURCE_LIST_ENABLED=true
SOURCE_LIST_URL=https://<bucket>.oss-cn-beijing.aliyuncs.com/pumper/v1/latest.json
SOURCE_LIST_PUBLIC_KEY=<base64-ed25519-public-key>
SOURCE_LIST_KEY_ID=pumper-source-2026-01
SOURCE_LIST_REFRESH_TIME=04:00
SOURCE_LIST_REFRESH_JITTER_SECONDS=1800
```

这些安全参数只从容器环境读取，不能通过 Web 控制台修改。新清单通过 `SIGHUP` 热加载，不重启容器或 Go 下载进程。OSS 访问、验签或清单校验失败时，容器继续使用 `/data` 中最后可用清单，再退回本地 `SOURCE_POOL`。

## 源清单发布器

发布器每天 `03:17 Asia/Shanghai` 对受控候选源进行 IPv4、重定向、可达性和有界速率检测，然后签名并上传到 OSS。容器无 Web、无入站端口，只需一个受限 RAM 账号和 Ed25519 私钥。

```bash
cp .env.publisher.example .env.publisher
bash install-publisher.sh
docker compose -f docker-compose.publisher.yml --env-file .env.publisher ps
```

手动检测和发布：

```bash
docker compose -f docker-compose.publisher.yml --env-file .env.publisher run --rm source-publisher validate-only
docker compose -f docker-compose.publisher.yml --env-file .env.publisher run --rm source-publisher publish-once
```

完整的 OSS 策略、secret 挂载、健康检查和故障恢复见 `docs/publisher-docker.md`。

## 运行控制

两个版本使用相同的浏览器控制台和 API：

- 启动/停止下载；
- 修改目标 Mbps、每线基础连接数、每线最大连接数和运行时间窗；
- 编辑公共 HTTP/HTTPS 源池；
- 查看当前、10 秒和 60 秒 Mbps、今日累计量、目标达成率；
- 查看每条线路的实时速度、连接数、绑定 IP、状态和错误；
- 查看源健康、连续失败、隔离倒计时与纯 UTF-8 日志。

`MAX_CONNECTIONS_PER_LINE` 的硬上限为 `12`。低于线路目标 90% 时每次增加一个连接；启用目标控制且高于 115% 时每次减少一个连接。连接调整通过信号完成，不重启下载引擎。

`TARGET_MBPS` 是自适应连接目标，不是内核整形限速。实际速度取决于线路、源池和运营商容量。默认验收口径为 60 秒均值达到目标的 90%，运行时间窗内累计流量达到理论值的 80%。

默认源池包含经过四条实际 WAN 验证的上海联通、上海在线、中科院、北京大学和华为云公开节点。连续失败 3 次的源会自动隔离，隔离时间依次为 10、30、60 分钟；到期只允许一个连接探测，成功后自动恢复。所有源都隔离时 helper 保持运行并等待探测，不会通过反复重启清空隔离状态。

## 数据与资源

- Go `discarder` 强制 `tcp4`，正文流入 `io.Discard`。
- 每条逻辑线路仅运行一个 Go 进程，进程内部使用 1-12 个连接协程。
- Python 只保留调度线程和指标线程，不为每个连接创建线程。
- 根文件系统可只读，临时文件使用 tmpfs，Docker 日志有大小和文件数上限。
- 配置原子写入 `/data/config.json`；镜像不包含 SteamCMD。

## 开发验证

```bash
python3 -m unittest discover -s tests -v
go test -race ./...
go vet ./...
docker compose -f docker-compose.multi-ip.yml config
docker build -f Dockerfile.ikuai-line .
docker build -f Dockerfile.multi-ip .
```

发布脚本会先完成全部测试和两个镜像的冒烟检查，再统一推送两个版本。
