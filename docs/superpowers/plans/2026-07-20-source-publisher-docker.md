# Source Publisher Docker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package the Aliyun OSS source-list publisher as a hardened Docker image that validates approved sources, signs a manifest, publishes it once per day, and exposes no inbound service.

**Architecture:** A dedicated `pumper-source-publisher` image contains a Python publisher package, the Go `manifestctl` signer/verifier, CA certificates, and a checksum-pinned Aliyun `ossutil 2.3.0` binary. Its default command is a single-process scheduler that publishes at 03:17 Asia/Shanghai; `publish-once`, `validate-only`, and `healthcheck` subcommands support operations and testing. Candidates, state, signing key, and RAM credentials are mounted at runtime, so neither credentials nor generated manifests enter image layers.

**Tech Stack:** Python 3.13 standard library, Go 1.23 standard library, Ed25519, Aliyun `ossutil 2.3.0`, Docker BuildKit/buildx, Docker Compose, `unittest`, Go tests.

---

## Relationship To The Download Image Plan

This plan supersedes the host-systemd packaging portion of Task 7 in `docs/superpowers/plans/2026-07-20-aliyun-oss-source-list-multi-ip-v2.md`. The manifest contract, validation rules, immutable-release-first upload order, client verification, and last-known-good behavior from that plan remain unchanged.

This creates a third support image but does not add another download topology:

```text
traveler1314/multi-ip-pumper              # multi-IP downloader
traveler1314/ikuai-line-pumper            # existing image, unchanged
traveler1314/pumper-source-publisher      # source-list publisher, no downloads
```

The publisher has no compatibility requirement with the iKuai Docker plugin.

## Fixed Runtime Contract

Image names:

```text
traveler1314/pumper-source-publisher:publisher-v1
traveler1314/pumper-source-publisher:<short-sha>
ghcr.io/mengxingfusheng/pumper-source-publisher:publisher-v1
ghcr.io/mengxingfusheng/pumper-source-publisher:<short-sha>
```

After three successful production publications, the accepted digest may also receive `latest`.

Commands:

```text
publisher scheduler       # default: stay alive and run at 03:17 Asia/Shanghai
publisher publish-once    # validate, sign, upload, verify, then exit
publisher validate-only   # probe and build an unsigned report without OSS writes
publisher healthcheck     # exit 0/1 based on persisted scheduler/publication state
```

Mounts:

```text
/config/candidates.json                  read-only candidate list
/state                                   writable persistent state
/run/secrets/source_signing_private_key  read-only Ed25519 private key
/run/secrets/oss_access_key_id           read-only RAM AccessKey ID
/run/secrets/oss_access_key_secret       read-only RAM AccessKey secret
```

Non-secret environment:

```text
OSS_BUCKET=pumper-source-list-<globally-unique-suffix>
OSS_REGION=cn-beijing
OSS_ENDPOINT=https://oss-cn-beijing.aliyuncs.com
OSS_PUBLIC_BASE_URL=https://<bucket>.oss-cn-beijing.aliyuncs.com/pumper/v1
SOURCE_LIST_KEY_ID=pumper-source-2026-01
PUBLISH_TIME=03:17
PUBLISH_TIMEZONE=Asia/Shanghai
PUBLISH_RETRY_SECONDS=900,3600,21600
MIN_HEALTHY_SOURCES=3
MAX_HEALTHY_SOURCES=100
PROBE_CONCURRENCY=4
PROBE_BYTES=8388608
PROBE_TIMEOUT_SECONDS=20
LOG_LEVEL=INFO
```

`<globally-unique-suffix>` and `<bucket>` are deployment parameters because OSS bucket names are globally unique. The Compose deployment must substitute both from its `.env`; no account-specific bucket is compiled into the image.

## Container Layout

```text
/app/source_publisher/
├── __init__.py
├── config.py          # immutable runtime configuration and secret-file reads
├── candidates.py      # candidate schema and URL safety checks
├── probe.py           # bounded IPv4 HTTP probes
├── manifest.py        # payload construction and manifestctl invocation
├── oss.py             # ossutil command construction and upload verification
├── service.py         # one publication transaction
├── scheduler.py       # daily schedule, retries, locking, SIGTERM handling
└── main.py            # CLI subcommands and exit codes

/usr/local/bin/
├── publisher          # tiny exec wrapper for python -m source_publisher.main
├── manifestctl        # static Go binary
└── ossutil            # official checksum-verified 2.3.0 binary
```

## Security Boundaries

