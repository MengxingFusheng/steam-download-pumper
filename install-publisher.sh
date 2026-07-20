#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_RAW="${REPOSITORY_RAW:-https://raw.githubusercontent.com/MengxingFusheng/steam-download-pumper/main}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || pwd)"
if [ -f "${SCRIPT_DIR}/docker-compose.publisher.yml" ]; then
  INSTALL_DIR="${INSTALL_DIR:-${SCRIPT_DIR}}"
else
  INSTALL_DIR="${INSTALL_DIR:-${PWD}/pumper-source-publisher}"
fi
COMPOSE_PATH="${INSTALL_DIR}/docker-compose.publisher.yml"
ENV_PATH="${INSTALL_DIR}/.env.publisher"
CONFIG_DIR="${INSTALL_DIR}/publisher-config"
SECRET_DIR="${INSTALL_DIR}/publisher-secrets"

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || fail "docker is required"
docker compose version >/dev/null 2>&1 || fail "Docker Compose V2 is required"
mkdir -p -m 0755 "$INSTALL_DIR" "$CONFIG_DIR"
mkdir -p -m 0700 "$SECRET_DIR"

fetch_unless_present() {
  local destination="$1" remote="$2"
  [ -f "$destination" ] && return 0
  command -v curl >/dev/null 2>&1 || fail "curl is required to download deployment files"
  curl -fsSL "$remote" -o "$destination"
}

fetch_unless_present "$COMPOSE_PATH" "${REPOSITORY_RAW}/docker-compose.publisher.yml"
if [ ! -f "$ENV_PATH" ]; then
  if [ -f "${SCRIPT_DIR}/.env.publisher.example" ]; then
    install -m 0600 "${SCRIPT_DIR}/.env.publisher.example" "$ENV_PATH"
  else
    fetch_unless_present "$ENV_PATH" "${REPOSITORY_RAW}/.env.publisher.example"
    chmod 0600 "$ENV_PATH"
  fi
fi
if [ ! -f "${CONFIG_DIR}/candidates.json" ]; then
  if [ -f "${SCRIPT_DIR}/source-list/candidates.json" ]; then
    install -m 0644 "${SCRIPT_DIR}/source-list/candidates.json" "${CONFIG_DIR}/candidates.json"
  else
    fetch_unless_present "${CONFIG_DIR}/candidates.json" "${REPOSITORY_RAW}/source-list/candidates.json"
  fi
fi

declare -A dotenv=()
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
  [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]] || fail "invalid dotenv syntax"
  key="${BASH_REMATCH[1]}"
  value="${BASH_REMATCH[2]}"
  case "$key" in
    PUBLISHER_IMAGE|OSS_BUCKET|OSS_REGION|OSS_ENDPOINT|OSS_PUBLIC_BASE_URL|SOURCE_LIST_KEY_ID|PUBLISH_TIME|PUBLISH_TIMEZONE|PUBLISH_RETRY_SECONDS|MIN_HEALTHY_SOURCES|MAX_HEALTHY_SOURCES|PROBE_CONCURRENCY|PROBE_TIMEOUT_SECONDS|LOG_LEVEL) ;;
    *) fail "unsupported dotenv key: ${key}" ;;
  esac
  [[ "$value" =~ ^[A-Za-z0-9_./,:@+-]+$ ]] || fail "unsafe dotenv value for ${key}"
  [[ -z "${dotenv[$key]+present}" ]] || fail "duplicate dotenv key: ${key}"
  dotenv["$key"]="$value"
done < "$ENV_PATH"

OSS_BUCKET_VALUE="${dotenv[OSS_BUCKET]:-}"
OSS_REGION_VALUE="${dotenv[OSS_REGION]:-cn-beijing}"
OSS_ENDPOINT_VALUE="${dotenv[OSS_ENDPOINT]:-}"
OSS_PUBLIC_BASE_URL_VALUE="${dotenv[OSS_PUBLIC_BASE_URL]:-}"
SOURCE_LIST_KEY_ID_VALUE="${dotenv[SOURCE_LIST_KEY_ID]:-}"
[[ "$OSS_BUCKET_VALUE" =~ ^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$ ]] || fail "OSS_BUCKET is invalid"
[ "$OSS_REGION_VALUE" = "cn-beijing" ] || fail "OSS_REGION must be cn-beijing"
[[ "$OSS_ENDPOINT_VALUE" =~ ^https://[^/?#]+$ ]] || fail "OSS_ENDPOINT must be an HTTPS origin"
[[ "$OSS_PUBLIC_BASE_URL_VALUE" =~ ^https://[^/?#]+/pumper/v1$ ]] || fail "OSS_PUBLIC_BASE_URL must be the HTTPS pumper/v1 prefix"
[[ "$SOURCE_LIST_KEY_ID_VALUE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || fail "SOURCE_LIST_KEY_ID is invalid"

for name in source_signing_private_key oss_access_key_id oss_access_key_secret; do
  path="${SECRET_DIR}/${name}"
  [ -f "$path" ] && [ ! -L "$path" ] && [ -s "$path" ] || fail "secret file ${name} must be a nonempty regular file"
  [ "$(stat -c '%a' "$path")" = "600" ] || fail "secret file ${name} must have mode 600"
done

compose=(docker compose --env-file "$ENV_PATH" -f "$COMPOSE_PATH")
"${compose[@]}" config >/dev/null
"${compose[@]}" pull
"${compose[@]}" run --rm --no-deps source-publisher validate-only
"${compose[@]}" up -d
printf 'publisher scheduled for %s %s; current status:\n' "${dotenv[PUBLISH_TIME]:-03:17}" "${dotenv[PUBLISH_TIMEZONE]:-Asia/Shanghai}"
"${compose[@]}" ps source-publisher
