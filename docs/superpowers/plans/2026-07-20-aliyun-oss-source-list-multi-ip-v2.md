# Aliyun OSS Source List Multi-IP V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a new standard-Docker multi-IP image that securely fetches a signed source list from Aliyun OSS once per day, hot-reloads healthy sources, and continues downloading from the last-known-good or local fallback pool when publication or OSS access fails.

**Architecture:** The existing `multi_ip` topology remains the only download target of this version; the iKuai plugin image is not modified or tested for compatibility. A separate publisher Docker image validates a controlled candidate list every day at 03:17 Asia/Shanghai, signs an immutable manifest with Ed25519, uploads it to a Beijing-region OSS bucket, and updates a mutable `latest.json` copy last. The download Docker fetches on startup and at 04:00 plus 0-30 minutes of jitter, verifies the signature and rollback protections, persists the last-known-good envelope under `/data`, then asks the long-running Go helper to reload an atomic source file with `SIGHUP`.

**Tech Stack:** Python 3.13 standard library, Go 1.23 standard library, Ed25519, Aliyun OSS/`ossutil`, systemd timer, Docker, `unittest`, Go tests.

---

## Scope And Fixed Decisions

- Only `traveler1314/multi-ip-pumper` receives this feature.
- `Dockerfile.ikuai-line` and `traveler1314/ikuai-line-pumper` remain on their current behavior.
- A third support image, `pumper-source-publisher`, provides publication and internal scheduling but no download workload or Web service. Its implementation is defined in `docs/superpowers/plans/2026-07-20-source-publisher-docker.md`.
- The OSS bucket is in `cn-beijing` and remains private except for anonymous `GetObject` access to `pumper/v1/latest.json` and `pumper/v1/releases/*`.
- The client contains no Aliyun AccessKey. It only has the public manifest URL and Ed25519 public key.
- The publisher runs once per day at `03:17` Asia/Shanghai.
- The Docker client fetches at startup and once per day at `04:00` plus a stable random jitter of `0-1800` seconds.
- A failed fetch retries after 5 minutes, 30 minutes, 2 hours, then every 6 hours until a successful refresh.
- A valid remote list has precedence. `/data/config.json` `source_pool` remains the local fallback and is never overwritten by remote data.
- A remote list must contain at least three unique HTTP/HTTPS URLs. An empty, undersized, stale-at-first-use, invalidly signed, oversized, or lower-revision list is rejected.
- The last-known-good list continues to run after its advertised expiry, but the API and UI report it as `stale`. Downloading does not stop solely because OSS is unavailable.
- Source-list updates do not restart the container or Go process. `SIGHUP` reloads the source set while preserving circuit-breaker state for unchanged URLs.

## Manifest Contract

`latest.json` is a signed envelope. Signing the base64-decoded `payload` bytes avoids JSON canonicalization ambiguity:

```json
{
  "key_id": "pumper-source-2026-01",
  "algorithm": "Ed25519",
  "payload": "eyJzY2hlbWEiOjEsLi4ufQ==",
  "signature": "base64-ed25519-signature"
}
```

The decoded payload is compact UTF-8 JSON:

```json
{
  "schema": 1,
  "revision": 20260720031700,
  "generated_at": "2026-07-20T03:17:00+08:00",
  "expires_at": "2026-07-23T03:17:00+08:00",
  "sources": [
    {
      "url": "https://mirror.example.cn/releases/file.iso",
      "checked_at": "2026-07-20T03:14:12+08:00",
      "probe_mbps": 312.5
    }
  ]
}
```

Rules:

- `schema` must equal `1`.
- `revision` is a 14-digit Asia/Shanghai publication timestamp and must be greater than or equal to the persisted accepted revision. Equal revision is an unchanged refresh; lower revision is a rollback attempt.
- `generated_at` cannot be more than 10 minutes in the future.
- `expires_at` is exactly 72 hours after generation.
- `sources` contains 3-100 entries, deduplicated by exact normalized URL while preserving order.
- URLs may use only `http` or `https`, cannot contain credentials or fragments, and cannot resolve to loopback, private, link-local, multicast, unspecified, or reserved addresses.
- `checked_at` must be an RFC3339 timestamp no older than 24 hours at publication.
- `probe_mbps` is informational, finite, and non-negative. The current discarder does not interpret it as a rate limit.

