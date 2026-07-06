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

write_env() {
  local parent subnet selected_ip
  parent="${LAN_PARENT:-$(default_parent)}"
  parent="${parent:-ens18}"
  subnet="${LAN_SUBNET:-$(default_subnet "$parent")}"
  subnet="${subnet:-192.168.1.0/24}"
  selected_ip="$(pick_lan_ip)"

  cat > .env <<EOF
LAN_PARENT=${parent}
LAN_SUBNET=${subnet}
LAN_GATEWAY=${LAN_GATEWAY:-192.168.1.1}
LAN_IP=${selected_ip}

TARGET_MBPS=${TARGET_MBPS:-800}
LINE_COUNT=${LINE_COUNT:-2}
CONNECTIONS_PER_LINE=${CONNECTIONS_PER_LINE:-6}
MAX_CONNECTIONS_PER_LINE=${MAX_CONNECTIONS_PER_LINE:-12}
RATE_LIMIT_ENABLED=${RATE_LIMIT_ENABLED:-true}
START_TIME=${START_TIME:-00:00}
END_TIME=${END_TIME:-18:00}
APP_IDS=${APP_IDS:-90}
DOWNLOAD_MODE=${DOWNLOAD_MODE:-public_http}
SOURCE_POOL=${SOURCE_POOL:-http://mobile.shunicomtest.com:8080/speedtest/random4000x4000.jpg,http://speedtest1.online.sh.cn:8080/speedtest/random4000x4000.jpg,http://5gzhenjiang.speedtest.jsinfo.net:8080/speedtest/random4000x4000.jpg,http://4gsuzhou1.speedtest.jsinfo.net:8080/speedtest/random4000x4000.jpg}
DELETE_AFTER_CYCLE=${DELETE_AFTER_CYCLE:-true}
DOWNLOAD_TMPFS_SIZE=${DOWNLOAD_TMPFS_SIZE:-8g}
LOOP_PAUSE_SECONDS=${LOOP_PAUSE_SECONDS:-0}
STARTUP_STAGGER_SECONDS=${STARTUP_STAGGER_SECONDS:-2}
WORKER_MIN_SESSION_SECONDS=${WORKER_MIN_SESSION_SECONDS:-300}
WORKER_RESTART_JITTER_SECONDS=${WORKER_RESTART_JITTER_SECONDS:-3}
BOOTSTRAP_TIMEOUT_SECONDS=${BOOTSTRAP_TIMEOUT_SECONDS:-1800}
SCHEDULE_POLL_SECONDS=${SCHEDULE_POLL_SECONDS:-30}
IKUAI_BASE_URL=${IKUAI_BASE_URL:-}
IKUAI_TOKEN=${IKUAI_TOKEN:-}
LOG_LEVEL=${LOG_LEVEL:-INFO}
EOF
}

main() {
  ensure_project_checkout
  install_docker_if_needed
  write_env
  mkdir -p data
  docker compose up -d --build
  selected_ip="$(awk -F= '$1 == "LAN_IP" {print $2}' .env)"
  log "部署完成: http://${selected_ip}/"
  log "如果宿主机无法访问 macvlan 容器，请用同局域网其他设备打开。"
}

main "$@"
