# Broadband Download Pumper

面向爱快多 WAN 环境的公共 HTTP/HTTPS 下载流量工具。下载正文直接写入内存中的 `io.Discard`，不生成下载文件，不持续擦写硬盘，并优先使用 IPv4。

项目保留两个下载器版本，并增加一个不承担下载流量的源清单发布器：

| 版本 | Docker Hub | 网络模型 |
|---|---|---|
| 爱快单线路 | `traveler1314/ikuai-line-pumper:latest` | 一个容器、一个 IP、一条 WAN |
| 多 IP V2 | `traveler1314/multi-ip-pumper:oss-v2` | 一个容器，每条 WAN 对应一个源 IP，支持 OSS 签名源清单 |
| 源清单发布器 | `traveler1314/pumper-source-publisher:publisher-v1` | 无入站端口，每日验证并发布清单 |
| Fleet | `traveler1314/fleet-pumper:latest` | 100+ 源 IP、SNMP 上传账本、按需启动下载线路 |

GHCR 对应镜像位于 `ghcr.io/mengxingfusheng/`。OSS 远程清单用于标准 Docker 的多 IP V2 和 Fleet，不作为爱快 Docker 插件的兼容功能。

## Fleet：100+ PPPoE 账号每日预测调度

Fleet 用于“每个 PPPoE 账号都需要持续拉下载”的场景，部署在 **PCDN 服务器本机** 的 Docker 中。它不接管、不重启或修改现有 PCDN 进程；容器使用独立的 macvlan 二层接口和 LAN 源 IP，因此不会向 PCDN 宿主机添加 IP、路由或防火墙规则。每个账号仍由你在 iKuai 中静态分流到对应 PPPoE，程序根据每日欠量、线路档位、分组和 WAN 预算按需启动下载线路，最多同时运行 20 条，不会常驻启动全部账号。

账号清单格式见 `fleet-config/accounts.daily.csv.example`：

```csv
account_id,bind_ip,snmp_interface,enabled,weight
pppoe-001,192.168.2.11,pppoe-wan001,true,1
```

创建仅自己可读的 `fleet-secrets/ikuai_snmp_community`，再复制 `.env.fleet.example` 为 `.env.fleet`，设置 `SNMP_HOST`、LAN 网段和容器首个地址后，即可先在 PCDN 服务器启动空账号实例：

```bash
docker compose --env-file .env.fleet -f docker-compose.fleet.yml up -d
```

生产账号清单由部署环境提供，仓库只提供格式示例。导入前应核对线路、绑定 IP 和带宽档案。
历史 129 账号完整映射保留在 `fleet-config/accounts.129.csv`（2026-09-21 更新），使用
`192.168.2.11–192.168.2.139/24`，iKuai/Fleet 网关为 `192.168.2.1`。CSV
导入是整表替换，不能只上传新增账号。账号下载连续 90 秒无进度时，会停止该线路、冷却 30 分钟并从其他账号
补位。连续 180 秒平均低于 `min(50 Mbps, 分配预算 × 25%)` 但仍有下载进度时，只记录低速提示，
继续下载，由引擎自动隔离慢源、切换下载源并探测恢复；健康线路仍遵守 30 分钟最短连续运行时间。

Fleet 统一使用每日预测调度：`00:00–18:00` 按基础目标优先下载，`18:00–23:00` 停止下载，`23:00–24:00` 在最多 2 Gbps 预算内补当天缺口。SNMP 采样和历史账本持续运行。

默认 `FLEET_AUTOSTART=false`：容器只进行低频 SNMP 采样和账本预热，**不会产生下载流量**。完成单账号方向校准、源 IP→PPPoE 分流和 iKuai 的每账号 200 Mbps / 总下载 4 Gbps 硬限速验证后，再通过控制台“启动”或将 `FLEET_AUTOSTART=true` 后重建容器。

账号映射可在部署后由同一 LAN 的管理设备通过 `http://CONTAINER_IP/` 上传 CSV；接口会整批校验，任一行有误不会修改当前账本或已运行线路。也可将 CSV 挂载到容器内并设置 `FLEET_ACCOUNTS_CSV` 作为首次启动的预置清单。

为尽量不影响原业务，Fleet 容器默认限制为 8 CPU、2 GiB 内存、512 个 PID（为 20 条 Go 下载进程及 SNMP 采集线程保留余量），设置低 CPU 权重，并以 `nice 19` 与 `ionice idle` 运行；根文件系统只读、临时目录为小型 tmpfs、Docker 日志上限 10 MiB，且没有 `host` 网络、特权模式或端口映射。`FLEET_DATA_DIR` 应放在与 PCDN 缓存低竞争的本地文件系统；它只写入小型 SQLite 账本，不写下载文件。