## Runtime Environment

The multi-IP image accepts these environment-only settings. They are intentionally excluded from the writable Web configuration:

```text
REMOTE_SOURCE_LIST_ENABLED=true
SOURCE_LIST_URL=https://<bucket>.oss-cn-beijing.aliyuncs.com/pumper/v1/latest.json
SOURCE_LIST_PUBLIC_KEY=<base64-raw-32-byte-ed25519-public-key>
SOURCE_LIST_KEY_ID=pumper-source-2026-01
SOURCE_LIST_REFRESH_TIME=04:00
SOURCE_LIST_REFRESH_JITTER_SECONDS=1800
SOURCE_LIST_FETCH_TIMEOUT_SECONDS=15
SOURCE_LIST_MAX_BYTES=524288
SOURCE_LIST_MIN_SOURCES=3
```

`<bucket>` is an installation parameter supplied when the OSS bucket is created; it is not compiled into the image. The image fails closed to the local `source_pool` when remote mode is enabled but the URL or public key is missing.

### Task 1: Define Manifest Types And Validation

**Files:**
- Create: `steam_pumper/remote_sources.py`
- Create: `tests/test_remote_sources.py`
- Modify: `steam_pumper/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing tests for environment-only settings**

Add tests proving `RemoteSourceSettings.from_env()` accepts the exact variables above, clamps no values silently, rejects refresh jitter above 3600 seconds, rejects body limits above 1 MiB, and does not add remote settings to `CommonConfig.to_dict()`.

```python
settings = RemoteSourceSettings.from_env({
    "REMOTE_SOURCE_LIST_ENABLED": "true",
    "SOURCE_LIST_URL": "https://bucket.oss-cn-beijing.aliyuncs.com/pumper/v1/latest.json",
    "SOURCE_LIST_PUBLIC_KEY": base64.b64encode(bytes(range(32))).decode(),
    "SOURCE_LIST_KEY_ID": "pumper-source-2026-01",
})
self.assertTrue(settings.enabled)
self.assertEqual(settings.refresh_time, "04:00")
self.assertEqual(settings.refresh_jitter_seconds, 1800)
self.assertNotIn("source_list_url", MultiIPConfig().to_dict())
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `python3 -m unittest tests.test_remote_sources tests.test_config -v`

Expected: FAIL because `RemoteSourceSettings` does not exist.

- [ ] **Step 3: Implement strict settings and payload models**

Create these frozen dataclasses in `steam_pumper/remote_sources.py`:

```python
@dataclass(frozen=True)
class RemoteSourceSettings:
    enabled: bool
    url: str
    public_key: str
    key_id: str
    refresh_time: str = "04:00"
    refresh_jitter_seconds: int = 1800
    fetch_timeout_seconds: int = 15
    max_bytes: int = 524_288
    min_sources: int = 3

@dataclass(frozen=True)
class RemoteSourceEntry:
    url: str
    checked_at: datetime
    probe_mbps: float

@dataclass(frozen=True)
class RemoteSourceManifest:
    revision: int
    generated_at: datetime
    expires_at: datetime
    sources: tuple[RemoteSourceEntry, ...]
```

Keep `SOURCE_LIST_URL` and the key out of `COMMON_ENV_MAP`. Parse them only for the multi-IP entrypoint so they cannot be changed through `/api/config` or persisted into `/data/config.json`.

- [ ] **Step 4: Add failing schema and safety tests**

Cover schema mismatch, duplicate URLs, credentials, fragments, fewer than three sources, more than 100 sources, NaN/negative `probe_mbps`, future `generated_at`, malformed timestamps, and non-public literal IPs. Mock `socket.getaddrinfo` to prove a hostname resolving to `127.0.0.1`, `10.0.0.1`, `169.254.169.254`, or `224.0.0.1` is rejected.

- [ ] **Step 5: Implement `parse_manifest_payload()` and URL validation**

Use `urllib.parse`, `ipaddress.ip_address`, `socket.getaddrinfo(AF_INET)`, and existing `validate_source_pool()` for shared syntax rules. Return a `RemoteSourceManifest` only after all entries pass validation.

- [ ] **Step 6: Run focused tests and commit**

Run: `python3 -m unittest tests.test_remote_sources tests.test_config -v`

