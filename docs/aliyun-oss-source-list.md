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

