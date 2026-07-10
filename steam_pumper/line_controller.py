from __future__ import annotations

import logging
import os
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .line_config import LineConfig, load_line_config, save_line_config
from .line_metrics import LineThroughputTracker, theoretical_window_bytes
from .line_worker import (
    LineDownloadWorker,
    LineSource,
    LineWorkerSpec,
    LineWorkerState,
    build_line_worker_plan,
    next_line_worker_count,
)


class LineController:
    def __init__(self, config_path: str | Path) -> None:
        self.config_path = Path(config_path)
        self.lock = threading.RLock()
        self.scheduler_stop = threading.Event()
        self.metrics_stop = threading.Event()
        self.manual_enabled = True
        self.downloads_starting = False
        self.reconfiguring = False
        self.cfg = load_line_config(self.config_path)
        self.workers: dict[int, LineDownloadWorker] = {}
        self.worker_states: dict[int, LineWorkerState] = {}
        self.sources: list[LineSource] = []
        self.desired_worker_count = self.cfg.connections
        self.tracker = LineThroughputTracker()
        self.logs: list[str] = []
        self.scheduler_thread = threading.Thread(target=self._scheduler_loop, name="line-scheduler", daemon=True)
        self.metrics_thread = threading.Thread(target=self._metrics_loop, name="line-metrics", daemon=True)
        self._last_scale_check = 0.0

    def start_scheduler(self) -> None:
        for name, thread in (("scheduler", self.scheduler_thread), ("metrics", self.metrics_thread)):
            try:
                thread.start()
            except RuntimeError as exc:
                self.log(
                    f"{name} thread failed: {exc}; increase the iKuai container memory/PID limit "
                    "and keep MAX_CONNECTIONS at 12 or lower"
                )

    def shutdown(self) -> None:
        self.scheduler_stop.set()
        self.metrics_stop.set()
        self.stop_downloads()

    def start_downloads(self) -> None:
        with self.lock:
            if self.workers or self.downloads_starting or self.reconfiguring:
                return
            self.downloads_starting = True
        try:
            self.sources = self.resolve_sources()
            with self.lock:
                self.desired_worker_count = self.cfg.connections
                self.worker_states = {}
                self.workers = {}
                self._set_worker_count_locked(self.desired_worker_count)
                self.log(f"started engine with {self.desired_worker_count} connections")
        finally:
            with self.lock:
                self.downloads_starting = False

    def stop_downloads(self) -> None:
        with self.lock:
            workers = list(self.workers.values())
            if not workers:
                return
            self.workers = {}
        for worker in workers:
            worker.stop()
        with self.lock:
            self.log("stopped workers")

    def update_config(self, data: dict[str, Any]) -> LineConfig:
        allowed = set(LineConfig.__dataclass_fields__.keys())
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ValueError(f"unsupported configuration fields: {', '.join(unknown)}")
        clean = {key: value for key, value in data.items() if key in allowed}
        with self.lock:
            merged = self.cfg.to_dict()
            merged.update(clean)
            if isinstance(merged.get("source_pool"), str):
                merged["source_pool"] = [item.strip() for item in merged["source_pool"].replace("\n", ",").split(",") if item.strip()]
            new_cfg = LineConfig(**merged).validate()
            running = bool(self.workers)
            self.reconfiguring = True
        should_restart = False
        try:
            save_line_config(self.config_path, new_cfg)
            if running:
                self.stop_downloads()
            with self.lock:
                self.cfg = new_cfg
                self.desired_worker_count = self.cfg.connections
                self.log("configuration updated")
                should_restart = running and self.manual_enabled and self.cfg.is_within_window(datetime.now().time())
        finally:
            with self.lock:
                self.reconfiguring = False
        if should_restart:
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
                "running": any(state.status in {"starting", "downloading", "restarting"} for state in self.worker_states.values()),
                "downloads_starting": self.downloads_starting,
                "manual_enabled": self.manual_enabled,
                "within_window": self.cfg.is_within_window(now.time()),
                "now": now.isoformat(timespec="seconds"),
                "config": self.cfg.to_dict(),
                "workers": [state.__dict__.copy() for state in self.worker_states.values()],
                "metrics": self.metrics(),
                "logs": self.logs[-120:],
            }

    def metrics(self) -> dict[str, Any]:
        with self.lock:
            avg10 = self.tracker.average_mbps(10)
            avg60 = self.tracker.average_mbps(60)
            target_pct = (avg60 / self.cfg.target_mbps * 100) if self.cfg.target_mbps else 0.0
            theoretical_bytes = theoretical_window_bytes(self.cfg)
            minimum_accept_bytes = int(theoretical_bytes * 0.8)
            return {
                "target_mbps": self.cfg.target_mbps,
                "current_mbps": self.tracker.current_mbps,
                "avg10_mbps": avg10,
                "avg60_mbps": avg60,
                "target_percent": target_pct,
                "today_bytes": self.tracker.today_bytes,
                "theoretical_window_bytes": theoretical_bytes,
                "minimum_accept_bytes": minimum_accept_bytes,
                "daily_target_percent": (self.tracker.today_bytes / theoretical_bytes * 100) if theoretical_bytes else 0.0,
                "worker_count": self.desired_worker_count if any(
                    state.status == "downloading" for state in self.worker_states.values()
                ) else 0,
                "managed_worker_count": len(self.workers),
                "desired_worker_count": self.desired_worker_count,
                "max_worker_count": self.cfg.max_connections,
                "capacity_warning": bool(
                    self.workers
                    and self.desired_worker_count >= self.cfg.max_connections
                    and avg60 < self.cfg.target_mbps * 0.9
                ),
            }

    def source_snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            sources = self.sources or self.resolve_sources()
            failures_by_url: dict[str, int] = {}
            for engine in self.workers.values():
                for url, failures in engine.source_failures.items():
                    failures_by_url[url] = failures_by_url.get(url, 0) + failures
            return [
                {
                    **source.__dict__,
                    "healthy": source.healthy and failures_by_url.get(source.url, 0) == 0,
                    "failures": source.failures + failures_by_url.get(source.url, 0),
                }
                for source in sources
            ]

    def resolve_sources(self) -> list[LineSource]:
        endpoints: list[LineSource] = []
        for url in self.cfg.source_pool:
            host = urlparse(url).hostname
            if not host:
                endpoints.append(LineSource(url=url, healthy=False, failures=1))
                continue
            try:
                infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
                ips = sorted({info[4][0] for info in infos})
                if not ips:
                    endpoints.append(LineSource(url=url, ip=host, healthy=False, failures=1))
                for ip in ips:
                    endpoints.append(LineSource(url=url, ip=ip, healthy=True))
            except OSError:
                endpoints.append(LineSource(url=url, ip=host, healthy=False, failures=1))
        return endpoints

    def log(self, message: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
        logging.info(message)
        with self.lock:
            self.logs.append(line)
            self.logs = self.logs[-500:]

    def sample_metrics(self) -> None:
        path = os.environ.get("METRICS_RX_BYTES_PATH", "/sys/class/net/eth0/statistics/rx_bytes")
        try:
            rx_bytes = int(Path(path).read_text(encoding="utf-8").strip())
        except Exception:
            return
        with self.lock:
            self.tracker.record(time.time(), rx_bytes)

    def _scheduler_loop(self) -> None:
        while not self.scheduler_stop.is_set():
            try:
                should_run = self.manual_enabled and self.cfg.is_within_window(datetime.now().time())
                if should_run and not self.workers:
                    self.start_downloads()
                elif not should_run and self.workers:
                    self.stop_downloads()
            except Exception as exc:
                self.log(f"scheduler error={exc}")
            self.scheduler_stop.wait(self.cfg.schedule_poll_seconds)

    def _metrics_loop(self) -> None:
        while not self.metrics_stop.is_set():
            try:
                self.sample_metrics()
                self._maintain_workers()
                self._maybe_scale_workers()
            except Exception as exc:
                self.log(f"metrics error={exc}")
            self.metrics_stop.wait(1)

    def _maybe_scale_workers(self) -> None:
        now = time.monotonic()
        if self.tracker.sample_span_seconds() < 10:
            return
        if now - self._last_scale_check < 10:
            return
        self._last_scale_check = now
        with self.lock:
            if not self.workers:
                return
            avg60 = self.tracker.average_mbps(60) or self.tracker.average_mbps(10) or self.tracker.current_mbps
            target_count = next_line_worker_count(self.cfg, self.desired_worker_count, avg60)
            if target_count == self.desired_worker_count:
                return
            self.desired_worker_count = target_count
            self._set_worker_count_locked(target_count)
            self.log(f"autoscale workers={target_count} avg60_mbps={avg60:.1f}")

    def _set_worker_count_locked(self, target_count: int) -> None:
        target_count = max(0, min(target_count, self.cfg.max_connections))
        if target_count == 0:
            for worker in self.workers.values():
                worker.stop()
            self.workers = {}
            return
        if self.workers:
            engine = next(iter(self.workers.values()))
            engine.set_connection_count(target_count)
            return
        plan = build_line_worker_plan(self.cfg, worker_count=target_count, sources=self.sources)
        primary = plan[0]
        all_targets = tuple(dict.fromkeys([spec.target for spec in plan] + list(primary.fallback_targets)))
        spec = LineWorkerSpec(
            worker_id=1,
            target=primary.target,
            target_ip=primary.target_ip,
            fallback_targets=tuple(url for url in all_targets if url != primary.target),
        )
        state = LineWorkerState(worker_id=1, target=spec.target, connections=target_count)
        self.worker_states = {1: state}
        engine = LineDownloadWorker(self.cfg, spec, state, self.log, connections=target_count)
        self.workers = {1: engine}
        engine.start()

    def _maintain_workers(self) -> None:
        with self.lock:
            now = time.monotonic()
            for worker in self.workers.values():
                worker.poll(now)
