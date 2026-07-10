# Two-Image Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the three divergent runtime paths with exactly two aligned images, `ikuai-line-pumper` and `multi-ip-pumper`, backed by one shared download/controller/web core.

**Architecture:** A topology adapter produces one or more `LogicalLine` values, while a shared controller owns scheduling, one Go engine per line, per-line metrics, autoscaling, sources, API, and web rendering. The iKuai adapter produces one unbound line; the multi-IP adapter applies and binds one IPv4 address per line. Both images are built, tested, tagged, and published together from one commit.

**Tech Stack:** Python 3.13 standard library, Go 1.23, Docker/Compose, macvlan, GitHub Actions, Docker Hub, GHCR.

---

## File Map

The refactor converges runtime behavior into these files:

- `steam_pumper/config.py`: common and topology-specific configuration, environment loading, validation, atomic persistence.
- `steam_pumper/topology.py`: `LogicalLine`, iKuai topology, multi-IP topology, target allocation, and address application.
- `steam_pumper/engine.py`: one non-threaded Python wrapper around each long-lived Go helper process.
- `steam_pumper/metrics.py`: reusable byte counter windows and aggregate metric calculation.
- `steam_pumper/controller.py`: the only scheduler/controller implementation for both images.
- `steam_pumper/web.py`: the only API server and shared console renderer.
- `steam_pumper/ikuai_main.py`: iKuai single-line entry point.
- `steam_pumper/multi_ip_main.py`: multi-IP entry point.
- `cmd/discarder/main.go`: shared IPv4 discard engine and JSON metric protocol.
- `Dockerfile.ikuai-line`, `Dockerfile.multi-ip`: the only supported image definitions.
- `docker-compose.multi-ip.yml`, `install-multi-ip.sh`: the only Compose deployment path.
- `publish-images.sh`: atomic two-image release workflow.

The following duplicate or unsupported files are deleted after their behavior is covered by the shared core: `Dockerfile`, `docker-compose.yml`, `docker-compose.one-to-one.yml`, `deploy.sh`, `install.sh`, `install-one-to-one.sh`, `publish-ikuai-line.sh`, `steam_pumper/line_config.py`, `steam_pumper/line_controller.py`, `steam_pumper/line_main.py`, `steam_pumper/line_metrics.py`, `steam_pumper/line_web.py`, `steam_pumper/line_worker.py`, `steam_pumper/main.py`, `steam_pumper/networking.py`, `steam_pumper/null_download.py`, and `steam_pumper/worker.py`.

### Task 1: Unified Configuration And Topology Contracts

**Files:**
- Modify: `steam_pumper/config.py`
- Create: `steam_pumper/topology.py`
- Create: `tests/test_topologies.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing tests for the two supported configurations**

Add these methods to `unittest.TestCase` classes with these assertions:

```python
from steam_pumper.config import IkuaiLineConfig, MultiIPConfig, load_config


def test_ikuai_config_uses_shared_connection_names(self):
    cfg = IkuaiLineConfig(
        target_mbps=400,
        connections_per_line=8,
        max_connections_per_line=12,
    ).validate()
    assert cfg.topology == "ikuai_line"
    assert cfg.connections_per_line == 8
    assert cfg.max_connections_per_line == 12


def test_hard_cap_is_rejected_for_both_topologies(self):
    for config_type in (IkuaiLineConfig, MultiIPConfig):
        kwargs = {"max_connections_per_line": 13}
        if config_type is MultiIPConfig:
            kwargs.update(line_count=2, lan_ips=["192.168.1.233", "192.168.1.234"])
        with self.assertRaisesRegex(ValueError, "at most 12"):
            config_type(**kwargs).validate()


def test_ikuai_rejects_multi_ip_environment(self):
    with self.assertRaisesRegex(ValueError, "LAN_IPS is not supported"):
        load_config("ikuai_line", "/missing.json", {"LAN_IPS": "192.168.1.233,192.168.1.234"})


def test_multi_ip_requires_one_unique_address_per_line(self):
    with self.assertRaisesRegex(ValueError, "exactly line_count"):
        MultiIPConfig(line_count=3, lan_ips=["192.168.1.233", "192.168.1.234"]).validate()
    with self.assertRaisesRegex(ValueError, "duplicates"):
        MultiIPConfig(line_count=2, lan_ips=["192.168.1.233", "192.168.1.233"]).validate()


def test_legacy_mode_fields_are_rejected(self):
    for topology in ("ikuai_line", "multi_ip"):
        with self.assertRaisesRegex(ValueError, "EGRESS_MODE is not supported"):
            load_config(topology, "/missing.json", {"EGRESS_MODE": "single_ip"})
```

- [ ] **Step 2: Run the configuration tests and verify they fail**

Run: `python3 -m unittest tests.test_config tests.test_topologies -v`

Expected: failures because `IkuaiLineConfig`, `MultiIPConfig`, and the topology-aware `load_config` function do not exist.

- [ ] **Step 3: Replace the two configuration models with one common contract**

Implement these public types and rules in `steam_pumper/config.py`:

```python
MAX_CONNECTIONS_PER_LINE = 12