Expected: PASS.

Commit:

```bash
git add steam_pumper/remote_sources.py steam_pumper/config.py tests/test_remote_sources.py tests/test_config.py
git commit -m "feat: define signed remote source manifest contract"
```

### Task 2: Add Ed25519 Sign And Verify Helper

**Files:**
- Create: `cmd/manifestctl/main.go`
- Create: `cmd/manifestctl/main_test.go`

- [ ] **Step 1: Write failing Go tests for key generation, signing, and verification**

Test that `signEnvelope(payload, privateKey, keyID)` round-trips through `verifyEnvelope`, and that changing one payload byte, signature byte, algorithm, or key ID is rejected.

```go
publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
if err != nil { t.Fatal(err) }
envelope, err := signEnvelope([]byte(`{"schema":1}`), privateKey, "pumper-source-2026-01")
if err != nil { t.Fatal(err) }
payload, err := verifyEnvelope(envelope, publicKey, "pumper-source-2026-01", 524288)
if err != nil || string(payload) != `{"schema":1}` { t.Fatalf("payload=%q err=%v", payload, err) }
```

- [ ] **Step 2: Run the Go test and confirm it fails**

Run: `go test ./cmd/manifestctl -v`

Expected: FAIL because the package and functions do not exist.

- [ ] **Step 3: Implement a standard-library-only CLI**

Implement three commands:

```text
manifestctl keygen --private-key /path/private.key --public-key /path/public.key
manifestctl sign --private-key /path/private.key --key-id pumper-source-2026-01 < payload.json
manifestctl verify --public-key-base64 BASE64 --key-id pumper-source-2026-01 --max-bytes 524288 < envelope.json
```

`keygen` writes private files with mode `0600` and public files with mode `0644`. `sign` emits one compact JSON envelope to stdout. `verify` emits only the decoded payload on success and exits nonzero without payload output on failure.

- [ ] **Step 4: Add malformed-input and size-limit tests**

Cover invalid JSON, unknown fields, invalid base64, wrong key length, duplicate JSON fields, trailing JSON values, and input larger than `--max-bytes`.

- [ ] **Step 5: Run race tests and vet, then commit**

Run: `go test -race ./cmd/manifestctl && go vet ./cmd/manifestctl`

Expected: PASS.

Commit: `git add cmd/manifestctl && git commit -m "feat: add Ed25519 manifest signing helper"`

### Task 3: Fetch, Verify, And Persist Last-Known-Good State

**Files:**
- Modify: `steam_pumper/remote_sources.py`
- Modify: `tests/test_remote_sources.py`
- Modify: `steam_pumper/multi_ip_main.py`

- [ ] **Step 1: Write failing fetch-state tests**

Use a local `HTTPServer` fixture and a temporary `/data` directory to prove:

- the body is capped at 512 KiB before verification;
- `ETag` is stored and sent as `If-None-Match`;
- HTTP `304` retains the current revision;
- TLS/HTTP/timeout errors retain the last-known-good list;
- a lower revision is rejected;
- an equal revision is accepted as unchanged;
- a newer valid revision is persisted with atomic `os.replace`;
- an invalid new envelope never replaces the persisted valid envelope.

- [ ] **Step 2: Run tests and confirm failure**

Run: `python3 -m unittest tests.test_remote_sources -v`

Expected: FAIL because `RemoteSourceManager` does not exist.

- [ ] **Step 3: Implement the manager without background threads**

Define:

```python
@dataclass(frozen=True)
class SourceListSnapshot:
    status: str
    revision: int
    generated_at: str
    expires_at: str
    source_count: int
    last_checked_at: str
    last_success_at: str
    next_refresh_at: str
    etag: str
    last_error: str
    stale: bool

class RemoteSourceManager:
    def load_last_known_good(self) -> tuple[list[str], SourceListSnapshot]: ...
    def refresh(self, now: datetime) -> tuple[bool, list[str], SourceListSnapshot]: ...
    def due(self, now: datetime) -> bool: ...
```

Fetch with `urllib.request`, set `User-Agent: multi-ip-pumper/<version>`, require HTTPS for the OSS manifest URL, pass the envelope to `manifestctl verify`, validate the decoded payload in Python, then atomically save:

```text
/data/source-list-envelope.json
/data/source-list-state.json
```

