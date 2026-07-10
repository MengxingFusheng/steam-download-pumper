# Broadband Download Pumper

面向爱快多 WAN 环境的公共 HTTP/HTTPS 下载流量工具。下载正文直接写入内存中的 `io.Discard`，不生成下载文件，不持续擦写硬盘，并优先使用 IPv4。

项目只维护两个同步版本：

| 版本 | Docker Hub | 网络模型 |
|---|---|---|
| 爱快单线路 | `traveler1314/ikuai-line-pumper:latest` | 一个容器、一个 IP、一条 WAN |
| 多 IP | `traveler1314/multi-ip-pumper:latest` | 一个容器，每条 WAN 对应一个源 IP |

GHCR 镜像分别为 `ghcr.io/mengxingfusheng/ikuai-line-pumper:latest` 和 `ghcr.io/mengxingfusheng/multi-ip-pumper:latest`。每次发布时，两个镜像使用相同的 Git 提交标签；共享下载引擎、调度、监控、API 和控制台的改进会同时进入两个版本。

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

## 运行控制

两个版本使用相同的浏览器控制台和 API：

- 启动/停止下载；
- 修改目标 Mbps、每线基础连接数、每线最大连接数和运行时间窗；
- 编辑公共 HTTP/HTTPS 源池；
- 查看当前、10 秒和 60 秒 Mbps、今日累计量、目标达成率；
- 查看每条线路的实时速度、连接数、绑定 IP、状态和错误；
- 查看源健康与纯 UTF-8 日志。

`MAX_CONNECTIONS_PER_LINE` 的硬上限为 `12`。低于线路目标 90% 时每次增加一个连接；启用目标控制且高于 115% 时每次减少一个连接。连接调整通过信号完成，不重启下载引擎。

`TARGET_MBPS` 是自适应连接目标，不是内核整形限速。实际速度取决于线路、源池和运营商容量。默认验收口径为 60 秒均值达到目标的 90%，运行时间窗内累计流量达到理论值的 80%。

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
