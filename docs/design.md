# Architecture

The repository produces exactly two images from one shared runtime.

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

When helper metrics are not yet available, the aggregate display falls back to the `eth0` RX counter. Per-line values remain unavailable rather than being estimated.

## Configuration

Environment values provide first-boot defaults. Persisted `/data/config.json` values then take precedence. Unknown keys, wrong scalar types, invalid topology fields, duplicate addresses, and values above the per-line connection cap are rejected before engines start.

Configuration persistence uses a same-directory temporary file, file synchronization, atomic replacement, and directory synchronization.

## Release Alignment

Both Dockerfiles copy the same Python package and build the same Go helper. CI runs the shared Python/Go suite and builds both images from one revision. The release script publishes matching `latest`, `ikuai3`, and commit tags only after both images pass their smoke checks.