@dataclass
class CommonConfig:
    target_mbps: int = 400
    connections_per_line: int = 8
    max_connections_per_line: int = 12
    rate_limit_enabled: bool = True
    start_time: str = "00:00"
    end_time: str = "18:00"
    source_pool: list[str] = field(default_factory=default_source_pool)
    loop_pause_seconds: int = 0
    startup_stagger_seconds: float = 2.0
    worker_min_session_seconds: int = 300
    worker_restart_jitter_seconds: float = 3.0
    schedule_poll_seconds: int = 30
    log_level: str = "INFO"
    topology: str = field(init=False)

    def validate_common(self) -> None:
        if self.target_mbps < 1:
            raise ValueError("target_mbps must be at least 1")
        if not 1 <= self.connections_per_line <= MAX_CONNECTIONS_PER_LINE:
            raise ValueError("connections_per_line must be between 1 and 12")
        if not 1 <= self.max_connections_per_line <= MAX_CONNECTIONS_PER_LINE:
            raise ValueError("max_connections_per_line must be at most 12")
        if self.max_connections_per_line < self.connections_per_line:
            raise ValueError("max_connections_per_line must be greater than or equal to connections_per_line")
        validate_schedule(self.start_time, self.end_time)
        self.source_pool = validate_source_pool(self.source_pool)


@dataclass
class IkuaiLineConfig(CommonConfig):
    topology: str = field(init=False, default="ikuai_line")

    def validate(self) -> "IkuaiLineConfig":
        self.validate_common()
        return self


@dataclass
class MultiIPConfig(CommonConfig):
    target_mbps: int = 800
    line_count: int = 2
    lan_ips: list[str] = field(default_factory=lambda: ["192.168.1.233", "192.168.1.234"])
    topology: str = field(init=False, default="multi_ip")

    def validate(self) -> "MultiIPConfig":
        self.validate_common()
        if not 2 <= self.line_count <= 10:
            raise ValueError("line_count must be between 2 and 10")
        self.lan_ips = validate_unique_ipv4(self.lan_ips)
        if len(self.lan_ips) != self.line_count:
            raise ValueError("lan_ips must contain exactly line_count addresses")
        return self
```

Use one `COMMON_ENV_MAP`; add `LINE_COUNT` and `LAN_IPS` only for `multi_ip`. Reject `EGRESS_MODE` and `LAN_IP` for both. Load defaults, then first-boot environment, then persisted JSON so saved web settings win on later starts. Reject unknown persisted keys. Write JSON atomically with `tempfile.NamedTemporaryFile(dir=config_path.parent)` followed by `os.replace()`.

- [ ] **Step 4: Implement topology adapters and deterministic target allocation**

Create `steam_pumper/topology.py` with this interface:

```python
@dataclass(frozen=True)
class LogicalLine:
    line_id: str
    target_mbps: int
    bind_ip: str = ""


def allocate_targets(total_mbps: int, line_count: int) -> list[int]:
    base, remainder = divmod(total_mbps, line_count)
    return [base + (1 if index < remainder else 0) for index in range(line_count)]


class IkuaiLineTopology:
    name = "ikuai_line"

    def lines(self, cfg: IkuaiLineConfig) -> list[LogicalLine]:
        return [LogicalLine(line_id="line-1", target_mbps=cfg.target_mbps)]

    def apply(self, cfg: IkuaiLineConfig, log: Callable[[str], None]) -> None:
        return


class MultiIPTopology:
    name = "multi_ip"

    def lines(self, cfg: MultiIPConfig) -> list[LogicalLine]:
        targets = allocate_targets(cfg.target_mbps, cfg.line_count)
        return [
            LogicalLine(line_id=f"line-{index + 1}", target_mbps=targets[index], bind_ip=ip)
            for index, ip in enumerate(cfg.lan_ips)
        ]

    def apply(self, cfg: MultiIPConfig, log: Callable[[str], None]) -> None:
        apply_ipv4_addresses(cfg.lan_ips, os.environ.get("LAN_INTERFACE", "eth0"), os.environ.get("LAN_PREFIX", "24"), log)
```

Retain the existing structured `ip -4 -o addr` parsing, but make address application transactional from the controller's perspective: validate every address first, add missing addresses, and raise an error naming the failed address before any engine starts.

- [ ] **Step 5: Run tests and commit the configuration boundary**

Run: `python3 -m unittest tests.test_config tests.test_topologies -v`

Expected: all tests pass.

```bash
git add steam_pumper/config.py steam_pumper/topology.py tests/test_config.py tests/test_topologies.py
git commit -m "refactor: define two supported topologies"
```

### Task 2: Go Engine JSON Status And Hard Resource Cap

**Files:**
- Modify: `cmd/discarder/main.go`
- Modify: `cmd/discarder/main_test.go`

- [ ] **Step 1: Write failing Go tests for resource limits and progress events**

Add tests around an extracted `validateOptions` function and a counting writer:

```go
func TestValidateOptionsRejectsMoreThanTwelveConnections(t *testing.T) {
	for _, opts := range []options{
		{connections: 13, maxConnections: 13, urls: []string{"http://example.test/file"}},
		{connections: 1, maxConnections: 13, urls: []string{"http://example.test/file"}},
	} {
		if err := validateOptions(&opts); err == nil || !strings.Contains(err.Error(), "12") {
			t.Fatalf("expected hard-cap error, got %v", err)
		}
	}
}

