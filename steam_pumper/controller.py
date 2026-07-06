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

from .config import PumperConfig, load_config, save_config
from .ikuai import fetch_interfaces_status
from .metrics import ThroughputTracker, next_worker_count, theoretical_window_bytes
from .worker import DownloadWorker, SourceEndpoint, WorkerState, bootstrap_steamcmd, build_worker_plan


class PumperController:
    def __init__(self, config_path: str | Path) -> None:
        self.config_path = Path(config_path)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.scheduler_stop = threading.Event()
        self.metrics_stop = threading.Event()
        self.manual_enabled = True
        self.bootstrap_in_progress = False
        self.cfg = load_config(self.config_path)
        self.workers: dict[int, DownloadWorker] = {}
        self.worker_states: dict[int, WorkerState] = {}
        self.sources: list[SourceEndpoint] = []
        self.desired_worker_count = self.cfg.line_count * self.cfg.connections_per_line
        self.tracker = ThroughputTracker()
        self.logs: list[str] = []
        self.scheduler_thread = threading.Thread(target=self._scheduler_loop, name="scheduler", daemon=True)
        self.metrics_thread = threading.Thread(target=self._metrics_loop, name="metrics", daemon=True)
        self._last_scale_check = 0.0

    def start_scheduler(self) -> None:
        self.scheduler_thread.start()
        self.metrics_thread.start()

    def shutdown(self) -> None:
        self.scheduler_stop.set()
        self.metrics_stop.set()
        self.stop_downloads()

    def start_downloads(self) -> None:
        with self.lock:
            if self.workers or self.bootstrap_in_progress:
                return
            self.bootstrap_in_progress = self.cfg.download_mode == "steam_tmpfs"
        ready = True
        bootstrap_log = ""
        if self.cfg.download_mode == "steam_tmpfs":
            ready, bootstrap_log = bootstrap_steamcmd(self.cfg.bootstrap_timeout_seconds)
        try:
            if not ready:
                self.log(f"steamcmd bootstrap failed: {bootstrap_log}")
                return
            if self.cfg.download_mode == "steam_tmpfs":
                self.log("steamcmd bootstrap completed")
            with self.lock:
                if self.workers:
                    return
                self.sources = self.resolve_sources()
                self.desired_worker_count = self.cfg.line_count * self.cfg.connections_per_line
                self.worker_states = {}
                self.workers = {}
                self._set_worker_count_locked(self.desired_worker_count)
                self.log(f"started {len(self.workers)} workers")
        finally:
            with self.lock:
                self.bootstrap_in_progress = False

    def stop_downloads(self) -> None:
        with self.lock:
            workers = list(self.workers.values())
            if not workers:
                return
            self.workers = {}
        for worker in workers:
            worker.stop()
            worker.join(timeout=20)
        with self.lock:
            self.log("stopped workers")

    def update_config(self, data: dict[str, Any]) -> PumperConfig:
        allowed = set(PumperConfig.__dataclass_fields__.keys())
        clean = {key: value for key, value in data.items() if key in allowed}
        with self.lock:
            merged = self.cfg.to_dict()
            merged.update(clean)
            if isinstance(merged.get("app_ids"), str):
                merged["app_ids"] = [item.strip() for item in merged["app_ids"].split(",") if item.strip()]
            new_cfg = PumperConfig(**merged).validate()
            save_config(self.config_path, new_cfg)
            running = bool(self.workers)
            if running:
                self.stop_downloads()
            self.cfg = new_cfg
            self.desired_worker_count = self.cfg.line_count * self.cfg.connections_per_line
            if running and self.manual_enabled and self.cfg.is_within_window(datetime.now().time()):
                self.start_downloads()
            self.log("configuration updated")
            return self.cfg

    def set_manual_enabled(self, enabled: bool) -> None:
        self.manual_enabled = enabled
        if not enabled:
            self.stop_downloads()

    def status(self) -> dict[str, Any]:
        now = datetime.now()
        with self.lock:
            return {
                "running": bool(self.workers),
                "bootstrap_in_progress": self.bootstrap_in_progress,
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
            theoretical = theoretical_window_bytes(self.cfg)
            minimum = int(theoretical * 0.8)
            avg10 = self.tracker.average_mbps(10)
            avg60 = self.tracker.average_mbps(60)
            current = self.tracker.current_mbps
            target_pct = (avg60 / self.cfg.target_mbps * 100) if self.cfg.target_mbps else 0.0
            daily_pct = (self.tracker.today_bytes / theoretical * 100) if theoretical else 0.0
            capacity_warning = bool(
                self.workers
                and len(self.workers) >= self.cfg.line_count * self.cfg.max_connections_per_line
                and avg60 < self.cfg.target_mbps * 0.9
            )
            return {
                "target_mbps": self.cfg.target_mbps,
                "current_mbps": current,
                "avg10_mbps": avg10,
                "avg60_mbps": avg60,
                "target_percent": target_pct,
                "today_bytes": self.tracker.today_bytes,
                "daily_percent": daily_pct,
                "theoretical_window_bytes": theoretical,
                "minimum_accept_bytes": minimum,
                "minimum_accept_percent": 80,
                "worker_count": len(self.workers),
                "desired_worker_count": self.desired_worker_count,
                "max_worker_count": self.cfg.line_count * self.cfg.max_connections_per_line,
                "capacity_warning": capacity_warning,
                "ikuai": fetch_interfaces_status(),
            }

    def source_snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            sources = self.sources or self.resolve_sources()
            return [source.__dict__.copy() for source in sources]

    def resolve_sources(self) -> list[SourceEndpoint]:
        if self.cfg.download_mode != "public_http":
            return [SourceEndpoint(url=app_id, ip=app_id, healthy=True) for app_id in self.cfg.app_ids]
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
                for ip in ips:
                    endpoints.append(SourceEndpoint(url=url, ip=ip, healthy=True))
            except OSError:
                endpoints.append(SourceEndpoint(url=url, ip=host, healthy=False, failures=1))
        if not any(source.healthy for source in endpoints):
            endpoints = [SourceEndpoint(url=url, ip=urlparse(url).hostname or url, healthy=True) for url in self.cfg.source_pool]
        return endpoints

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
                self._maybe_scale_workers()
            except Exception as exc:
                self.log(f"metrics error={exc}")
            self.metrics_stop.wait(1)

    def sample_metrics(self) -> None:
        path = os.environ.get("METRICS_RX_BYTES_PATH", "/sys/class/net/eth0/statistics/rx_bytes")
        try:
            rx_bytes = int(Path(path).read_text(encoding="utf-8").strip())
        except Exception:
            return
        self.tracker.record(time.time(), rx_bytes)

    def _maybe_scale_workers(self) -> None:
        now = time.monotonic()
        if now - self._last_scale_check < 10:
            return
        self._last_scale_check = now
        with self.lock:
            if not self.workers:
                return
            avg60 = self.tracker.average_mbps(60) or self.tracker.average_mbps(10) or self.tracker.current_mbps
            target_count = next_worker_count(self.cfg, len(self.workers), avg60)
            if target_count == len(self.workers):
                return
            self.desired_worker_count = target_count
            self._set_worker_count_locked(target_count)
            self.log(f"autoscale workers={target_count} avg60_mbps={avg60:.1f}")

    def _set_worker_count_locked(self, target_count: int) -> None:
        target_count = max(0, target_count)
        if len(self.workers) > target_count:
            for worker_id in sorted(self.workers.keys(), reverse=True):
                if len(self.workers) <= target_count:
                    break
                worker = self.workers.pop(worker_id)
                worker.stop()
                if worker_id in self.worker_states:
                    self.worker_states[worker_id].status = "stopped"
        if len(self.workers) >= target_count:
            return
        plan = build_worker_plan(self.cfg, worker_count=target_count, sources=self.sources)
        for spec in plan:
            if spec.worker_id in self.workers:
                continue
            stop_event = threading.Event()
            state = WorkerState(worker_id=spec.worker_id, line_index=spec.line_index, target=spec.target)
            self.worker_states[spec.worker_id] = state
            worker = DownloadWorker(self.cfg, spec, state, stop_event, self.log)
            self.workers[spec.worker_id] = worker
            worker.start()
