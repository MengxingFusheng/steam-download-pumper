# Broadband Download Pumper

一个 Docker 化的宽带下载流量生成器，用于在自有网络中按时间窗产生公共 HTTP 下载流量，并用多 worker 新建连接帮助多 WAN/多线路负载均衡设备按连接分流。

## 功能

- 默认使用公共 HTTP 丢弃下载模式：强制 IPv4，流式读取到 `io.Discard`，不写下载缓存到硬盘。
- 按每天开始/结束时间自动启动和停止。
- 通过 `线路条数 * 每条线路连接数` 创建基础 worker，并在 60 秒均值低于目标 90% 时自动增加 worker。
- 目标带宽、线路条数、基础/最大连接数、运行时间窗和 URL 源池均可在 Web 控制台修改。
- Web 控制台包含实时 Mbps 曲线、10/60 秒均值、今日累计流量和源健康表。
- Docker Compose 默认使用 macvlan，容器 LAN IP 优先为 `192.168.1.233`，部署脚本会在 `.233-.240` 中自动顺移，网关为 `192.168.1.1`。
- 多线路支持两种出口方式：`single_ip` 为一个容器 IP 依赖爱快按新建连接数分流；`multi_ip` 会生成与线路数相同数量的 LAN IP，并让公共 HTTP worker 按线路绑定源 IP，便于在爱快中手动配置一对一分流。

## 快速部署

### 爱快 Docker 插件单线路镜像

爱快 3.5.0+ 的 Docker 插件可直接使用单线路镜像，一个容器对应一条外网线路。容器 IP 不由镜像分配，在爱快 Docker 插件创建容器时选择网络接口并设置固定 IPv4，然后在分流策略里做“容器源 IP -> 指定 WAN”的一对一绑定。

镜像地址：

```text
traveler1314/ikuai-line-pumper:latest
ghcr.io/mengxingfusheng/ikuai-line-pumper:latest
```

爱快创建容器时请注意：

- `内存占用(M)` 不要填 `0`，建议填 `128` 或 `256`。部分爱快 3.x Docker 插件会把 `0` 校验成无效 JSON，启用时报 `internal error, verify json err`。
- 容器 IPv4 地址在爱快界面里设置，例如 `192.168.10.233`。
- 一个容器对应一条外网线路，多条线路请创建多个容器，并在爱快分流策略里按容器源 IP 绑定对应 WAN。
- 如需在删除并重建容器后保留 Web 配置，请把持久化目录挂载到容器内 `/data`；普通重启无需额外挂载。

单线路镜像使用一个 Go 下载引擎管理全部连接，不再为每个连接创建 Python 线程和独立 Go 进程。连接数可在 1-12 内热扩缩，扩缩时不会重启整个下载引擎。HTTP 正文使用 30 秒无数据超时，活跃长连接不会每 30 秒被强制断开；失败源会自动轮换并渐进退避。

可选环境变量：

- `TARGET_MBPS`: 当前容器对应线路的目标 Mbps，默认 `400`。
- `CONNECTIONS`: 基础连接数，默认 `8`。
- `MAX_CONNECTIONS`: 最大连接数，默认 `12`，超过会自动压到 `12`。
- `START_TIME` / `END_TIME`: 每天运行时间窗，默认 `00:00-18:00`。
- `SOURCE_POOL`: 公共 HTTP/HTTPS 源 URL，多个用英文逗号分隔。

`TARGET_MBPS` 是自动扩缩连接的吞吐目标：60 秒均值低于 90% 时逐个增加连接，超过 115% 时可逐个减少连接。它不是 Linux 内核级硬限速；实际速度仍受宽带、爱快分流和公共源容量影响。控制台同时显示 60 秒速度达成率和当天理论流量完成率。

创建容器后访问爱快里设置的容器 IP，例如：

```text
http://容器IP/
```

也可以使用 GitHub Release 附件版镜像包，在爱快“镜像管理”中引用镜像文件后创建容器。

### 一键部署

