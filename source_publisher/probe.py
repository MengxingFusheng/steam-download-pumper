from __future__ import annotations

import http.client
import ipaddress
import math
import queue
import socket
import ssl
import statistics
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit

from .candidates import resolve_public_ipv4, validate_source_url
from .ookla import is_ookla_download


PROBE_BYTES = 64 * 1024 * 1024
SCREEN_BYTES = 2 * 1024 * 1024
MIN_PROBE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_WORKERS = 4

Resolver = Callable[[str, int], tuple[str, ...]]
ConnectionFactory = Callable[[str, str, str, int, float], http.client.HTTPConnection]


@dataclass
class _ResolverTask:
    resolver: Resolver
    hostname: str
    port: int
    outcome: queue.Queue[tuple[bool, object]]
    cancelled: threading.Event


class _ResolverPool:
    def __init__(self, workers: int = MAX_WORKERS) -> None:
        self.tasks: queue.Queue[_ResolverTask] = queue.Queue(maxsize=workers)
        for index in range(workers):
            threading.Thread(
                target=self._worker,
                name=f"publisher-dns-{index}",
                daemon=True,
            ).start()

    def _worker(self) -> None:
        while True:
            task = self.tasks.get()
            try:
                if task.cancelled.is_set():
                    continue
                try:
                    item: tuple[bool, object] = (
                        True,
                        tuple(task.resolver(task.hostname, task.port)),
                    )
                except Exception as exc:
                    item = (False, exc)
                if not task.cancelled.is_set():
                    try:
                        task.outcome.put_nowait(item)
                    except queue.Full:
                        pass
            finally:
                self.tasks.task_done()

    def submit(
        self,
        resolver: Resolver,
        hostname: str,
        port: int,
        deadline: float,
        cancel_event: threading.Event,
    ) -> _ResolverTask:
        task = _ResolverTask(
            resolver,
            hostname,
            port,
            queue.Queue(maxsize=1),
            threading.Event(),
        )
        while True:
            _check_deadline(deadline, cancel_event)
            try:
                self.tasks.put(
                    task,
                    timeout=min(0.05, max(0.0, deadline - time.monotonic())),
                )
                return task
            except queue.Full:
                continue


_resolver_pool: _ResolverPool | None = None
_resolver_pool_lock = threading.Lock()


def _get_resolver_pool() -> _ResolverPool:
    global _resolver_pool
    with _resolver_pool_lock:
        if _resolver_pool is None:
            _resolver_pool = _ResolverPool()
        return _resolver_pool


@dataclass(frozen=True)
class ProbeResult:
    url: str
    checked_at: datetime
    probe_mbps: float
    bytes_read: int
    success: bool
    error: str
    success_rate: float = 1.0
    worst_mbps: float = 0.0
    attempts: int = 0
    successful_probes: int = 0
    stage: str = "refine"


