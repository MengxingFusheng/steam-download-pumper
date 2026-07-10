#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

DRY_RUN="${DRY_RUN:-0}"
SHORT_SHA="${SHORT_SHA:-$(git rev-parse --short HEAD)}"
FULL_SHA="${FULL_SHA:-$(git rev-parse HEAD)}"
RELEASE_TAG="pumper-${SHORT_SHA}"
DIST_DIR="${DIST_DIR:-dist}"

DOCKERHUB_IKUAI="${DOCKERHUB_IKUAI:-traveler1314/ikuai-line-pumper}"
DOCKERHUB_MULTI="${DOCKERHUB_MULTI:-traveler1314/multi-ip-pumper}"
GHCR_IKUAI="${GHCR_IKUAI:-ghcr.io/mengxingfusheng/ikuai-line-pumper}"
GHCR_MULTI="${GHCR_MULTI:-ghcr.io/mengxingfusheng/multi-ip-pumper}"
LOCAL_IKUAI="local/ikuai-line-pumper:${SHORT_SHA}"
LOCAL_MULTI="local/multi-ip-pumper:${SHORT_SHA}"

active_containers=()
cleanup() {
  if [ "${#active_containers[@]}" -gt 0 ]; then
    docker rm -f "${active_containers[@]}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

run_go_checks() {
  if command -v go >/dev/null 2>&1; then
    go test -race ./...
    go vet ./...
  else
    docker run --rm -v "$PWD:/src" -w /src golang:1.23 \
      sh -c '/usr/local/go/bin/go test -race ./... && /usr/local/go/bin/go vet ./...'
  fi
}

build_image() {
  local name="$1" dockerfile="$2" image="$3"
  printf 'Building %s from %s\n' "$name" "$dockerfile"
  docker build -f "$dockerfile" -t "$image" .
}

smoke_image() {
  local name="$1" image="$2" module="$3"
  local container="pumper-smoke-${name}-${SHORT_SHA}-$$"
  local run_args=(--detach --rm --name "$container" --read-only --tmpfs /tmp:size=32m --tmpfs /run:size=8m)
  if [ "$name" = "multi-ip" ]; then
    run_args+=(--cap-add NET_ADMIN)
  fi
  run_args+=(
    -e CONFIG_PATH=/tmp/config.json
    -e SOURCE_POOL=http://127.0.0.1:9/file
    -p 127.0.0.1::80
    "$image"
  )

  docker run --rm --entrypoint python3 "$image" -c "import ${module}"
  if [ "$name" = "ikuai-line" ]; then
    docker run --rm --entrypoint sh "$image" -c 'test ! -e /sbin/ip && command -v discarder >/dev/null'
  else
    docker run --rm --entrypoint sh "$image" -c 'command -v ip >/dev/null && command -v discarder >/dev/null'
  fi

  docker run "${run_args[@]}" >/dev/null
  active_containers+=("$container")
  local host_port
  host_port="$(docker port "$container" 80/tcp | awk -F: 'NR==1 {print $NF}')"
  for _attempt in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${host_port}/api/status" >/dev/null \
      && curl -fsS "http://127.0.0.1:${host_port}/api/metrics" >/dev/null \
      && curl -fsS "http://127.0.0.1:${host_port}/api/sources" >/dev/null; then
      docker rm -f "$container" >/dev/null
      active_containers=()
      return
    fi
    sleep 1
  done
  docker logs "$container" >&2 || true
  return 1
}

tag_release_images() {
  local tag image source
  for tag in latest ikuai3 "$SHORT_SHA"; do
    for image in "$DOCKERHUB_IKUAI" "$GHCR_IKUAI"; do
      docker tag "$LOCAL_IKUAI" "${image}:${tag}"
    done
    for image in "$DOCKERHUB_MULTI" "$GHCR_MULTI"; do
      docker tag "$LOCAL_MULTI" "${image}:${tag}"
    done
  done
}

create_archives() {
  mkdir -p "$DIST_DIR"
  docker save "${DOCKERHUB_IKUAI}:${SHORT_SHA}" | gzip -1 > "${DIST_DIR}/ikuai-line-pumper-${SHORT_SHA}.docker.tar.gz"
  docker save "${DOCKERHUB_MULTI}:${SHORT_SHA}" | gzip -1 > "${DIST_DIR}/multi-ip-pumper-${SHORT_SHA}.docker.tar.gz"
}

push_repository() {
  local repository="$1" tag output digest=""
  for tag in latest ikuai3 "$SHORT_SHA"; do
    output="$(docker push "${repository}:${tag}" 2>&1)"
    printf '%s\n' "$output" >&2
    if [ "$tag" = "latest" ]; then
      digest="$(printf '%s\n' "$output" | sed -n 's/.*digest: \(sha256:[0-9a-f]*\).*/\1/p' | tail -n 1)"
    fi
  done
  [ -n "$digest" ] || { printf 'Unable to determine digest for %s\n' "$repository" >&2; return 1; }
  printf '%s@%s\n' "$repository" "$digest"
}

run_all_gates() {
  command -v docker >/dev/null 2>&1
  command -v curl >/dev/null 2>&1
  python3 -m unittest discover -s tests -v
  run_go_checks
  bash -n install-multi-ip.sh publish-images.sh
  docker compose -f docker-compose.multi-ip.yml config >/dev/null
  build_image "ikuai-line" Dockerfile.ikuai-line "$LOCAL_IKUAI"
  build_image "multi-ip" Dockerfile.multi-ip "$LOCAL_MULTI"
  smoke_image "ikuai-line" "$LOCAL_IKUAI" steam_pumper.ikuai_main
  smoke_image "multi-ip" "$LOCAL_MULTI" steam_pumper.multi_ip_main
  tag_release_images
  create_archives
}

publish_all() {
  command -v gh >/dev/null 2>&1
  gh auth status >/dev/null 2>&1
  gh auth token | docker login ghcr.io -u "$(gh api user --jq .login)" --password-stdin >/dev/null

  local digest_path="${DIST_DIR}/image-digests-${SHORT_SHA}.txt"
  {
    push_repository "$DOCKERHUB_IKUAI"
    push_repository "$DOCKERHUB_MULTI"
    push_repository "$GHCR_IKUAI"
    push_repository "$GHCR_MULTI"
  } > "$digest_path"

  local notes="Two aligned pumper images from commit ${FULL_SHA}.\n\n$(cat "$digest_path")"
  if gh release view "$RELEASE_TAG" --repo MengxingFusheng/steam-download-pumper >/dev/null 2>&1; then
    gh release upload "$RELEASE_TAG" \
      "${DIST_DIR}/ikuai-line-pumper-${SHORT_SHA}.docker.tar.gz" \
      "${DIST_DIR}/multi-ip-pumper-${SHORT_SHA}.docker.tar.gz" \
      "$digest_path" \
      --repo MengxingFusheng/steam-download-pumper --clobber
  else
    gh release create "$RELEASE_TAG" \
      "${DIST_DIR}/ikuai-line-pumper-${SHORT_SHA}.docker.tar.gz" \
      "${DIST_DIR}/multi-ip-pumper-${SHORT_SHA}.docker.tar.gz" \
      "$digest_path" \
      --repo MengxingFusheng/steam-download-pumper \
      --target "$FULL_SHA" \
      --title "Aligned pumper images ${SHORT_SHA}" \
      --notes "$notes"
  fi
}

run_all_gates
if [ "$DRY_RUN" = "1" ]; then
  {
    printf '%s local-image-id=%s\n' "$DOCKERHUB_IKUAI" "$(docker image inspect "$LOCAL_IKUAI" --format '{{.Id}}')"
    printf '%s local-image-id=%s\n' "$DOCKERHUB_MULTI" "$(docker image inspect "$LOCAL_MULTI" --format '{{.Id}}')"
    printf '%s local-image-id=%s\n' "$GHCR_IKUAI" "$(docker image inspect "$LOCAL_IKUAI" --format '{{.Id}}')"
    printf '%s local-image-id=%s\n' "$GHCR_MULTI" "$(docker image inspect "$LOCAL_MULTI" --format '{{.Id}}')"
  } > "${DIST_DIR}/image-digests-${SHORT_SHA}.txt"
  printf 'Dry run complete for %s\n' "$SHORT_SHA"
  exit 0
fi
publish_all
printf 'Published aligned images for %s\n' "$SHORT_SHA"
