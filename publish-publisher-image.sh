#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

DRY_RUN="${DRY_RUN:-0}"
SHORT_SHA="${SHORT_SHA:-$(git rev-parse --short HEAD)}"
DOCKERHUB_IMAGE="${DOCKERHUB_PUBLISHER:-traveler1314/pumper-source-publisher}"
GHCR_IMAGE="${GHCR_PUBLISHER:-ghcr.io/mengxingfusheng/pumper-source-publisher}"
LOCAL_IMAGE="local/pumper-source-publisher:${SHORT_SHA}"
RELEASE_TAG="publisher-v1"

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
  bash -n install-publisher.sh publish-publisher-image.sh
  docker compose -f docker-compose.publisher.yml --env-file .env.publisher.example config >/dev/null
  docker build -f Dockerfile.publisher -t "$LOCAL_IMAGE" .
  docker run --rm --read-only --tmpfs /tmp:size=64m --entrypoint /usr/local/bin/publisher \
    "$LOCAL_IMAGE" --help >/dev/null
  docker run --rm --read-only --tmpfs /tmp:size=64m --entrypoint sh "$LOCAL_IMAGE" -c \
    'command -v publisher >/dev/null && command -v manifestctl >/dev/null && command -v ossutil >/dev/null'
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
  docker image inspect "$LOCAL_IMAGE" --format 'publisher local-image-id={{.Id}}'
  exit 0
fi

if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  gh auth token | docker login ghcr.io -u "$(gh api user --jq .login)" --password-stdin >/dev/null
fi
push_images

