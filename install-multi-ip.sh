#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_RAW="${REPOSITORY_RAW:-https://raw.githubusercontent.com/MengxingFusheng/steam-download-pumper/main}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || pwd)"
if [ -f "${SCRIPT_DIR}/docker-compose.multi-ip.yml" ]; then
  INSTALL_DIR="${INSTALL_DIR:-${SCRIPT_DIR}}"
else
  INSTALL_DIR="${INSTALL_DIR:-${PWD}/multi-ip-pumper}"
fi
COMPOSE_PATH="${INSTALL_DIR}/docker-compose.multi-ip.yml"
ENV_PATH="${INSTALL_DIR}/.env"

LINE_COUNT="${LINE_COUNT:-2}"
TARGET_MBPS="${TARGET_MBPS:-800}"
CONNECTIONS_PER_LINE="${CONNECTIONS_PER_LINE:-8}"
MAX_CONNECTIONS_PER_LINE="${MAX_CONNECTIONS_PER_LINE:-12}"
LAN_NETWORK_PREFIX="${LAN_NETWORK_PREFIX:-192.168.1}"
LAN_PARENT="${LAN_PARENT:-ens18}"
LAN_SUBNET="${LAN_SUBNET:-192.168.1.0/24}"
LAN_GATEWAY="${LAN_GATEWAY:-192.168.1.1}"
LAN_PREFIX="${LAN_PREFIX:-24}"
PUMPER_IMAGE="${PUMPER_IMAGE:-traveler1314/multi-ip-pumper:latest}"
REMOTE_SOURCE_LIST_ENABLED="${REMOTE_SOURCE_LIST_ENABLED:-false}"
SOURCE_LIST_URL="${SOURCE_LIST_URL:-}"
SOURCE_LIST_PUBLIC_KEY="${SOURCE_LIST_PUBLIC_KEY:-}"
SOURCE_LIST_KEY_ID="${SOURCE_LIST_KEY_ID:-}"
SOURCE_LIST_REFRESH_TIME="${SOURCE_LIST_REFRESH_TIME:-04:00}"
SOURCE_LIST_REFRESH_JITTER_SECONDS="${SOURCE_LIST_REFRESH_JITTER_SECONDS:-1800}"
SOURCE_LIST_FETCH_TIMEOUT_SECONDS="${SOURCE_LIST_FETCH_TIMEOUT_SECONDS:-15}"
SOURCE_LIST_MAX_BYTES="${SOURCE_LIST_MAX_BYTES:-524288}"
SOURCE_LIST_MIN_SOURCES="${SOURCE_LIST_MIN_SOURCES:-3}"