> macvlan 容器默认不能从宿主机直接访问。控制台请从同一 LAN 的其他管理设备访问 `http://CONTAINER_IP/`；或仅在需要本机管理时，按 Docker/macvlan 文档额外建立宿主机 shim 接口。该 shim 不属于默认部署，避免改变 PCDN 宿主机网络。

Fleet 下载进程的 DNS 查询与 HTTP/TCP 连接使用相同的账号源 IP。
`DISCARDER_DNS_SERVERS` 默认为 `223.5.5.5,119.29.29.29`，可配置为逗号分隔的
IPv4 DNS 地址。查询直接访问这些服务器，避免容器主地址所属 PPPoE 断线时，
Docker 内置 DNS 的共享转发出口导致其他健康账号也无法下载。此设置只作用于
下载进程；容器内其他程序仍使用系统 DNS。

Fleet 的 HTTP API：

- `GET /api/fleet/summary`：滚动总量、倍率、预热和 SNMP 状态；
- `GET /api/fleet/accounts`：逐账号状态；
- `GET /api/fleet/report.csv`：滚动 15 天对账导出；
- `POST /api/fleet/accounts/import`：以 `Content-Type: text/csv` 原子替换账号清单。

下载源使用签名清单，由 `192.168.1.70` 上的发布器集中发现。所有端点统一经过真实字节初筛和同一 Go 引擎的两轮持续测速：12 连接、每轮 15 秒、预热 3 秒，取较低成绩且需达到 `MIN_PROBE_MBPS=100`。先选择不同主机的最佳端点，再补同主机次优端点，每主机最多两个 URL；不足三个主机时报告冗余不足，不用低速源凑数。部署时将发布器 `MIN_HEALTHY_SOURCES` 和节点 `SOURCE_LIST_MIN_SOURCES` 设为 `1` 可使用小型合格源池；没有合格源则保留有效旧清单。节点将连接集中到实测快源并受控探索备用源，单线最多 12 连接。配置和兼容升级顺序见 [发布器说明](docs/publisher-docker.md)。

### 每日预测、分组调度与 5G 预算

每日预测是 Fleet 唯一调度模式；旧文件或环境中的 `daily_mode_enabled=false` /
`DAILY_MODE_ENABLED=false` 在加载时自动迁移为开启，API 不再允许关闭。旧的
`per_line_mbps`、`line_minimum_mbps` 配置只在旧文件读取时兼容忽略，不再提供控制项。
开始下载前，必须为所有启用账号通过整表 CSV 确认 `weight`（仅接受 `1` 或 `10`），并在爱快设置单线及
全路由硬限速。**程序不实施下载限速、不读取或修改爱快分流配置**；CSV 权重是
你核对多线负载比例后导入的带宽档案，不能从 SNMP IF-MIB 自动推断。

```csv
account_id,bind_ip,snmp_interface,enabled,weight,group_id
pppoe-001,192.168.2.11,pppoe-wan001,true,1,standard
pppoe-002,192.168.2.12,pppoe-wan002,true,10,symmetric500
```

权重 `1` 代表上传 50 Mbps / 下载 200 Mbps，权重 `10` 代表 500 Mbps 对等。
对于 500 Mbps 下载、50 Mbps 上传的节点，可设置 `FLEET_STANDARD_DOWNLOAD_MBPS=500`；
它只调整该实例普通线路的下载调度档位，上传预测仍按 50 Mbps，网络限速需另行配置。
`group_id` 可省略，分别进入 `standard`（4000 Mbps / 20 条）或
`symmetric500`（5000 Mbps / 10 条）。Web 可新建、修改分组及批量移动账号。
所有组共享最多 20 个下载进程，分组带宽是调度预算，不是程序硬限速。

每天 `Asia/Shanghai` 00–24 点独立对账，昨日欠量不结转：

| 时段 | 每日调度 |
|---|---|
| 00–18 点 | 先补输入上传带宽 × 6 小时 × 目标倍数的下限，再补到预测上限；达到上限停止 |
| 18–23 点 | 不下载，继续 SNMP 采样 |
| 23–24 点 | 仅补当天缺口，下载调度预算不超过 2000 Mbps |