- [ ] **Step 4: Implement daily scheduling and retry persistence**

Calculate a stable jitter from `sha256(socket.gethostname())` so ten clients do not request OSS simultaneously. Persist `next_refresh_at`; schedule successful refreshes for the next local day at `04:00 + jitter`, and apply the fixed retry sequence `300, 1800, 7200, 21600` seconds after failures.

- [ ] **Step 5: Run tests and commit**

Run: `python3 -m unittest tests.test_remote_sources -v`

Expected: PASS with no `threading.Thread` use.

Commit: `git add steam_pumper/remote_sources.py steam_pumper/multi_ip_main.py tests/test_remote_sources.py && git commit -m "feat: fetch signed OSS source lists daily"`

### Task 4: Hot-Reload Sources In The Go Data Plane

**Files:**
- Modify: `cmd/discarder/main.go`
- Modify: `cmd/discarder/main_test.go`
- Modify: `steam_pumper/engine.py`
- Modify: `steam_pumper/controller.py`
- Modify: `tests/test_engine.py`

- [ ] **Step 1: Write failing Go tests for source-file reload**

Add `--sources-file` and test that a running helper receives `SIGHUP`, loads a new JSON URL array, retains health state for unchanged URLs, removes state for deleted URLs, and never exposes an empty source set. Add `--reject-private-destinations` and prove that literal or DNS-resolved loopback, private, link-local, multicast, unspecified, reserved, and metadata addresses are rejected before dialing.

```go
initial := []string{"https://one.example/file", "https://two.example/file"}
updated := []string{"https://two.example/file", "https://three.example/file"}
set := newSourceSet(initial)
if err := set.replace(updated); err != nil { t.Fatal(err) }
if got := set.snapshot(); !reflect.DeepEqual(got, updated) { t.Fatalf("got=%v", got) }
```

- [ ] **Step 2: Run the focused Go tests and confirm failure**

Run: `go test ./cmd/discarder -run 'SourceFile|Reload' -v`

Expected: FAIL because the reloadable source set does not exist.

- [ ] **Step 3: Implement atomic source snapshots and `SIGHUP`**

Replace worker reads of immutable `opts.urls` with an `atomic.Value`-backed `sourceSet`. Add `SIGHUP` to the existing signal loop. Reload only a JSON array containing 1-100 valid HTTP/HTTPS URLs; log a structured source-list error and keep the old set when parsing fails.

Do not restart workers on reload. Each worker reads a fresh snapshot before choosing its next request. Preserve circuit-breaker entries for URLs still present and prune only removed URLs.

When `--reject-private-destinations` is enabled, resolve IPv4 explicitly, filter every candidate address before `DialContext`, and reapply the same rule after every redirect. Limit redirects to three. The multi-IP V2 controller enables this flag for both remote and local fallback sources; unit tests may disable it for local `httptest` fixtures. This closes DNS-rebinding and redirect-based SSRF paths rather than relying only on publication-time checks.

- [ ] **Step 4: Write failing Python engine tests**

Test that `EngineProcess.set_sources()` writes `/run/pumper/<line-id>.sources.json` atomically and sends exactly one `SIGHUP` to a running helper. When the helper is stopped, it updates the file without sending a signal.

- [ ] **Step 5: Implement source-file ownership in `EngineProcess`**

Change `build_engine_command()` to pass `--sources-file` rather than URLs as positional arguments. Add:

```python
def set_sources(self, sources: list[str]) -> bool:
    """Atomically replace sources and signal a live helper; return whether they changed."""
```

Store source files under `/run/pumper`, which is already expected to be tmpfs. Keep the current 1-12 connection cap and resize signals unchanged.

Pass `reject_private_destinations=True` only from the new multi-IP V2 controller construction path. The unchanged iKuai entrypoint does not opt into this new runtime behavior.

- [ ] **Step 6: Run data-plane tests and commit**

Run: `python3 -m unittest tests.test_engine -v && go test -race ./cmd/discarder`

Expected: PASS.

Commit: `git add cmd/discarder steam_pumper/engine.py steam_pumper/controller.py tests/test_engine.py && git commit -m "feat: hot reload download sources without engine restart"`

### Task 5: Integrate Remote Refresh Into The Multi-IP Controller

