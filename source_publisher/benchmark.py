"""Bounded throughput measurements using exactly the node download engine."""
from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import deque


def measure_engine(url: str, engine_path: str, deadline: float,
                   cancel_event: threading.Event, *, duration: float = 15,
                   warmup: float = 3) -> tuple[int, float]:
    if deadline - time.monotonic() < duration or cancel_event.is_set():
        raise TimeoutError("insufficient time for sustained measurement")
    command = [engine_path, "--worker-id", "publisher-probe", "--line-id", "publisher-probe",
               "--connections", "12", "--max-connections", "12", "--min-session-seconds", "300",
               "--startup-jitter-seconds", "0", "--restart-jitter-seconds", "3",
               "--read-timeout-seconds", "15", "--status-interval-seconds", "1",
               "--reject-private-destinations", url]
    samples: deque[tuple[float, int]] = deque(maxlen=120)
    started = time.monotonic()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                event = json.loads(line)
                count = event.get("bytes")
                if event.get("type") == "status" and isinstance(count, int) and count >= 0:
                    samples.append((time.monotonic(), count))
            except (ValueError, TypeError):
                continue

    reader = threading.Thread(target=read, name="publisher-engine-output", daemon=True)
    reader.start()
    try:
        while process.poll() is None and time.monotonic() < started + duration:
            if cancel_event.wait(min(0.05, max(0.001, started + duration - time.monotonic()))):
                raise InterruptedError("probe interrupted")
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        reader.join(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
    measured = [(at, count) for at, count in samples if started + warmup <= at <= started + duration + 0.5]
    if len(measured) < 2 or measured[-1][0] - measured[0][0] < (duration - warmup) * 0.7:
        raise ValueError("insufficient sustained samples")
    count = measured[-1][1] - measured[0][1]
    if count < 2 * 1024 * 1024:
        raise ValueError("sustained response is shorter than 2 MiB")
    return max((count for _, count in samples), default=0), count * 8 / (measured[-1][0] - measured[0][0]) / 1_000_000
