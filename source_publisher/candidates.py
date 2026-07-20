from __future__ import annotations

import ipaddress
import json
import socket
from pathlib import Path
from urllib.parse import urlsplit


MAX_CANDIDATE_FILE_BYTES = 256 * 1024
MAX_CANDIDATES = 200


def validate_source_url(url: object) -> str:
    if not isinstance(url, str) or not url or url != url.strip() or any(ch.isspace() for ch in url):
        raise ValueError("candidate URL must be a nonempty string without whitespace")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("candidate URL has an invalid port") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("candidate URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("candidate URL must not contain credentials")
    if parsed.fragment:
        raise ValueError("candidate URL must not contain a fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("candidate URL has an invalid port")
    return url


def load_candidates(path: Path) -> list[str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("candidate file is unavailable") from exc
    if not raw or len(raw) > MAX_CANDIDATE_FILE_BYTES:
        raise ValueError("candidate file is empty or too large")
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("candidate file is not valid JSON") from exc
    if not isinstance(document, dict) or set(document) != {"schema", "sources"}:
        raise ValueError("candidate file must contain only schema and sources")
    sources = document["sources"]
    if document["schema"] != 1 or not isinstance(sources, list):
        raise ValueError("candidate schema must be 1 with a sources list")
    if len(sources) > MAX_CANDIDATES:
        raise ValueError("candidate list exceeds 200 entries")

    result: list[str] = []
    seen: set[str] = set()
    for item in sources:
        if not isinstance(item, dict) or set(item) != {"url", "enabled"}:
            raise ValueError("candidate entry must contain url and enabled")
        if not isinstance(item["enabled"], bool):
            raise ValueError("candidate enabled value must be boolean")
        url = validate_source_url(item["url"])
        if url in seen:
            raise ValueError("candidate URLs must be unique")
        seen.add(url)
        if item["enabled"]:
            result.append(url)
    return result


def resolve_public_ipv4(host: str, port: int) -> tuple[str, ...]:
    try:
        answers = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("candidate host did not resolve to public IPv4") from exc
    addresses: list[str] = []
    for family, _type, _protocol, _canonical, sockaddr in answers:
        if family != socket.AF_INET:
            continue
        address = ipaddress.ip_address(sockaddr[0])
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
            raise ValueError("candidate host resolved to a non-public IPv4 address")
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise ValueError("candidate host did not resolve to public IPv4")
    return tuple(addresses)