每分钟预测一次；最近 7 个完整日、相同 15 分钟区间的有效上传利用率取中位数，
未来上传留 20% 余量并受物理校验上限约束，实测超额上行不按额定档位截断。每小时重算历史模型。
区间不足 3 个有效历史日时，18–24 点按额定上传上限预测，其它未来区间按零处理。
每日下载下限固定为输入上传带宽 × 6 小时 × 目标倍数，例如 500 Mbps、2 倍为 2.7 TB（十进制）。
上限取该下限与预测下载量的较大值；预测低于下限时以下限为准。下限不随当天剩余时间缩减。
共享预算先满足未达下限的账号，再向预测上限补量，不再主动追加 2.5/3 倍额外目标。

账号已记录上下行以 SNMP 为准；实测、估算、未知保守预算和预测分别展示，预测
不写入实际账本。午夜不清空 SNMP 基线；重拨重新建立基线但保留历史累计。

路由下行只读取按名称定位的 `wan1`，不叠加 VLAN/PPPoE 子接口：

```text
其它业务 ≈ max(0, wan1 下行 Mbps − Fleet 近 60 秒平均 Mbps)
Fleet 预算 = max(0, 5000 − 其它业务 − 200) Mbps
```

两个采样窗口不用严格对齐，负差值视为其它业务为零。WAN 首次采样、重置或有效
数据超过 120 秒未更新时，Fleet 暂停下载；重新获得两个计数器快照后恢复。
仅按完整的 200/500 Mbps 线路档案分配额度，不分配无法实际执行的部分线路额度；
名额迁移先停止再启动，可能留下一小段无法利用的预算。低速提示阈值为
`min(50 Mbps, 分配预算 × 25%)`，有下载进度的低速线路继续运行并自动换源。故障、达标、预算下降和时段切换可打断 30 分钟
最短运行时间，其他情况下按剩余需要运行时间优先、当天已分配时间公平轮转。

新增接口：

- `GET /api/fleet/groups`：分组及当前预算分配；
- `POST /api/fleet/groups`：`{"groups":[{"group_id":"standard","name":"普通组","download_limit_mbps":4000,"max_active":20,"enabled":true}]}`，原子新增/更新（不删除未提交组）；
- `POST /api/fleet/groups/assign`：`{"group_id":"standard","account_ids":["pppoe-001"]}`；
- `GET /api/fleet/report.csv?period=daily&date=2026-10-03`：每日对账，包含 18 点前目标结果、全天结果及未达标量；默认无参数仍为滚动 15 天报表。
- `POST /api/fleet/evening-override`：`{"date":"2026-10-03","account_ids":["adsl1001","adsl1002"]}`，仅当晚临时放行指定线路；18–23 点不启动其他账号，23–24 点指定线路优先且仍合计最多 2G，午夜自动失效。例外不绕过单线/分组预算、WAN 数据有效性和手动停止。

上线前先备份 SQLite 数据库与账号清单，核对最新映射、权重、分组，再验证普通及
500M 线路。用户在爱快设置**全路由 5G**硬限速、普通线 200M 和对等线 500M 下载
限速；预测失准或容量不足只报告未达标，不突破预算、不把欠量带到次日。

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

这些安全参数只从容器环境读取，不能通过 Web 控制台修改。新清单通过 `SIGHUP` 热加载，不重启容器或 Go 下载进程；Fleet 按逐线路 generation 确认结果区分等待、部分成功、失败和已应用。OSS 访问、验签或清单校验失败时，继续使用 `/data` 中最后可信清单；远程源全部不可用时可使用节点本地已验证源，主池恢复后自动切回。

## 源清单发布器

当前生产实例位于 `192.168.1.70:/opt/source-publisher`，使用主机网络实际筛选和验证；Ubuntu 实例已退役。生产运维以 [部署记录](docs/ookla-publisher70-rollout-20261020.md) 为准，以下为新环境的通用安装说明。

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

## 独立云端 Fleet Dashboard

Fleet 可选接入独立云端 Dashboard：自动注册、10 分钟 SNMP/拉流双口径采集、
24 小时曲线、线路及分时时段完成情况，以及有审计的远程启动、停止和调度参数修改。
云端采用独立 Docker Compose、HTTPS 和 SQLite，不承担下载流量；节点仅出站通信。

完整启动、阿里云安全组、节点镜像升级、凭据配置、API 整合及备份说明见
[Dashboard 部署文档](docs/dashboard-deployment.md)。云端配置为
`docker-compose.dashboard.yml`；节点通过可选 `docker-compose.fleet-dashboard.yml`
overlay 接入。未设置 `DASHBOARD_URL` 时，现有 Fleet 行为不变。
