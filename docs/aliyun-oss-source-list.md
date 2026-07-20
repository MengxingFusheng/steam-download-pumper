# Aliyun OSS Source List

Multi-IP V2 reads a signed manifest from the OSS public endpoint. The bucket is a distribution channel, not a trust anchor: the client accepts content only after Ed25519 verification with its configured public key and key ID.

## Refresh Behavior

- Fetch immediately when the container starts.
- After success, fetch on the next local day at `04:00` plus stable 0-30 minute jitter.
- Retry failures after 5 minutes, 30 minutes, 2 hours, and then every 6 hours.
- Send `If-None-Match` when an OSS ETag is available.
- Reject payloads larger than 512 KiB.
- Reject a revision lower than the persisted accepted revision.
- Require at least three unique valid sources.

The effective priority is:

```text
valid remote manifest
last-known-good remote manifest
local source_pool from /data/config.json
image defaults
```

An expired last-known-good list is marked stale but remains usable. A failed or empty remote response never replaces active sources and never stops downloading.

## Hot Reload

Python atomically writes the effective URL array under `/run/pumper`. The long-running `discarder` receives `SIGHUP`, validates the new file, preserves circuit-breaker state for unchanged URLs, and exposes the new pool to workers without replacing the process.

The multi-IP V2 data plane rejects private, loopback, link-local, multicast, reserved, and metadata destinations during every IPv4 connection and after every redirect. Redirects are limited to three.

## API

`GET /api/source-list` reports the effective origin, revision, source count, generated/expiry time, last success, next refresh, stale state, and last error.

`POST /api/source-list/refresh` requests an immediate refresh through the same size, TLS, signature, schema, and rollback checks used by the scheduler.

## Downloader Deployment

Run one multi-IP container on each Ubuntu download server. Determine the physical
LAN interface first; this is commonly `ens18`, `ens160`, or `eth0`:

```bash
ip -4 route show default
curl -fsSLO https://raw.githubusercontent.com/MengxingFusheng/steam-download-pumper/main/install-multi-ip.sh
chmod +x install-multi-ip.sh
```

Use the publisher's public key and the exact public OSS `latest.json` URL. The
following example creates four LAN source IPs and never exceeds 12 connections per
line:

```bash
REMOTE_SOURCE_LIST_ENABLED=true \
SOURCE_LIST_URL=https://<bucket>.oss-cn-beijing.aliyuncs.com/pumper/v1/latest.json \
SOURCE_LIST_PUBLIC_KEY='<base64-ed25519-public-key>' \
SOURCE_LIST_KEY_ID=pumper-source-2026-01 \
LINE_COUNT=4 \
LAN_IPS=192.168.1.233,192.168.1.234,192.168.1.235,192.168.1.236 \
TARGET_MBPS=1600 \
CONNECTIONS_PER_LINE=8 \
MAX_CONNECTIONS_PER_LINE=12 \
LAN_PARENT=ens18 \
LAN_SUBNET=192.168.1.0/24 \
LAN_GATEWAY=192.168.1.1 \
bash install-multi-ip.sh
```

Open `http://192.168.1.233/` from another LAN device. With Docker macvlan, the
Ubuntu host itself normally cannot reach the container directly unless a separate
host-side macvlan interface is configured.

Verify that the remote list is active:

```bash
curl -fsS http://192.168.1.233/api/source-list
docker compose --env-file .env -f docker-compose.multi-ip.yml logs --tail 100 multi-ip-pumper
```

Expected API fields are `status=healthy`, `origin=remote`, the current `revision`,
and `source_count` greater than or equal to three. A fetch or signature failure
leaves the last-known-good pool active and exposes the reason through `last_error`.