func TestCountingWriterReportsBytesBeforeRequestCompletes(t *testing.T) {
	var total atomic.Int64
	writer := countingWriter{total: &total}
	n, err := writer.Write(make([]byte, 4096))
	if err != nil || n != 4096 || total.Load() != 4096 {
		t.Fatalf("n=%d total=%d err=%v", n, total.Load(), err)
	}
}

func TestStatusEventIsNewlineDelimitedJSON(t *testing.T) {
	var output bytes.Buffer
	err := writeStatus(&output, statusEvent{Type: "status", LineID: "line-1", BindIP: "192.168.1.233", Bytes: 42, Connections: 2})
	if err != nil || !strings.HasSuffix(output.String(), "\n") {
		t.Fatalf("output=%q err=%v", output.String(), err)
	}
	var decoded statusEvent
	if err := json.Unmarshal(bytes.TrimSpace(output.Bytes()), &decoded); err != nil || decoded.Bytes != 42 {
		t.Fatalf("decoded=%+v err=%v", decoded, err)
	}
}
```

- [ ] **Step 2: Run Go tests and verify they fail**

Run: `go test ./cmd/discarder`

Expected: compile failures for `validateOptions`, `countingWriter`, `statusEvent`, and `writeStatus`.

- [ ] **Step 3: Add the shared status protocol**

Add `--line-id` and `--status-interval-seconds`. Validate `connections` and `max-connections` in the inclusive range 1-12. Track bytes continuously with `atomic.Int64`, active connections with `atomic.Int32`, and emit records using:

```go
type statusEvent struct {
	Type        string `json:"type"`
	LineID      string `json:"line_id"`
	BindIP      string `json:"bind_ip,omitempty"`
	Bytes       int64  `json:"bytes"`
	Connections int32  `json:"connections"`
	URL         string `json:"url,omitempty"`
	Error       string `json:"error,omitempty"`
	Recovered   bool   `json:"recovered,omitempty"`
}

type countingWriter struct{ total *atomic.Int64 }

func (writer countingWriter) Write(buffer []byte) (int, error) {
	writer.total.Add(int64(len(buffer)))
	return len(buffer), nil
}

func writeStatus(output io.Writer, event statusEvent) error {
	return json.NewEncoder(output).Encode(event)
}
```

Use `io.MultiWriter(io.Discard, countingWriter{total: total})` as the `io.CopyBuffer` destination so bytes are visible during long responses. Emit periodic `type=status` records to stdout and source failure/recovery records to the same JSON stream. Keep human startup/fatal messages on stderr and never emit ANSI sequences.

- [ ] **Step 4: Verify Go behavior, formatting, and races**

Run:

```bash
gofmt -w cmd/discarder/main.go cmd/discarder/main_test.go
go test -race ./cmd/discarder
go vet ./cmd/discarder
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit the data-plane protocol**

```bash
git add cmd/discarder/main.go cmd/discarder/main_test.go
git commit -m "feat: report per-line engine metrics"
```

### Task 3: Shared Engine Wrapper And Metrics

**Files:**
- Create: `steam_pumper/engine.py`
- Modify: `steam_pumper/metrics.py`
- Create: `tests/test_engine.py`
- Modify: `tests/test_metrics.py`

- [ ] **Step 1: Write failing tests for one process per line**

```python
def test_engine_command_binds_only_when_line_has_an_ip(self):
    cfg = IkuaiLineConfig(connections_per_line=4, max_connections_per_line=12)
    unbound = build_engine_command(cfg, LogicalLine("line-1", 400), ["http://a.test/file"])
    bound = build_engine_command(cfg, LogicalLine("line-1", 400, "192.168.1.233"), ["http://a.test/file"])
    assert "--bind-ip" not in unbound
    assert bound[bound.index("--bind-ip") + 1] == "192.168.1.233"
    assert unbound[unbound.index("--connections") + 1] == "4"
    assert unbound[unbound.index("--max-connections") + 1] == "12"


def test_engine_parses_status_without_a_reader_thread(self):
    config = IkuaiLineConfig(connections_per_line=4)
    sources = ["http://a.test/file"]
    engine = EngineProcess(config, LogicalLine("line-1", 400), sources, lambda message: None)
    engine._consume_line('{"type":"status","line_id":"line-1","bytes":1048576,"connections":4}')
    assert engine.state.total_bytes == 1048576
    assert engine.state.connections == 4


def test_engine_hot_scales_without_restarting_pid(self):
    config = IkuaiLineConfig(connections_per_line=4)
    engine = EngineProcess(config, LogicalLine("line-1", 400), ["http://a.test/file"], lambda message: None)
    engine.process = Mock(pid=321)
    engine.process.poll.return_value = None
    engine.state.connections = 4
    with patch("steam_pumper.engine.os.kill") as kill:
        engine.set_connections(6)
    assert engine.process.pid == 321
    assert kill.call_count == 2
```

