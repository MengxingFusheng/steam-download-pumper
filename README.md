# Steam Download Pumper

一个 Docker 化的宽带下载流量生成器，用于在自有网络中按时间窗产生公共 HTTP 或 Steam 下载流量，并用多 worker 新建连接帮助多 WAN/多线路负载均衡设备按连接分流。

## 功能

- 默认使用公共 HTTP 丢弃下载模式：强制 IPv4，流式读取到 `io.Discard`，不写下载缓存到硬盘。
- 可选 `steam_tmpfs` 模式：`steamcmd +login anonymous` 下载匿名可访问 AppID，下载目录使用 tmpfs。
- 按每天开始/结束时间自动启动和停止。
- 通过 `线路条数 * 每条线路连接数` 创建基础 worker，并在 60 秒均值低于目标 90% 时自动增加 worker。
- 目标带宽、线路条数、基础/最大连接数、运行时间窗和 URL 源池均可在 Web 控制台修改。
- 游戏下载目录默认挂载为 `tmpfs` 内存盘，避免反复写硬盘；下载完成后默认删除目录，再进入下一轮循环。
- Web 控制台包含实时 Mbps 曲线、10/60 秒均值、今日累计流量和源健康表。
- Docker Compose 默认使用 macvlan，容器 LAN IP 优先为 `192.168.1.233`，部署脚本会在 `.233-.240` 中自动顺移，网关为 `192.168.1.1`。
- 多线路支持两种出口方式：`single_ip` 为一个容器 IP 依赖爱快按新建连接数分流；`multi_ip` 会生成与线路数相同数量的 LAN IP，并让公共 HTTP worker 按线路绑定源 IP，便于在爱快中手动配置一对一分流。

## 快速部署

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
- `APP_IDS`: Steam AppID 列表，多个用英文逗号分隔。匿名模式下只能下载 SteamCMD 允许匿名访问的内容。默认 `90`，测试中可匿名下载；`4020` 也可匿名下载但体积更大。
- `DOWNLOAD_MODE`: `public_http` 或 `steam_tmpfs`。旧值 `null` 会兼容为 `public_http`，旧值 `steam` 会兼容为 `steam_tmpfs`。
- `SOURCE_POOL`: `public_http` 模式使用的下载 URL，多个用英文逗号分隔。
- `STARTUP_STAGGER_SECONDS`: worker 启动间隔秒数，用于错峰建立连接。
- `WORKER_MIN_SESSION_SECONDS`: 公共 HTTP worker 的最小会话时间，worker 进程会在内部持续重连短文件。
- `WORKER_RESTART_JITTER_SECONDS`: 短文件 EOF 后的随机重连抖动秒数。
- `DOWNLOAD_TMPFS_SIZE`: 游戏下载内存盘大小，例如 `8g`。它限制 `/steam/downloads` 可用内存空间。
- `BOOTSTRAP_TIMEOUT_SECONDS`: SteamCMD 首次自更新超时秒数，网络较慢时可以调大。

启动：

```bash
docker compose up -d --build
```

## SteamCMD 首次更新

SteamCMD 第一次启动会先自更新，当前镜像无法保证这个更新包已经存在。项目已经把下面两个 SteamCMD 目录持久化为 Docker volume：

- `/home/steam/steamcmd`
- `/home/steam/Steam`

因此只要不要删除 Docker volume，后续重启或重建容器不会从零开始更新。

公共 HTTP 模式不会写下载内容。Steam 游戏下载内容不会写入这些 volume。`/steam/downloads` 使用 `tmpfs` 内存盘，重启后自动清空，避免高频写硬盘。

更新完成后也可以把当前容器打包为预热镜像：

```bash
./bake-warmed-image.sh
```

默认生成：

```text
steam-download-pumper:warmed
```

这个脚本会复制容器中已更新的 `/home/steam/steamcmd` 和 `/home/steam/Steam` 到一个新镜像层；直接 `docker commit` 不适合这里，因为 Docker 不会把 volume 内容提交进镜像。

SteamCMD 自更新通常是单进程下载，不能像游戏下载 worker 那样通过多连接明显加速。可以做的主要是保持当前容器不重启、不要反复保存配置触发重启、确保上游网络/DNS 到 Steam 更新服务可达。

查看日志：

```bash
docker compose logs -f
```

停止：

```bash
docker compose down
```

## 说明

Steam 匿名下载不适用于所有游戏。默认 AppID `90` 已在匿名模式下验证可下载；如果你需要下载账号拥有的游戏，需要后续改为账号登录模式并妥善处理 Steam Guard 和凭据。

多线路均衡有两种使用方式：

- `single_ip`: 只有一个容器源 IP，容器内会创建多个独立 worker，最终是否均匀落到各外线由爱快“新建连接数”策略决定。
- `multi_ip`: 容器会把 `LAN_IPS` 中的地址加到 macvlan 网卡，公共 HTTP worker 会按线路绑定源 IP。你需要在爱快中手动把这些源 IP 分别绑定到不同 WAN，这种方式更适合追求连续、均匀分流。

`multi_ip` 绑定源 IP 目前作用于默认的 `public_http` 下载模式。`steam_tmpfs` 使用 SteamCMD，不能像 Go `discarder` 一样直接给每个 worker 绑定本地源 IP。

默认验收口径：

- `TARGET_MBPS=800` 时，60 秒均值应尽量达到 `720 Mbps` 以上。
- 默认 `00:00-18:00` 理论下载量为 `TARGET_MBPS * 64800 / 8` MB，今日累计目标为理论值的 80% 以上。
- 如果 worker 已扩到上限但 60 秒均值仍低于 90%，控制台会提示源池或线路容量不足。
