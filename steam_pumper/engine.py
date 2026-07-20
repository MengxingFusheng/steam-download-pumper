from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import CommonConfig, MAX_CONNECTIONS_PER_LINE
from .topology import LogicalLine


@dataclass
class SourceRuntimeState:
    state: str = "healthy"
    consecutive_failures: int = 0
    retry_after: str = ""
    retry_in_seconds: int = 0
    last_error: str = ""


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
    source_states: dict[str, SourceRuntimeState] = field(default_factory=dict)
    last_error: str = ""
    restarts: int = 0
    pending_source_generation: str = ""
    confirmed_source_generation: str = ""
    source_reload_error: str = ""


def build_engine_command(
    cfg: CommonConfig,
    line: LogicalLine,
    sources: list[str],
    *,
    sources_file: str | Path | None = None,
    reject_private_destinations: bool = False,
) -> list[str]:
    command = [
        "discarder",
        "--worker-id",
        line.line_id,
        "--line-id",
        line.line_id,
        "--connections",
        str(cfg.connections_per_line),
        "--max-connections",
        str(cfg.max_connections_per_line),
        "--min-session-seconds",
        str(cfg.worker_min_session_seconds),
        "--startup-jitter-seconds",
        str(cfg.startup_stagger_seconds),
        "--restart-jitter-seconds",
        str(cfg.worker_restart_jitter_seconds),
        "--status-interval-seconds",
        "1",
    ]
    if line.bind_ip:
        command.extend(["--bind-ip", line.bind_ip])
    if sources_file is not None:
        command.extend(["--sources-file", str(sources_file)])
    if reject_private_destinations:
        command.append("--reject-private-destinations")
    return command if sources_file is not None else [*command, *sources]