- [ ] **Step 2: Run focused tests and verify they fail**

Run: `python3 -m unittest tests.test_engine tests.test_metrics -v`

Expected: failures because `steam_pumper.engine` and per-line counter support do not exist.

- [ ] **Step 3: Build the shared non-threaded engine wrapper**

Move the proven process lifecycle from `line_worker.py` into `engine.py`. Use these public records:

```python
@dataclass
class EngineState:
    line_id: str
    bind_ip: str = ""
    status: str = "idle"
    pid: int | None = None
    connections: int = 0
    total_bytes: int = 0
    has_metrics: bool = False
    current_source: str = ""
    source_failures: dict[str, int] = field(default_factory=dict)
    last_error: str = ""
    restarts: int = 0


class EngineProcess:
    def start(self) -> None:
        if self.stop_requested or (self.process and self.process.poll() is None):
            return
        self.process = subprocess.Popen(
            build_engine_command(self.cfg, self.line, self.sources),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.state.pid = self.process.pid
        self.state.status = "downloading"
        os.set_blocking(self.process.stdout.fileno(), False)

    def poll(self, now: float | None = None) -> None:
        self._drain_output()
        if self.process and self.process.poll() is not None and not self.stop_requested:
            self._schedule_restart(time.monotonic() if now is None else now)

    def set_connections(self, target: int) -> None:
        target = max(1, min(target, self.cfg.max_connections_per_line, 12))
        signal_to_send = signal.SIGUSR1 if target > self.state.connections else signal.SIGUSR2
        for _index in range(abs(target - self.state.connections)):
            os.kill(self.process.pid, signal_to_send)
        self.state.connections = target

    def stop(self) -> None:
        self.stop_requested = True
        if self.process and self.process.poll() is None:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            self.process.wait(timeout=5)
        self.state.status = "stopped"
```

Complete `_schedule_restart()` with the existing bounded 1-60 second exponential delay and make `stop()` escalate to `SIGKILL` after its five-second timeout. `_consume_line()` parses JSON, ignores malformed lines after recording a bounded error, sets `has_metrics` on the first status event, and updates only events matching the engine's `line_id`. `set_connections()` clamps to 1-12 and sends one `SIGUSR1` or `SIGUSR2` per connection delta without replacing the process.

- [ ] **Step 4: Consolidate metric tracking**

Keep one reusable `ThroughputTracker` with `record(timestamp, total_bytes, day=None)`, `average_mbps(seconds)`, and `sample_span_seconds()`. Add:

```python
def next_connection_count(base: int, maximum: int, current: int, avg60_mbps: float, target_mbps: int, reduce_above_target: bool) -> int:
    current = max(base, min(current, maximum, 12))
    if avg60_mbps < target_mbps * 0.9 and current < maximum:
        return current + 1
    if reduce_above_target and avg60_mbps > target_mbps * 1.15 and current > base:
        return current - 1
    return current
```

Aggregate metrics sum current, 10-second, 60-second, and daily values from line trackers. Keep `theoretical_window_bytes(target_mbps, start_time, end_time)` independent of a topology-specific config type.

- [ ] **Step 5: Run tests and commit the shared engine layer**

Run: `python3 -m unittest tests.test_engine tests.test_metrics -v`

Expected: all tests pass.

```bash
git add steam_pumper/engine.py steam_pumper/metrics.py tests/test_engine.py tests/test_metrics.py
git commit -m "refactor: share engine and metrics across images"
```

### Task 4: One Controller For Both Topologies

**Files:**
- Rewrite: `steam_pumper/controller.py`
- Rewrite: `tests/test_controller.py`
- Create: `tests/test_alignment.py`

- [ ] **Step 1: Write failing controller alignment tests**

```python
def test_same_controller_builds_both_topologies(self):
    cases = [
        ("ikuai_line", {}, 1),
        ("multi_ip", {"LINE_COUNT": "2", "LAN_IPS": "192.168.1.233,192.168.1.234"}, 2),
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        for topology_name, env, expected_lines in cases:
            with self.subTest(topology=topology_name):
                path = Path(tmpdir) / f"{topology_name}.json"
                controller = PumperController(topology_name, path, env=env)
                assert len(controller.lines) == expected_lines
                assert len(controller.line_runtimes) == expected_lines


def test_multi_ip_scales_slow_line_without_scaling_fast_line(self):
    env = {"LINE_COUNT": "2", "LAN_IPS": "192.168.1.233,192.168.1.234"}
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = PumperController("multi_ip", Path(tmpdir) / "config.json", env=env)
        slow = controller.line_runtimes["line-1"]
        fast = controller.line_runtimes["line-2"]
        slow.engine.state.has_metrics = True
        fast.engine.state.has_metrics = True
        slow.tracker.record(0, 0)
        slow.tracker.record(10, 10_000_000)
        fast.tracker.record(0, 0)
        fast.tracker.record(10, 500_000_000)
        controller._scale_lines(now=20)
        assert slow.desired_connections == 9
        assert fast.desired_connections == 8


def test_metrics_schema_is_identical_for_both_topologies(self):
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        ikuai = PumperController("ikuai_line", root / "ikuai.json").metrics()
        multi = PumperController("multi_ip", root / "multi.json").metrics()
        assert set(ikuai) == set(multi)
        assert set(ikuai["lines"][0]) == set(multi["lines"][0])
```