@dataclass(frozen=True)
class PinnedResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    bytes_read: int = 0


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self, connect_ip: str, server_hostname: str, port: int, timeout: float
    ) -> None:
        super().__init__(
            server_hostname,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._connect_ip = connect_ip

    def connect(self) -> None:
        self.sock = self._create_connection(
            (self._connect_ip, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _default_connection_factory(
    scheme: str, connect_ip: str, hostname: str, port: int, timeout: float
) -> http.client.HTTPConnection:
    if scheme == "https":
        return _PinnedHTTPSConnection(connect_ip, hostname, port, timeout)
    return http.client.HTTPConnection(connect_ip, port=port, timeout=timeout)


def _check_deadline(deadline: float, cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise InterruptedError("probe interrupted")
    if time.monotonic() >= deadline:
        raise TimeoutError("probe deadline exceeded")


def _resolve_with_deadline(
    resolver: Resolver,
    hostname: str,
    port: int,
    deadline: float,
    cancel_event: threading.Event,
) -> tuple[str, ...]:
    task = _get_resolver_pool().submit(
        resolver, hostname, port, deadline, cancel_event
    )
    try:
        while True:
            _check_deadline(deadline, cancel_event)
            remaining = deadline - time.monotonic()
            try:
                succeeded, value = task.outcome.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if succeeded:
                return value  # type: ignore[return-value]
            raise value  # type: ignore[misc]
    finally:
        task.cancelled.set()


def _validated_addresses(
    url: str,
    resolver: Resolver,
    deadline: float,
    cancel_event: threading.Event,
    resolved_addresses: tuple[str, ...] | None = None,
) -> tuple[str, str, int, str, tuple[str, ...]]:
    parsed = urlsplit(validate_source_url(url))
    hostname = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    raw_addresses = resolved_addresses if resolved_addresses is not None else _resolve_with_deadline(
        resolver, hostname, port, deadline, cancel_event
    )
    addresses: list[str] = []
    for raw in raw_addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise ValueError("resolver returned an invalid IPv4 address") from exc
        if (
            address.version != 4
            or not address.is_global
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            or address.is_loopback
            or address.is_link_local
            or address.is_private
        ):
            raise ValueError("resolver returned a non-public IPv4 address")
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise ValueError("resolver returned no public IPv4 address")
    target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    return parsed.scheme, hostname, port, target, tuple(addresses)


def pinned_request(
    url: str,
    *,
    headers: Mapping[str, str],
    max_bytes: int,
    timeout: float,
    resolver: Resolver = resolve_public_ipv4,
    connection_factory: ConnectionFactory = _default_connection_factory,
    deadline: float | None = None,
    cancel_event: threading.Event | None = None,
    discard_body: bool = False,
    resolved_addresses: tuple[str, ...] | None = None,
) -> PinnedResponse:
    cancellation = cancel_event or threading.Event()
    absolute_deadline = deadline or (time.monotonic() + max(0.1, timeout))
    scheme, hostname, port, target, addresses = _validated_addresses(
        url, resolver, absolute_deadline, cancellation, resolved_addresses
    )
    _check_deadline(absolute_deadline, cancellation)
    default_port = 443 if scheme == "https" else 80
    host_header = hostname if port == default_port else f"{hostname}:{port}"
    request_headers = dict(headers)
    request_headers["Host"] = host_header
    connection = connection_factory(
        scheme,
        addresses[0],
        hostname,
        port,
        min(max(0.1, timeout), max(0.1, absolute_deadline - time.monotonic())),
    )
    response: http.client.HTTPResponse | None = None
    finished = threading.Event()
    transport: list[object] = []

    def abort_at_deadline() -> None:
        while not finished.wait(0.05):
            if cancellation.is_set() or time.monotonic() >= absolute_deadline:
                sock = transport[0] if transport else getattr(connection, "sock", None)
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                connection.close()
                return

    watcher = threading.Thread(
        target=abort_at_deadline,
        name="publisher-request-deadline",
        daemon=True,
    )
    watcher.start()
    try:
        connection.request("GET", target, headers=request_headers)
        if connection.sock is not None:
            transport.append(connection.sock)
        _check_deadline(absolute_deadline, cancellation)
        response = connection.getresponse()
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        chunks: list[bytes] = []
        prefix = b""
        bytes_read = 0
        remaining_bytes = max_bytes
        if response.status not in {301, 302, 303, 307, 308}:
            while remaining_bytes > 0:
                try:
                    _check_deadline(absolute_deadline, cancellation)
                    chunk = response.read(min(64 * 1024, remaining_bytes))
                except (OSError, http.client.HTTPException):
                    # A timed probe may use the bytes already received. Cancellation
                    # and transport failures before its time limit remain failures.
                    if discard_body and not cancellation.is_set() and time.monotonic() >= absolute_deadline:
                        break
                    raise
                if not chunk:
                    break
                if discard_body:
                    if not prefix:
                        prefix = chunk[:512]
                else:
                    chunks.append(chunk)
                bytes_read += len(chunk)
                remaining_bytes -= len(chunk)
        if cancellation.is_set() or not discard_body:
            _check_deadline(absolute_deadline, cancellation)
        body = prefix if discard_body else b"".join(chunks)
        return PinnedResponse(response.status, response_headers, body, bytes_read)
    finally:
        finished.set()
        if response is not None:
            response.close()
        connection.close()


def _probe_once(
    url: str,
    timeout: float,
    resolver: Resolver,
    cancel_event: threading.Event,
    connection_factory: ConnectionFactory,
    deadline: float,
    max_bytes: int,
) -> tuple[int, float]:
    current_url = url
    started = time.monotonic()
    for redirect_count in range(MAX_REDIRECTS + 1):
        _check_deadline(deadline, cancel_event)
        headers = {"Accept-Encoding": "identity", "User-Agent": "pumper-source-publisher/1"}
        if urlsplit(current_url).path == "/download":
            headers["User-Agent"] = "fleet-speedtest-authorized/1.0"
        else:
            headers["Range"] = f"bytes=0-{max_bytes - 1}"
        response = pinned_request(
            current_url,
            headers=headers,
            max_bytes=max_bytes,
            timeout=max(0.1, timeout),
            resolver=resolver,
            connection_factory=connection_factory,
            deadline=deadline,
            cancel_event=cancel_event,
            discard_body=True,
        )
        if response.status in {301, 302, 303, 307, 308}:
            location = response.headers.get("location", "")
            if redirect_count >= MAX_REDIRECTS or not location:
                raise ValueError("probe redirect limit exceeded")
            current_url = validate_source_url(urljoin(current_url, location))
            continue
        if response.status not in (200, 206):
            raise ValueError("unexpected HTTP status")
        content_type = response.headers.get("content-type", "").lower()
        prefix = response.body.lstrip().lower()
        if "text/html" in content_type or prefix.startswith((b"<!doctype html", b"<html", b"<?xml", b"{\"error\"")):
            raise ValueError("probe response is an HTML or error document")
        total = response.bytes_read
        if total < MIN_PROBE_BYTES:
            raise ValueError("probe response is shorter than 2 MiB")
        elapsed = max(time.monotonic() - started, 1e-9)
        mbps = total * 8 / elapsed / 1_000_000
        if not math.isfinite(mbps) or mbps < 0:
            raise ValueError("probe speed is invalid")
        return total, mbps
    raise ValueError("probe redirect limit exceeded")


def _expand_speedtest_template(url: str) -> str:
    """Expand the per-request values required by Speedtest XHR download URLs."""
    if "{{guid}}" not in url and "{{nonce}}" not in url:
        return url
    value = str(uuid.uuid4())
    return url.replace("{{guid}}", value).replace("{{nonce}}", value)


def probe_source(
    url: str,
    *,
    timeout: float = 8,
    resolver: Resolver = resolve_public_ipv4,
    cancel_event: threading.Event | None = None,
    connection_factory: ConnectionFactory = _default_connection_factory,
    deadline: float | None = None,
    rounds: int = 3,
    min_successes: int = 2,
    max_bytes: int = PROBE_BYTES,
) -> ProbeResult:
    cancellation = cancel_event or threading.Event()
    checked_at = datetime.now(timezone.utc)
    absolute_deadline = deadline or (time.monotonic() + max(0.1, timeout) * rounds)
    speeds: list[float] = []
    total_bytes = 0
    attempted = 0
    errors: list[str] = []
    for _ in range(rounds):
        if cancellation.is_set() or time.monotonic() >= absolute_deadline:
            break
        attempted += 1
        try:
            read, mbps = _probe_once(
                _expand_speedtest_template(url), timeout, resolver, cancellation,
                connection_factory, min(absolute_deadline, time.monotonic() + timeout),
                max_bytes,
            )
            total_bytes += read
            speeds.append(mbps)
        except (OSError, ValueError, http.client.HTTPException, ssl.SSLError) as exc:
            # Only fixed diagnostic messages are retained, never raw URL/error values.
            if isinstance(exc, ValueError):
                errors.append(str(exc))
            else:
                errors.append("request timeout or transport failure")
    success = len(speeds) >= min_successes and not cancellation.is_set()
    error = "" if success else (
        "interrupted" if cancellation.is_set() else errors[-1] if errors else "probe deadline exceeded"
    )
    return ProbeResult(
        url, checked_at, round(float(statistics.median(speeds)), 3) if success else 0.0,
        total_bytes, success, error, len(speeds) / rounds,
        round(min(speeds), 3) if speeds else 0.0, attempted, len(speeds),
    )


def rank_result(result: ProbeResult) -> tuple:
    return (-result.success_rate, -(result.worst_mbps or result.probe_mbps), -result.probe_mbps, result.url)


def probe_candidates(
    urls: Iterable[str],
    *,
    timeout: float = 8,
    concurrency: int = 4,
    resolver: Resolver = resolve_public_ipv4,
    cancel_event: threading.Event | None = None,
    connection_factory: ConnectionFactory = _default_connection_factory,
    deadline: float | None = None,
    engine_path: str | None = None,
) -> list[ProbeResult]:
    """Screen every candidate, then measure up to 40 individually without contention."""
    candidates = list(urls)
    cancellation = cancel_event or threading.Event()
    absolute_deadline = deadline or (time.monotonic() + 30 * 60)
    workers = min(MAX_WORKERS, max(1, concurrency))

    def screen(url: str) -> ProbeResult:
        return probe_source(
            url, timeout=5, resolver=resolver, cancel_event=cancellation,
            connection_factory=connection_factory, deadline=absolute_deadline,
            rounds=2 if is_ookla_download(url) else 1,
            min_successes=2 if is_ookla_download(url) else 1, max_bytes=SCREEN_BYTES,
        )

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="publisher-probe")
    futures = {pool.submit(screen, url): index for index, url in enumerate(candidates)}
    pending = set(futures)
    screened: dict[str, ProbeResult] = {}
    try:
        while pending and not cancellation.is_set() and time.monotonic() < absolute_deadline:
            completed, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
            for future in completed:
                url = candidates[futures[future]]
                try:
                    screened[url] = future.result()
                except Exception:
                    screened[url] = ProbeResult(url, datetime.now(timezone.utc), 0, 0, False, "screen failed")
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)

    shortlist: list[ProbeResult] = []
    hosts: dict[str, int] = {}
    for result in sorted((r for r in screened.values() if r.success), key=rank_result):
        host = urlsplit(result.url).hostname or ""
        if hosts.get(host, 0) >= 3:
            continue
        shortlist.append(result)
        hosts[host] = hosts.get(host, 0) + 1
        if len(shortlist) >= 40:
            break
    measured: dict[str, ProbeResult] = {}
    for initial in shortlist:
        if cancellation.is_set() or time.monotonic() >= absolute_deadline:
            break
        if engine_path:
            from .benchmark import measure_engine
            speeds, received, attempted = [], 0, 0
            for _ in range(2):
                attempted += 1
                try:
                    count, speed = measure_engine(initial.url, engine_path, absolute_deadline, cancellation)
                    received += count
                    speeds.append(speed)
                except (OSError, ValueError, TimeoutError, InterruptedError):
                    break
            success = len(speeds) == 2 and not cancellation.is_set()
            score = round(min(speeds), 3) if success else 0.0
            measured[initial.url] = ProbeResult(
                initial.url, datetime.now(timezone.utc), score, received, success,
                "" if success else "sustained measurement failed", len(speeds) / 2,
                score, attempted, len(speeds), "sustained",
            )
        else:
            measured[initial.url] = probe_source(
                initial.url, timeout=min(timeout, 8), resolver=resolver,
                cancel_event=cancellation, connection_factory=connection_factory,
                deadline=absolute_deadline,
            )
    results: list[ProbeResult] = []
    for url in candidates:
        if url in measured:
            result = measured[url]
            initial = screened[url]
            result = ProbeResult(
                result.url, result.checked_at, result.probe_mbps,
                result.bytes_read + initial.bytes_read, result.success, result.error,
                result.success_rate, result.worst_mbps, result.attempts, result.successful_probes,
                result.stage,
            )
        else:
            initial = screened.get(url)
            error = "interrupted" if cancellation.is_set() else (
                initial.error if initial and not initial.success else "not shortlisted or publication deadline exceeded"
            )
            result = ProbeResult(url, datetime.now(timezone.utc), 0, initial.bytes_read if initial else 0, False, error)
            result = ProbeResult(
                result.url, result.checked_at, result.probe_mbps, result.bytes_read,
                result.success, result.error, stage="screen" if initial and not initial.success else "shortlist",
            )
        results.append(result)
    return results
