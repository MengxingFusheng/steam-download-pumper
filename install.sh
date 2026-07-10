#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

log() {
  printf '[steam-pumper] %s\n' "$*"
}

need_sudo() {
  if [ "$(id -u)" -eq 0 ]; then
    return 1
  fi
  command -v sudo >/dev/null 2>&1
}

run_privileged() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif need_sudo; then
    sudo "$@"
  else
    log "需要 root 或 sudo 来安装 Docker。"
    exit 1
  fi
}

install_git_if_needed() {
  if command -v git >/dev/null 2>&1; then
    return
  fi
  if command -v apt-get >/dev/null 2>&1; then
    log "正在安装 git..."
    run_privileged apt-get update
    run_privileged apt-get install -y git ca-certificates curl
    return
  fi
  log "git 不存在，请先安装 git，或手动下载仓库后运行 ./install.sh。"
  exit 1
}

ensure_project_checkout() {
  if [ -f docker-compose.yml ] && [ -d steam_pumper ]; then
    return
  fi
  install_git_if_needed
  local repo_url install_dir
  repo_url="${STEAM_PUMPER_REPO_URL:-https://github.com/MengxingFusheng/steam-download-pumper.git}"
  install_dir="${INSTALL_DIR:-$HOME/steam-download-pumper}"
  if [ -d "$install_dir/.git" ]; then
    log "更新已有目录: $install_dir"
    cd "$install_dir"
    git pull --ff-only
    return
  fi
  if [ -e "$install_dir" ]; then
    log "安装目录已存在但不是 Git 仓库: $install_dir"
    exit 1
  fi
  mkdir -p "$(dirname "$install_dir")"
  log "克隆仓库到: $install_dir"
  git clone "$repo_url" "$install_dir"
  cd "$install_dir"
}

install_docker_if_needed() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    return
  fi
  if [ "${INSTALL_DOCKER:-1}" != "1" ]; then
    log "Docker 或 docker compose 不存在，且 INSTALL_DOCKER=0。"
    exit 1
  fi
  log "正在安装 Docker..."
  tmp_script="$(mktemp)"
  curl -fsSL https://get.docker.com -o "$tmp_script"
  run_privileged sh "$tmp_script"
  rm -f "$tmp_script"
  if [ "$(id -u)" -ne 0 ]; then
    run_privileged usermod -aG docker "$USER" || true
  fi
}

default_parent() {
  ip route show default 2>/dev/null | awk '{print $5; exit}'
}

default_subnet() {
  local parent="$1"
  ip -4 -o addr show dev "$parent" 2>/dev/null | awk '{print $4; exit}'
}

ip_available() {
  local ip="$1"
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
  if [ -n "${LAN_IPS:-}" ]; then
    printf '%s\n' "${LAN_IPS%%,*}"
    return
  fi
  if [ -n "${LAN_IP:-}" ]; then
    printf '%s\n' "$LAN_IP"
    return
  fi
  for last in 233 234 235 236 237 238 239 240; do
    ip="192.168.1.${last}"
    if ip_available "$ip"; then
      printf '%s\n' "$ip"
      return
    fi
  done
  log "192.168.1.233-192.168.1.240 均被占用。"
  exit 1
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

normalize_egress_mode() {
  case "${1:-single_ip}" in
    single|single_ip|connection_balance|connection_count) printf 'single_ip\n' ;;
    multi|multi_ip|one_to_one|one-to-one) printf 'multi_ip\n' ;;
    *)
      log "EGRESS_MODE 只能是 single_ip 或 multi_ip。"
      exit 1
      ;;
  esac
}

