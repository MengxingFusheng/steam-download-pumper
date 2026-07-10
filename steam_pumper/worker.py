from __future__ import annotations

import os
import math
import random
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from .config import PumperConfig


@dataclass(frozen=True)
class SourceEndpoint:
    url: str
    ip: str = ""
    healthy: bool = True
    recent_mbps: float = 0.0
    failures: int = 0


@dataclass(frozen=True)
class WorkerSpec:
    worker_id: int
    line_index: int
    rate_limit_kbps: int | None
    target: str = ""
    target_ip: str = ""
    source_ip: str = ""


@dataclass
class WorkerState:
    worker_id: int
    line_index: int
    target: str = ""
    source_ip: str = ""
    cycles: int = 0
    status: str = "idle"
    last_error: str = ""
    current_pid: int | None = None


def build_worker_plan(
    cfg: PumperConfig,
    worker_count: int | None = None,
    sources: list[SourceEndpoint] | None = None,
) -> list[WorkerSpec]:
    total_workers = worker_count or cfg.line_count * cfg.connections_per_line
    max_workers = cfg.line_count * cfg.max_connections_per_line
    if total_workers > max_workers:
        raise ValueError(f"worker_count must be at most {max_workers}")
    per_worker_kbps = None
    if cfg.rate_limit_enabled:
        per_worker_kbps = max(1, int((cfg.rate_limit_mbps * 1000) / total_workers))
    assignments = _target_assignments(cfg, total_workers, sources)
    plan: list[WorkerSpec] = []
    for i in range(total_workers):
        line_index = (i % cfg.line_count) + 1
        source_ip = cfg.lan_ips[line_index - 1] if cfg.egress_mode == "multi_ip" else ""
        plan.append(
            WorkerSpec(
                worker_id=i + 1,
                line_index=line_index,
                rate_limit_kbps=per_worker_kbps,
                target=assignments[i].url,
                target_ip=assignments[i].ip,
                source_ip=source_ip,
            )
        )
    return plan


def _target_assignments(
    cfg: PumperConfig,
    total_workers: int,
    sources: list[SourceEndpoint] | None = None,
) -> list[SourceEndpoint]:
    candidates = [source for source in (sources or source_endpoints_from_urls(cfg.source_pool)) if source.healthy]
    if not candidates:
        raise ValueError("source_pool must contain at least one healthy source")
    assignments: list[SourceEndpoint] = []
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


def source_endpoints_from_urls(urls: list[str]) -> list[SourceEndpoint]:
    endpoints: list[SourceEndpoint] = []
    for url in urls:
        host = urlparse(url).hostname or url
        endpoints.append(SourceEndpoint(url=url, ip=host))
    return endpoints


def public_http_command(cfg: PumperConfig, url: str, worker_id: int, source_ip: str = "") -> list[str]:
    command = [
        "discarder",
        "--worker-id",
        str(worker_id),
        "--min-session-seconds",
        str(cfg.worker_min_session_seconds),
        "--restart-jitter-seconds",
        str(cfg.worker_restart_jitter_seconds),
    ]
    if source_ip:
        command.extend(["--bind-ip", source_ip])
    command.append(url)
    return command


def build_download_command(cfg: PumperConfig, target: str, worker_id: int, source_ip: str = "") -> list[str]:
    return public_http_command(cfg, target, worker_id, source_ip)


class DownloadWorker(threading.Thread):
    def __init__(
        self,
        cfg: PumperConfig,
        spec: WorkerSpec,
        state: WorkerState,
        stop_event: threading.Event,
        log: Callable[[str], None],
    ) -> None:
        super().__init__(name=f"download-worker-{spec.worker_id}", daemon=True)
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
            target = self.spec.target
            self.state.target = target
            self.state.source_ip = self.spec.source_ip
            self.state.status = "downloading"
            self.state.last_error = ""
            base_command = build_download_command(self.cfg, target, self.spec.worker_id, self.spec.source_ip)
            command = base_command
            source_part = f" source_ip={self.spec.source_ip}" if self.spec.source_ip else ""
            self.log(f"worker={self.spec.worker_id} line={self.spec.line_index}{source_part} target={target} start")
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
                    self.log(f"worker={self.spec.worker_id} target={target} completed")
                elif not self.stop_event.is_set():
                    self.state.status = "error"
                    self.state.last_error = f"download command exited with {code}"
                    self.log(f"worker={self.spec.worker_id} target={target} failed exit={code}")
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

    def _terminate_process(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                self.process.wait(timeout=10)
            except Exception:
                if self.process and self.process.poll() is None:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)

    def stop(self) -> None:
        self.stop_event.set()
        self._terminate_process()
