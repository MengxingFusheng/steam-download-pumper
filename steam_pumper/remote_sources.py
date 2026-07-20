from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import os
import socket
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


DEFAULT_VERIFIER_PATH = "/usr/local/bin/manifestctl"
ENVELOPE_FILENAME = "source-list-envelope.json"
STATE_FILENAME = "source-list-state.json"
RETRY_DELAYS_SECONDS = (300, 1800, 7200, 21600)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _load_json(data: bytes | str, description: str) -> Any:
    try:
        return json.loads(data, object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid {description}: {exc}") from exc


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _parse_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed


def _aware_now(value: datetime | None = None) -> datetime:
    current = datetime.now().astimezone() if value is None else value
    if current.tzinfo is None or current.utcoffset() is None:
        return current.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return current


@dataclass(frozen=True)
class RemoteSourceSettings:
    enabled: bool
    url: str
    public_key: str
    key_id: str
    refresh_time: str = "04:00"
    refresh_jitter_seconds: int = 1800
    fetch_timeout_seconds: int = 15
    max_bytes: int = 524_288
    min_sources: int = 3

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> RemoteSourceSettings:
        enabled = _env_bool(env, "REMOTE_SOURCE_LIST_ENABLED", False)
        url = env.get("SOURCE_LIST_URL", "").strip()
        public_key = env.get("SOURCE_LIST_PUBLIC_KEY", "").strip()
        key_id = env.get("SOURCE_LIST_KEY_ID", "").strip()
        refresh_time = env.get("SOURCE_LIST_REFRESH_TIME", "04:00").strip()
        jitter = _env_int(env, "SOURCE_LIST_REFRESH_JITTER_SECONDS", 1800)
        timeout = _env_int(env, "SOURCE_LIST_FETCH_TIMEOUT_SECONDS", 15)
        max_bytes = _env_int(env, "SOURCE_LIST_MAX_BYTES", 524_288)
        min_sources = _env_int(env, "SOURCE_LIST_MIN_SOURCES", 3)

        try:
            datetime.strptime(refresh_time, "%H:%M")
        except ValueError as exc:
            raise ValueError("SOURCE_LIST_REFRESH_TIME must use HH:MM") from exc
        if not 0 <= jitter <= 3600:
            raise ValueError("SOURCE_LIST_REFRESH_JITTER_SECONDS must be between 0 and 3600")
        if not 1 <= timeout <= 300:
            raise ValueError("SOURCE_LIST_FETCH_TIMEOUT_SECONDS must be between 1 and 300")
        if not 1024 <= max_bytes <= 1_048_576:
            raise ValueError("SOURCE_LIST_MAX_BYTES must be between 1024 and 1048576")
        if not 3 <= min_sources <= 100:
            raise ValueError("SOURCE_LIST_MIN_SOURCES must be between 3 and 100")

        if enabled:
            parsed_url = urlsplit(url)
            if parsed_url.scheme.lower() != "https" or not parsed_url.hostname:
                raise ValueError("SOURCE_LIST_URL must be an HTTPS URL")
            if parsed_url.username or parsed_url.password or parsed_url.fragment:
                raise ValueError("SOURCE_LIST_URL cannot contain credentials or a fragment")
            try:
                decoded_key = base64.b64decode(public_key, validate=True)
            except (ValueError, base64.binascii.Error) as exc:
                raise ValueError("SOURCE_LIST_PUBLIC_KEY must be valid base64") from exc
            if len(decoded_key) != 32:
                raise ValueError("SOURCE_LIST_PUBLIC_KEY must decode to 32 bytes")
            if not key_id:
                raise ValueError("SOURCE_LIST_KEY_ID is required")

        return cls(
            enabled=enabled,
            url=url,
            public_key=public_key,
            key_id=key_id,
            refresh_time=refresh_time,
            refresh_jitter_seconds=jitter,
            fetch_timeout_seconds=timeout,
            max_bytes=max_bytes,
            min_sources=min_sources,
        )


@dataclass(frozen=True)
class RemoteSourceEntry:
    url: str
    checked_at: datetime
    probe_mbps: float


@dataclass(frozen=True)
class RemoteSourceManifest:
    revision: int
    generated_at: datetime
    expires_at: datetime
    sources: tuple[RemoteSourceEntry, ...]

    @property
    def urls(self) -> tuple[str, ...]:
        return tuple(source.url for source in self.sources)


@dataclass(frozen=True)
class SourceListSnapshot:
    enabled: bool = True
    status: str = "pending"
    revision: int = 0
    generated_at: str = ""
    expires_at: str = ""
    source_count: int = 0
    last_checked_at: str = ""
    last_success_at: str = ""
    next_refresh_at: str = ""
    etag: str = ""
    last_error: str = ""
    stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_source_url(
    raw_url: Any,
    resolver: Callable[..., list[tuple[Any, ...]]],
    *,
    resolve_dns: bool,
) -> str:
    if not isinstance(raw_url, str) or not raw_url:
        raise ValueError("source URL must be a non-empty string")
    parsed = urlsplit(raw_url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("source URL must use HTTP or HTTPS")
    if not parsed.hostname:
        raise ValueError("source URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("source URL cannot contain credentials")
    if parsed.fragment:
        raise ValueError("source URL cannot contain a fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("source URL has an invalid port") from exc

    hostname = parsed.hostname.lower()
    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        literal_address = None
    addresses: set[str] = set()
    if literal_address is not None:
        addresses.add(hostname)
    elif resolve_dns:
        try:
            infos = resolver(hostname, port, socket.AF_INET, socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"source hostname did not resolve: {hostname}") from exc
        addresses = {info[4][0] for info in infos if len(info) > 4 and info[4]}
        if not addresses:
            raise ValueError(f"source hostname has no public IPv4 address: {hostname}")
    for address in addresses:
        try:
            parsed_ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError(f"source hostname has an invalid IPv4 address: {address}") from exc
        if (
            parsed_ip.version != 4
            or not parsed_ip.is_global
            or parsed_ip.is_multicast
            or parsed_ip.is_loopback
            or parsed_ip.is_private
            or parsed_ip.is_link_local
            or parsed_ip.is_reserved
            or parsed_ip.is_unspecified
        ):
            raise ValueError(f"source hostname must resolve only to public IPv4 addresses: {hostname}")

    host = hostname if port is None else f"{hostname}:{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), host, path, parsed.query, ""))


def parse_manifest_payload(
    payload: bytes,
    *,
    min_sources: int,
    now: datetime | None = None,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    resolve_dns: bool = True,
    allow_expired: bool = False,
) -> RemoteSourceManifest:
    document = _load_json(payload, "manifest payload")
    if not isinstance(document, dict):
        raise ValueError("manifest payload must be a JSON object")
    required = {"schema", "revision", "generated_at", "expires_at", "sources"}
    if set(document) != required:
        raise ValueError("manifest payload has unsupported or missing fields")
    if document["schema"] != 1 or isinstance(document["schema"], bool):
        raise ValueError("manifest schema must equal 1")
    revision = document["revision"]
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or len(str(revision)) != 14
        or revision <= 0
    ):
        raise ValueError("manifest revision must be a positive 14-digit integer")
    try:
        datetime.strptime(str(revision), "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise ValueError("manifest revision must be a valid YYYYMMDDHHMMSS Asia/Shanghai timestamp") from exc

    current = _aware_now(now)
    generated_at = _parse_timestamp(document["generated_at"], "generated_at")
    expires_at = _parse_timestamp(document["expires_at"], "expires_at")
    if generated_at > current + timedelta(minutes=10):
        raise ValueError("manifest generated_at is too far in the future")
    if expires_at - generated_at != timedelta(hours=72):
        raise ValueError("manifest expires_at must be exactly 72 hours after generated_at")
    if not allow_expired and expires_at <= current:
        raise ValueError("manifest is expired")

    raw_sources = document["sources"]
    if not isinstance(raw_sources, list):
        raise ValueError("manifest sources must be a JSON array")
    if len(raw_sources) < min_sources:
        raise ValueError(f"manifest must contain at least {min_sources} sources")
    if len(raw_sources) > 100:
        raise ValueError("manifest cannot contain more than 100 sources")

    entries: list[RemoteSourceEntry] = []
    seen_urls: set[str] = set()
    for raw_entry in raw_sources:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {"url", "checked_at", "probe_mbps"}:
            raise ValueError("manifest source entries have unsupported or missing fields")
        normalized_url = _normalize_source_url(
            raw_entry["url"],
            resolver,
            resolve_dns=resolve_dns,
        )
        if normalized_url in seen_urls:
            raise ValueError(f"manifest contains duplicate source URL: {normalized_url}")
        seen_urls.add(normalized_url)
        checked_at = _parse_timestamp(raw_entry["checked_at"], "checked_at")
        if checked_at > generated_at + timedelta(minutes=10):
            raise ValueError("source checked_at is after publication")
        if checked_at < generated_at - timedelta(hours=24):
            raise ValueError("source checked_at is older than 24 hours at publication")
        probe_mbps = raw_entry["probe_mbps"]
        if (
            not isinstance(probe_mbps, (int, float))
            or isinstance(probe_mbps, bool)
            or not math.isfinite(float(probe_mbps))
            or probe_mbps < 0
        ):
            raise ValueError("source probe_mbps must be finite and non-negative")
        entries.append(
            RemoteSourceEntry(
                url=normalized_url,
                checked_at=checked_at,
                probe_mbps=float(probe_mbps),
            )
        )

    return RemoteSourceManifest(
        revision=revision,
        generated_at=generated_at,
        expires_at=expires_at,
        sources=tuple(entries),
    )


class RemoteSourceManager:
    def __init__(
        self,
        settings: RemoteSourceSettings,
        *,
        data_dir: str | Path = "/data",
        verifier_path: str | Path = DEFAULT_VERIFIER_PATH,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
        hostname: str | None = None,
    ) -> None:
        self.settings = settings
        self.data_dir = Path(data_dir)
        self.envelope_path = self.data_dir / ENVELOPE_FILENAME
        self.state_path = self.data_dir / STATE_FILENAME
        self.verifier_path = Path(verifier_path)
        self.urlopen = urlopen
        self.resolver = resolver
        stable_hostname = socket.gethostname() if hostname is None else hostname
        digest = hashlib.sha256(stable_hostname.encode("utf-8")).digest()
        self.stable_jitter_seconds = (
            int.from_bytes(digest[:8], "big") % (settings.refresh_jitter_seconds + 1)
            if settings.refresh_jitter_seconds
            else 0
        )
        self.current_urls: list[str] = []
        self.manifest: RemoteSourceManifest | None = None
        self.envelope_bytes: bytes | None = None
        self.snapshot = SourceListSnapshot(enabled=settings.enabled)
        self.failure_count = 0
        self.startup_refresh_pending = settings.enabled

    def load_last_known_good(
        self,
        now: datetime | None = None,
    ) -> tuple[list[str], SourceListSnapshot]:
        current = _aware_now(now)
        if not self.settings.enabled:
            self.snapshot = SourceListSnapshot(enabled=False, status="disabled")
            return [], self.snapshot

        state: dict[str, Any] | None = None
        state_error = ""
        try:
            state = self._read_state()
        except (OSError, ValueError, TypeError) as exc:
            if not isinstance(exc, FileNotFoundError):
                state_error = str(exc)

        envelope: bytes | None = None
        manifest: RemoteSourceManifest | None = None
        envelope_error = ""
        try:
            envelope = self.envelope_path.read_bytes()
            payload = self._verify_envelope(envelope)
            manifest = parse_manifest_payload(
                payload,
                min_sources=self.settings.min_sources,
                now=current,
                resolver=self.resolver,
                resolve_dns=False,
                allow_expired=True,
            )
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            if not isinstance(exc, FileNotFoundError):
                envelope_error = str(exc)

        if manifest is not None and envelope is not None:
            self.manifest = manifest
            self.envelope_bytes = envelope
            self.current_urls = list(manifest.urls)
            if state is not None and state["revision"] == manifest.revision:
                self.failure_count = state["failure_count"]
                self.snapshot = self._snapshot_for_manifest(manifest, current, state)
            else:
                self.failure_count = 0
                self.snapshot = self._snapshot_for_manifest(manifest, current)
                try:
                    self._save_state(self.snapshot, self.failure_count)
                except OSError:
                    pass
            self.startup_refresh_pending = True
            return list(self.current_urls), self.snapshot

        self.manifest = None
        self.envelope_bytes = None
        self.current_urls = []
        if state is not None and state["revision"] == 0:
            self.failure_count = state["failure_count"]
            last_error = self._state_text(state, "last_error")
            self.snapshot = SourceListSnapshot(
                enabled=True,
                status="error" if last_error else "pending",
                revision=0,
                last_checked_at=self._state_text(state, "last_checked_at"),
                last_success_at=self._state_text(state, "last_success_at"),
                next_refresh_at=self._state_text(state, "next_refresh_at"),
                etag=self._state_text(state, "etag"),
                last_error=last_error,
            )
            self.startup_refresh_pending = not bool(
                self.failure_count and self.snapshot.next_refresh_at
            )
        else:
            self.failure_count = 0
            self.snapshot = SourceListSnapshot(
                enabled=True,
                status="pending",
                last_error=envelope_error or state_error,
            )
            self.startup_refresh_pending = True
        return list(self.current_urls), self.snapshot

    def due(self, now: datetime | None = None) -> bool:
        if not self.settings.enabled:
            return False
        if self.startup_refresh_pending:
            return True
        current = _aware_now(now)
        if not self.snapshot.next_refresh_at:
            return True
        try:
            return current >= _parse_timestamp(self.snapshot.next_refresh_at, "next_refresh_at")
        except ValueError:
            return True

    def refresh(
        self,
        now: datetime | None = None,
    ) -> tuple[bool, list[str], SourceListSnapshot]:
        current = _aware_now(now)
        if not self.settings.enabled:
            self.snapshot = SourceListSnapshot(enabled=False, status="disabled")
            return False, list(self.current_urls), self.snapshot
        self.startup_refresh_pending = False
        checked_at = current.isoformat()
        previous_manifest = self.manifest
        previous_envelope = self.envelope_bytes
        previous_urls = list(self.current_urls)
        previous_snapshot = self.snapshot
        previous_failure_count = self.failure_count
        try:
            envelope, etag, not_modified = self._fetch_envelope()
            if not_modified:
                if previous_manifest is None:
                    raise ValueError("server returned 304 without a last-known-good source list")
                manifest = previous_manifest
            else:
                payload = self._verify_envelope(envelope)
                manifest = parse_manifest_payload(
                    payload,
                    min_sources=self.settings.min_sources,
                    now=current,
                    resolver=self.resolver,
                    resolve_dns=False,
                )
                if previous_manifest is not None and manifest.revision < previous_manifest.revision:
                    raise ValueError(
                        f"source list rollback rejected: {manifest.revision} < {previous_manifest.revision}"
                    )
                if previous_manifest is not None and manifest.revision == previous_manifest.revision:
                    if previous_envelope is None or envelope != previous_envelope:
                        raise ValueError("equal revision requires an identical accepted envelope and payload")

            changed = previous_manifest is None or manifest.urls != previous_manifest.urls
            stale = manifest.expires_at <= current
            next_refresh = self._next_daily_refresh(current)
            candidate_snapshot = SourceListSnapshot(
                enabled=True,
                status="stale" if stale else "ok",
                revision=manifest.revision,
                generated_at=manifest.generated_at.isoformat(),
                expires_at=manifest.expires_at.isoformat(),
                source_count=len(manifest.urls),
                last_checked_at=checked_at,
                last_success_at=checked_at,
                next_refresh_at=next_refresh.isoformat(),
                etag=etag or previous_snapshot.etag,
                stale=stale,
            )
            self._save_state(candidate_snapshot, 0)
            if not not_modified and manifest.revision != (
                previous_manifest.revision if previous_manifest is not None else None
            ):
                self._atomic_write(self.envelope_path, envelope)
            self.manifest = manifest
            self.envelope_bytes = previous_envelope if not_modified else envelope
            self.current_urls = list(manifest.urls)
            self.failure_count = 0
            self.snapshot = candidate_snapshot
            return changed, list(self.current_urls), self.snapshot
        except (OSError, ValueError, urllib.error.URLError, subprocess.SubprocessError) as exc:
            self.manifest = previous_manifest
            self.envelope_bytes = previous_envelope
            self.current_urls = previous_urls
            self.failure_count = previous_failure_count + 1
            delay = RETRY_DELAYS_SECONDS[min(self.failure_count - 1, len(RETRY_DELAYS_SECONDS) - 1)]
            stale = bool(previous_manifest and previous_manifest.expires_at <= current)
            self.snapshot = replace(
                previous_snapshot,
                enabled=True,
                status="stale" if stale else "error",
                last_checked_at=checked_at,
                next_refresh_at=(current + timedelta(seconds=delay)).isoformat(),
                last_error=str(exc),
                stale=stale,
            )
            try:
                self._save_state(self.snapshot, self.failure_count)
            except OSError:
                pass
            return False, list(self.current_urls), self.snapshot

    def _fetch_envelope(self) -> tuple[bytes, str, bool]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "multi-ip-pumper/2",
        }
        if self.snapshot.etag:
            headers["If-None-Match"] = self.snapshot.etag
        request = urllib.request.Request(self.settings.url, headers=headers, method="GET")
        try:
            with self.urlopen(request, timeout=self.settings.fetch_timeout_seconds) as response:
                if getattr(response, "status", 200) == 304:
                    return b"", self.snapshot.etag, True
                content_length = response.headers.get("Content-Length")
                if content_length is not None and int(content_length) > self.settings.max_bytes:
                    raise ValueError("source list envelope is too large")
                body = response.read(self.settings.max_bytes + 1)
                if len(body) > self.settings.max_bytes:
                    raise ValueError("source list envelope is too large")
                return body, response.headers.get("ETag", ""), False
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return b"", self.snapshot.etag, True
            raise
    def _verify_envelope(self, envelope: bytes) -> bytes:
        if len(envelope) > self.settings.max_bytes:
            raise ValueError("source list envelope is too large")
        document = _load_json(envelope, "source list envelope")
        if not isinstance(document, dict) or set(document) != {
            "key_id",
            "algorithm",
            "payload",
            "signature",
        }:
            raise ValueError("source list envelope has unsupported or missing fields")
        if document["key_id"] != self.settings.key_id:
            raise ValueError("source list envelope key_id does not match")
        if document["algorithm"] != "Ed25519":
            raise ValueError("source list envelope algorithm must be Ed25519")
        try:
            expected_payload = base64.b64decode(document["payload"], validate=True)
            signature = base64.b64decode(document["signature"], validate=True)
        except (TypeError, ValueError, base64.binascii.Error) as exc:
            raise ValueError("source list envelope contains invalid base64") from exc
        if len(signature) != 64:
            raise ValueError("source list envelope signature must be 64 bytes")
        if len(expected_payload) > self.settings.max_bytes:
            raise ValueError("source list payload is too large")
        completed = subprocess.run(
            [
                str(self.verifier_path),
                "verify",
                "--public-key-base64",
                self.settings.public_key,
                "--key-id",
                self.settings.key_id,
                "--max-bytes",
                str(self.settings.max_bytes),
            ],
            input=envelope,
            capture_output=True,
            check=False,
            timeout=self.settings.fetch_timeout_seconds,
        )
        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(f"manifest signature verification failed: {error or 'verifier rejected input'}")
        if len(completed.stdout) > self.settings.max_bytes:
            raise ValueError("verified source list payload is too large")
        if completed.stdout != expected_payload:
            raise ValueError("manifest verifier returned a different payload")
        return completed.stdout

    def _next_daily_refresh(self, now: datetime) -> datetime:
        hour, minute = (int(part) for part in self.settings.refresh_time.split(":"))
        scheduled = datetime.combine(now.date(), datetime_time(hour, minute), tzinfo=now.tzinfo)
        scheduled += timedelta(seconds=self.stable_jitter_seconds)
        return scheduled if now < scheduled else scheduled + timedelta(days=1)

    def _read_state(self) -> dict[str, Any]:
        raw_state = self.state_path.read_bytes()
        if len(raw_state) > self.settings.max_bytes:
            raise ValueError("source list state is too large")
        state = _load_json(raw_state, "source list state")
        if not isinstance(state, dict):
            raise ValueError("source list state must be a JSON object")
        revision = state.get("revision")
        failure_count = state.get("failure_count")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("source list state revision must be a non-negative integer")
        if (
            not isinstance(failure_count, int)
            or isinstance(failure_count, bool)
            or failure_count < 0
        ):
            raise ValueError("source list state failure_count must be a non-negative integer")
        return state

    @staticmethod
    def _state_text(state: Mapping[str, Any], field_name: str) -> str:
        value = state.get(field_name, "")
        return value if isinstance(value, str) else ""

    def _snapshot_for_manifest(
        self,
        manifest: RemoteSourceManifest,
        now: datetime,
        state: Mapping[str, Any] | None = None,
    ) -> SourceListSnapshot:
        metadata = {} if state is None else state
        stale = manifest.expires_at <= now
        last_error = self._state_text(metadata, "last_error")
        return SourceListSnapshot(
            enabled=True,
            status="stale" if stale else ("error" if last_error else "ok"),
            revision=manifest.revision,
            generated_at=manifest.generated_at.isoformat(),
            expires_at=manifest.expires_at.isoformat(),
            source_count=len(manifest.urls),
            last_checked_at=self._state_text(metadata, "last_checked_at"),
            last_success_at=self._state_text(metadata, "last_success_at"),
            next_refresh_at=self._state_text(metadata, "next_refresh_at"),
            etag=self._state_text(metadata, "etag"),
            last_error=last_error,
            stale=stale,
        )

    def _save_state(
        self,
        snapshot: SourceListSnapshot | None = None,
        failure_count: int | None = None,
    ) -> None:
        state_snapshot = self.snapshot if snapshot is None else snapshot
        state_failure_count = self.failure_count if failure_count is None else failure_count
        payload = {
            **state_snapshot.to_dict(),
            "failure_count": state_failure_count,
        }
        self._atomic_write(
            self.state_path,
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
        )

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, path)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_descriptor = os.open(path.parent, directory_flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise


class RemoteSourceRefreshWorker:
    """Run bounded remote refreshes away from the controller and HTTP server."""

    def __init__(self, manager: RemoteSourceManager) -> None:
        self.manager = manager
        self._condition = threading.Condition()
        self._state = "idle"
        self._requested_at: datetime | None = None
        self._result: tuple[bool, list[str], SourceListSnapshot] | Exception | None = None
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run,
            name="source-list-refresh",
            daemon=True,
        )
        self._thread.start()

    def request(self, now: datetime | None = None) -> str:
        with self._condition:
            if self._stopped:
                return "stopped"
            if self._state in {"queued", "running"}:
                return self._state
            if self._result is not None:
                return "completed"
            self._requested_at = _aware_now(now)
            self._state = "queued"
            self._condition.notify()
            return self._state

    def request_if_due(self, now: datetime | None = None) -> str:
        current = _aware_now(now)
        if not self.manager.due(current):
            return self.status()
        return self.request(current)

    def poll_result(self) -> tuple[bool, list[str], SourceListSnapshot] | Exception | None:
        with self._condition:
            result = self._result
            self._result = None
            return result

    def status(self) -> str:
        with self._condition:
            if self._result is not None:
                return "completed"
            return self._state

    def shutdown(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()
        settings = getattr(self.manager, "settings", None)
        timeout = getattr(settings, "fetch_timeout_seconds", 15)
        if not isinstance(timeout, (int, float)):
            timeout = 15
        self._thread.join(timeout=timeout + 1)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._stopped and self._state != "queued":
                    self._condition.wait()
                if self._stopped:
                    self._state = "stopped"
                    return
                requested_at = self._requested_at
                self._state = "running"
            try:
                result: tuple[bool, list[str], SourceListSnapshot] | Exception = (
                    self.manager.refresh(requested_at)
                )
            except Exception as exc:
                result = exc
            with self._condition:
                self._result = result
                self._state = "idle"