pick_lan_ips() {
  local count="$1"
  local ips=()
  local ip raw last scan_end min_end provided
  if [ -n "${LAN_IPS:-}" ]; then
    raw="${LAN_IPS//[[:space:]]/}"
    IFS=',' read -r -a provided <<< "$raw"
    if (( ${#provided[@]} != count )); then
      log "LAN_IPS 数量必须等于 LINE_COUNT=${count}。"
      exit 1
    fi
    printf '%s\n' "$raw"
    return
  fi
  if [ -n "${LAN_IP:-}" ]; then
    ips+=("$LAN_IP")
  fi
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
      return
    fi
  done
  log "无法为 multi_ip 模式找到 ${count} 个可用 IP。可通过 LAN_IPS=ip1,ip2,... 手动指定。"
  exit 1
}

write_env() {
  local parent subnet selected_ip selected_ips line_count egress_mode
  parent="${LAN_PARENT:-$(default_parent)}"
  parent="${parent:-ens18}"
  subnet="${LAN_SUBNET:-$(default_subnet "$parent")}"
  subnet="${subnet:-192.168.1.0/24}"
  line_count="${LINE_COUNT:-2}"
  egress_mode="$(normalize_egress_mode "${EGRESS_MODE:-single_ip}")"
  if [ "$egress_mode" = "multi_ip" ] && [ "$line_count" -gt 1 ]; then
    selected_ips="$(pick_lan_ips "$line_count")"
    selected_ip="${selected_ips%%,*}"
  else
    selected_ip="$(pick_lan_ip)"
    selected_ips="$selected_ip"
  fi

  cat > .env <<EOF
LAN_PARENT=${parent}
LAN_SUBNET=${subnet}
LAN_GATEWAY=${LAN_GATEWAY:-192.168.1.1}
LAN_IP=${selected_ip}
LAN_IPS=${selected_ips}
EGRESS_MODE=${egress_mode}

TARGET_MBPS=${TARGET_MBPS:-800}
LINE_COUNT=${line_count}
CONNECTIONS_PER_LINE=${CONNECTIONS_PER_LINE:-6}
MAX_CONNECTIONS_PER_LINE=${MAX_CONNECTIONS_PER_LINE:-12}
RATE_LIMIT_ENABLED=${RATE_LIMIT_ENABLED:-true}
START_TIME=${START_TIME:-00:00}
END_TIME=${END_TIME:-18:00}
SOURCE_POOL=${SOURCE_POOL:-http://mobile.shunicomtest.com:8080/speedtest/random4000x4000.jpg,http://speedtest1.online.sh.cn:8080/speedtest/random4000x4000.jpg,http://5gzhenjiang.speedtest.jsinfo.net:8080/speedtest/random4000x4000.jpg,http://4gsuzhou1.speedtest.jsinfo.net:8080/speedtest/random4000x4000.jpg}
LOOP_PAUSE_SECONDS=${LOOP_PAUSE_SECONDS:-0}
STARTUP_STAGGER_SECONDS=${STARTUP_STAGGER_SECONDS:-2}
WORKER_MIN_SESSION_SECONDS=${WORKER_MIN_SESSION_SECONDS:-300}
WORKER_RESTART_JITTER_SECONDS=${WORKER_RESTART_JITTER_SECONDS:-3}
SCHEDULE_POLL_SECONDS=${SCHEDULE_POLL_SECONDS:-30}
IKUAI_BASE_URL=${IKUAI_BASE_URL:-}
IKUAI_TOKEN=${IKUAI_TOKEN:-}
LOG_LEVEL=${LOG_LEVEL:-INFO}
EOF
}

main() {
  local compose_file selected_ip egress_mode selected_ips
  ensure_project_checkout
  install_docker_if_needed
  write_env
  mkdir -p data
  compose_file="${COMPOSE_FILE_PATH:-docker-compose.yml}"
  if [ ! -f "$compose_file" ]; then
    log "Compose 文件不存在: ${compose_file}"
    exit 1
  fi
  if [ "${PULL_IMAGE:-0}" = "1" ]; then
    docker compose -f "$compose_file" pull
  fi
  if [ "${COMPOSE_BUILD:-1}" = "1" ]; then
    docker compose -f "$compose_file" up -d --build
  else
    docker compose -f "$compose_file" up -d
  fi
  selected_ip="$(awk -F= '$1 == "LAN_IP" {print $2}' .env)"
  egress_mode="$(awk -F= '$1 == "EGRESS_MODE" {print $2}' .env)"
  selected_ips="$(awk -F= '$1 == "LAN_IPS" {print $2}' .env)"
  log "部署完成: http://${selected_ip}/"
  if [ "$egress_mode" = "multi_ip" ]; then
    log "多 IP 一对一模式已启用，IP 列表: ${selected_ips}"
    log "请在爱快中按顺序将这些源 IP 分别绑定到 wan1..wanN。"
  else
    log "单 IP 模式已启用，将依赖爱快按新建连接数分流。"
  fi
  log "如果宿主机无法访问 macvlan 容器，请用同局域网其他设备打开。"
}

main "$@"