- No `EXPOSE`, HTTP server, SSH server, or inbound listener.
- No Docker Socket, host PID namespace, privileged mode, or added Linux capability.
- Final image runs as UID/GID `10001`, with a read-only root filesystem.
- Only `/state` is persistent and writable; `/tmp` is a 64 MiB tmpfs.
- Secret values are read from files, never accepted as CLI flags, never written to state, and never included in exception text.
- The child `ossutil` process receives credentials through a private environment dictionary. They do not appear in `docker inspect` or process arguments.
- The RAM policy permits `oss:GetObject` and `oss:PutObject` only for `acs:oss:*:*:<bucket>/pumper/v1/*`.
- Public reads are granted by an OSS bucket policy for the exact `pumper/v1/latest.json` and `pumper/v1/releases/*` resources. The publisher does not need `oss:PutObjectAcl` or bucket-policy permissions.
- The signing private key is available only inside the publisher container. Download containers receive the public key only.
- Publisher logs are structured UTF-8 JSON and redact AccessKey IDs, secrets, session tokens, query strings, and signing-key paths.

### Task 1: Create Publisher Configuration And CLI Skeleton

**Files:**
- Create: `source_publisher/__init__.py`
- Create: `source_publisher/config.py`
- Create: `source_publisher/main.py`
- Create: `bin/publisher`
- Create: `tests/test_publisher_config.py`
- Create: `tests/test_publisher_cli.py`

- [ ] **Step 1: Write failing configuration tests**

Cover required variables, exact defaults, invalid bucket/region/endpoint, invalid `HH:MM`, unknown timezone, `MIN_HEALTHY_SOURCES < 3`, maximum source count above 100, probe concurrency above 8, probe body above 16 MiB, and unreadable secret files.

```python
cfg = PublisherConfig.from_env(
    {
        "OSS_BUCKET": "pumper-source-list-example",
        "OSS_REGION": "cn-beijing",
        "OSS_ENDPOINT": "https://oss-cn-beijing.aliyuncs.com",
        "OSS_PUBLIC_BASE_URL": "https://pumper-source-list-example.oss-cn-beijing.aliyuncs.com/pumper/v1",
        "SOURCE_LIST_KEY_ID": "pumper-source-2026-01",
    },
    secret_root=Path(tmpdir),
)
self.assertEqual(cfg.publish_time, time(3, 17))
self.assertEqual(cfg.probe_concurrency, 4)
self.assertEqual(cfg.retry_seconds, (900, 3600, 21600))
```

- [ ] **Step 2: Run the tests and confirm failure**

Run: `python3 -m unittest tests.test_publisher_config tests.test_publisher_cli -v`

Expected: FAIL because `source_publisher` does not exist.

- [ ] **Step 3: Implement immutable configuration and secret-file reads**

Create a frozen `PublisherConfig` with typed URL, timing, limits, paths, and OSS settings, plus a separate `PublisherSecrets` loaded only by `scheduler`, `publish-once`, and `healthcheck`. `validate-only` must run without signing or OSS secrets. Read secrets through:

```python
def read_secret(path: Path, name: str) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"{name} secret is empty")
    return value
```

Error messages may contain the logical secret name but never the secret value. Reject symlinks for all three secret paths with `Path.is_symlink()` and require regular files.

- [ ] **Step 4: Implement CLI routing and stable exit codes**

Use these exits:

```text
0 success
2 invalid configuration or candidate file
3 insufficient healthy sources
4 signing or verification failure
5 OSS upload or public verification failure
6 lock already held
7 healthcheck unhealthy
```

`bin/publisher` must use `exec python3 -m source_publisher.main "$@"` so SIGTERM reaches Python.

- [ ] **Step 5: Run tests and commit**

Run: `python3 -m unittest tests.test_publisher_config tests.test_publisher_cli -v`

Expected: PASS.

Commit:

```bash
git add source_publisher bin/publisher tests/test_publisher_config.py tests/test_publisher_cli.py
git commit -m "feat: scaffold source publisher service"
```

### Task 2: Implement Candidate Validation And Bounded Probes

**Files:**
- Create: `source_publisher/candidates.py`
- Create: `source_publisher/probe.py`
- Create: `source-list/candidates.json`
- Create: `tests/test_publisher_candidates.py`
- Create: `tests/test_publisher_probe.py`

- [ ] **Step 1: Write failing candidate tests**

Require the input shape:

```json
{
  "schema": 1,
  "sources": [
    {"url": "https://mirror.example.cn/releases/file.iso", "enabled": true}
  ]
}
```