**Files:**
- Modify: `steam_pumper/controller.py`
- Modify: `steam_pumper/application.py`
- Modify: `steam_pumper/multi_ip_main.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/test_entrypoints.py`

- [ ] **Step 1: Write failing controller tests**

Cover these decisions:

- iKuai construction does not create a remote manager;
- multi-IP startup loads a valid persisted remote pool before engines are built;
- missing or invalid persisted state uses `cfg.source_pool`;
- `tick()` refreshes only when due and creates no thread;
- a changed remote pool calls `set_sources()` on every line;
- an unchanged revision sends no `SIGHUP`;
- refresh failure leaves every engine on its current source set;
- Web updates to local `source_pool` change only the fallback while a healthy remote list is active.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `python3 -m unittest tests.test_controller tests.test_entrypoints -v`

Expected: FAIL because controller remote integration is absent.

- [ ] **Step 3: Add a multi-IP-only manager injection point**

Extend `run_application()` and `PumperController` with an optional `remote_source_manager`. `multi_ip_main` builds it from environment; `ikuai_main` passes none and remains unchanged.

Maintain explicit controller fields:

```python
self.effective_source_pool: list[str]
self.effective_source_origin: str  # remote, last-known-good, or local-fallback
self.remote_source_snapshot: SourceListSnapshot | None
```

- [ ] **Step 4: Apply refreshes through `EngineProcess.set_sources()`**

When a newer valid list arrives, update `effective_source_pool`, resolve it for `/api/sources`, and call `set_sources()` for each existing line runtime. Do not call `stop_downloads()`, `_build_runtimes()`, or topology reconfiguration.

- [ ] **Step 5: Run tests and commit**

Run: `python3 -m unittest tests.test_controller tests.test_entrypoints -v`

Expected: PASS.

Commit: `git add steam_pumper/controller.py steam_pumper/application.py steam_pumper/multi_ip_main.py tests/test_controller.py tests/test_entrypoints.py && git commit -m "feat: apply daily OSS sources to multi-ip controller"`

### Task 6: Add Status API And Console Controls

**Files:**
- Modify: `steam_pumper/web.py`
- Modify: `tests/test_web.py`

- [ ] **Step 1: Write failing API tests**

Require `GET /api/source-list` to return the remote state and effective origin, and `POST /api/source-list/refresh` to perform a manual refresh using the same verification path. The endpoint returns `503` with JSON when remote mode is disabled or OSS cannot be reached, without changing active sources.

- [ ] **Step 2: Write failing HTML assertions**

The multi-IP console must show:

- `远程源清单` status;
- current revision and source count;
- last success and next refresh;
- `remote`, `last-known-good`, or `local-fallback` origin;
- stale/error warning;
- `立即刷新` button.

The source-list URL and public key are display-only or omitted; they are not editable form fields.

- [ ] **Step 3: Implement API and UI behavior**

Add the two endpoints and refresh the status from the existing five-second browser loop. Keep DOM updates escaped and logs assigned with `textContent`.

- [ ] **Step 4: Validate JavaScript and API tests**

Run: `python3 -m unittest tests.test_web -v`

Expected: PASS, including `node --check` when Node is installed.

- [ ] **Step 5: Commit**

Commit: `git add steam_pumper/web.py tests/test_web.py && git commit -m "feat: expose remote source list status"`

### Task 7: Build The Daily Publisher And OSS Upload Path

> **Superseded:** Do not execute the host-systemd packaging steps in this task. Implement the publisher core, Docker packaging, internal scheduler, secrets, and deployment from `docs/superpowers/plans/2026-07-20-source-publisher-docker.md`. The validation and upload-order requirements below remain acceptance criteria.

**Files:**
- Create: `source-list/candidates.json`
- Create: `tools/publish_source_list.py`
- Create: `tests/test_source_publisher.py`
- Create: `deploy/source-list-publisher.service`
- Create: `deploy/source-list-publisher.timer`
- Create: `install-source-publisher.sh`
- Test: `tests/test_publish.py`

- [ ] **Step 1: Add the controlled candidate list**

Seed `source-list/candidates.json` with the five current built-in public sources. Use objects with `url` and `enabled`; do not add automatic internet crawling or shared-document ingestion.

- [ ] **Step 2: Write failing publisher tests**

Use local fast, slow, redirecting, malformed, and failing HTTP fixtures. Require the publisher to:

- force IPv4 resolution;
- reject private/reserved destinations and credentials;
- revalidate every redirect target, maximum three redirects;
- perform two bounded 8 MiB probes with 20-second deadlines;
- accept a source only when both probes receive HTTP 200/206 and at least 2 MiB;
- record median `probe_mbps`;
- refuse publication when fewer than three sources pass;
- produce deterministic source ordering by health, throughput, then URL;
- set `expires_at` to 72 hours;
- never overwrite an existing output when signing fails.

- [ ] **Step 3: Implement publisher staging**

`tools/publish_source_list.py` writes a compact payload, invokes `manifestctl sign`, verifies the produced envelope with `manifestctl verify`, and stages these files:

```text
dist/source-list/releases/<revision>.json
dist/source-list/latest.json
```

- [ ] **Step 4: Implement least-privilege OSS upload**

`install-source-publisher.sh` installs the script, `manifestctl`, candidate file, service, and timer. The service reads `/etc/pumper-publisher/oss.env` with mode `0600` and runs:

```bash
ossutil cp "dist/source-list/releases/${REVISION}.json" \
  "oss://${OSS_BUCKET}/pumper/v1/releases/${REVISION}.json" \
  --acl public-read --meta "Content-Type:application/json#Cache-Control:no-cache,max-age=300"
ossutil cp "dist/source-list/latest.json" \
  "oss://${OSS_BUCKET}/pumper/v1/latest.json" \
  --acl public-read --meta "Content-Type:application/json#Cache-Control:no-cache,max-age=300"
```

Upload and externally verify the immutable release first; upload `latest.json` only after verification passes. Configure the RAM user for `GetObject` and `PutObject` only under `pumper/v1/*`, with no bucket deletion, policy, ACL-management, or listing permissions.

- [ ] **Step 5: Configure the daily timer**

Use:

```ini
[Timer]
OnCalendar=*-*-* 03:17:00 Asia/Shanghai
Persistent=true
RandomizedDelaySec=120
```

`Persistent=true` runs a missed publication after the host returns. The service must have no listening port and use `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, and a writable `dist` state directory only.

- [ ] **Step 6: Run publisher and shell tests, then commit**

Run: `python3 -m unittest tests.test_source_publisher tests.test_publish -v && bash -n install-source-publisher.sh`

Expected: PASS.

Commit:

```bash
git add source-list/candidates.json tools/publish_source_list.py tests/test_source_publisher.py \
  deploy/source-list-publisher.service deploy/source-list-publisher.timer \
  install-source-publisher.sh tests/test_publish.py