- [ ] **Step 2: Run controller tests and verify they fail**

Run: `python3 -m unittest tests.test_controller tests.test_alignment -v`

Expected: failures because the existing controller only supports the legacy configuration and Python thread workers.

- [ ] **Step 3: Rewrite the controller around logical lines**

Use one runtime record and one constructor:

```python
@dataclass
class LineRuntime:
    spec: LogicalLine
    engine: EngineProcess
    tracker: ThroughputTracker = field(default_factory=ThroughputTracker)
    desired_connections: int = 0


class PumperController:
    def __init__(self, topology_name: str, config_path: str | Path, env: Mapping[str, str] | None = None):
        self.topology = topology_for(topology_name)
        self.cfg = load_config(topology_name, config_path, env)
        self.lines = self.topology.lines(self.cfg)
        self.line_runtimes: dict[str, LineRuntime] = self._build_runtimes()
```

Resolve sources once through IPv4 DNS. On start, apply topology addresses before constructing engines, then create exactly one `EngineProcess` per logical line. On each one-second poll, call every engine's `poll()`, feed its cumulative byte count into its tracker, and independently calculate the next connection count every 10 seconds. Keep only scheduler and metrics threads; no Python thread is created per connection or line.

Also maintain one interface `ThroughputTracker` sampled from `/sys/class/net/eth0/statistics/rx_bytes`. When every engine has emitted metrics, aggregate values come from line trackers. Before helper metrics arrive, aggregate values come from the interface tracker. Per-line metric objects expose `metrics_available=false` and numeric values of zero during that fallback; the controller does not invent an equal traffic split. Independent autoscaling waits for line metrics, except the single-line topology may safely use the interface tracker for its only line.

`update_config()` validates a complete candidate before writing. Restart all engines for source or topology changes. Use hot scaling for base/maximum/target-only changes when line identities remain unchanged. Ensure `downloads_starting` and `reconfiguring` prevent duplicate starts.

- [ ] **Step 4: Return aggregate and per-line metrics from one schema**

Return this stable shape from `metrics()`:

```python
{
    "target_mbps": cfg.target_mbps,
    "current_mbps": sum(line.current_mbps for line in lines),
    "avg10_mbps": sum(line.avg10_mbps for line in lines),
    "avg60_mbps": sum(line.avg60_mbps for line in lines),
    "target_percent": aggregate_percent,
    "today_bytes": sum(line.today_bytes for line in lines),
    "theoretical_window_bytes": theoretical,
    "minimum_accept_bytes": int(theoretical * 0.8),
    "daily_target_percent": daily_percent,
    "capacity_warning": any(line.capacity_warning for line in lines),
    "lines": [line_metric_dict(runtime) for runtime in line_runtimes.values()],
}
```

Always include `bind_ip` and `metrics_available` in each line object, using an empty bind IP for iKuai. This keeps schemas identical.

- [ ] **Step 5: Run tests and commit the shared controller**

Run: `python3 -m unittest tests.test_controller tests.test_alignment -v`

Expected: all tests pass.

```bash
git add steam_pumper/controller.py tests/test_controller.py tests/test_alignment.py
git commit -m "refactor: run both topologies through one controller"
```

### Task 5: Shared Web Console, API, And Explicit Entry Points

**Files:**
- Rewrite: `steam_pumper/web.py`
- Create: `steam_pumper/ikuai_main.py`
- Create: `steam_pumper/multi_ip_main.py`
- Rewrite: `tests/test_web.py`
- Create: `tests/test_entrypoints.py`

- [ ] **Step 1: Write failing shared-renderer and API tests**

```python
def test_ikuai_console_has_no_ip_or_line_count_fields(self):
    html = render_html("ikuai_line")
    assert 'name="target_mbps"' in html
    assert 'name="connections_per_line"' in html
    assert 'name="line_count"' not in html
    assert 'name="lan_ips"' not in html
    assert "EGRESS_MODE" not in html


def test_multi_ip_console_adds_only_topology_fields(self):
    html = render_html("multi_ip")
    assert 'name="line_count"' in html
    assert 'name="lan_ips"' in html
    assert 'name="egress_mode"' not in html


def test_user_content_is_rendered_with_text_content_not_inner_html(self):
    html = render_html("multi_ip")
    assert "escapeHtml" in html
    assert "status.logs.join" in html


def test_entrypoints_select_explicit_topologies(self):
    assert IKUAI_TOPOLOGY == "ikuai_line"
    assert MULTI_IP_TOPOLOGY == "multi_ip"
```

- [ ] **Step 2: Run web tests and verify they fail**

Run: `python3 -m unittest tests.test_web tests.test_entrypoints -v`

Expected: failures because there is no shared conditional renderer or explicit pair of entry points.

