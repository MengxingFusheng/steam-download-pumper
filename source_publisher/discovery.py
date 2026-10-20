from __future__ import annotations

import json
import posixpath
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from itertools import zip_longest
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from .candidates import MAX_CANDIDATES, load_candidate_document, load_candidates, validate_source_url
from .probe import MAX_REDIRECTS, PinnedResponse, pinned_request
from .ookla import download_url, is_ookla_download


DEFAULT_DIRECTORIES = (
    {"kind": "librespeed", "url": "https://librespeed.org/backend-servers/servers.php", "enabled": True,
     "purpose": "Official LibreSpeed public speed-test backend directory"},
    {"kind": "ookla_api", "url": "https://www.speedtest.net/api/js/servers?engine=js&limit=100", "enabled": True,
     "purpose": "Ookla web server directory"},
)
DIRECTORY_BYTES = 2 * 1024 * 1024
CACHE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class DiscoveryResult:
    urls: list[str]
    diagnostics: dict[str, object]


def _directories(document: dict) -> list[dict]:
    custom = document.get("directories", [])
    if not isinstance(custom, list) or len(custom) > 30:
        raise ValueError("directories must be a list with at most 30 entries")
    directories = [dict(item) for item in DEFAULT_DIRECTORIES]
    for entry in custom:
        if not isinstance(entry, dict) or set(entry) - {"kind", "url", "enabled", "pattern", "purpose"}:
            raise ValueError("invalid discovery directory entry")
        kind = entry.get("kind")
        if kind not in {"librespeed", "ookla", "ookla_api", "mirror"} or not isinstance(entry.get("enabled", True), bool):
            raise ValueError("invalid discovery directory kind or enabled value")
        url = validate_source_url(entry.get("url"))
        if kind == "mirror":
            if not urlsplit(url).path.endswith("/") or urlsplit(url).query:
                raise ValueError("mirror directory must end in / without a query")
            if not isinstance(entry.get("purpose"), str) or not entry["purpose"].strip():
                raise ValueError("mirror directory needs an operator-specified purpose")
            pattern = entry.get("pattern", r".*\.(?:iso|img|bin)$")
            if not isinstance(pattern, str) or len(pattern) > 256:
                raise ValueError("invalid mirror file pattern")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError("invalid mirror file pattern") from exc
        # An explicit entry can disable or override a built-in directory.
        directories = [item for item in directories if (item["kind"], item["url"]) != (kind, url)]
        directories.append({**entry, "url": url})
    return [entry for entry in directories if entry.get("enabled", True)]


def _within_directory(url: str, base: str) -> bool:
    parsed, root = urlsplit(url), urlsplit(base)
    path = unquote(unquote(parsed.path))
    normalized = posixpath.normpath(path)
    return (parsed.scheme, parsed.netloc) == (root.scheme, root.netloc) and "\\" not in path and (
        normalized + ("/" if parsed.path.endswith("/") else "")
    ).startswith(root.path)


def _fetch_directory(entry: dict, deadline: float, cancel_event: threading.Event,
                     request_fn: Callable[..., PinnedResponse]) -> bytes:
    current = entry["url"]
    directory_deadline = min(deadline, time.monotonic() + 10)
    for redirects in range(MAX_REDIRECTS + 1):
        response = request_fn(
            current, headers={"Accept-Encoding": "identity", "User-Agent": "pumper-source-publisher/2",
                              "Accept": "application/json" if entry["kind"] == "ookla_api" else "*/*"},
            max_bytes=DIRECTORY_BYTES + 1, timeout=10, deadline=directory_deadline,
            cancel_event=cancel_event,
        )
        if response.status in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if not location or redirects == MAX_REDIRECTS:
                raise ValueError("directory redirect limit exceeded")
            current = validate_source_url(urljoin(current, location))
            if entry["kind"] == "mirror" and not _within_directory(current, entry["url"]):
                raise ValueError("mirror redirect left allowed directory")
            continue
        if response.status != 200:
            raise ValueError(f"directory HTTP {response.status}")
        if len(response.body) > DIRECTORY_BYTES:
            raise ValueError("directory exceeds size limit")
        return response.body
    raise ValueError("directory redirect limit exceeded")


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a" and len(self.links) < 1000:
            for name, value in attrs:
                if name == "href" and value:
                    self.links.append(value)


def _parse_directory(entry: dict, raw: bytes) -> list[str]:
    urls: list[str] = []
    if entry["kind"] == "librespeed":
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError("invalid LibreSpeed directory")
        for item in items[:2000]:
            if not isinstance(item, dict) or not isinstance(item.get("server"), str) or not isinstance(item.get("dlURL"), str):
                continue
            # dlURL is a path relative to the supplied backend root, not a guessed path.
            server = item["server"]
            if server.startswith("//"):
                server = "https:" + server
            download = urljoin(server.rstrip("/") + "/", item["dlURL"])
            parsed = urlsplit(download)
            # The upstream PHP backend defaults to only 4 MiB. Request 64 MiB
            # explicitly so a short default response cannot mask sustained speed.
            if parsed.path.rsplit("/", 1)[-1] == "garbage.php":
                query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "ckSize"]
                download = urlunsplit(parsed._replace(query=urlencode([*query, ("ckSize", "64")])))
            urls.append(download)
    elif entry["kind"] == "ookla_api":
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError("invalid Ookla server list")
        for server in items[:200]:
            if isinstance(server, dict):
                try:
                    urls.append(download_url(server))
                except (TypeError, ValueError):
                    continue
    elif entry["kind"] == "ookla":
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise ValueError("XML declarations are not allowed")
        root = ET.fromstring(raw)
        for server in list(root.iter("server"))[:10000]:
            upload = server.get("url", "")
            if upload:
                urls.append(urljoin(upload, "random4000x4000.jpg"))
    else:
        parser = _Links()
        parser.feed(raw.decode("utf-8", errors="replace"))
        pattern = re.compile(entry.get("pattern", r".*\.(?:iso|img|bin)$"))
        for link in parser.links:
            target = urljoin(entry["url"], link)
            if _within_directory(target, entry["url"]) and pattern.fullmatch(urlsplit(target).path.rsplit("/", 1)[-1]):
                urls.append(target)
    valid: list[str] = []
    for url in urls:
        try:
            valid.append(validate_source_url(url))
        except ValueError:
            continue
    return list(dict.fromkeys(valid))