fail() {
  printf '错误: %s\n' "$*" >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || fail "未找到 docker"
docker compose version >/dev/null 2>&1 || fail "未找到 docker compose 插件"
[[ "$LINE_COUNT" =~ ^[0-9]+$ ]] && (( LINE_COUNT >= 2 && LINE_COUNT <= 10 )) || fail "LINE_COUNT 必须为 2-10"
[[ "$CONNECTIONS_PER_LINE" =~ ^[0-9]+$ ]] && (( CONNECTIONS_PER_LINE >= 1 && CONNECTIONS_PER_LINE <= 12 )) || fail "CONNECTIONS_PER_LINE 必须为 1-12"
[[ "$MAX_CONNECTIONS_PER_LINE" =~ ^[0-9]+$ ]] && (( MAX_CONNECTIONS_PER_LINE >= CONNECTIONS_PER_LINE && MAX_CONNECTIONS_PER_LINE <= 12 )) || fail "MAX_CONNECTIONS_PER_LINE 必须介于基础连接数和 12 之间"

valid_ipv4() {
  local ip="$1" octet
  IFS='.' read -r -a octets <<< "$ip"
  [ "${#octets[@]}" -eq 4 ] || return 1
  for octet in "${octets[@]}"; do
    [[ "$octet" =~ ^[0-9]+$ ]] && (( 10#$octet <= 255 )) || return 1
  done
}

ip_is_occupied() {
  local ip="$1"
  if command -v ping >/dev/null 2>&1 && ping -4 -c 1 -W 1 "$ip" >/dev/null 2>&1; then
    return 0
  fi
  if command -v ip >/dev/null 2>&1 && ip neigh show "$ip" 2>/dev/null | grep -Eq 'REACHABLE|STALE|DELAY|PROBE|PERMANENT'; then
    return 0
  fi
  return 1
}

if [ -z "${LAN_IPS:-}" ]; then
  selected=()
  for host in $(seq 233 254); do
    candidate="${LAN_NETWORK_PREFIX}.${host}"
    if ! ip_is_occupied "$candidate"; then
      selected+=("$candidate")
      [ "${#selected[@]}" -eq "$LINE_COUNT" ] && break
    fi
  done
  [ "${#selected[@]}" -eq "$LINE_COUNT" ] || fail "无法找到足够的空闲 IPv4 地址，请手动设置 LAN_IPS"
  LAN_IPS="$(IFS=,; printf '%s' "${selected[*]}")"
fi

IFS=',' read -r -a lan_ip_array <<< "$LAN_IPS"
[ "${#lan_ip_array[@]}" -eq "$LINE_COUNT" ] || fail "LAN_IPS 数量必须等于 LINE_COUNT"
declare -A seen_ips=()
for index in "${!lan_ip_array[@]}"; do
  ip="${lan_ip_array[$index]//[[:space:]]/}"
  valid_ipv4 "$ip" || fail "无效 IPv4 地址: $ip"
  [ -z "${seen_ips[$ip]:-}" ] || fail "LAN_IPS 不能重复: $ip"
  seen_ips[$ip]=1
  lan_ip_array[$index]="$ip"
done
LAN_IPS="$(IFS=,; printf '%s' "${lan_ip_array[*]}")"
CONTAINER_IP="${lan_ip_array[0]}"

mkdir -p "$INSTALL_DIR/data"
if [ ! -f "$COMPOSE_PATH" ]; then
  command -v curl >/dev/null 2>&1 || fail "未找到 curl，无法下载 Compose 文件"
  curl -fsSL "${REPOSITORY_RAW}/docker-compose.multi-ip.yml" -o "$COMPOSE_PATH"
fi

cat > "$ENV_PATH" <<EOF
PUMPER_IMAGE=${PUMPER_IMAGE}
CONTAINER_IP=${CONTAINER_IP}
LINE_COUNT=${LINE_COUNT}
LAN_IPS=${LAN_IPS}
TARGET_MBPS=${TARGET_MBPS}
CONNECTIONS_PER_LINE=${CONNECTIONS_PER_LINE}
MAX_CONNECTIONS_PER_LINE=${MAX_CONNECTIONS_PER_LINE}
LAN_PARENT=${LAN_PARENT}
LAN_SUBNET=${LAN_SUBNET}
LAN_GATEWAY=${LAN_GATEWAY}
LAN_PREFIX=${LAN_PREFIX}
REMOTE_SOURCE_LIST_ENABLED=${REMOTE_SOURCE_LIST_ENABLED}
SOURCE_LIST_URL=${SOURCE_LIST_URL}
SOURCE_LIST_PUBLIC_KEY=${SOURCE_LIST_PUBLIC_KEY}
SOURCE_LIST_KEY_ID=${SOURCE_LIST_KEY_ID}
SOURCE_LIST_REFRESH_TIME=${SOURCE_LIST_REFRESH_TIME}
SOURCE_LIST_REFRESH_JITTER_SECONDS=${SOURCE_LIST_REFRESH_JITTER_SECONDS}
SOURCE_LIST_FETCH_TIMEOUT_SECONDS=${SOURCE_LIST_FETCH_TIMEOUT_SECONDS}
SOURCE_LIST_MAX_BYTES=${SOURCE_LIST_MAX_BYTES}
SOURCE_LIST_MIN_SOURCES=${SOURCE_LIST_MIN_SOURCES}
EOF

docker compose --env-file "$ENV_PATH" -f "$COMPOSE_PATH" pull
docker compose --env-file "$ENV_PATH" -f "$COMPOSE_PATH" up -d
printf '已启动 multi-ip-pumper，控制台: http://%s/\n' "$CONTAINER_IP"
