from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TIME_RE = re.compile(r"^(\d{2}):(\d{2})$")
FIXED_PROBE_BYTES = 64 * 1024 * 1024
SECRET_DIR = Path("/run/secrets")


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _integer(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _https_url(value: str, name: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or value != value.strip()
    ):
        raise ValueError(f"{name} must be a valid HTTPS URL")
    return value.rstrip("/")


@dataclass(frozen=True)
class PublisherConfig:
    bucket: str
    region: str
    endpoint: str
    public_base_url: str
    key_id: str
    publish_time: time
    timezone: ZoneInfo
    retry_seconds: tuple[int, int, int]
    min_healthy_sources: int
    max_healthy_sources: int
    probe_concurrency: int
    probe_bytes: int
    probe_timeout_seconds: int
    min_probe_mbps: int
    candidates_path: Path
    state_dir: Path
    secret_dir: Path
    manifestctl_path: str
    ossutil_path: str
    log_level: str
    discovery_enabled: bool
    doh_url: str
    ookla_discovery_enabled: bool = True
    ookla_search_terms: tuple[str, ...] = ("Tianjin", "Beijing", "Shanghai")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "PublisherConfig":
        values = os.environ if env is None else env
        bucket = _required(values, "OSS_BUCKET")
        if not BUCKET_RE.fullmatch(bucket):
            raise ValueError("OSS_BUCKET must be a valid OSS bucket name")
        region = values.get("OSS_REGION", "cn-beijing").strip()
        if region != "cn-beijing":
            raise ValueError("OSS_REGION must be cn-beijing")
        endpoint = _https_url(_required(values, "OSS_ENDPOINT"), "OSS_ENDPOINT")
        public_base_url = _https_url(
            _required(values, "OSS_PUBLIC_BASE_URL"), "OSS_PUBLIC_BASE_URL"
        )
        if urlsplit(public_base_url).path != "/pumper/v1":
            raise ValueError("OSS_PUBLIC_BASE_URL path must be /pumper/v1")
        key_id = _required(values, "SOURCE_LIST_KEY_ID")
        if not KEY_ID_RE.fullmatch(key_id):
            raise ValueError("SOURCE_LIST_KEY_ID contains invalid characters")

        publish_raw = values.get("PUBLISH_TIME", "03:17").strip()
        match = TIME_RE.fullmatch(publish_raw)
        if match is None or int(match.group(1)) > 23 or int(match.group(2)) > 59:
            raise ValueError("PUBLISH_TIME must use HH:MM")
        publish_time = time(int(match.group(1)), int(match.group(2)))
        timezone_name = values.get("PUBLISH_TIMEZONE", "Asia/Shanghai").strip()
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("PUBLISH_TIMEZONE is unknown") from exc

        retry_raw = values.get("PUBLISH_RETRY_SECONDS", "900,3600,21600")
        try:
            retry_seconds = tuple(int(item.strip()) for item in retry_raw.split(","))
        except ValueError as exc:
            raise ValueError("PUBLISH_RETRY_SECONDS must contain integers") from exc
        if len(retry_seconds) != 3 or any(value <= 0 for value in retry_seconds):
            raise ValueError("PUBLISH_RETRY_SECONDS must contain three positive delays")

        minimum = _integer(values, "MIN_HEALTHY_SOURCES", 3)
        maximum = _integer(values, "MAX_HEALTHY_SOURCES", 30)
        concurrency = _integer(values, "PROBE_CONCURRENCY", 4)
        probe_bytes = _integer(values, "PROBE_BYTES", FIXED_PROBE_BYTES)
        timeout = _integer(values, "PROBE_TIMEOUT_SECONDS", 8)
        min_probe_mbps = _integer(values, "MIN_PROBE_MBPS", 100)
        if minimum < 3:
            raise ValueError("MIN_HEALTHY_SOURCES must be at least 3")
        # Previously deployed values of 100 are migrated to the new bound.
        if maximum < minimum or maximum > 100 or minimum > 30:
            raise ValueError("MAX_HEALTHY_SOURCES must be between the minimum and 100; minimum at most 30")
        if not 1 <= concurrency <= 8:
            raise ValueError("PROBE_CONCURRENCY must be between 1 and 8")
        if probe_bytes not in (8 * 1024 * 1024, FIXED_PROBE_BYTES):
            raise ValueError("PROBE_BYTES is fixed at 67108864 (legacy 8388608 is migrated)")
        if not 1 <= timeout <= 25:
            raise ValueError("PROBE_TIMEOUT_SECONDS must be between 1 and 25")
        if not 0 <= min_probe_mbps <= 10_000:
            raise ValueError("MIN_PROBE_MBPS must be between 0 and 10000")
        discovery = values.get("DISCOVERY_ENABLED", "true").strip().lower()
        if discovery not in {"true", "false"}:
            raise ValueError("DISCOVERY_ENABLED must be true or false")
        ookla = values.get("OOKLA_DISCOVERY_ENABLED", "true").strip().lower()
        if ookla not in {"true", "false"}:
            raise ValueError("OOKLA_DISCOVERY_ENABLED must be true or false")
        terms = tuple(dict.fromkeys(s.strip() for s in values.get(
            "OOKLA_SEARCH_TERMS", "Tianjin,Beijing,Shanghai").split(",") if s.strip()))
        if not terms or len(terms) > 10 or any(len(s) > 80 for s in terms):
            raise ValueError("OOKLA_SEARCH_TERMS requires 1 to 10 search terms of at most 80 characters")
        doh_url = values.get("PUBLISHER_DOH_URL", "").strip()
        if doh_url:
            doh_url = _https_url(doh_url, "PUBLISHER_DOH_URL")

        return cls(
            bucket=bucket,
            region=region,
            endpoint=endpoint,
            public_base_url=public_base_url,
            key_id=key_id,
            publish_time=publish_time,
            timezone=timezone,
            retry_seconds=retry_seconds,  # type: ignore[arg-type]
            min_healthy_sources=minimum,
            max_healthy_sources=maximum,
            probe_concurrency=concurrency,
            probe_bytes=FIXED_PROBE_BYTES,
            probe_timeout_seconds=min(timeout, 8),
            min_probe_mbps=min_probe_mbps,
            candidates_path=Path(values.get("CANDIDATES_PATH", "/config/candidates.json")),
            state_dir=Path(values.get("STATE_DIR", "/state")),
            secret_dir=SECRET_DIR,
            manifestctl_path=values.get("MANIFESTCTL_PATH", "/usr/local/bin/manifestctl"),
            ossutil_path=values.get("OSSUTIL_PATH", "/usr/local/bin/ossutil"),
            log_level=values.get("LOG_LEVEL", "INFO").upper(),
            discovery_enabled=discovery == "true",
            doh_url=doh_url,
            ookla_discovery_enabled=ookla == "true",
            ookla_search_terms=terms,
        )


def read_secret(path: Path, logical_name: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{logical_name} secret is unavailable") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"{logical_name} secret is not a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            value = handle.read(128 * 1024 + 1).strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{logical_name} secret is unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not value or len(value) > 128 * 1024:
        raise ValueError(f"{logical_name} secret is empty or too large")
    return value


@dataclass(frozen=True, repr=False)
class PublisherSecrets:
    signing_private_key: str
    oss_access_key_id: str
    oss_access_key_secret: str

    @classmethod
    def from_directory(cls, directory: Path) -> "PublisherSecrets":
        return cls(
            signing_private_key=read_secret(
                directory / "source_signing_private_key", "source signing private key"
            ),
            oss_access_key_id=read_secret(directory / "oss_access_key_id", "OSS access key ID"),
            oss_access_key_secret=read_secret(
                directory / "oss_access_key_secret", "OSS access key secret"
            ),
        )
