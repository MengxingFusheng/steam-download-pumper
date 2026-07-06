#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

current_container_ip() {
  docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' steam-download-pumper 2>/dev/null || true
}

ip_available() {
  local ip="$1"
  local current
  current="$(current_container_ip)"
  if [[ "$ip" == "$current" ]]; then
    return 0
  fi
  if ping -c 1 -W 1 "$ip" >/dev/null 2>&1; then
    return 1
  fi
  if ip neigh show "$ip" 2>/dev/null | grep -Eq 'lladdr|REACHABLE|STALE|DELAY|PROBE'; then
    return 1
  fi
  return 0
}

pick_lan_ip() {
  local ip
  for last in 233 234 235 236 237 238 239 240; do
    ip="192.168.1.${last}"
    if ip_available "$ip"; then
      printf '%s\n' "$ip"
      return 0
    fi
  done
  echo "192.168.1.233-192.168.1.240 均被占用，无法自动选择容器 IP。" >&2
  return 1
}

ask() {
  local name="$1"
  local prompt="$2"
  local default="$3"
  local value
  read -r -p "${prompt} [${default}]: " value
  printf '%s=%s\n' "$name" "${value:-$default}"
}

LAN_IP_VALUE="$(pick_lan_ip)"

{
  ask LAN_PARENT "宿主机连接局域网的网卡名" "ens18"
  ask LAN_SUBNET "局域网网段" "192.168.1.0/24"
  ask LAN_GATEWAY "局域网网关" "192.168.1.1"
  printf 'LAN_IP=%s\n' "$LAN_IP_VALUE"
  ask TARGET_MBPS "目标下载带宽 Mbps" "900"
  ask LINE_COUNT "外部线路条数" "2"
  ask CONNECTIONS_PER_LINE "每条线路连接数" "12"
  ask MAX_CONNECTIONS_PER_LINE "每条线路最大连接数" "12"
  ask RATE_LIMIT_ENABLED "是否限速 true/false" "true"
  ask START_TIME "每天开始时间 HH:MM" "00:00"
  ask END_TIME "每天结束时间 HH:MM" "18:00"
  ask DOWNLOAD_MODE "下载模式 public_http/steam_tmpfs" "public_http"
  ask SOURCE_POOL "公共源 URL，多个用英文逗号分隔" "http://mobile.shunicomtest.com:8080/speedtest/random4000x4000.jpg,http://speedtest1.online.sh.cn:8080/speedtest/random4000x4000.jpg,http://5gzhenjiang.speedtest.jsinfo.net:8080/speedtest/random4000x4000.jpg,http://4gsuzhou1.speedtest.jsinfo.net:8080/speedtest/random4000x4000.jpg"
  ask APP_IDS "Steam tmpfs 模式 AppID，多个用英文逗号分隔" "90"
  ask DELETE_AFTER_CYCLE "每轮下载后删除以便循环 true/false" "true"
  ask DOWNLOAD_TMPFS_SIZE "游戏下载内存盘大小，例如 8g/16g" "8g"
  ask STARTUP_STAGGER_SECONDS "worker 启动间隔秒数" "2"
  ask WORKER_MIN_SESSION_SECONDS "worker 最小会话秒数" "300"
  ask WORKER_RESTART_JITTER_SECONDS "短文件重连随机抖动秒数" "3"
  ask BOOTSTRAP_TIMEOUT_SECONDS "SteamCMD 首次自更新超时秒数" "1800"
} > .env

docker compose up -d --build

echo
echo "已启动。Web 控制台: http://${LAN_IP_VALUE}/"
echo "如宿主机不能直接访问 macvlan 容器，请从同一局域网其他设备访问，或在宿主机添加 macvlan shim。"
