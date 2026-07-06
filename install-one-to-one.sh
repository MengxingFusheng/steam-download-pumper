#!/usr/bin/env bash
set -euo pipefail

export EGRESS_MODE="${EGRESS_MODE:-multi_ip}"
export LINE_COUNT="${LINE_COUNT:-2}"
export TARGET_MBPS="${TARGET_MBPS:-800}"
export CONNECTIONS_PER_LINE="${CONNECTIONS_PER_LINE:-6}"
export MAX_CONNECTIONS_PER_LINE="${MAX_CONNECTIONS_PER_LINE:-12}"
export COMPOSE_FILE_PATH="${COMPOSE_FILE_PATH:-docker-compose.one-to-one.yml}"
export COMPOSE_BUILD="${COMPOSE_BUILD:-0}"
export PULL_IMAGE="${PULL_IMAGE:-1}"
export PUMPER_IMAGE="${PUMPER_IMAGE:-ghcr.io/mengxingfusheng/steam-download-pumper:one-to-one}"

if [ -f ./install.sh ] && [ -f ./docker-compose.one-to-one.yml ]; then
  exec bash ./install.sh
fi

tmp_script="$(mktemp)"
cleanup() {
  rm -f "$tmp_script"
}
trap cleanup EXIT

install_url="${STEAM_PUMPER_INSTALL_URL:-https://raw.githubusercontent.com/MengxingFusheng/steam-download-pumper/main/install.sh}"
curl -fsSL "$install_url" -o "$tmp_script"
exec bash "$tmp_script"