class EngineProcess:
    """Own one long-lived Go helper without allocating a Python reader thread."""

    def __init__(
        self,
        cfg: CommonConfig,
        line: LogicalLine,
        sources: list[str],
        log: Callable[[str], None],
        *,
        sources_file: str | Path | None = None,
        reject_private_destinations: bool = False,
    ) -> None:
        self.cfg = cfg
        self.line = line
        self.sources = list(sources)
        self.sources_file = Path(sources_file) if sources_file is not None else None
        self.reject_private_destinations = reject_private_destinations
        self.log = log
        self.state = EngineState(
            line_id=line.line_id,
            bind_ip=line.bind_ip,
            connections=cfg.connections_per_line,
        )
        self.process: subprocess.Popen[bytes] | None = None
        self.stop_requested = True
        self.next_restart_at = 0.0
        self.consecutive_failures = 0
        self._output_buffer = ""
        self._byte_offset = 0
        self._process_bytes = 0
        self._pending_sources: list[str] = []

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        self.stop_requested = False
        self.state.status = "starting"
        self.state.last_error = ""
        try:
            if self.sources_file is not None:
                self._write_sources_file(
                    generation=self.state.pending_source_generation or None,
                )
            self.process = subprocess.Popen(
                build_engine_command(
                    self.cfg,
                    self.line,
                    self.sources,
                    sources_file=self.sources_file,
                    reject_private_destinations=self.reject_private_destinations,
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            self.process = None
            self._schedule_restart(f"unable to start discarder: {exc}")
            return
        self.state.pid = self.process.pid
        if self.process.stdout is not None:
            os.set_blocking(self.process.stdout.fileno(), False)
        self.state.status = "downloading"
        self.log(
            f"line={self.line.line_id} engine_pid={self.process.pid} "
            f"connections={self.state.connections} start"
        )

    def set_sources(self, sources: list[str]) -> bool:
        if not sources:
            raise ValueError("source list cannot be empty")
        new_sources = list(sources)
        changed = new_sources != self.sources
        if self.sources_file is not None:
            self._write_sources_file(new_sources)
        self.sources = new_sources
        process = self.process
        if changed and process is not None and process.poll() is None:
            try:
                os.kill(process.pid, signal.SIGHUP)
            except OSError as exc:
                self.log(f"line={self.line.line_id} unable_to_reload_sources error={exc}")
            else:
                self.log(f"line={self.line.line_id} sources_reloaded count={len(new_sources)}")
        return changed

    def stage_sources(self, sources: list[str], generation: str) -> bool:
        if not sources:
            raise ValueError("source list cannot be empty")
        if not generation:
            raise ValueError("source generation cannot be empty")
        new_sources = list(sources)
        self._pending_sources = new_sources
        self.state.pending_source_generation = generation
        self.state.source_reload_error = ""
        try:
            if self.sources_file is not None:
                self._write_sources_file(new_sources, generation=generation)
        except Exception as exc:
            self.state.source_reload_error = str(exc)[-500:]
            raise
        self.sources = new_sources
        process = self.process
        if process is None or process.poll() is not None:
            self._confirm_source_generation(generation)
            return True
        try:
            os.kill(process.pid, signal.SIGHUP)
        except OSError as exc:
            self.state.source_reload_error = str(exc)[-500:]
            self.log(f"line={self.line.line_id} unable_to_reload_sources error={exc}")
            raise
        self.log(
            f"line={self.line.line_id} sources_reload_pending "
            f"generation={generation} count={len(new_sources)}"
        )
        return True

    def source_generation_confirmed(self, generation: str) -> bool:
        return self.state.confirmed_source_generation == generation

    def _confirm_source_generation(self, generation: str) -> None:
        if generation != self.state.pending_source_generation:
            return
        active = set(self._pending_sources or self.sources)
        self.state.source_failures = {
            url: failures for url, failures in self.state.source_failures.items() if url in active
        }
        self.state.source_states = {
            url: source_state for url, source_state in self.state.source_states.items() if url in active
        }
        self.state.confirmed_source_generation = generation
        self.state.pending_source_generation = ""
        self.state.source_reload_error = ""
        self._pending_sources = []

    def _write_sources_file(
        self,
        sources: list[str] | None = None,
        *,
        generation: str | None = None,
    ) -> None:
        if self.sources_file is None:
            return
        sources_to_write = self.sources if sources is None else sources
        self.sources_file.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.sources_file.name}.",
            dir=self.sources_file.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                payload: Any = sources_to_write
                if generation is not None:
                    payload = {"generation": generation, "sources": sources_to_write}
                json.dump(payload, temporary, ensure_ascii=True, separators=(",", ":"))
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self.sources_file)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def poll(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self.process is not None:
            self._drain_output()
            code = self.process.poll()
            if code is None:
                return
            self.process = None
            self.state.pid = None
            if self.stop_requested:
                self.state.status = "stopped"
                return
            self._schedule_restart(f"discarder exited with {code}", now)
        if not self.stop_requested and self.process is None and now >= self.next_restart_at:
            self.start()

    def set_connections(self, target: int) -> None:
        target = max(1, min(target, self.cfg.max_connections_per_line, MAX_CONNECTIONS_PER_LINE))
        current = self.state.connections
        process = self.process
        if process and process.poll() is None and target != current:
            resize_signal = signal.SIGUSR1 if target > current else signal.SIGUSR2
            try:
                for _index in range(abs(target - current)):
                    os.kill(process.pid, resize_signal)
            except OSError as exc:
                self.log(f"line={self.line.line_id} unable_to_resize error={exc}")
                return
        self.state.connections = target
        self.log(f"line={self.line.line_id} connections={target}")

    def stop(self) -> None:
        self.stop_requested = True
        process = self.process
        self.process = None
        self.state.pid = None
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
        self._byte_offset = self.state.total_bytes
        self._process_bytes = 0
        self.consecutive_failures += 1
        self.state.restarts += 1
        self.state.status = "restarting"
        self.state.last_error = error[-500:]
        delay = min(60.0, float(2 ** min(self.consecutive_failures - 1, 6)))
        self.next_restart_at = (time.monotonic() if now is None else now) + delay
        self.log(f"line={self.line.line_id} error={error} restart_in={delay:.0f}s")

    def _drain_output(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        try:
            chunk = self.process.stdout.read()
        except (BlockingIOError, OSError):
            return
        if not chunk:
            return
        self._output_buffer = (self._output_buffer + chunk.decode("utf-8", errors="replace"))[-65_536:]
        lines = self._output_buffer.split("\n")
        self._output_buffer = lines.pop()
        for line in lines:
            self._consume_line(line)

    def _consume_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            self.state.last_error = line[-500:]
            return
        if not isinstance(event, dict) or event.get("line_id") != self.line.line_id:
            return
        event_type = event.get("type")
        if event_type == "source-list":
            generation = event.get("generation")
            error = event.get("error")
            if isinstance(error, str) and error:
                if not generation or generation == self.state.pending_source_generation:
                    self.state.source_reload_error = error[-500:]
                return
            if event.get("state") == "reloaded" and isinstance(generation, str):
                self._confirm_source_generation(generation)
            return
        if event_type == "status":
            total_bytes = event.get("bytes")
            connections = event.get("connections")
            if isinstance(total_bytes, int) and total_bytes >= 0:
                self._process_bytes = total_bytes
                self.state.total_bytes = self._byte_offset + self._process_bytes
                self.state.has_metrics = True
                self.consecutive_failures = 0
            if isinstance(connections, int) and 1 <= connections <= MAX_CONNECTIONS_PER_LINE:
                self.state.connections = connections
            if isinstance(event.get("url"), str):
                self.state.current_source = event["url"]
            return
        if event_type != "source" or not isinstance(event.get("url"), str):
            return
        url = event["url"]
        if url not in self.sources:
            return
        if event.get("recovered") is True:
            self.state.source_failures[url] = 0
            self.state.source_states[url] = SourceRuntimeState()
            return
        error = event.get("error")
        raw_state = event.get("state")
        state = raw_state if raw_state in {"healthy", "degraded", "quarantined", "probing"} else "degraded"
        failures = event.get("consecutive_failures")
        if not isinstance(failures, int) or isinstance(failures, bool) or failures < 0:
            failures = self.state.source_failures.get(url, 0) + (1 if error else 0)
        retry_after = event.get("retry_after")
        if not isinstance(retry_after, str):
            retry_after = ""
        retry_in_seconds = event.get("retry_in_seconds")
        if (
            not isinstance(retry_in_seconds, int)
            or isinstance(retry_in_seconds, bool)
            or retry_in_seconds < 0
        ):
            retry_in_seconds = 0
        last_error = error if isinstance(error, str) else ""
        self.state.source_failures[url] = failures
        self.state.source_states[url] = SourceRuntimeState(
            state=state,
            consecutive_failures=failures,
            retry_after=retry_after,
            retry_in_seconds=retry_in_seconds,
            last_error=last_error,
        )
        if isinstance(error, str) and error:
            self.state.last_error = error[-500:]
