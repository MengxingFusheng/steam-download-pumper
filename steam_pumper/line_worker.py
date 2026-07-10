from __future__ import annotations

import math
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from .line_config import LineConfig


@dataclass(frozen=True)
class LineSource:
    url: str
    ip: str = ""
    healthy: bool = True
    recent_mbps: float = 0.0
    failures: int = 0


@dataclass(frozen=True)
class LineWorkerSpec:
    worker_id: int
    target: str
    target_ip: str = ""
    fallback_targets: tuple[str, ...] = ()


@dataclass
class LineWorkerState:
    worker_id: int
    target: str = ""
    cycles: int = 0
    status: str = "idle"
    last_error: str = ""
    current_pid: int | None = None
    restarts: int = 0
    connections: int = 0


def next_line_worker_count(cfg: LineConfig, current_workers: int, avg60_mbps: float) -> int:
    min_workers = cfg.connections
    max_workers = cfg.max_connections
    current_workers = max(min_workers, min(current_workers, max_workers))
    if avg60_mbps < cfg.target_mbps * 0.9 and current_workers < max_workers:
        return current_workers + 1
    if cfg.rate_limit_enabled and avg60_mbps > cfg.target_mbps * 1.15 and current_workers > min_workers:
        return current_workers - 1
    return current_workers


def build_line_worker_plan(
    cfg: LineConfig,
    worker_count: int | None = None,
    sources: list[LineSource] | None = None,
) -> list[LineWorkerSpec]:
    total_workers = worker_count or cfg.connections
    if total_workers > cfg.max_connections:
        raise ValueError(f"worker_count must be at most {cfg.max_connections}")
    candidates = [source for source in (sources or source_endpoints_from_urls(cfg.source_pool)) if source.healthy]
    assignments = _target_assignments(total_workers, candidates)
    unique_urls = list(dict.fromkeys(source.url for source in candidates))
    return [
        LineWorkerSpec(
            worker_id=index + 1,
            target=assignments[index].url,
            target_ip=assignments[index].ip,
            fallback_targets=tuple(url for url in unique_urls if url != assignments[index].url),
        )
        for index in range(total_workers)
    ]


def source_endpoints_from_urls(urls: list[str]) -> list[LineSource]:
    return [LineSource(url=url, ip=urlparse(url).hostname or url) for url in urls]


def public_http_command(cfg: LineConfig, spec: LineWorkerSpec, connections: int = 1) -> list[str]:
    return [
        "discarder",
        "--worker-id",
        str(spec.worker_id),
        "--connections",
        str(connections),
        "--max-connections",
        str(cfg.max_connections),
        "--min-session-seconds",
        str(cfg.worker_min_session_seconds),
        "--startup-jitter-seconds",
        str(cfg.startup_stagger_seconds),
        "--restart-jitter-seconds",
        str(cfg.worker_restart_jitter_seconds),
        spec.target,
        *spec.fallback_targets,
    ]


class LineDownloadWorker:
    """Owns one long-lived Go helper without allocating a Python thread."""

    def __init__(
        self,
        cfg: LineConfig,
        spec: LineWorkerSpec,
        state: LineWorkerState,
        log: Callable[[str], None],
        connections: int | None = None,
    ) -> None:
        self.cfg = cfg
        self.spec = spec
        self.state = state
        self.log = log
        self.process: subprocess.Popen[bytes] | None = None
        self.stop_requested = False
        self.next_restart_at = 0.0
        self.consecutive_failures = 0
        self.connection_count = connections or cfg.connections
        self.state.connections = self.connection_count
        self.source_failures: dict[str, int] = {}
        self._output_buffer = ""

    def start(self) -> None:
        if self.stop_requested or (self.process and self.process.poll() is None):
            return
        self.state.target = self.spec.target
        self.state.status = "starting"
        self.state.last_error = ""
        try:
            self.process = subprocess.Popen(
                public_http_command(self.cfg, self.spec, self.connection_count),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            self.process = None
            self._schedule_restart(f"unable to start discarder: {exc}")
            return
        self.state.current_pid = self.process.pid
        if self.process.stdout is not None:
            os.set_blocking(self.process.stdout.fileno(), False)
        self.state.status = "downloading"
        self.log(
            f"engine pid={self.process.pid} connections={self.connection_count} "
            f"primary={self.spec.target} start"
        )

    def set_connection_count(self, target: int) -> None:
        target = max(1, min(target, self.cfg.max_connections))
        process = self.process
        if process and process.poll() is None:
            signal_to_send = signal.SIGUSR1 if target > self.connection_count else signal.SIGUSR2
            for _ in range(abs(target - self.connection_count)):
                try:
                    os.kill(process.pid, signal_to_send)
                except OSError as exc:
                    self.log(f"unable to resize engine: {exc}")
                    return
        self.connection_count = target
        self.state.connections = target
        self.log(f"engine connections={target}")

    def poll(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self.process is not None:
            self._drain_output()
            code = self.process.poll()
            if code is None:
                return
            self.process = None
            self.state.current_pid = None
            if self.stop_requested:
                self.state.status = "stopped"
                return
            self._schedule_restart(f"discarder exited with {code}", now)
        if not self.stop_requested and self.process is None and now >= self.next_restart_at:
            self.start()

    def stop(self) -> None:
        self.stop_requested = True
        process = self.process
        self.process = None
        self.state.current_pid = None
        if process and process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except OSError:
                        pass
        self.state.status = "stopped"

    def _schedule_restart(self, error: str, now: float | None = None) -> None:
        self.consecutive_failures += 1
        self.state.restarts += 1
        self.state.status = "restarting"
        self.state.last_error = error
        delay = min(60.0, float(2 ** min(self.consecutive_failures - 1, 6)))
        self.next_restart_at = (time.monotonic() if now is None else now) + delay
        self.log(f"worker={self.spec.worker_id} error={error} restart_in={delay:.0f}s")

    def _drain_output(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        try:
            chunk = self.process.stdout.read()
        except (BlockingIOError, OSError):
            return
        if not chunk:
            return
        self._output_buffer = (self._output_buffer + chunk.decode("utf-8", errors="replace"))[-16_384:]
        lines = self._output_buffer.split("\n")
        self._output_buffer = lines.pop()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            url = _field_value(line, "url")
            if not url:
                continue
            if "recovered=true" in line:
                self.source_failures[url] = 0
            elif " error=" in line:
                self.source_failures[url] = self.source_failures.get(url, 0) + 1
                self.state.last_error = line[-500:]


def _field_value(line: str, field: str) -> str:
    marker = f"{field}="
    start = line.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = line.find(" ", start)
    return line[start:] if end < 0 else line[start:end]


def _target_assignments(total_workers: int, candidates: list[LineSource]) -> list[LineSource]:
    if not candidates:
        raise ValueError("source_pool must contain at least one healthy source")
    assignments: list[LineSource] = []
    counts_by_ip: dict[str, int] = {}
    unique_ips = {source.ip or source.url for source in candidates}
    cap_per_ip = math.ceil(total_workers / max(1, len(unique_ips))) + 2
    for worker_index in range(total_workers):
        ordered = sorted(
            candidates,
            key=lambda source: (
                counts_by_ip.get(source.ip or source.url, 0) >= cap_per_ip,
                counts_by_ip.get(source.ip or source.url, 0),
                (candidates.index(source) - worker_index) % len(candidates),
            ),
        )
        chosen = ordered[0]
        assignments.append(chosen)
        key = chosen.ip or chosen.url
        counts_by_ip[key] = counts_by_ip.get(key, 0) + 1
    return assignments