Test duplicate URLs, unsupported schemes, URL credentials, fragments, whitespace, malformed ports, more than 200 candidates, and non-boolean `enabled` values.

- [ ] **Step 2: Write failing network safety tests**

Mock IPv4 DNS results and reject loopback, RFC1918, link-local, multicast, unspecified, reserved, documentation, and `169.254.169.254` metadata addresses. Resolve and validate every redirect destination, cap redirects at three, and reject IPv6-only hosts.

- [ ] **Step 3: Write failing bounded-probe tests**

Use local HTTP fixtures to prove:

- `Range: bytes=0-8388607` is sent;
- only HTTP 200/206 is accepted;
- response reading stops at 8 MiB;
- at least 2 MiB must be read;
- two probes must both pass;
- median Mbps is finite and non-negative;
- only four probes run concurrently;
- timeouts and truncated bodies are recorded as source failures without crashing the publication.

- [ ] **Step 4: Implement candidate parsing and probes**

Use `socket.AF_INET`, `urllib.request`, `ThreadPoolExecutor(max_workers=4)`, and bounded reads. This short-lived probe pool is allowed; the scheduler itself remains a single process and creates no permanent thread.

Seed the candidate file with the five sources currently in `default_source_pool()` and keep automatic internet crawling out of V1.

- [ ] **Step 5: Run tests and commit**

Run: `python3 -m unittest tests.test_publisher_candidates tests.test_publisher_probe -v`

Expected: PASS.

Commit:

```bash
git add source_publisher/candidates.py source_publisher/probe.py source-list/candidates.json \
  tests/test_publisher_candidates.py tests/test_publisher_probe.py
git commit -m "feat: validate and probe publisher candidates"
```

### Task 3: Build And Sign Deterministic Manifests

**Files:**
- Create: `source_publisher/manifest.py`
- Modify: `cmd/manifestctl/main.go`
- Modify: `cmd/manifestctl/main_test.go`
- Create: `tests/test_publisher_manifest.py`

- [ ] **Step 1: Write failing deterministic-manifest tests**

For a fixed clock and probe results, require byte-identical compact payload output, sources sorted by healthy status then descending `probe_mbps` then URL, a 14-digit Asia/Shanghai revision, and `expires_at` exactly 72 hours after `generated_at`.

- [ ] **Step 2: Write failing signer integration tests**

Generate a temporary Ed25519 key, invoke `manifestctl sign`, then invoke `manifestctl verify`. Tampering with payload, signature, key ID, or algorithm must fail without emitting decoded payload.

- [ ] **Step 3: Implement payload construction and subprocess isolation**

Invoke `manifestctl` using an argument list, `check=False`, a 10-second timeout, and a reduced environment. Send payload/key data over stdin or private files; never put key bytes into arguments.

Write outputs atomically under:

```text
/state/staging/<revision>.json
/state/staging/latest.json
```

- [ ] **Step 4: Run Python and Go verification**

Run:

```bash
python3 -m unittest tests.test_publisher_manifest -v
go test -race ./cmd/manifestctl
go vet ./cmd/manifestctl
```

Expected: PASS.

- [ ] **Step 5: Commit**

Commit:

```bash
git add source_publisher/manifest.py cmd/manifestctl tests/test_publisher_manifest.py
git commit -m "feat: create signed source manifests"
```

### Task 4: Implement OSS Publication Transaction

**Files:**
- Create: `source_publisher/oss.py`
- Create: `source_publisher/service.py`
- Create: `tests/test_publisher_oss.py`
- Create: `tests/test_publisher_service.py`

- [ ] **Step 1: Write failing command-security tests**

Use a fake `ossutil` executable to capture argv and environment. Assert that AccessKey values are absent from argv, logs, state JSON, and exceptions; present only in the child environment; and removed from the parent environment copy after the subprocess returns.

- [ ] **Step 2: Write failing publication-order tests**

Require this transaction:

```text
1. upload pumper/v1/releases/<revision>.json
2. public HTTPS GET immutable release
3. manifestctl verify immutable release
4. upload pumper/v1/latest.json
5. public HTTPS GET latest.json
6. manifestctl verify latest.json
7. atomically record successful state
```

If steps 1-3 fail, `latest.json` must not be uploaded. If steps 4-6 fail, the run is failed and the prior successful state remains. The next run always uses a higher revision.

- [ ] **Step 3: Implement ossutil environment and commands**

Construct a private child environment containing:

```text
OSS_ACCESS_KEY_ID
OSS_ACCESS_KEY_SECRET
OSS_REGION=cn-beijing
OSS_ENDPOINT=https://oss-cn-beijing.aliyuncs.com
```

Upload with `ossutil cp --force` and JSON output. Do not pass `--acl`, AccessKey flags, or bucket-management commands. The bucket policy is provisioned separately and grants anonymous read only to the two public paths.

- [ ] **Step 4: Implement public verification and state**

Limit public verification responses to 512 KiB and require HTTPS, matching key ID, signature, revision, and exact envelope bytes. Store only non-secret operational state:

```json
{
  "last_attempt_at": "2026-07-20T03:17:00+08:00",
  "last_success_at": "2026-07-20T03:18:12+08:00",
  "last_revision": 20260720031700,
  "last_source_count": 5,
  "last_error": "",
  "consecutive_failures": 0
}
```

- [ ] **Step 5: Run transaction tests and commit**

Run: `python3 -m unittest tests.test_publisher_oss tests.test_publisher_service -v`

Expected: PASS.

Commit:

```bash
git add source_publisher/oss.py source_publisher/service.py \
  tests/test_publisher_oss.py tests/test_publisher_service.py
git commit -m "feat: publish and verify manifests on Aliyun OSS"
```

### Task 5: Add Internal Daily Scheduler, Locking, And Healthcheck

**Files:**
- Create: `source_publisher/scheduler.py`
- Modify: `source_publisher/main.py`
- Create: `tests/test_publisher_scheduler.py`
- Modify: `tests/test_publisher_cli.py`

- [ ] **Step 1: Write failing next-run tests**

Inject a clock and cover times before/after 03:17, month/year boundaries, leap day, timezone handling, host clock changes, and restart after a missed schedule. A start after 03:17 with no successful publication for the current local date must run immediately.

- [ ] **Step 2: Write failing retry and shutdown tests**

Require retry delays of 15 minutes, 1 hour, and then 6 hours capped. SIGTERM during sleep exits within five seconds. SIGTERM during probes cancels pending work, skips OSS publication, writes an interrupted attempt, and exits within the 25-second bounded probe deadline.

- [ ] **Step 3: Add an exclusive persistent lock**

Use `fcntl.flock(LOCK_EX | LOCK_NB)` on `/state/publish.lock`. A second scheduler or manual `publish-once` exits `6` without probing or uploading.

- [ ] **Step 4: Implement healthcheck rules**

`publisher healthcheck` returns healthy when:

- configuration and secret files are readable;
- scheduler heartbeat is newer than 5 minutes;
- no publication is currently stuck longer than 30 minutes;
- the last success is newer than 36 hours.

Allow startup grace until two hours after the first calculated publication due time. This avoids an unhealthy interval when the container starts shortly before 03:17. Persist heartbeat, process start, and first due timestamps under `/state/health.json`.

- [ ] **Step 5: Run tests and commit**

Run: `python3 -m unittest tests.test_publisher_scheduler tests.test_publisher_cli -v`

Expected: PASS without real waiting; tests use a fake clock/sleeper.

Commit:

```bash
git add source_publisher/scheduler.py source_publisher/main.py \
  tests/test_publisher_scheduler.py tests/test_publisher_cli.py
git commit -m "feat: schedule daily source publication in container"
```

### Task 6: Build A Hardened Multi-Architecture Image

**Files:**
- Create: `Dockerfile.publisher`
- Modify: `.dockerignore`
- Modify: `tests/test_images.py`
- Create: `tests/test_publisher_image.py`

- [ ] **Step 1: Write failing Dockerfile assertions**

Require exactly three maintained Dockerfiles, no `EXPOSE` in the publisher, a non-root `USER 10001:10001`, copied `publisher`, `manifestctl`, and `ossutil` binaries, a `HEALTHCHECK`, and no `ARG`/`ENV` names containing AccessKey or private-key contents.

- [ ] **Step 2: Add pinned ossutil download stages**

Pin official `ossutil 2.3.0` artifacts and verify before extraction:

```text
linux/amd64  3ae4d9fc85a7a6e9f5654d1599766f1a3a42a3692870887b5ae9338d582ef65a
linux/arm64  f6c95ba0c2d2ef30290af686ce4d706c701f4734ce8090bee4288a77e3f1d764
```

Use `TARGETARCH` to select the official archive, fail unsupported architectures, and copy only the verified binary into the final stage. Do not run a remote install shell script during the image build.