- [ ] **Step 3: Consolidate the HTTP server and console**

Keep one `BaseHTTPRequestHandler` implementation and one `render_html(topology_name)` function. Build topology fields server-side:

```python
def topology_fields(topology_name: str) -> str:
    if topology_name == "ikuai_line":
        return ""
    if topology_name == "multi_ip":
        return """
        <label>线路数量 <input name="line_count" type="number" min="2" max="10"></label>
        <label>线路 IPv4 地址 <textarea name="lan_ips"></textarea></label>
        """
    raise ValueError(f"unsupported topology: {topology_name}")
```

Use the existing chart and controls, rename connection fields to the shared names, add a per-line table populated from `metrics.lines`, and remove the iKuai WAN monitor and legacy worker table. Use `textContent` for logs and an `escapeHtml()` helper for every value inserted into table markup. Catch refresh/save errors and display them in the existing error pill.

- [ ] **Step 4: Add two thin entry points**

Both entry points call one helper:

```python
def run_application(topology_name: str) -> int:
    config_path = os.environ.get("CONFIG_PATH", "/data/config.json")
    controller = PumperController(topology_name, config_path)
    configure_plain_logging(controller.cfg.log_level)
    install_shutdown_handlers(controller)
    controller.start_scheduler()
    run_web(controller, "0.0.0.0", int(os.environ.get("WEB_PORT", "80")), topology_name)
    return 0
```

`ikuai_main.py` defines `TOPOLOGY = "ikuai_line"`; `multi_ip_main.py` defines `TOPOLOGY = "multi_ip"`. Both call `run_application(TOPOLOGY)` and set no color-related environment behavior in Python.

- [ ] **Step 5: Run tests and commit the common UI/API**

Run: `python3 -m unittest tests.test_web tests.test_entrypoints tests.test_alignment -v`

Expected: all tests pass.

```bash
git add steam_pumper/web.py steam_pumper/ikuai_main.py steam_pumper/multi_ip_main.py tests/test_web.py tests/test_entrypoints.py
git commit -m "refactor: align web and API across images"
```

### Task 6: Package Exactly Two Images

**Files:**
- Modify: `Dockerfile.ikuai-line`
- Create: `Dockerfile.multi-ip`
- Create: `docker-compose.multi-ip.yml`
- Create: `install-multi-ip.sh`
- Create: `tests/test_images.py`
- Delete: `Dockerfile`
- Delete: `docker-compose.yml`
- Delete: `docker-compose.one-to-one.yml`
- Delete: `deploy.sh`
- Delete: `install.sh`
- Delete: `install-one-to-one.sh`
- Delete: `tests/test_one_to_one_image.py`
- Delete: `tests/test_install_script.py`

- [ ] **Step 1: Write failing artifact tests**

```python
def test_repository_has_exactly_two_dockerfiles(self):
    assert sorted(path.name for path in ROOT.glob("Dockerfile*")) == ["Dockerfile.ikuai-line", "Dockerfile.multi-ip"]


def test_supported_images_have_explicit_entrypoints(self):
    ikuai = read("Dockerfile.ikuai-line")
    multi = read("Dockerfile.multi-ip")
    assert 'CMD ["python3", "-m", "steam_pumper.ikuai_main"]' in ikuai
    assert 'CMD ["python3", "-m", "steam_pumper.multi_ip_main"]' in multi
    assert "iproute2" not in ikuai
    assert "iproute2" in multi


def test_multi_ip_compose_uses_only_new_image_and_fields(self):
    compose = read("docker-compose.multi-ip.yml")
    assert "traveler1314/multi-ip-pumper:latest" in compose
    assert "NET_ADMIN" in compose
    assert "LAN_IPS" in compose
    assert "CONTAINER_IP" in compose
    assert "EGRESS_MODE" not in compose
    assert "steam-download-pumper" not in compose
```

- [ ] **Step 2: Run artifact tests and verify they fail**

Run: `python3 -m unittest tests.test_images -v`

Expected: failures because legacy artifacts remain and `Dockerfile.multi-ip` does not exist.

- [ ] **Step 3: Build the two minimal Dockerfiles**

Both Dockerfiles use the same Go builder stage, copy the same shared Python files, expose port 80, set `PYTHONDONTWRITEBYTECODE=1`, `PYTHON_COLORS=0`, `NO_COLOR=1`, `TERM=dumb`, and use read-only-compatible `/data`, `/tmp`, and `/run` paths. Only `Dockerfile.multi-ip` installs `iproute2`. Neither image contains SteamCMD, `trickle`, a Docker client, or a shell entrypoint.

Set common defaults using `CONNECTIONS_PER_LINE=8` and `MAX_CONNECTIONS_PER_LINE=12`. The iKuai target defaults to 400 Mbps. The multi-IP target defaults to 800 Mbps with two addresses.

- [ ] **Step 4: Add the multi-IP Compose and installer**

Compose uses:

```yaml
services:
  multi-ip-pumper:
    image: ${PUMPER_IMAGE:-traveler1314/multi-ip-pumper:latest}
    restart: unless-stopped
    cap_add: [NET_ADMIN]
    read_only: true
    networks:
      pumper_lan:
        ipv4_address: ${CONTAINER_IP:-192.168.1.233}
    environment:
      LINE_COUNT: ${LINE_COUNT:-2}
      LAN_IPS: ${LAN_IPS:-192.168.1.233,192.168.1.234}
      TARGET_MBPS: ${TARGET_MBPS:-800}
      CONNECTIONS_PER_LINE: ${CONNECTIONS_PER_LINE:-8}
      MAX_CONNECTIONS_PER_LINE: ${MAX_CONNECTIONS_PER_LINE:-12}
```

The installer validates `LINE_COUNT=2..10`, parses exactly that many unique IPv4 values, derives `CONTAINER_IP` from the first, writes `.env`, pulls the image, and runs `docker compose -f docker-compose.multi-ip.yml up -d`. It never asks for an egress mode.

- [ ] **Step 5: Validate packaging and commit**

Run:

```bash
bash -n install-multi-ip.sh
docker compose -f docker-compose.multi-ip.yml config >/dev/null
python3 -m unittest tests.test_images -v
```

Expected: all commands exit 0.

```bash
git add -A
git commit -m "build: package exactly two aligned images"
```

### Task 7: Remove Duplicate Runtime Paths And Rewrite Documentation

**Files:**
- Delete: `steam_pumper/line_config.py`
- Delete: `steam_pumper/line_controller.py`
- Delete: `steam_pumper/line_main.py`
- Delete: `steam_pumper/line_metrics.py`
- Delete: `steam_pumper/line_web.py`
- Delete: `steam_pumper/line_worker.py`
- Delete: `steam_pumper/main.py`
- Delete: `steam_pumper/networking.py`
- Delete: `steam_pumper/null_download.py`
- Delete: `steam_pumper/worker.py`
- Delete: `steam_pumper/ikuai.py`
- Rewrite: `README.md`
- Rewrite: `docs/design.md`
- Create: `.github/workflows/test.yml`
- Modify: tests that import deleted modules

- [ ] **Step 1: Add a failing legacy-surface scan**

Create a test that scans supported runtime files, Dockerfiles, Compose, scripts, README, and current design docs:

```python
FORBIDDEN = (
    "single_ip",
    "EGRESS_MODE",
    "one-to-one",
    "steam-download-pumper:one-to-one",
    "新建连接数分流",
)


def test_supported_surface_has_no_removed_mode_or_image(self):
    paths = [*ROOT.glob("Dockerfile*"), *ROOT.glob("*.yml"), *ROOT.glob("*.sh"), ROOT / "README.md", ROOT / "docs/design.md"]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths if path.exists())
    for forbidden in FORBIDDEN:
        assert forbidden not in combined
```

- [ ] **Step 2: Run the scan and verify it fails**

Run: `python3 -m unittest tests.test_alignment -v`

Expected: failure on legacy files and documentation.

- [ ] **Step 3: Delete duplicate modules and update imports**

Delete the listed modules only after all active imports point to `config`, `topology`, `engine`, `metrics`, `controller`, and `web`. Remove the old iKuai WAN API client because neither supported topology reads WAN state from iKuai. Confirm with:

```bash
rg -n "line_(config|controller|metrics|web|worker)|from \.worker|from \.networking|from \.ikuai" steam_pumper tests
```

Expected: no matches.

- [ ] **Step 4: Rewrite user documentation around only two choices**

README starts with a two-row deployment table:

```markdown
| Version | Image | Network model |
|---|---|---|
| iKuai single line | `traveler1314/ikuai-line-pumper:latest` | One container IP mapped manually to one WAN |
| Multi IP | `traveler1314/multi-ip-pumper:latest` | One container, one source IP per WAN |
```

Document direct iKuai plugin installation for the first image and `install-multi-ip.sh` for the second. State that all common fixes ship in both images under the same commit tag. Remove every old tag, single-IP/multiple-WAN explanation, Steam reference, and obsolete environment variable.

- [ ] **Step 5: Add a two-image CI matrix**

Create `.github/workflows/test.yml` with Python tests, Go race tests, shell syntax checks, Compose validation, and:

```yaml
strategy:
  matrix:
    include:
      - name: ikuai-line
        dockerfile: Dockerfile.ikuai-line
      - name: multi-ip
        dockerfile: Dockerfile.multi-ip
steps:
  - uses: actions/checkout@v4
  - uses: docker/setup-buildx-action@v3
  - uses: docker/build-push-action@v6
    with:
      context: .
      file: ${{ matrix.dockerfile }}
      load: true
      tags: local/${{ matrix.name }}:${{ github.sha }}
```

Add a smoke command for each loaded image that runs its `--help` or imports its entry point without starting downloads.

- [ ] **Step 6: Run the full source suite and commit cleanup**

Run:

```bash
python3 -m unittest discover -s tests -v
go test -race ./...
git diff --check
```

Expected: all tests pass and `git diff --check` prints nothing.

```bash
git add -A
git commit -m "docs: support only two aligned pumper images"
```

### Task 8: Atomic Two-Image Publication

