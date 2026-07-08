#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

GHCR_IMAGE="${GHCR_IMAGE:-ghcr.io/mengxingfusheng/ikuai-line-pumper}"
SHORT_SHA="${SHORT_SHA:-$(git rev-parse --short HEAD)}"
FULL_SHA="${FULL_SHA:-$(git rev-parse HEAD)}"
RELEASE_TAG="${RELEASE_TAG:-ikuai-line-${SHORT_SHA}}"
ARCHIVE_DIR="${ARCHIVE_DIR:-dist}"
ARCHIVE_PATH="${ARCHIVE_PATH:-${ARCHIVE_DIR}/ikuai-line-pumper-${SHORT_SHA}.docker.tar.gz}"

mkdir -p "$ARCHIVE_DIR"

if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  if ! docker manifest inspect "${GHCR_IMAGE}:latest" >/dev/null 2>&1; then
    gh auth token | docker login ghcr.io -u "$(gh api user --jq .login)" --password-stdin >/dev/null 2>&1 || true
  fi
fi

docker build \
  -f Dockerfile.ikuai-line \
  -t "${GHCR_IMAGE}:latest" \
  -t "${GHCR_IMAGE}:${SHORT_SHA}" \
  .

docker push "${GHCR_IMAGE}:latest"
docker push "${GHCR_IMAGE}:${SHORT_SHA}"

if [ -n "${DOCKERHUB_IMAGE:-}" ]; then
  docker tag "${GHCR_IMAGE}:latest" "${DOCKERHUB_IMAGE}:latest"
  docker tag "${GHCR_IMAGE}:latest" "${DOCKERHUB_IMAGE}:${SHORT_SHA}"
  docker push "${DOCKERHUB_IMAGE}:latest"
  docker push "${DOCKERHUB_IMAGE}:${SHORT_SHA}"
fi

docker save "${GHCR_IMAGE}:latest" | gzip -1 > "$ARCHIVE_PATH"

if command -v gh >/dev/null 2>&1; then
  notes="iKuai Docker plugin single-line image.

GHCR:
- ${GHCR_IMAGE}:latest
- ${GHCR_IMAGE}:${SHORT_SHA}

Manual load:
\`\`\`bash
gzip -dc $(basename "$ARCHIVE_PATH") | docker load
\`\`\`"
  if gh release view "$RELEASE_TAG" --repo MengxingFusheng/steam-download-pumper >/dev/null 2>&1; then
    gh release upload "$RELEASE_TAG" "$ARCHIVE_PATH" --repo MengxingFusheng/steam-download-pumper --clobber
  else
    gh release create "$RELEASE_TAG" "$ARCHIVE_PATH" \
      --repo MengxingFusheng/steam-download-pumper \
      --target "$FULL_SHA" \
      --title "iKuai line image ${SHORT_SHA}" \
      --notes "$notes"
  fi
fi

printf '%s\n' "published ${GHCR_IMAGE}:latest"
printf '%s\n' "published ${GHCR_IMAGE}:${SHORT_SHA}"
printf '%s\n' "archive ${ARCHIVE_PATH}"