- [ ] **Step 3: Build manifestctl and the final runtime**

Use a Go builder for static `manifestctl`. Base the final stage on `python:3.13-slim`, install only `ca-certificates` and `tzdata`, create UID/GID 10001, copy publisher code and binaries, and set:

```dockerfile
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/publisher"]
CMD ["scheduler"]
HEALTHCHECK --interval=5m --timeout=10s --start-period=5m --retries=2 \
  CMD ["/usr/local/bin/publisher", "healthcheck"]
```

Add `publisher-secrets/`, `/state` output, generated manifests, and local credential files to the shared `.dockerignore`; no secret or runtime-state path may enter any Docker build context.

- [ ] **Step 4: Add image smoke tests**

Build the image and assert:

- `publisher --help`, `manifestctl`, and `ossutil version` work;
- `/proc/1/status` identifies UID 10001;
- no listening TCP/UDP sockets exist;
- writing outside `/state` and `/tmp` fails under a read-only root;
- `validate-only` works with local fixtures and no OSS secrets;
- the image filesystem contains no sample AccessKey or signing private key.

- [ ] **Step 5: Run image tests and commit**

Run:

```bash
python3 -m unittest tests.test_images tests.test_publisher_image -v
docker build -f Dockerfile.publisher -t local/pumper-source-publisher:test .
```

Expected: PASS on linux/amd64.

Commit:

```bash
git add Dockerfile.publisher .dockerignore tests/test_images.py tests/test_publisher_image.py
git commit -m "build: package hardened source publisher image"
```

### Task 7: Add Secure Compose Deployment

**Files:**
- Create: `docker-compose.publisher.yml`
- Create: `.env.publisher.example`
- Create: `install-publisher.sh`
- Create: `tests/test_publisher_deploy.py`

- [ ] **Step 1: Write failing Compose assertions**

Require:

```yaml
read_only: true
restart: unless-stopped
cap_drop: [ALL]
security_opt: [no-new-privileges:true]
pids_limit: 64
mem_limit: 192m
cpus: 0.50
tmpfs:
  - /tmp:size=64m,noexec,nosuid,nodev
```

Assert there is no `ports`, `network_mode: host`, `privileged`, Docker Socket mount, or secret value in environment.

- [ ] **Step 2: Define config, state, and secrets mounts**

Use a named volume for `/state`, bind-mount `./publisher-config/candidates.json` read-only, and mount three Docker Compose secrets under `/run/secrets`. The secret source files live under `./publisher-secrets/`, are excluded by `.gitignore`, and must be mode `0600` before startup.

- [ ] **Step 3: Implement installer validation**

`install-publisher.sh` must:

1. require Docker Compose V2;
2. validate bucket, endpoint, public URL, and key ID;
3. create config/state/secret directories without overwriting existing secrets;
4. reject group/world-readable secret files;
5. run `docker compose config` and `publisher validate-only`;
6. start the scheduler only after validation succeeds;
7. print the next scheduled time and health status without printing secrets.

- [ ] **Step 4: Test the deployment files**

Run:

```bash
bash -n install-publisher.sh
docker compose -f docker-compose.publisher.yml --env-file .env.publisher.example config >/dev/null
python3 -m unittest tests.test_publisher_deploy -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Commit:

```bash
git add docker-compose.publisher.yml .env.publisher.example install-publisher.sh \
  tests/test_publisher_deploy.py .gitignore
