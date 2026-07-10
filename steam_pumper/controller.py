from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import CommonConfig, load_config, save_config
from .engine import EngineProcess
from .metrics import ThroughputTracker, next_connection_count, theoretical_window_bytes
from .topology import LogicalLine, topology_for


@dataclass(frozen=True)
class SourceEndpoint:
    url: str
    ip: str = ""
    healthy: bool = True
    failures: int = 0


@dataclass
class LineRuntime:
    spec: LogicalLine
    engine: EngineProcess
    tracker: ThroughputTracker = field(default_factory=ThroughputTracker)
    desired_connections: int = 0


class PumperController:
    def __init__(
        self,
        topology_name: str,
        config_path: str | Path,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.topology_name = topology_name
        self.topology = topology_for(topology_name)
        self.config_path = Path(config_path)
        self.lock = threading.RLock()
        self.scheduler_stop = threading.Event()
        self.metrics_stop = threading.Event()
        self.manual_enabled = True
        self.downloads_starting = False
        self.reconfiguring = False
        self.cfg = load_config(topology_name, self.config_path, env)
        self.lines = self.topology.lines(self.cfg)
        self.logs: list[str] = []
        self.sources: list[SourceEndpoint] = []
        self.interface_tracker = ThroughputTracker()
        self.line_runtimes = self._build_runtimes()
        self.scheduler_thread = threading.Thread(target=self._scheduler_loop, name="scheduler", daemon=True)
        self.metrics_thread = threading.Thread(target=self._metrics_loop, name="metrics", daemon=True)
        self._last_scale_check = 0.0

    def _build_runtimes(self) -> dict[str, LineRuntime]:
        return {
            line.line_id: LineRuntime(
                spec=line,
                engine=EngineProcess(self.cfg, line, self.cfg.source_pool, self.log),
                desired_connections=self.cfg.connections_per_line,
            )
            for line in self.lines
        }

    def start_scheduler(self) -> None:
        for name, thread in (("scheduler", self.scheduler_thread), ("metrics", self.metrics_thread)):
            try:
                thread.start()
            except RuntimeError as exc:
                self.log(f"{name} thread failed: {exc}")

    def shutdown(self) -> None:
        self.scheduler_stop.set()
        self.metrics_stop.set()
        self.stop_downloads()

    def start_downloads(self) -> None:
        with self.lock:
            if self._is_running_locked() or self.downloads_starting or self.reconfiguring:
                return
            self.downloads_starting = True
        try:
            self.topology.apply(self.cfg, self.log)
            self.sources = self.resolve_sources()
            with self.lock:
                for runtime in self.line_runtimes.values():
                    runtime.engine.start()
                self.log(f"started {len(self.line_runtimes)} line engines")
        finally:
            with self.lock:
                self.downloads_starting = False

    def stop_downloads(self) -> None:
        with self.lock:
            engines = [runtime.engine for runtime in self.line_runtimes.values()]
        for engine in engines:
            engine.stop()
        if engines:
            self.log("stopped line engines")

    def update_config(self, data: dict[str, Any]) -> CommonConfig:
        allowed = {name for name, config_field in type(self.cfg).__dataclass_fields__.items() if config_field.init}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ValueError(f"unsupported configuration fields: {', '.join(unknown)}")
        merged = self.cfg.to_dict()
        merged.pop("topology", None)
        merged.update(data)
        for list_field in ("source_pool", "lan_ips"):
            if isinstance(merged.get(list_field), str):
                merged[list_field] = [
                    item.strip()
                    for item in merged[list_field].replace("\n", ",").split(",")
                    if item.strip()
                ]
        new_cfg = type(self.cfg)(**merged).validate()
        new_lines = self.topology.lines(new_cfg)
        old_lines = [(line.line_id, line.bind_ip) for line in self.lines]
        new_line_ids = [(line.line_id, line.bind_ip) for line in new_lines]
        restart_fields = {
            "source_pool",
            "max_connections_per_line",
            "worker_min_session_seconds",
            "startup_stagger_seconds",
            "worker_restart_jitter_seconds",
            "line_count",
            "lan_ips",
        }
        requires_restart = old_lines != new_line_ids or bool(restart_fields.intersection(data))

        with self.lock:
            running = self._is_running_locked()
            self.reconfiguring = True
        try:
            if requires_restart:
                self.topology.apply(new_cfg, self.log)
            save_config(self.config_path, new_cfg)
            if requires_restart and running:
                self.stop_downloads()
            with self.lock:
                self.cfg = new_cfg
                self.lines = new_lines
                if requires_restart:
                    self.line_runtimes = self._build_runtimes()
                else:
                    lines_by_id = {line.line_id: line for line in new_lines}
                    for line_id, runtime in self.line_runtimes.items():
                        runtime.spec = lines_by_id[line_id]
                        runtime.engine.cfg = new_cfg
                        runtime.engine.line = runtime.spec
                        runtime.desired_connections = new_cfg.connections_per_line
                        runtime.engine.set_connections(runtime.desired_connections)
                self.log("configuration updated")
        finally:
            with self.lock:
                self.reconfiguring = False
        if requires_restart and running and self.manual_enabled and new_cfg.is_within_window(datetime.now().time()):
            self.start_downloads()
        return self.cfg

    def set_manual_enabled(self, enabled: bool) -> None:
        self.manual_enabled = enabled
        if not enabled:
            self.stop_downloads()

    def status(self) -> dict[str, Any]:
        now = datetime.now()
        with self.lock:
            return {
                "topology": self.topology_name,
                "running": self._is_running_locked(),
                "downloads_starting": self.downloads_starting,
                "manual_enabled": self.manual_enabled,
                "within_window": self.cfg.is_within_window(now.time()),
                "now": now.isoformat(timespec="seconds"),
                "config": self.cfg.to_dict(),
                "metrics": self.metrics(),
                "logs": self.logs[-120:],
            }

    def metrics(self) -> dict[str, Any]:
        with self.lock:
            line_metrics = [self._line_metrics(runtime) for runtime in self.line_runtimes.values()]
            all_available = bool(line_metrics) and all(line["metrics_available"] for line in line_metrics)
            if all_available:
                current_mbps = sum(line["current_mbps"] for line in line_metrics)
                avg10_mbps = sum(line["avg10_mbps"] for line in line_metrics)
                avg60_mbps = sum(line["avg60_mbps"] for line in line_metrics)
                today_bytes = sum(line["today_bytes"] for line in line_metrics)
            else:
                current_mbps = self.interface_tracker.current_mbps
                avg10_mbps = self.interface_tracker.average_mbps(10)
                avg60_mbps = self.interface_tracker.average_mbps(60)
                today_bytes = self.interface_tracker.today_bytes
            theoretical = theoretical_window_bytes(self.cfg.target_mbps, self.cfg.start_time, self.cfg.end_time)
            return {
                "target_mbps": self.cfg.target_mbps,
                "current_mbps": current_mbps,
                "avg10_mbps": avg10_mbps,
                "avg60_mbps": avg60_mbps,
                "target_percent": (avg60_mbps / self.cfg.target_mbps * 100) if self.cfg.target_mbps else 0.0,
                "today_bytes": today_bytes,
                "theoretical_window_bytes": theoretical,
                "minimum_accept_bytes": int(theoretical * 0.8),
                "daily_target_percent": (today_bytes / theoretical * 100) if theoretical else 0.0,
                "worker_count": sum(line["connections"] for line in line_metrics if line["status"] == "downloading"),
                "max_worker_count": len(line_metrics) * self.cfg.max_connections_per_line,
                "capacity_warning": any(line["capacity_warning"] for line in line_metrics),
                "lines": line_metrics,
            }

    def _line_metrics(self, runtime: LineRuntime) -> dict[str, Any]:
        available = runtime.engine.state.has_metrics
        current = runtime.tracker.current_mbps if available else 0.0
        avg10 = runtime.tracker.average_mbps(10) if available else 0.0
        avg60 = runtime.tracker.average_mbps(60) if available else 0.0
        today = runtime.tracker.today_bytes if available else 0
        return {
            "line_id": runtime.spec.line_id,
            "bind_ip": runtime.spec.bind_ip,
            "target_mbps": runtime.spec.target_mbps,
            "current_mbps": current,
            "avg10_mbps": avg10,
            "avg60_mbps": avg60,
            "today_bytes": today,
            "connections": runtime.desired_connections,
            "max_connections": self.cfg.max_connections_per_line,
            "status": runtime.engine.state.status,
            "metrics_available": available,
            "capacity_warning": bool(
                available
                and runtime.engine.state.status == "downloading"
                and runtime.desired_connections >= self.cfg.max_connections_per_line
                and avg60 < runtime.spec.target_mbps * 0.9
            ),
            "last_error": runtime.engine.state.last_error,
        }

    def source_snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            sources = self.sources or self.resolve_sources()
            failures: dict[str, int] = {}
            for runtime in self.line_runtimes.values():
                for url, count in runtime.engine.state.source_failures.items():
                    failures[url] = failures.get(url, 0) + count
            return [
                {
                    "url": source.url,
                    "ip": source.ip,
                    "healthy": source.healthy and failures.get(source.url, 0) == 0,
                    "failures": source.failures + failures.get(source.url, 0),
                }
                for source in sources
            ]

    def resolve_sources(self) -> list[SourceEndpoint]:
        endpoints: list[SourceEndpoint] = []
        for url in self.cfg.source_pool:
            host = urlparse(url).hostname
            if not host:
                endpoints.append(SourceEndpoint(url=url, healthy=False, failures=1))
                continue
            try:
                infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
                ips = sorted({info[4][0] for info in infos})
                if not ips:
                    endpoints.append(SourceEndpoint(url=url, ip=host, healthy=False, failures=1))
                else:
                    endpoints.extend(SourceEndpoint(url=url, ip=ip) for ip in ips)
            except OSError:
                endpoints.append(SourceEndpoint(url=url, ip=host, healthy=False, failures=1))
        return endpoints

    def sample_metrics(self, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        for runtime in self.line_runtimes.values():
            runtime.engine.poll()
            if runtime.engine.state.has_metrics:
                runtime.tracker.record(timestamp, runtime.engine.state.total_bytes)
        path = os.environ.get("METRICS_RX_BYTES_PATH", "/sys/class/net/eth0/statistics/rx_bytes")
        try:
            rx_bytes = int(Path(path).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return
        self.interface_tracker.record(timestamp, rx_bytes)

    def _scale_lines(self, now: float | None = None) -> None:
        monotonic_now = time.monotonic() if now is None else now
        if monotonic_now - self._last_scale_check < 10:
            return
        self._last_scale_check = monotonic_now
        with self.lock:
            for runtime in self.line_runtimes.values():
                if runtime.engine.state.status != "downloading":
                    continue
                if runtime.engine.state.has_metrics:
                    if runtime.tracker.sample_span_seconds() < 10:
                        continue
                    avg60 = runtime.tracker.average_mbps(60) or runtime.tracker.average_mbps(10)
                elif len(self.line_runtimes) == 1 and self.interface_tracker.sample_span_seconds() >= 10:
                    avg60 = self.interface_tracker.average_mbps(60) or self.interface_tracker.average_mbps(10)
                else:
                    continue
                target = next_connection_count(
                    self.cfg.connections_per_line,
                    self.cfg.max_connections_per_line,
                    runtime.desired_connections,
                    avg60,
                    runtime.spec.target_mbps,
                    self.cfg.rate_limit_enabled,
                )
                if target != runtime.desired_connections:
                    runtime.desired_connections = target
                    runtime.engine.set_connections(target)
                    self.log(
                        f"line={runtime.spec.line_id} autoscale_connections={target} avg60_mbps={avg60:.1f}"
                    )

    def log(self, message: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
        logging.info(message)
        with self.lock:
            self.logs.append(line)
            self.logs = self.logs[-500:]

    def _scheduler_loop(self) -> None:
        while not self.scheduler_stop.is_set():
            try:
                should_run = self.manual_enabled and self.cfg.is_within_window(datetime.now().time())
                with self.lock:
                    running = self._is_running_locked()
                if should_run and not running:
                    self.start_downloads()
                elif not should_run and running:
                    self.stop_downloads()
            except Exception as exc:
                self.log(f"scheduler error={exc}")
            self.scheduler_stop.wait(self.cfg.schedule_poll_seconds)

    def _metrics_loop(self) -> None:
        while not self.metrics_stop.is_set():
            try:
                self.sample_metrics()
                self._scale_lines()
            except Exception as exc:
                self.log(f"metrics error={exc}")
            self.metrics_stop.wait(1)

    def _is_running_locked(self) -> bool:
        return any(
            runtime.engine.state.status in {"starting", "downloading", "restarting"}
            for runtime in self.line_runtimes.values()
        )
