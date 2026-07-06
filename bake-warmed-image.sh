#!/usr/bin/env bash
set -euo pipefail

container="${1:-steam-download-pumper}"
image="${2:-steam-download-pumper:warmed}"
base_image="${3:-steam-download-pumper:local}"

if ! docker ps --format '{{.Names}}' | grep -Fxq "$container"; then
  echo "容器未运行: $container" >&2
  exit 1
fi

if docker top "$container" aux | grep -F 'steamcmd.sh +quit' >/dev/null; then
  echo "SteamCMD 仍在首次自更新，等它完成后再打包。" >&2
  exit 2
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

docker cp "$container:/home/steam/steamcmd" "$tmpdir/steamcmd"
docker cp "$container:/home/steam/Steam" "$tmpdir/Steam"

cat > "$tmpdir/Dockerfile" <<EOF
FROM ${base_image}
USER root
COPY --chown=steam:steam steamcmd /home/steam/steamcmd
COPY --chown=steam:steam Steam /home/steam/Steam
USER steam
EOF

docker build -t "$image" "$tmpdir"
echo "已生成预热镜像: $image"
echo "后续可把 docker-compose.yml 的 image 改成 $image，并注释 build: ."