git commit -m "feat: add secure publisher Docker deployment"
```

### Task 8: Add Independent Publisher Image Release

**Files:**
- Create: `publish-publisher-image.sh`
- Modify: `.github/workflows/test.yml`
- Modify: `tests/test_publish.py`

- [ ] **Step 1: Write failing release-script tests**

Require Python/Go tests, Dockerfile build, read-only smoke test, secret scan, and a dry-run mode. Assert publisher release does not rebuild or push either download image.

- [ ] **Step 2: Implement multi-architecture build and tags**

Use `docker buildx build --platform linux/amd64,linux/arm64`. Push matching `publisher-v1` and short-SHA tags to Docker Hub and GHCR only after all tests pass. Record both platform digests and the manifest-list digest.

- [ ] **Step 3: Add GitHub Release artifacts**

Attach:

```text
pumper-source-publisher-<sha>-linux-amd64.docker.tar.gz
pumper-source-publisher-<sha>-linux-arm64.docker.tar.gz
pumper-source-publisher-<sha>-digests.txt
```

Release notes include the pinned ossutil version/checksums, manifest key ID, image digest, and minimum RAM policy. They contain no bucket credentials or private-key material.

- [ ] **Step 4: Update CI**

CI runs all publisher unit tests, Go race/vet, shell syntax, Compose config, amd64 image build, no-listener smoke test, and Trivy or the repository's available image scanner when configured.

- [ ] **Step 5: Run dry-run publication and commit**

Run: `DRY_RUN=1 bash publish-publisher-image.sh`

Expected: tests and local image build pass; no registry push occurs.

Commit:

```bash
git add publish-publisher-image.sh .github/workflows/test.yml tests/test_publish.py
git commit -m "build: release publisher image independently"
```

### Task 9: Document OSS, Secrets, Operations, And Recovery

**Files:**
- Create: `docs/publisher-docker.md`
- Modify: `README.md`
- Modify: `docs/design.md`

- [ ] **Step 1: Document OSS provisioning**

Include:

- create a private `cn-beijing` bucket;
- enable object versioning;
- add anonymous `GetObject` bucket-policy statements only for `pumper/v1/latest.json` and `pumper/v1/releases/*`;
- deny anonymous listing and all writes;
- add a lifecycle rule deleting noncurrent release versions after 30 days;
- create a RAM user limited to `GetObject`/`PutObject` on the same prefix;
- configure billing and abnormal-request alerts.

- [ ] **Step 2: Document key generation and backup**

Generate keys through the built image in an offline temporary container, store the private key as a Docker secret with mode `0600`, copy only the public key to downloader deployments, and keep one encrypted offline backup. Document key-ID rotation without placing private material in Git.

- [ ] **Step 3: Document operations**

Provide exact commands for install, manual validation, one-shot publication, logs, health, candidate updates, image upgrade by digest, state backup, failed-publication diagnosis, and rollback.

- [ ] **Step 4: Run documentation and full verification checks**

Run:

```bash
python3 -m unittest discover -s tests -v
go test -race ./...
go vet ./...
bash -n install-publisher.sh publish-publisher-image.sh
docker compose -f docker-compose.publisher.yml --env-file .env.publisher.example config >/dev/null
docker build -f Dockerfile.publisher -t local/pumper-source-publisher:test .
```

Expected: PASS.

- [ ] **Step 5: Commit**

Commit:

```bash
git add docs/publisher-docker.md README.md docs/design.md
git commit -m "docs: add publisher container operations guide"
```

## Acceptance Test

Run `publisher-v1` for three days before assigning `latest`:

1. Container opens no listening socket and remains healthy between runs.
2. It publishes once at 03:17 Asia/Shanghai each day and does not duplicate a successful daily run after restart.
3. A forced failure retries at 15 minutes, 1 hour, then 6 hours without changing `latest.json`.
4. Two concurrent `publish-once` attempts result in exactly one lock owner and one exit code `6`.
5. At least three sources are required; two healthy sources produce no OSS write.
6. The immutable release is publicly downloaded and verified before `latest.json` changes.
7. OSS AccessKeys and Ed25519 private key do not appear in `docker inspect`, image history, filesystem layers, process arguments, logs, state files, or release artifacts.
8. Revoking RAM credentials causes a clean publication failure while the prior OSS manifest remains available to download clients.
9. Tampering with OSS content causes client and publisher verification failure.
10. CPU is effectively idle between schedules, resident memory stays below 96 MiB, and `/state` growth remains below 10 MiB excluding explicitly retained diagnostic reports.

## Rollback And Disaster Recovery

- Image rollback: deploy the previously recorded publisher digest; do not rely on a mutable tag.
- Publication rollback: publish corrected content with a higher revision because download clients reject decreasing revisions.
- Lost state volume: read and verify current OSS `latest.json`, restore its revision into fresh state, then resume; never reset revision to zero against a populated bucket.
- Lost RAM key: revoke it, create a replacement under the same least-privilege policy, replace only the two Docker secret files, and restart the publisher.
- Signing-key compromise: stop the publisher, revoke RAM credentials, build/configure downloader clients with a new public key ID, then resume publication with a new private key. Replacing only the OSS object cannot recover trust.
- Publisher host compromise: move the secrets and image digest to a clean host; no inbound service or Docker Socket is required for recovery.

## Completion Criteria

The publisher Docker work is complete only after the container build, secret-file handling, source probes, Ed25519 signing, immutable-first OSS publication, public verification, daily scheduler, retries, exclusive lock, healthcheck, Compose hardening, multi-architecture release, documentation, and three-day production canary all pass.