def collect_candidates(path: Path, state_dir: Path, *, accepted_urls: Iterable[str] = (),
                       enabled: bool = True, deadline: float, cancel_event: threading.Event,
                       ookla_enabled: bool = True, ookla_search_terms: tuple[str, ...] = ("Tianjin", "Beijing", "Shanghai"),
                       request_fn: Callable[..., PinnedResponse] = pinned_request) -> DiscoveryResult:
    document = load_candidate_document(path)
    manual = load_candidates(path)
    disabled = {item["url"] for item in document["sources"] if not item["enabled"]}
    directories = _directories(document)
    api_entries = [entry for entry in directories if entry["kind"] == "ookla_api"]
    directories = [entry for entry in directories if entry["kind"] != "ookla_api"]
    if ookla_enabled:
        # Include the publisher's nearest servers plus searches for the node
        # regions. A single publisher-location query misses useful node routes.
        for entry in api_entries:
            for term in ("", *ookla_search_terms):
                directories.append({**entry, "url": entry["url"] + (
                    "&" if urlsplit(entry["url"]).query else "?") + urlencode({"search": term})})
    cache_path = state_dir / "discovered-sources.json"
    cache: dict = {}
    try:
        raw = cache_path.read_bytes()
        if len(raw) <= CACHE_BYTES:
            value = json.loads(raw)
            if isinstance(value, dict):
                cache = value
    except (OSError, ValueError):
        pass
    now = datetime.now(timezone.utc)
    discovered: list[str] = []
    details: list[dict] = []
    next_cache: dict = {}
    ookla_batches: list[list[str]] = []
    for entry in directories if enabled else []:
        key = entry["kind"] + ":" + entry["url"]
        cached = cache.get(key, {})
        urls: list[str] = []
        error = ""
        fetched_at = ""
        metadata: list[dict] = []
        try:
            if cancel_event.is_set() or time.monotonic() >= deadline:
                raise InterruptedError("discovery interrupted")
            raw = _fetch_directory(entry, deadline, cancel_event, request_fn)
            urls = _parse_directory(entry, raw)
            if entry["kind"] == "ookla_api":
                metadata = [{key: server.get(key) for key in ("id", "host", "name", "country", "sponsor")}
                            for server in json.loads(raw)[:200] if isinstance(server, dict)]
            if not urls:
                raise ValueError("directory has no usable download paths")
            fetched_at = now.isoformat(timespec="seconds")
        except Exception as exc:
            error = str(exc) if isinstance(exc, ValueError) else "directory unavailable"
            try:
                fetched = datetime.fromisoformat(cached["fetched_at"])
                if fetched.tzinfo and timedelta(0) <= now - fetched <= timedelta(days=7):
                    urls = [validate_source_url(url) for url in cached["urls"][:10000]]
                    fetched_at = cached["fetched_at"]
            except (KeyError, TypeError, ValueError):
                urls = []
        details.append({"kind": entry["kind"], "directory": entry["url"], "count": len(urls), "servers": metadata,
                        "cached": bool(error and urls), "error": error})
        if urls:
            next_cache[key] = {"fetched_at": fetched_at, "purpose": entry.get("purpose", ""), "urls": urls}
            discovered.extend(urls)
            if entry["kind"] == "ookla_api":
                ookla_batches.append(urls)
    # Manual candidates and the last signed manifest have priority. Explicitly
    # disabled manual entries veto the same URL from discovery and previous releases.
    merged: list[str] = []
    # Reserve candidate room for fresh direct endpoints ahead of historical
    # static URLs; a full old manifest must not starve discovery indefinitely.
    direct = list(dict.fromkeys(url for group in zip_longest(*ookla_batches)
                               for url in group if url and is_ookla_download(url)))
    if enabled and ookla_enabled:
        # Existing operator-provided legacy endpoints are useful regional seeds
        # when the web search API omits their region. Verify modern download
        # endpoints at those same hosts rather than losing the known routes.
        seeds = []
        for url in [*manual, *accepted_urls]:
            parsed = urlsplit(url)
            if parsed.path == "/speedtest/random4000x4000.jpg" and url not in disabled:
                seeds.append(download_url({"url": url, "https_functional": 1}))
                if parsed.scheme == "http":
                    seeds.append(download_url({"url": url, "https_functional": 0}))
        direct = list(dict.fromkeys([*seeds, *direct]))
    for url in [*direct[:160], *manual, *accepted_urls, *direct[160:], *discovered]:
        try:
            url = validate_source_url(url)
        except ValueError:
            continue
        if url not in disabled and url not in merged:
            merged.append(url)
        if len(merged) == MAX_CANDIDATES:
            break
    if enabled and not cancel_event.is_set() and time.monotonic() < deadline:
        from .service import atomic_write
        data = json.dumps(next_cache, separators=(",", ":")).encode()
        if len(data) <= CACHE_BYTES:
            atomic_write(cache_path, data)
    return DiscoveryResult(merged, {"manual_count": len(manual), "discovered_count": len(set(discovered)),
                                    "candidate_count": len(merged), "directories": details})
