# Architecture

The repository produces two downloader images plus one independent source-list publisher image.

## Topologies

- `ikuai_line`: one logical line without an explicit bind address. The iKuai Docker plugin supplies the container IPv4 address and the operator maps it to one WAN.
- `multi_ip`: two to ten logical lines. Each line has one unique local IPv4 address and one independently controlled download engine.

`steam_pumper.topology` is the only topology boundary. The controller receives a list of `LogicalLine` objects and has no deployment-mode branches in its scheduling, metrics, source, API, or web behavior.

## Data Plane

Each logical line owns one long-running Go `discarder` process. The helper forces IPv4, reads HTTP response bodies into `io.Discard`, and manages 1-12 concurrent connections. A per-line circuit breaker is shared by all workers: three consecutive failures quarantine a source for 10 minutes, failed half-open probes escalate to 30 and 60 minutes, and only one worker may probe a quarantined source. It emits newline-delimited JSON with cumulative bytes, active connections, source state, retry time, failures, and recovery events.

The Python `EngineProcess` drains that pipe without a reader thread, preserves monotonic byte totals across helper restarts, and hot-scales connections with `SIGUSR1` and `SIGUSR2`.

## Control Plane

One `PumperController` serves both topologies. It owns:

- schedule and manual run state;
- source validation and IPv4 health resolution;
- engine lifecycle and bounded restart delay;
- per-line 10/60-second throughput windows;
- independent per-line autoscaling;
- aggregate daily traffic targets;
- one shared API and browser console.

The multi-IP V2 entrypoint may additionally own a `RemoteSourceManager`. It fetches a signed OSS envelope on startup and once per day, verifies it with the image's `manifestctl`, persists last-known-good state under `/data`, and updates the running helper through a source file plus `SIGHUP`. Remote settings remain environment-only and do not enter the writable Web configuration. The iKuai entrypoint does not enable this manager.

## Source Publisher

`pumper-source-publisher` is a separate support image, not a download topology. It has no inbound listener and no dependency on the downloader controller. A single scheduler process runs at 03:17 Asia/Shanghai, validates a mounted candidate list, signs a schema-versioned manifest, uploads an immutable release, verifies that release through the public OSS endpoint, and only then replaces `latest.json`.

The publisher reads its Ed25519 private key and least-privilege OSS RAM credentials from mounted secret files. Download images receive only the public key. Publication failures leave the previous `latest.json` untouched; downloader failures leave the current or last-known-good source pool active.

When helper metrics are not yet available, the aggregate display falls back to the `eth0` RX counter. Per-line values remain unavailable rather than being estimated.

## Configuration

Environment values provide first-boot defaults. Persisted `/data/config.json` values then take precedence. Unknown keys, wrong scalar types, invalid topology fields, duplicate addresses, and values above the per-line connection cap are rejected before engines start.

Configuration persistence uses a same-directory temporary file, file synchronization, atomic replacement, and directory synchronization.

## Release Boundaries

The legacy aligned release script still publishes the two original downloader targets together. Multi-IP OSS V2 and the publisher have independent release scripts and canary tags (`oss-v2` and `publisher-v1`) so neither rollout changes a mutable `latest` tag before acceptance. CI runs the full Python/Go suite and builds all three Dockerfiles.
