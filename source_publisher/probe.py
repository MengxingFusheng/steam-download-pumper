from __future__ import annotations

import math
import statistics
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit

from .candidates import resolve_public_ipv4, validate_source_url


MIN_PROBE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_WORKERS = 4


@dataclass(frozen=True)
class ProbeResult:
    url: str
    checked_at: datetime
    probe_mbps: float
    bytes_read: int
    success: bool
    error: str


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, resolver: Callable[[str, int], tuple[str, ...]]) -> None:
        super().__init__()
        self._resolver = resolver

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        target = validate_source_url(urljoin(req.full_url, newurl))
        count = getattr(req, "_publisher_redirect_count", 0) + 1
        if count > MAX_REDIRECTS:
            raise urllib.error.HTTPError(target, code, "redirect limit exceeded", headers, fp)
        _validate_destination(target, self._resolver)
        redirected = super().redirect_request(req, fp, code, msg, headers, target)
        if redirected is not None:
            setattr(redirected, "_publisher_redirect_count", count)
        return redirected


def _validate_destination(
    url: str, resolver: Callable[[str, int], tuple[str, ...]]
) -> None:
    parsed = urlsplit(validate_source_url(url))
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    resolver(parsed.hostname or "", port)


def _probe_once(
    url: str,
    probe_bytes: int,
    timeout: float,
    resolver: Callable[[str, int], tuple[str, ...]],
    cancel_event: threading.Event,
) -> tuple[int, float]:
    if cancel_event.is_set():
        raise InterruptedError("probe interrupted")
    _validate_destination(url, resolver)
    request = urllib.request.Request(
        url,
        headers={
            "Accept-Encoding": "identity",
            "Range": f"bytes=0-{probe_bytes - 1}",
            "User-Agent": "pumper-source-publisher/1",
        },
    )
    opener = urllib.request.build_opener(_SafeRedirectHandler(resolver))
    started = time.monotonic()
    with opener.open(request, timeout=max(0.1, timeout)) as response:
        if response.status not in (200, 206):
            raise ValueError("unexpected HTTP status")
        total = 0
        while total < probe_bytes:
            if cancel_event.is_set():
                raise InterruptedError("probe interrupted")
            chunk = response.read(min(64 * 1024, probe_bytes - total))
            if not chunk:
                break
            total += len(chunk)
    if total < MIN_PROBE_BYTES:
        raise ValueError("probe response is shorter than 2 MiB")
    elapsed = max(time.monotonic() - started, 1e-9)
    mbps = total * 8 / elapsed / 1_000_000
    if not math.isfinite(mbps) or mbps < 0:
        raise ValueError("probe speed is invalid")
    return total, mbps


def probe_source(
    url: str,
    *,
    probe_bytes: int = 8 * 1024 * 1024,
    timeout: float = 20,
    resolver: Callable[[str, int], tuple[str, ...]] = resolve_public_ipv4,
    cancel_event: threading.Event | None = None,
) -> ProbeResult:
    cancellation = cancel_event or threading.Event()
    checked_at = datetime.now(timezone.utc)
    deadline = time.monotonic() + min(25.0, max(0.1, timeout * 2))
    speeds: list[float] = []
    total_bytes = 0
    try:
        for _ in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("probe deadline exceeded")
            read, mbps = _probe_once(
                url,
                probe_bytes,
                min(timeout, remaining),
                resolver,
                cancellation,
            )
            total_bytes += read
            speeds.append(mbps)
        speed = float(statistics.median(speeds))
        return ProbeResult(url, checked_at, round(speed, 3), total_bytes, True, "")
    except (OSError, ValueError, TimeoutError, InterruptedError, urllib.error.URLError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        reason = "interrupted" if cancellation.is_set() else "probe failed"
        return ProbeResult(url, checked_at, 0.0, total_bytes, False, reason)


def probe_candidates(
    urls: Iterable[str],
    *,
    probe_bytes: int = 8 * 1024 * 1024,
    timeout: float = 20,
    concurrency: int = 4,
    resolver: Callable[[str, int], tuple[str, ...]] = resolve_public_ipv4,
    cancel_event: threading.Event | None = None,
) -> list[ProbeResult]:
    candidates = list(urls)
    cancellation = cancel_event or threading.Event()
    workers = min(MAX_WORKERS, max(1, concurrency))

    def run(url: str) -> ProbeResult:
        return probe_source(
            url,
            probe_bytes=probe_bytes,
            timeout=timeout,
            resolver=resolver,
            cancel_event=cancellation,
        )

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="publisher-probe") as pool:
        return list(pool.map(run, candidates))
