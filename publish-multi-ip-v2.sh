#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

DRY_RUN="${DRY_RUN:-0}"
SHORT_SHA="${SHORT_SHA:-$(git rev-parse --short HEAD)}"
DOCKERHUB_IMAGE="${DOCKERHUB_MULTI:-traveler1314/multi-ip-pumper}"
GHCR_IMAGE="${GHCR_MULTI:-ghcr.io/mengxingfusheng/multi-ip-pumper}"
LOCAL_IMAGE="local/multi-ip-pumper:${SHORT_SHA}"
RELEASE_TAG="oss-v2"

run_go_checks() {
  if command -v go >/dev/null 2>&1; then
    go test -race ./...
    go vet ./...
  else
    docker run --rm -v "$PWD:/src" -w /src golang:1.23 \
      sh -c '/usr/local/go/bin/go test -race ./... && /usr/local/go/bin/go vet ./...'
  fi
}

run_gates() {
  python3 -m unittest discover -s tests -v
  run_go_checks
  bash -n install-multi-ip.sh publish-multi-ip-v2.sh
  docker compose -f docker-compose.multi-ip.yml config >/dev/null
  docker build -f Dockerfile.multi-ip -t "$LOCAL_IMAGE" .
  docker run --rm --entrypoint python3 "$LOCAL_IMAGE" -c 'import steam_pumper.multi_ip_main'
  docker run --rm --entrypoint sh "$LOCAL_IMAGE" -c \
    'command -v discarder >/dev/null && command -v manifestctl >/dev/null && command -v ip >/dev/null'
}

tag_images() {
  local repository
  for repository in "$DOCKERHUB_IMAGE" "$GHCR_IMAGE"; do
    docker tag "$LOCAL_IMAGE" "${repository}:${RELEASE_TAG}"
    docker tag "$LOCAL_IMAGE" "${repository}:${SHORT_SHA}"
  done
}

push_images() {
  local repository tag
  for repository in "$DOCKERHUB_IMAGE" "$GHCR_IMAGE"; do
    for tag in "$RELEASE_TAG" "$SHORT_SHA"; do
      docker push "${repository}:${tag}"
    done
  done
}

run_gates
tag_images

if [ "$DRY_RUN" = "1" ]; then
  docker image inspect "$LOCAL_IMAGE" --format 'multi-ip-v2 local-image-id={{.Id}}'
  exit 0
fi

if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  gh auth token | docker login ghcr.io -u "$(gh api user --jq .login)" --password-stdin >/dev/null
fi
push_images

