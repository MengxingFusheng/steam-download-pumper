# Two-Image Alignment Design

## Objective

The project will maintain exactly two deployable images:

1. `ikuai-line-pumper`: one container, one container IP, and one WAN line. The iKuai Docker plugin assigns the container IP and the user binds that source IP to one WAN.
2. `multi-ip-pumper`: one container with multiple LAN IPv4 addresses. Each configured IP represents one WAN line and is bound manually in iKuai.

The former single-IP/multiple-WAN connection-count balancing mode is removed. It will not remain as a hidden option, compatibility tag, Compose profile, API field, or documented deployment path.

Both images must be built from the same download, source-health, scheduling, metrics, API, and web-console core. A shared behavior fix is complete only when the test matrix passes for both images.

## Image Names And Support Policy

The supported image names are:

- Docker Hub: `traveler1314/ikuai-line-pumper`
- Docker Hub: `traveler1314/multi-ip-pumper`
- GHCR: `ghcr.io/mengxingfusheng/ikuai-line-pumper`
- GHCR: `ghcr.io/mengxingfusheng/multi-ip-pumper`

Each release publishes `latest`, `ikuai3`, and the same short Git commit tag to both registries. Both images from one release therefore identify the exact same source revision.

The old `steam-download-pumper:one-to-one` image and the old single-IP/multiple-WAN image receive no new tags or compatibility aliases. Documentation and deployment scripts must not reference them.

## Configuration Semantics

Shared fields use the same names and validation in both images:

- `target_mbps`
- `connections_per_line`
- `max_connections_per_line`
- `start_time`
- `end_time`
- `source_pool`
- worker session, restart jitter, scheduler, and log settings

`max_connections_per_line` has a hard upper limit of 12 in configuration validation, controller scaling, and the Go helper. Values above 12 are rejected rather than silently consuming more resources.

For `ikuai-line-pumper`, `target_mbps` is the target for that container's single WAN. The iKuai plugin supplies the container IP, so LAN IP fields are unsupported.

For `multi-ip-pumper`, `target_mbps` is the aggregate target for the whole container. `line_count` must be between 2 and 10, and `lan_ips` must contain exactly that many unique IPv4 addresses. The controller divides the aggregate target evenly among lines; any integer remainder is assigned one Mbps at a time from the first line. Each line has its own connection controller and the same per-line hard cap of 12.

`egress_mode`, `single_ip`, and application-level `LAN_IP` are removed from the effective configuration. The multi-IP application accepts only `LAN_IPS`; its first address is used to access the web console. Compose may use a generated `CONTAINER_IP` deployment variable for macvlan's primary address, but that value is derived from the first `LAN_IPS` entry and is not a second runtime setting. Unsupported topology fields produce a clear startup or API validation error.

## Shared Architecture

The Python runtime is reorganized into a shared core plus two topology adapters.

### Shared Core

The shared core owns:

- common configuration fields and validation;
- schedule and manual start/stop state;
- source URL validation and IPv4 resolution;
- one long-lived Go engine process per logical line;
- source health, retry cooldown, and process restart policy;
- per-line and aggregate throughput windows;
- autoscaling decisions;
- daily theoretical traffic and completion metrics;
- common API handlers and web-console rendering;
- plain UTF-8, non-ANSI logs.

Shared code must not branch on legacy deployment modes. It operates on a list of logical line specifications supplied by a topology adapter.

### Topology Adapters

The iKuai adapter returns one logical line with no explicit bind IP. Outbound IPv4 connections therefore use the container IP assigned by the iKuai plugin.

The multi-IP adapter validates and applies all configured secondary IPv4 addresses, then returns one logical line per address. Every Go engine receives its line's address through `--bind-ip`, guaranteeing that connections for that engine use the corresponding iKuai source-IP policy.

Topology-specific code is limited to configuration fields, address setup, image entry point, and the small topology section in the web form. Download and monitoring behavior remains shared.

## Download Engine And Metrics

The existing Go `discarder` remains the only data-plane implementation. It forces IPv4, streams response bodies into `io.Discard`, keeps no download cache, and uses one process with 1-12 goroutines for each logical line.

The helper will emit newline-delimited JSON status records at a fixed interval. Records include the logical line ID, bind IP, cumulative bytes, active connection count, current source, source failures, and restart/recovery events. Output contains no color escape sequences.

The Python engine wrapper parses these records without blocking. Per-line byte counters drive 10-second and 60-second throughput windows; aggregate values are sums of the line values. The kernel `eth0` RX counter remains a cross-check and fallback when helper status has not yet arrived.

Each line scales independently. Below 90% of its assigned target, it adds one connection at a time. Above 115% while target control is enabled, it removes one connection at a time. Scaling never crosses the configured base or maximum and never exceeds 12 connections per line. Independent line scaling prevents a fast WAN from hiding an underperforming WAN in the aggregate result.