**Files:**
- Create: `publish-images.sh`
- Create: `tests/test_publish.py`

- [ ] **Step 1: Write failing release-script tests**

```python
def test_publish_script_contains_both_registry_names_and_dockerfiles(self):
    script = read("publish-images.sh")
    for value in (
        "traveler1314/ikuai-line-pumper",
        "traveler1314/multi-ip-pumper",
        "ghcr.io/mengxingfusheng/ikuai-line-pumper",
        "ghcr.io/mengxingfusheng/multi-ip-pumper",
        "Dockerfile.ikuai-line",
        "Dockerfile.multi-ip",
    ):
        assert value in script


def test_publish_script_tests_before_first_push(self):
    script = read("publish-images.sh")
    assert script.index("python3 -m unittest discover") < script.index("docker push")
    assert script.index("go test") < script.index("docker push")
```

- [ ] **Step 2: Run publication tests and verify they fail**

Run: `python3 -m unittest tests.test_publish -v`

Expected: failure because `publish-images.sh` does not exist.

- [ ] **Step 3: Implement build-all-before-push publication**

The script uses `set -euo pipefail`, calculates one `SHORT_SHA`, runs all Python/Go/shell/Compose tests, builds both images locally, and smoke tests both. Only after every gate succeeds does it tag each local image as `latest`, `ikuai3`, and `${SHORT_SHA}` for Docker Hub and GHCR, then push all tags.

Generate:

```text
dist/ikuai-line-pumper-${SHORT_SHA}.docker.tar.gz
dist/multi-ip-pumper-${SHORT_SHA}.docker.tar.gz
dist/image-digests-${SHORT_SHA}.txt
```

The digest file contains the immutable digest for both Docker Hub images and both GHCR images. Create one GitHub Release tag `pumper-${SHORT_SHA}` and attach all three files.

- [ ] **Step 4: Validate the release script without pushing**

Provide `DRY_RUN=1` support that performs tests, builds, smoke tests, tagging, and archive creation but skips logins, pushes, and GitHub Release creation.

Run:

```bash
bash -n publish-images.sh
DRY_RUN=1 ./publish-images.sh
python3 -m unittest tests.test_publish -v
```

Expected: both images build and smoke-test; no registry push occurs.

- [ ] **Step 5: Commit the synchronized release path**

```bash
git add publish-images.sh tests/test_publish.py
git commit -m "build: publish aligned images atomically"
```

### Task 9: Final Verification And Release

**Files:**
- Verify all tracked files
- Generate ignored files under: `dist/`

- [ ] **Step 1: Run complete verification from a clean working tree**

Run:

```bash
python3 -m unittest discover -s tests -v
go test -race ./...
go vet ./...
bash -n install-multi-ip.sh publish-images.sh
docker compose -f docker-compose.multi-ip.yml config >/dev/null
git diff --check
git status --short
```

Expected: tests and validators exit 0; `git diff --check` and `git status --short` print nothing.

- [ ] **Step 2: Build and inspect both release images**

Run:

```bash
docker build -f Dockerfile.ikuai-line -t verify/ikuai-line-pumper:local .
docker build -f Dockerfile.multi-ip -t verify/multi-ip-pumper:local .
docker run --rm --entrypoint sh verify/ikuai-line-pumper:local -c 'test ! -e /sbin/ip && ! command -v steamcmd && command -v discarder'
docker run --rm --entrypoint sh verify/multi-ip-pumper:local -c 'command -v ip && ! command -v steamcmd && command -v discarder'
```

Expected: both image inspections exit 0.

- [ ] **Step 3: Smoke-test HTTP and resource limits**

Start each image against a local test source. For iKuai use `--read-only --tmpfs /tmp --tmpfs /run -p 18080:80`; for multi-IP use an isolated Docker network with `--cap-add NET_ADMIN -p 18081:80`. Verify `/api/status`, `/api/metrics`, `/api/sources`, stop, start, and config save. Inspect processes and assert no line reports more than 12 connections.

- [ ] **Step 4: Push source commits**

Run: `git push origin main`

Expected: GitHub reports `main` updated to the final verified commit.

- [ ] **Step 5: Publish both images in one release**

Run: `./publish-images.sh`

Expected: Docker Hub and GHCR each contain `latest`, `ikuai3`, and the same short SHA for both image names; GitHub contains release `pumper-<short-sha>` with both archives and the digest manifest.

- [ ] **Step 6: Re-pull immutable tags and verify registry artifacts**

Run:

```bash
SHORT_SHA="$(git rev-parse --short HEAD)"
docker pull "traveler1314/ikuai-line-pumper:${SHORT_SHA}"
docker pull "traveler1314/multi-ip-pumper:${SHORT_SHA}"
docker image inspect "traveler1314/ikuai-line-pumper:${SHORT_SHA}" --format '{{index .RepoDigests 0}}'
docker image inspect "traveler1314/multi-ip-pumper:${SHORT_SHA}" --format '{{index .RepoDigests 0}}'
```

Expected: both pulls succeed and both commands print immutable Docker Hub digests recorded in the release manifest.
