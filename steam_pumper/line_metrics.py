from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class LineThroughputTracker:
    samples: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=600))
    current_mbps: float = 0.0
    today_bytes: int = 0
    day: str = ""
    _last_time: float | None = None
    _last_rx_bytes: int | None = None

    def record(self, timestamp: float, rx_bytes: int, day: str | None = None) -> None:
        current_day = day or datetime.now().date().isoformat()
        if self.day and current_day != self.day:
            self.today_bytes = 0
            self._last_rx_bytes = rx_bytes
            self._last_time = timestamp
            self.day = current_day
            self.samples.clear()
            self.current_mbps = 0.0
            return
        if not self.day:
            self.day = current_day
        if self._last_time is not None and self._last_rx_bytes is not None:
            elapsed = max(0.001, timestamp - self._last_time)
            delta = max(0, rx_bytes - self._last_rx_bytes)
            self.current_mbps = (delta * 8) / elapsed / 1_000_000
            self.today_bytes += delta
        self._last_time = timestamp
        self._last_rx_bytes = rx_bytes
        self.samples.append((timestamp, rx_bytes))

    def average_mbps(self, seconds: int) -> float:
        if len(self.samples) < 2:
            return 0.0
        newest_time, newest_bytes = self.samples[-1]
        oldest_time, oldest_bytes = self.samples[0]
        for sample_time, sample_bytes in reversed(self.samples):
            if newest_time - sample_time >= seconds:
                oldest_time, oldest_bytes = sample_time, sample_bytes
                break
        elapsed = newest_time - oldest_time
        if elapsed <= 0:
            return 0.0
        return max(0, newest_bytes - oldest_bytes) * 8 / elapsed / 1_000_000