If a line reaches its maximum while its 60-second average remains below 90%, the UI marks that line capacity-limited and shows its current source failures. The aggregate daily target remains 80% of the theoretical scheduled traffic.

## Web And API

Both images expose the same routes and common response schema:

- `GET /`
- `GET /api/status`
- `GET /api/metrics`
- `GET /api/sources`
- `POST /api/config`
- `POST /api/start`
- `POST /api/stop`

The metrics response contains aggregate metrics plus a `lines` array. A line item includes its ID, bind IP when applicable, target Mbps, current/10-second/60-second Mbps, active and maximum connections, status, and capacity warning.

The shared console shows aggregate cards, the real-time traffic chart, schedule, source health, and a compact per-line table. The iKuai image omits all LAN IP and line-count inputs. The multi-IP image adds only line count and LAN IP list inputs. Neither console contains an egress-mode selector or connection-count balancing instructions.

Configuration updates are validated before persistence. A running controller restarts only the engines affected by topology or source changes; connection target changes use hot scaling when possible. API errors are JSON encoded and user-provided URLs and log text are HTML escaped.

## Packaging And Deployment

There are two explicit Dockerfiles and two explicit entry points. The iKuai image has no `NET_ADMIN`, macvlan setup, or IP management dependency. The multi-IP image includes only the capability and `iproute2` tooling needed to add its configured addresses.

The supported multi-IP Compose file uses macvlan, grants `NET_ADMIN`, mounts `/data`, and keeps the root filesystem read-only with bounded tmpfs and Docker logs. Its installer scans or accepts exactly `line_count` addresses, derives Compose's `CONTAINER_IP` from the first address, and writes only the multi-IP application configuration.

A single release script builds, tests, tags, and publishes both images. It must stop before pushing either image if either build or smoke test fails. GitHub Release archives are generated for both image names with the same commit tag.

## Removal Scope

The implementation deletes or replaces:

- the `single_ip` configuration value and aliases;
- `EGRESS_MODE` and its web selector;
- single-IP/multiple-WAN deployment instructions;
- the old `one-to-one` image name, Compose filename, installer name, and publication references;
- duplicate line/main controllers, metrics trackers, source resolvers, worker wrappers, API implementations, and web templates after their behavior is moved into the shared core;
- obsolete tests that assert the removed modes or old image tags.

The repository name and Python package directory may remain unchanged because they are internal source locations, not supported runtime modes. They must not appear as user-facing product names in the two consoles or current deployment documentation.

## Failure Handling

- Invalid or duplicate IPv4 addresses fail before downloads start.
- Failure to add a secondary address identifies the exact address and leaves all engines stopped.
- DNS resolution uses IPv4 only; an unresolved or IPv6-only source is unhealthy rather than fatal when another healthy source exists.
- Engine exits use bounded exponential restart delay and do not spawn extra Python threads.
- Missing helper metrics fall back to interface totals and display per-line metrics as unavailable instead of inventing a split.
- Persistence writes use an atomic temporary-file replacement so a crash cannot leave partial JSON.
- Startup and runtime logs remain plain UTF-8 so iKuai 3.x log pages do not display ANSI control codes.

## Test And Release Gates

Unit tests cover common config validation, time windows, URL safety, source health, autoscaling boundaries, the hard cap of 12, target allocation, atomic persistence, and API schemas.

Topology tests prove:

- the iKuai adapter creates exactly one unbound line and rejects all multi-IP fields;
- the multi-IP adapter creates one bound line per unique configured IP and rejects missing or mismatched lists;
- no source file, HTML page, Compose file, or installer exposes `single_ip` or `EGRESS_MODE`;
- no supported artifact references the old image name.

Go tests cover forced IPv4, bind-IP behavior, discard-only reads, JSON status counters, source fallback, idle timeout, hot scaling, and the 12-connection cap.

Integration tests run the same local fast/slow/failing HTTP source scenarios against both image entry points. Image smoke tests verify port 80, start/stop, config updates, metrics, read-only operation, absence of SteamCMD, and expected Linux capabilities.

Release succeeds only when both Dockerfiles build from the same commit and both smoke-test suites pass. The release output records both immutable image digests so an operator can verify that deployed containers belong to the same aligned release.

## Acceptance Criteria

- Exactly two supported images and deployment paths remain.
- No single-IP/multiple-WAN mode can be selected or started.
- A shared download/controller fix is exercised by both image test variants.
- Both images report the same API schema and use the same Go engine behavior.
- `ikuai-line-pumper` uses no IP-management capability.
- `multi-ip-pumper` creates one engine bound to each configured LAN IP.
- Every logical line is capped at 12 active download connections.
- Both images publish together under the same commit tag, with no old compatibility tags.
