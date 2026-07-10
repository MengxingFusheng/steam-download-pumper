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

contains_ip() {
  local needle="$1"
  shift
  local item
  for item in "$@"; do
    if [ "$item" = "$needle" ]; then
      return 0
    fi
  done
  return 1
}

join_csv() {
  local IFS=,
  printf '%s\n' "$*"
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

pick_lan_ips() {
  local count="$1"
  local ips=()
  local ip last scan_end min_end
  min_end=$((232 + count))
  scan_end="${LAN_IP_SCAN_END:-240}"
  if (( scan_end < min_end )); then
    scan_end="$min_end"
  fi
  for ((last=233; last<=scan_end; last++)); do
    ip="192.168.1.${last}"
    if contains_ip "$ip" "${ips[@]}"; then
      continue
    fi
    if ip_available "$ip"; then
      ips+=("$ip")
    fi
    if (( ${#ips[@]} >= count )); then
      join_csv "${ips[@]:0:count}"
      return 0
    fi
  done
  echo "无法为 multi_ip 模式找到 ${count} 个可用 IP。" >&2
  return 1
}

ask_value() {
  local prompt="$1"
  local default="$2"
  local value
  read -r -p "${prompt} [${default}]: " value
  printf '%s\n' "${value:-$default}"
}

ask_env() {
  local name="$1"
  local prompt="$2"
  local default="$3"
  local value
  value="$(ask_value "$prompt" "$default")"
  printf '%s=%s\n' "$name" "$value"
}

LINE_COUNT_VALUE="$(ask_value "外部线路条数" "2")"
EGRESS_MODE_VALUE="$(ask_value "出口 IP 模式 single_ip/multi_ip" "single_ip")"
if [ "$EGRESS_MODE_VALUE" = "multi_ip" ]; then
  LAN_IPS_VALUE="$(pick_lan_ips "$LINE_COUNT_VALUE")"
  LAN_IP_VALUE="${LAN_IPS_VALUE%%,*}"
else
  LAN_IP_VALUE="$(pick_lan_ip)"
  LAN_IPS_VALUE="$LAN_IP_VALUE"
fi

{
  ask_env LAN_PARENT "宿主机连接局域网的网卡名" "ens18"
  ask_env LAN_SUBNET "局域网网段" "192.168.1.0/24"
  ask_env LAN_GATEWAY "局域网网关" "192.168.1.1"
  printf 'LAN_IP=%s\n' "$LAN_IP_VALUE"
  printf 'LAN_IPS=%s\n' "$LAN_IPS_VALUE"
  printf 'EGRESS_MODE=%s\n' "$EGRESS_MODE_VALUE"
  ask_env TARGET_MBPS "目标下载带宽 Mbps" "900"
  printf 'LINE_COUNT=%s\n' "$LINE_COUNT_VALUE"
  ask_env CONNECTIONS_PER_LINE "每条线路连接数" "12"
  ask_env MAX_CONNECTIONS_PER_LINE "每条线路最大连接数" "12"
  ask_env RATE_LIMIT_ENABLED "是否限速 true/false" "true"
  ask_env START_TIME "每天开始时间 HH:MM" "00:00"
  ask_env END_TIME "每天结束时间 HH:MM" "18:00"
  ask_env SOURCE_POOL "公共源 URL，多个用英文逗号分隔" "http://mobile.shunicomtest.com:8080/speedtest/random4000x4000.jpg,http://speedtest1.online.sh.cn:8080/speedtest/random4000x4000.jpg,http://5gzhenjiang.speedtest.jsinfo.net:8080/speedtest/random4000x4000.jpg,http://4gsuzhou1.speedtest.jsinfo.net:8080/speedtest/random4000x4000.jpg"
  ask_env STARTUP_STAGGER_SECONDS "worker 启动间隔秒数" "2"
  ask_env WORKER_MIN_SESSION_SECONDS "worker 最小会话秒数" "300"
  ask_env WORKER_RESTART_JITTER_SECONDS "短文件重连随机抖动秒数" "3"
} > .env

docker compose up -d --build

echo
echo "已启动。Web 控制台: http://${LAN_IP_VALUE}/"
if [ "$EGRESS_MODE_VALUE" = "multi_ip" ]; then
  echo "多 IP 一对一模式 IP 列表: ${LAN_IPS_VALUE}"
  echo "请在爱快中按顺序将这些源 IP 分别绑定到 wan1..wanN。"
fi
echo "如宿主机不能直接访问 macvlan 容器，请从同一局域网其他设备访问，或在宿主机添加 macvlan shim。"
