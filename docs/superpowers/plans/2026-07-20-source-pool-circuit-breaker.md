# Source Pool Circuit Breaker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand the verified China IPv4 source pool and prevent failed sources from consuming workers through a shared circuit breaker with automatic recovery.

**Architecture:** Extend the Go `sourceHealth` into a per-line concurrency-safe state machine shared by all workers. Emit source state through the existing JSON stream, aggregate it in the shared Python controller, then publish both aligned images from one revision.

**Tech Stack:** Go 1.23, Python 3.13 standard library, `unittest`, Docker, GHCR, Docker Hub, SSH/Paramiko.

---

## File Map

- Modify `cmd/discarder/main.go`: circuit-breaker state, single-probe claiming, wait calculation and source events.
- Modify `cmd/discarder/main_test.go`: deterministic state-machine and healthy/dead source integration tests.
- Modify `steam_pumper/config.py`: verified domestic IPv4 defaults.
- Modify `steam_pumper/engine.py`: latest structured source state per line.
- Modify `steam_pumper/controller.py`: aggregate source states for `/api/sources`.
- Modify `tests/test_config.py`, `tests/test_engine.py`, `tests/test_controller.py`, and `tests/test_alignment.py`: regression coverage.

### Task 1: Verify Candidate Sources

**Files:**
- Inspect: `steam_pumper/config.py`
- Modify later: `steam_pumper/config.py`

- [ ] **Step 1: Collect candidates**

Collect public endpoints from official operator, university mirror, and vendor distribution pages. Reject credentials, cookies, and browser-only flows.

- [ ] **Step 2: Probe candidates from the deployed four-line server**

```bash
curl -4 -L --fail --silent --show-error --output /dev/null \
  --interface 192.168.1.233 --connect-timeout 5 --max-time 20 \
  --write-out '%{http_code} %{remote_ip} %{size_download} %{speed_download}\n' URL
```

Repeat through `.233-.236`. Require a successful status, non-zero sustained body, China IPv4 remote, and stable repeated probes.

- [ ] **Step 3: Select independent backends**

Reject duplicate aliases, endpoints failing any required route, and tiny responses. Preserve the two healthy Shanghai sources if they still pass.

### Task 2: Implement the Go Circuit Breaker with TDD

**Files:**
- Modify: `cmd/discarder/main_test.go`
- Modify: `cmd/discarder/main.go`

- [ ] **Step 1: Write failing state-machine tests**

Add tests for 1s/2s degraded cooldowns, 10m quarantine on the third failure, 30m and 60m failed-probe quarantines, one concurrent probe claimant, success reset, and cancellation.

- [ ] **Step 2: Confirm the tests fail**

```bash
go test ./cmd/discarder -run 'TestSourceCircuit|TestSourceHealth' -count=1
```

Expected: FAIL because quarantine state and probe ownership do not exist.

- [ ] **Step 3: Implement explicit source state**

```go
type sourceState struct {
    consecutiveFailures int
    quarantineLevel     int
    retryAfter          time.Time
    probeInFlight       bool
    lastError           string
}
```

Add locked methods to claim a normal request or one half-open probe, record failure, record success, release a canceled probe, and return the earliest retry duration.

- [ ] **Step 4: Integrate worker selection**

Skip quarantined sources, allow one probe, and sleep until the earliest retry with bounded jitter when all sources are unavailable. Keep the hard cap of 12 connections per line.

- [ ] **Step 5: Emit structured state**

```go
State               string `json:"state,omitempty"`
ConsecutiveFailures int    `json:"consecutive_failures,omitempty"`
RetryAfter          string `json:"retry_after,omitempty"`
RetryInSeconds      int64  `json:"retry_in_seconds,omitempty"`
```

Emit newline-delimited UTF-8 JSON on failure, quarantine and recovery, with no ANSI output.

- [ ] **Step 6: Add HTTP integration coverage**

Use one failing `httptest.Server` and one streaming healthy server. Assert the healthy source continues producing bytes while requests to the failing source stop at the threshold.

- [ ] **Step 7: Verify Go**

```bash
gofmt -w cmd/discarder/main.go cmd/discarder/main_test.go
go test -race ./cmd/discarder
go vet ./cmd/discarder
```