在 Ubuntu/Debian 主机上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/MengxingFusheng/steam-download-pumper/main/install.sh | bash
```

使用已经封装好的一对一预构建镜像，不在本机编译：

```bash
curl -fsSL https://raw.githubusercontent.com/MengxingFusheng/steam-download-pumper/main/install-one-to-one.sh | bash
```

如果已经安装 Docker，也可以 clone 后本地部署：

```bash
git clone https://github.com/MengxingFusheng/steam-download-pumper.git
cd steam-download-pumper
./install.sh
```

常用覆盖参数：

```bash
TARGET_MBPS=800 LINE_COUNT=2 CONNECTIONS_PER_LINE=6 MAX_CONNECTIONS_PER_LINE=12 ./install.sh
```

启用多 IP 一对一模式：

```bash
EGRESS_MODE=multi_ip LINE_COUNT=4 TARGET_MBPS=1600 ./install.sh
```

如果希望直接使用 GitHub Container Registry 的一对一镜像：

```bash
LINE_COUNT=4 TARGET_MBPS=1600 ./install-one-to-one.sh
```

脚本会写入类似下面的配置：

```text
LAN_IP=192.168.1.233
LAN_IPS=192.168.1.233,192.168.1.234,192.168.1.235,192.168.1.236
EGRESS_MODE=multi_ip
```

然后在爱快中按顺序手动做源 IP 到 WAN 的一对一分流，例如 `192.168.1.233 -> wan1`、`192.168.1.234 -> wan2`。如果你想固定 IP 列表，也可以直接传入：

```bash
EGRESS_MODE=multi_ip LINE_COUNT=4 LAN_IPS=192.168.1.233,192.168.1.234,192.168.1.235,192.168.1.236 ./install.sh
```

单 IP 模式自动扫描 `192.168.1.233-192.168.1.240`。多 IP 模式会从 `.233` 开始凑齐 `LINE_COUNT` 个地址；当线路数超过 8 时会继续向后取，例如 `.241/.242`，也可以用 `LAN_IPS` 明确指定。

`MAX_CONNECTIONS_PER_LINE` 硬上限为 `12`，用于避免 worker 数无限扩张占用 CPU 和内存。

### 预构建镜像

一对一模式镜像已封装为：

```text
ghcr.io/mengxingfusheng/steam-download-pumper:one-to-one
```

这个镜像默认 `EGRESS_MODE=multi_ip`，默认两条线路，默认 IP 为 `192.168.1.233,192.168.1.234`。实际部署时推荐通过 `install-one-to-one.sh` 生成 `.env`，它会按 `LINE_COUNT` 自动扫描并写入对应数量的 LAN IP。

直接拉取镜像：

```bash
docker pull ghcr.io/mengxingfusheng/steam-download-pumper:one-to-one
```

### 交互部署

```bash
cd /home/mengxing/文档/steam-download-pumper
chmod +x deploy.sh
./deploy.sh
```

启动后访问：

```text
http://192.168.1.233/
```

如果宿主机无法访问 macvlan 容器，请从同一局域网其他设备访问，或在宿主机创建 macvlan shim。

## 手动配置

复制示例环境文件：

```bash
cp .env.example .env
```

常用参数：

- `LAN_PARENT`: 宿主机连接局域网的网卡名，例如 `ens18`。
- `LAN_IP`: 容器局域网 IP，部署脚本会自动选择 `192.168.1.233-192.168.1.240`。
- `EGRESS_MODE`: `single_ip` 或 `multi_ip`。`single_ip` 使用一个源 IP，交给爱快按新建连接数负载均衡；`multi_ip` 为每条外线准备一个源 IP。
- `LAN_IPS`: `multi_ip` 模式下的源 IP 列表，数量必须等于 `LINE_COUNT`，第 1 个 IP 也是 Web 控制台访问 IP。
- `TARGET_MBPS`: 目标下载带宽 Mbps。源池和线路可承载时，60 秒均值目标为该值的 90% 以上。
- `LINE_COUNT`: 外部线路条数。
- `CONNECTIONS_PER_LINE`: 每条线路创建多少个 worker。
- `MAX_CONNECTIONS_PER_LINE`: 自动扩容时每条线路最多创建多少个 worker，硬上限为 `12`。
- `RATE_LIMIT_ENABLED`: `true` 或 `false`。为 `true` 时，明显超过目标会收敛 worker 数。
- `START_TIME` / `END_TIME`: 每天运行时间窗，格式 `HH:MM`，支持跨午夜。
- `SOURCE_POOL`: 公共 HTTP/HTTPS 下载 URL，多个用英文逗号分隔。
- `STARTUP_STAGGER_SECONDS`: worker 启动间隔秒数，用于错峰建立连接。
- `WORKER_MIN_SESSION_SECONDS`: 公共 HTTP worker 的最小会话时间，worker 进程会在内部持续重连短文件。
- `WORKER_RESTART_JITTER_SECONDS`: 短文件 EOF 后的随机重连抖动秒数。

启动：

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f
```

停止：

```bash
docker compose down
```

## 说明

多线路均衡有两种使用方式：

- `single_ip`: 只有一个容器源 IP，容器内会创建多个独立 worker，最终是否均匀落到各外线由爱快“新建连接数”策略决定。
- `multi_ip`: 容器会把 `LAN_IPS` 中的地址加到 macvlan 网卡，公共 HTTP worker 会按线路绑定源 IP。你需要在爱快中手动把这些源 IP 分别绑定到不同 WAN，这种方式更适合追求连续、均匀分流。

`multi_ip` 模式下，Go `discarder` 会为每条线路绑定对应的本地源 IP。

默认验收口径：

- `TARGET_MBPS=800` 时，60 秒均值应尽量达到 `720 Mbps` 以上。
- 默认 `00:00-18:00` 理论下载量为 `TARGET_MBPS * 64800 / 8` MB，今日累计目标为理论值的 80% 以上。
- 如果 worker 已扩到上限但 60 秒均值仍低于 90%，控制台会提示源池或线路容量不足。
