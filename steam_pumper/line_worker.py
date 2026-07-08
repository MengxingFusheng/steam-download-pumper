from __future__ import annotations

import math
import os
import random
import signal
import subprocess
import threading
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


@dataclass
class LineWorkerState:
    worker_id: int
    target: str = ""
    cycles: int = 0
    status: str = "idle"
    last_error: str = ""
    current_pid: int | None = None


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
    assignments = _target_assignments(cfg, total_workers, sources)
    return [
        LineWorkerSpec(worker_id=index + 1, target=assignments[index].url, target_ip=assignments[index].ip)
        for index in range(total_workers)
    ]


def source_endpoints_from_urls(urls: list[str]) -> list[LineSource]:
    endpoints: list[LineSource] = []
    for url in urls:
        endpoints.append(LineSource(url=url, ip=urlparse(url).hostname or url))
    return endpoints


def public_http_command(cfg: LineConfig, url: str, worker_id: int) -> list[str]:
    return [
        "discarder",
        "--worker-id",
        str(worker_id),
        "--min-session-seconds",
        str(cfg.worker_min_session_seconds),
        "--restart-jitter-seconds",
        str(cfg.worker_restart_jitter_seconds),
        url,
    ]


class LineDownloadWorker(threading.Thread):
    def __init__(
        self,
        cfg: LineConfig,
        spec: LineWorkerSpec,
        state: LineWorkerState,
        stop_event: threading.Event,
        log: Callable[[str], None],
    ) -> None:
        super().__init__(name=f"line-worker-{spec.worker_id}", daemon=True)
        self.cfg = cfg
        self.spec = spec
        self.state = state
        self.stop_event = stop_event
        self.log = log
        self.process: subprocess.Popen[bytes] | None = None

    def run(self) -> None:
        if self.cfg.startup_stagger_seconds:
            delay = random.random() * self.cfg.startup_stagger_seconds
            if self.stop_event.wait(delay):
                self.state.status = "stopped"
                return
        while not self.stop_event.is_set():
            self.state.target = self.spec.target
            self.state.status = "downloading"
            self.state.last_error = ""
            command = public_http_command(self.cfg, self.spec.target, self.spec.worker_id)
            self.log(f"worker={self.spec.worker_id} target={self.spec.target} start")
            try:
                self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
                self.state.current_pid = self.process.pid
                while self.process.poll() is None:
                    if self.stop_event.wait(1):
                        self._terminate_process()
                        break
                code = self.process.poll()
                if code == 0:
                    self.state.cycles += 1
                    self.state.status = "completed"
                    self.log(f"worker={self.spec.worker_id} target={self.spec.target} completed")
                elif not self.stop_event.is_set():
                    self.state.status = "error"
                    self.state.last_error = f"discarder exited with {code}"
                    self.log(f"worker={self.spec.worker_id} target={self.spec.target} failed exit={code}")
            except Exception as exc:
                self.state.status = "error"
                self.state.last_error = str(exc)
                self.log(f"worker={self.spec.worker_id} error={exc}")
            finally:
                self.state.current_pid = None
                self.process = None
            if self.stop_event.wait(self.cfg.loop_pause_seconds):
                break
        self.state.status = "stopped"

    def stop(self) -> None:
        self.stop_event.set()
        self._terminate_process()

    def _terminate_process(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                self.process.wait(timeout=10)
            except Exception:
                if self.process and self.process.poll() is None:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)


def _target_assignments(
    cfg: LineConfig,
    total_workers: int,
    sources: list[LineSource] | None = None,
) -> list[LineSource]:
    candidates = [source for source in (sources or source_endpoints_from_urls(cfg.source_pool)) if source.healthy]
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