Expected: all commands exit 0.

### Task 3: Align Python State and API

**Files:**
- Modify: `steam_pumper/engine.py`
- Modify: `steam_pumper/controller.py`
- Modify: `tests/test_engine.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/test_alignment.py`

- [ ] **Step 1: Write failing parser and aggregation tests**

Feed quarantined and recovered source events into `EngineProcess._consume_line()`. Assert state, consecutive failures, retry time, last error, aggregate health, and per-line details.

- [ ] **Step 2: Confirm focused tests fail**

```bash
python3 -m unittest tests.test_engine tests.test_controller tests.test_alignment -v
```

Expected: FAIL because only cumulative integer failure counts are stored.

- [ ] **Step 3: Add a typed runtime record**

```python
@dataclass
class SourceRuntimeState:
    state: str = "healthy"
    consecutive_failures: int = 0
    retry_after: str = ""
    retry_in_seconds: int = 0
    last_error: str = ""
```

Store records by URL in `EngineState.source_states`; recovery replaces the record with healthy defaults.

- [ ] **Step 4: Aggregate source snapshots**

Keep `url`, `ip`, `healthy`, and `failures`. Add `state`, `retry_after`, `retry_in_seconds`, `last_error`, and `lines`. A source is globally healthy when at least one active line reports it healthy.

- [ ] **Step 5: Verify Python alignment**

```bash
python3 -m unittest tests.test_engine tests.test_controller tests.test_alignment -v
```

Expected: all focused tests pass.

### Task 4: Update the Verified Defaults

**Files:**
- Modify: `steam_pumper/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write a failing default-pool test**

Assert dead Jiangsu endpoints are absent, URLs are unique HTTP/HTTPS values, and all selected verified candidates are present.

- [ ] **Step 2: Confirm the config test fails**

```bash
python3 -m unittest tests.test_config -v
```

Expected: FAIL because stale endpoints remain.

- [ ] **Step 3: Replace and deduplicate defaults**

Update `default_source_pool()` with verified URLs. Make `validate_source_pool()` remove exact duplicates while preserving order.

- [ ] **Step 4: Run the full suites**

```bash
python3 -m unittest discover -s tests -v
go test -race ./...
```

Expected: all tests pass.

### Task 5: Build and Smoke-Test Both Images

**Files:**
- Verify: `Dockerfile.ikuai-line`
- Verify: `Dockerfile.multi-ip`
- Verify: `publish-images.sh`

- [ ] **Step 1: Run static checks**

```bash
go vet ./...
bash -n install-multi-ip.sh publish-images.sh
docker compose -f docker-compose.multi-ip.yml config >/dev/null
```

- [ ] **Step 2: Build aligned images**

```bash
DRY_RUN=1 ./publish-images.sh
```

Expected: both images build, all three APIs respond, and both archives are created.

- [ ] **Step 3: Commit**

```bash
git add cmd/discarder steam_pumper tests docs/superpowers/plans/2026-07-20-source-pool-circuit-breaker.md
git commit -m "feat: quarantine failed download sources"
```

### Task 6: Publish, Deploy, and Observe

**Files:**
- Runtime config: remote `/opt/multi-ip-pumper/data/config.json`

- [ ] **Step 1: Push and publish**

```bash
git push origin main
./publish-images.sh
```

Expected: Docker Hub and GHCR receive `latest`, `ikuai3`, and commit tags for both images, plus release archives and digest metadata.

- [ ] **Step 2: Back up and update the server**

Back up the remote config, install the verified source list, pull the immutable tag or digest, and recreate only `multi-ip-pumper` with the existing four IPs and routing.

- [ ] **Step 3: Observe for at least 10 minutes**

Sample `/api/metrics` and `/api/sources` every 10 seconds. Require a stable 60-second average of at least 1440 Mbps for the 1600 Mbps target, bounded workers, and no periodic all-line zero caused by a failed source.

- [ ] **Step 4: Exercise quarantine**

Temporarily add one unreachable URL. Confirm three failures per logical line trigger quarantine, healthy sources continue downloading, API exposes retry state, and production configuration is restored.

- [ ] **Step 5: Record rollout evidence**

Record deployed and rollback digests, final source list, worker count, 60-second throughput, and source states.