git commit -m "feat: publish validated source lists to Aliyun OSS daily"
```

### Task 8: Build And Publish The Multi-IP V2 Image Independently

**Files:**
- Modify: `Dockerfile.multi-ip`
- Modify: `tests/test_images.py`
- Create: `publish-multi-ip-v2.sh`
- Modify: `.github/workflows/test.yml`

- [ ] **Step 1: Write failing image assertions**

Require `Dockerfile.multi-ip` to build and copy both `/usr/local/bin/discarder` and `/usr/local/bin/manifestctl`, expose the remote-list environment defaults, and contain no OSS credentials. Keep `MAX_CONNECTIONS_PER_LINE=12`.

- [ ] **Step 2: Modify only the multi-IP build target**

Build `cmd/manifestctl` in the existing Go builder stage and copy it into the multi-IP final image. Do not modify `Dockerfile.ikuai-line` for this version.

- [ ] **Step 3: Add independent release gates**

`publish-multi-ip-v2.sh` runs all Python and Go tests, builds only `Dockerfile.multi-ip`, starts a read-only smoke container with `/data` and `/run` tmpfs, verifies `/api/source-list`, and publishes:

```text
traveler1314/multi-ip-pumper:oss-v2
traveler1314/multi-ip-pumper:<short-sha>
ghcr.io/mengxingfusheng/multi-ip-pumper:oss-v2
ghcr.io/mengxingfusheng/multi-ip-pumper:<short-sha>
```

Do not move `latest` during the first release. Promote the accepted digest to `latest` only after the 72-hour canary.

- [ ] **Step 4: Update CI for the new helper and multi-IP smoke test**

CI runs `go test -race ./...`, `go vet ./...`, Python tests, shell syntax validation, a multi-IP image build, and a smoke request to `/api/source-list` with remote mode disabled.

- [ ] **Step 5: Run dry-run publication and commit**

Run: `DRY_RUN=1 bash publish-multi-ip-v2.sh`

Expected: all tests pass, the image starts, `manifestctl` exists, and no registry push occurs.

Commit: `git add Dockerfile.multi-ip publish-multi-ip-v2.sh .github/workflows/test.yml tests/test_images.py && git commit -m "build: package OSS source updater in multi-ip v2"`

### Task 9: Documentation, Security Review, And Acceptance

**Files:**
- Modify: `README.md`
- Modify: `docs/design.md`
- Create: `docs/aliyun-oss-source-list.md`
- Modify: `tests/test_alignment.py`

- [ ] **Step 1: Update supported-version documentation**

State explicitly that remote OSS updates are a multi-IP V2 feature and are not supported by the iKuai image. Remove the old assertion that every future feature is necessarily aligned across both images, while keeping both existing image names documented.

- [ ] **Step 2: Document OSS creation and permissions**

Include console steps for a `cn-beijing` private bucket, object versioning, exact public-read object paths, lifecycle cleanup of noncurrent objects after 30 days, RAM least-privilege policy, `ossutil` credentials file permissions, key generation, timer installation, and Docker environment setup.

- [ ] **Step 3: Run the complete local verification suite**

Run:

```bash
python3 -m unittest discover -s tests -v
go test -race ./...
go vet ./...
bash -n install-multi-ip.sh install-source-publisher.sh publish-images.sh publish-multi-ip-v2.sh
docker compose -f docker-compose.multi-ip.yml config >/dev/null
docker build -f Dockerfile.multi-ip -t local/multi-ip-pumper:oss-v2 .
```

Expected: all commands succeed.

- [ ] **Step 4: Perform failure-path integration checks**

Run a local signed-manifest server and confirm:

1. A valid newer revision appears in `/api/source-list` and all lines reload without PID changes.
2. A one-byte payload mutation is rejected and current sources remain active.
3. A lower revision is rejected as rollback.
4. HTTP 404, 500, timeout, oversized body, bad JSON, and invalid signature all retain the last-known-good list.
5. Deleting `/data/source-list-envelope.json` and blocking OSS causes the configured local `source_pool` to start normally.
6. No Python background thread is created.
7. Worker count never exceeds `line_count * 12`.

- [ ] **Step 5: Run the 72-hour canary**

Deploy `traveler1314/multi-ip-pumper:oss-v2` on one Ubuntu server with the production OSS URL and public key. Acceptance requires:

- three consecutive daily publications;
- three successful automatic client refreshes;
- no container or engine restarts caused by source refresh;
- no empty effective source pool;
- continued downloads during a simulated OSS block of at least two hours;
- 60-second throughput remaining at or above 90% of the configured target when source and line capacity permit;
- no credential or private key present in `docker inspect`, the image filesystem, API responses, or logs.

- [ ] **Step 6: Promote the tested digest**

After the canary passes, retag the exact `oss-v2` digest as `latest` on Docker Hub and GHCR. Record the digest, manifest key ID, OSS revision, and rollback image tag in the GitHub Release notes.

Commit:

```bash
git add README.md docs/design.md docs/aliyun-oss-source-list.md tests/test_alignment.py
git commit -m "docs: document Aliyun OSS source distribution"
```

## Rollback

- Docker rollback: redeploy the previous recorded `multi-ip-pumper:<short-sha>` digest.
- Source-list rollback: republish a corrected manifest with a **higher** revision. Clients intentionally reject lower revisions, so restoring an old OSS object version alone is insufficient.
- Key compromise: stop the publisher timer, generate a new key ID, publish a new Docker image containing/configured with the new public key, then resume publication with the new private key. Never distribute a replacement public key through a manifest signed only by the compromised key.
- OSS outage: no operator action is required for downloading; clients continue using last-known-good or local fallback sources and report degraded remote status.

## Completion Criteria

The feature is complete only when the signed publication path, client verification, daily scheduler, hot reload, last-known-good persistence, local fallback, API/UI state, independent multi-IP build, full automated tests, and 72-hour canary have all passed. Publishing a Docker image before those checks does not complete this plan.
