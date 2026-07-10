from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import CommonConfig, MAX_CONNECTIONS_PER_LINE
from .topology import LogicalLine


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


def build_engine_command(cfg: CommonConfig, line: LogicalLine, sources: list[str]) -> list[str]:
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
    return [*command, *sources]


class EngineProcess:
    """Own one long-lived Go helper without allocating a Python reader thread."""

    def __init__(
        self,
        cfg: CommonConfig,
        line: LogicalLine,
        sources: list[str],
        log: Callable[[str], None],
    ) -> None:
        self.cfg = cfg
        self.line = line
        self.sources = list(sources)
        self.log = log
        self.state = EngineState(
            line_id=line.line_id,
            bind_ip=line.bind_ip,
            connections=cfg.connections_per_line,
        )
        self.process: subprocess.Popen[bytes] | None = None
        self.stop_requested = False
        self.next_restart_at = 0.0
        self.consecutive_failures = 0
        self._output_buffer = ""

    def start(self) -> None:
        if self.stop_requested or (self.process and self.process.poll() is None):
            return
        self.state.status = "starting"
        self.state.last_error = ""
        try:
            self.process = subprocess.Popen(
                build_engine_command(self.cfg, self.line, self.sources),
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
        if event_type == "status":
            total_bytes = event.get("bytes")
            connections = event.get("connections")
            if isinstance(total_bytes, int) and total_bytes >= 0:
                self.state.total_bytes = total_bytes
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
        if event.get("recovered") is True:
            self.state.source_failures[url] = 0
            return
        error = event.get("error")
        if isinstance(error, str) and error:
            self.state.source_failures[url] = self.state.source_failures.get(url, 0) + 1
            self.state.last_error = error[-500:]
