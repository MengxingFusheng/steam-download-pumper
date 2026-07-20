from __future__ import annotations

import logging
import math
import os
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import CommonConfig, load_config, save_config
from .engine import EngineProcess, SourceRuntimeState
from .metrics import ThroughputTracker, next_connection_count, theoretical_window_bytes
from .remote_sources import RemoteSourceManager, RemoteSourceRefreshWorker, SourceListSnapshot
from .topology import LogicalLine, topology_for


@dataclass(frozen=True)
class SourceEndpoint:
    url: str
    ip: str = ""
    healthy: bool = True
    failures: int = 0


class SourceListRefreshError(RuntimeError):
    pass


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
        remote_source_manager: RemoteSourceManager | None = None,
        remote_source_error: str = "",
        remote_source_refresher: RemoteSourceRefreshWorker | None = None,
    ) -> None:
        self.topology_name = topology_name
        self.topology = topology_for(topology_name)
        self.config_path = Path(config_path)
        self.lock = threading.RLock()
        self.manual_enabled = True
        self.downloads_starting = False
        self.reconfiguring = False
        self.cfg = load_config(topology_name, self.config_path, env)
        self.remote_source_manager = remote_source_manager if topology_name == "multi_ip" else None
        self.remote_source_error = remote_source_error if topology_name == "multi_ip" else ""
        self.remote_source_snapshot = SourceListSnapshot(
            enabled=bool(self.remote_source_manager or self.remote_source_error),
            status="error" if self.remote_source_error else "disabled",
            last_error=self.remote_source_error,
        )
        self.effective_source_pool = list(self.cfg.source_pool)
        self.effective_source_origin = "local-fallback"
        self.active_source_revision = 0
        if self.remote_source_manager is not None:
            remote_sources, self.remote_source_snapshot = self.remote_source_manager.load_last_known_good()
            if remote_sources:
                self.effective_source_pool = list(remote_sources)
                self.effective_source_origin = "last-known-good"
                self.active_source_revision = self.remote_source_snapshot.revision
        runtime_dir = (os.environ if env is None else env).get("SOURCES_RUNTIME_DIR", "/run/pumper")
        self.sources_runtime_dir = Path(runtime_dir)
        self.lines = self.topology.lines(self.cfg)
        self.logs: list[str] = []
        self.sources: list[SourceEndpoint] = (
            [SourceEndpoint(url=url) for url in self.effective_source_pool]
            if self.topology_name == "multi_ip"
            else []
        )
        self.interface_tracker = ThroughputTracker()
        self.line_runtimes = self._build_runtimes()
        self._last_scale_check = 0.0
        self._last_schedule_tick: float | None = None
        self._last_metrics_tick: float | None = None
        self.remote_source_refresher = (
            remote_source_refresher if self.remote_source_manager is not None else None
        )
        if self.remote_source_manager is not None and self.remote_source_refresher is None:
            self.remote_source_refresher = RemoteSourceRefreshWorker(self.remote_source_manager)
            self.remote_source_refresher.request()
        self._pending_source_pool: list[str] = []
        self._pending_source_revision = 0
        self._pending_source_generation = ""
        self._source_apply_attempts = 0
        self._source_apply_error = ""
        self._source_apply_retry_at = 0.0

    def _build_runtimes(self) -> dict[str, LineRuntime]:
        return {
            line.line_id: LineRuntime(
                spec=line,
                engine=EngineProcess(
                    self.cfg,
                    line,
                    self.effective_source_pool,
                    self.log,
                    sources_file=(
                        self.sources_runtime_dir / f"{line.line_id}.sources.json"
                        if self.topology_name == "multi_ip"
                        else None
                    ),
                    reject_private_destinations=self.topology_name == "multi_ip",
                ),
                desired_connections=self.cfg.connections_per_line,
            )
            for line in self.lines
        }

    def tick(self, monotonic_now: float | None = None, wall_time: datetime | None = None) -> None:
        now = time.monotonic() if monotonic_now is None else monotonic_now
        current = datetime.now() if wall_time is None else wall_time
        if self.remote_source_refresher is not None:
            try:
                result = self.remote_source_refresher.poll_result()
                if result is not None:
                    self._consume_source_refresh_result(result, now)
                self.remote_source_refresher.request_if_due(current)
                self._advance_pending_source_update(now)
            except Exception as exc:
                self.log(f"source-list refresh error={exc}")
        if self._last_schedule_tick is None or now - self._last_schedule_tick >= self.cfg.schedule_poll_seconds:
            self._last_schedule_tick = now
            try:
                should_run = self.manual_enabled and self.cfg.is_within_window(current.time())
                with self.lock:
                    running = self._is_running_locked()
                if should_run and not running:
                    self.start_downloads()
                elif not should_run and running:
                    self.stop_downloads()
            except Exception as exc:
                self.log(f"scheduler error={exc}")
        if self._last_metrics_tick is None or now - self._last_metrics_tick >= 1:
            self._last_metrics_tick = now
            try:
                self.sample_metrics()
                self._scale_lines(now)
            except Exception as exc:
                self.log(f"metrics error={exc}")

    def shutdown(self) -> None:
        self.stop_downloads()
        if self.remote_source_refresher is not None:
            self.remote_source_refresher.shutdown()

    def start_downloads(self) -> None:
        with self.lock:
            if self._is_running_locked() or self.downloads_starting or self.reconfiguring:
                return
            self.downloads_starting = True
        try:
            self.topology.apply(self.cfg, self.log)
            self.sources = (
                [SourceEndpoint(url=url) for url in self.effective_source_pool]
                if self.topology_name == "multi_ip"
                else self.resolve_sources(self.effective_source_pool)
            )
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
        remote_active = self.effective_source_origin != "local-fallback"
        if remote_active:
            restart_fields.discard("source_pool")
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
                if not remote_active:
                    self.effective_source_pool = list(new_cfg.source_pool)
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

    def request_source_list_refresh(self, now: datetime | None = None) -> dict[str, Any]:
        refresher = self.remote_source_refresher
        if refresher is None:
            raise SourceListRefreshError(self.remote_source_error or "remote source list is disabled")
        request_state = refresher.request(now)
        status = self.source_list_status()
        status["refresh_request_state"] = request_state
        return status

    def refresh_source_list(self, now: datetime | None = None) -> dict[str, Any]:
        return self.request_source_list_refresh(now)

    def _consume_source_refresh_result(
        self,
        result: tuple[bool, list[str], SourceListSnapshot] | Exception,
        monotonic_now: float,
    ) -> None:
        if isinstance(result, Exception):
            self.log(f"source-list refresh error={result}")
            return
        changed, sources, snapshot = result
        self.remote_source_snapshot = snapshot
        if snapshot.status in {"error", "stale"} and snapshot.last_error:
            if self.effective_source_origin == "remote":
                self.effective_source_origin = "last-known-good"
            self.log(f"source-list refresh failed error={snapshot.last_error}")
            return
        if sources:
            if changed:
                self._pending_source_pool = list(sources)
                self._pending_source_revision = snapshot.revision
                self._pending_source_generation = str(snapshot.revision)
                self._source_apply_attempts = 0
                self._source_apply_error = ""
                self._stage_pending_source_update(monotonic_now)
            elif not self._pending_source_generation:
                self.effective_source_origin = "remote"
                self.active_source_revision = snapshot.revision

    def _stage_pending_source_update(self, monotonic_now: float) -> None:
        if not self._pending_source_generation:
            return
        errors: list[str] = []
        for runtime in self.line_runtimes.values():
            try:
                runtime.engine.stage_sources(
                    self._pending_source_pool,
                    self._pending_source_generation,
                )
            except Exception as exc:
                errors.append(f"{runtime.spec.line_id}: {exc}")
        self._source_apply_attempts += 1
        self._source_apply_error = "; ".join(errors)
        self._source_apply_retry_at = monotonic_now + 5.0

    def _advance_pending_source_update(self, monotonic_now: float) -> None:
        generation = self._pending_source_generation
        if not generation:
            return
        confirmations = [
            runtime.engine.source_generation_confirmed(generation)
            for runtime in self.line_runtimes.values()
        ]
        if all(confirmations):
            with self.lock:
                self.effective_source_pool = list(self._pending_source_pool)
                self.sources = [SourceEndpoint(url=url) for url in self.effective_source_pool]
                self.effective_source_origin = "remote"
                self.active_source_revision = self._pending_source_revision
                self._pending_source_pool = []
                self._pending_source_revision = 0
                self._pending_source_generation = ""
                self._source_apply_error = ""
            self.log(
                f"source-list revision={self.active_source_revision} "
                f"sources={len(self.effective_source_pool)} applied"
            )
            return
        helper_errors = [
            runtime.engine.state.source_reload_error
            for runtime in self.line_runtimes.values()
            if runtime.engine.state.source_reload_error
        ]
        if helper_errors:
            self._source_apply_error = "; ".join(helper_errors)
        if monotonic_now >= self._source_apply_retry_at:
            self._stage_pending_source_update(monotonic_now)

    def source_list_status(self) -> dict[str, Any]:
        with self.lock:
            status = self.remote_source_snapshot.to_dict()
            status["origin"] = self.effective_source_origin
            status["active_revision"] = self.active_source_revision
            status["refresh_state"] = (
                self.remote_source_refresher.status() if self.remote_source_refresher is not None else "disabled"
            )
            status["apply_state"] = "pending" if self._pending_source_generation else "applied"
            status["pending_revision"] = self._pending_source_revision
            status["apply_attempts"] = self._source_apply_attempts
            status["apply_error"] = self._source_apply_error
            return status

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
            sources = self.sources or (
                [SourceEndpoint(url=url) for url in self.effective_source_pool]
                if self.topology_name == "multi_ip"
                else self.resolve_sources()
            )
            snapshots: list[dict[str, Any]] = []
            for source in sources:
                line_states = [
                    self._line_source_snapshot(runtime, source.url)
                    for runtime in self.line_runtimes.values()
                ]
                states = [line["state"] for line in line_states]
                if not source.healthy:
                    state = "unhealthy"
                elif "healthy" in states:
                    state = "healthy"
                elif "probing" in states:
                    state = "probing"
                elif states and all(value == "quarantined" for value in states):
                    state = "quarantined"
                else:
                    state = "degraded"
                retry_line = max(line_states, key=lambda line: line["retry_in_seconds"], default=None)
                latest_error = next(
                    (line["last_error"] for line in reversed(line_states) if line["last_error"]),
                    "",
                )
                failures = source.failures + sum(line["consecutive_failures"] for line in line_states)
                snapshots.append(
                    {
                        "url": source.url,
                        "ip": source.ip,
                        "healthy": source.healthy and "healthy" in states,
                        "failures": failures,
                        "state": state,
                        "retry_after": retry_line["retry_after"] if retry_line else "",
                        "retry_in_seconds": retry_line["retry_in_seconds"] if retry_line else 0,
                        "last_error": latest_error,
                        "lines": line_states,
                    }
                )
            return snapshots

    @staticmethod
    def _line_source_snapshot(runtime: LineRuntime, url: str) -> dict[str, Any]:
        source_state = runtime.engine.state.source_states.get(url)
        if source_state is None:
            failures = runtime.engine.state.source_failures.get(url, 0)
            source_state = SourceRuntimeState(
                state="degraded" if failures else "healthy",
                consecutive_failures=failures,
            )
        retry_in_seconds = source_state.retry_in_seconds
        if source_state.retry_after:
            try:
                retry_at = datetime.fromisoformat(source_state.retry_after.replace("Z", "+00:00"))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                retry_in_seconds = max(
                    0,
                    math.ceil((retry_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()),
                )
            except ValueError:
                pass
        return {
            "line_id": runtime.spec.line_id,
            "bind_ip": runtime.spec.bind_ip,
            "state": source_state.state,
            "consecutive_failures": source_state.consecutive_failures,
            "retry_after": source_state.retry_after,
            "retry_in_seconds": retry_in_seconds,
            "last_error": source_state.last_error,
        }

    def resolve_sources(self, source_pool: list[str] | None = None) -> list[SourceEndpoint]:
        endpoints: list[SourceEndpoint] = []
        for url in self.effective_source_pool if source_pool is None else source_pool:
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

    def _is_running_locked(self) -> bool:
        return any(
            runtime.engine.state.status in {"starting", "downloading", "restarting"}
            for runtime in self.line_runtimes.values()
        )
