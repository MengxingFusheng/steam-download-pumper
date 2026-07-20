from __future__ import annotations

import http.client
import ipaddress
import math
import ssl
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit

from .candidates import resolve_public_ipv4, validate_source_url


PROBE_BYTES = 8 * 1024 * 1024
MIN_PROBE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_WORKERS = 4

Resolver = Callable[[str, int], tuple[str, ...]]
ConnectionFactory = Callable[[str, str, str, int, float], http.client.HTTPConnection]


@dataclass(frozen=True)
class ProbeResult:
    url: str
    checked_at: datetime
    probe_mbps: float
    bytes_read: int
    success: bool
    error: str


@dataclass(frozen=True)
class PinnedResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


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


def _validated_addresses(
    url: str, resolver: Resolver
) -> tuple[str, str, int, str, tuple[str, ...]]:
    parsed = urlsplit(validate_source_url(url))
    hostname = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    raw_addresses = tuple(resolver(hostname, port))
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
) -> PinnedResponse:
    scheme, hostname, port, target, addresses = _validated_addresses(url, resolver)
    default_port = 443 if scheme == "https" else 80
    host_header = hostname if port == default_port else f"{hostname}:{port}"
    request_headers = dict(headers)
    request_headers["Host"] = host_header
    connection = connection_factory(scheme, addresses[0], hostname, port, timeout)
    response: http.client.HTTPResponse | None = None
    try:
        connection.request("GET", target, headers=request_headers)
        response = connection.getresponse()
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        body = b"" if response.status in {301, 302, 303, 307, 308} else response.read(max_bytes)
        return PinnedResponse(response.status, response_headers, body)
    finally:
        if response is not None:
            response.close()
        connection.close()


def _probe_once(
    url: str,
    timeout: float,
    resolver: Resolver,
    cancel_event: threading.Event,
    connection_factory: ConnectionFactory,
) -> tuple[int, float]:
    current_url = url
    started = time.monotonic()
    for redirect_count in range(MAX_REDIRECTS + 1):
        if cancel_event.is_set():
            raise InterruptedError("probe interrupted")
        response = pinned_request(
            current_url,
            headers={
                "Accept-Encoding": "identity",
                "Range": f"bytes=0-{PROBE_BYTES - 1}",
                "User-Agent": "pumper-source-publisher/1",
            },
            max_bytes=PROBE_BYTES,
            timeout=max(0.1, timeout),
            resolver=resolver,
            connection_factory=connection_factory,
        )
        if response.status in {301, 302, 303, 307, 308}:
            location = response.headers.get("location", "")
            if redirect_count >= MAX_REDIRECTS or not location:
                raise ValueError("probe redirect limit exceeded")
            current_url = validate_source_url(urljoin(current_url, location))
            continue
        if response.status not in (200, 206):
            raise ValueError("unexpected HTTP status")
        total = len(response.body)
        if total < MIN_PROBE_BYTES:
            raise ValueError("probe response is shorter than 2 MiB")
        elapsed = max(time.monotonic() - started, 1e-9)
        mbps = total * 8 / elapsed / 1_000_000
        if not math.isfinite(mbps) or mbps < 0:
            raise ValueError("probe speed is invalid")
        return total, mbps
    raise ValueError("probe redirect limit exceeded")


def probe_source(
    url: str,
    *,
    timeout: float = 20,
    resolver: Resolver = resolve_public_ipv4,
    cancel_event: threading.Event | None = None,
    connection_factory: ConnectionFactory = _default_connection_factory,
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
                min(timeout, remaining),
                resolver,
                cancellation,
                connection_factory,
            )
            total_bytes += read
            speeds.append(mbps)
        speed = float(statistics.median(speeds))
        return ProbeResult(url, checked_at, round(speed, 3), total_bytes, True, "")
    except (
        OSError,
        ValueError,
        TimeoutError,
        InterruptedError,
        http.client.HTTPException,
        ssl.SSLError,
    ):
        reason = "interrupted" if cancellation.is_set() else "probe failed"
        return ProbeResult(url, checked_at, 0.0, total_bytes, False, reason)


def probe_candidates(
    urls: Iterable[str],
    *,
    timeout: float = 20,
    concurrency: int = 4,
    resolver: Resolver = resolve_public_ipv4,
    cancel_event: threading.Event | None = None,
    connection_factory: ConnectionFactory = _default_connection_factory,
) -> list[ProbeResult]:
    candidates = list(urls)
    cancellation = cancel_event or threading.Event()
    workers = min(MAX_WORKERS, max(1, concurrency))

    def run(url: str) -> ProbeResult:
        return probe_source(
            url,
            timeout=timeout,
            resolver=resolver,
            cancel_event=cancellation,
            connection_factory=connection_factory,
        )

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="publisher-probe") as pool:
        return list(pool.map(run, candidates))
