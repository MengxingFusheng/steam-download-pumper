from __future__ import annotations

import json
import math
import os
import re
import tempfile
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import time
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


TIME_RE = re.compile(r"^\d{2}:\d{2}$")
MAX_CONNECTIONS_PER_LINE = 12


def default_source_pool() -> list[str]:
    return [
        "http://mobile.shunicomtest.com:8080/speedtest/random4000x4000.jpg",
        "http://speedtest1.online.sh.cn:8080/speedtest/random4000x4000.jpg",
        "https://mirror.iscas.ac.cn/ubuntu-releases/24.04.4/ubuntu-24.04.4-live-server-amd64.iso",
        "https://mirrors.pku.edu.cn/ubuntu-releases/24.04.4/ubuntu-24.04.4-live-server-amd64.iso",
        "https://mirrors.huaweicloud.com/ubuntu-releases/24.04/ubuntu-24.04.4-live-server-amd64.iso",
    ]


@dataclass
class CommonConfig(ABC):
    target_mbps: int = 400
    connections_per_line: int = 8
    max_connections_per_line: int = MAX_CONNECTIONS_PER_LINE
    rate_limit_enabled: bool = True
    start_time: str = "00:00"
    end_time: str = "18:00"
    source_pool: list[str] = field(default_factory=default_source_pool)
    loop_pause_seconds: int = 0
    startup_stagger_seconds: float = 2.0
    worker_min_session_seconds: int = 300
    worker_restart_jitter_seconds: float = 3.0
    schedule_poll_seconds: int = 30
    log_level: str = "INFO"
    topology: str = field(init=False, default="")

    def validate_common(self) -> None:
        _require_int("target_mbps", self.target_mbps)
        _require_int("connections_per_line", self.connections_per_line)
        _require_int("max_connections_per_line", self.max_connections_per_line)
        _require_bool("rate_limit_enabled", self.rate_limit_enabled)
        _require_int("loop_pause_seconds", self.loop_pause_seconds)
        _require_finite_number("startup_stagger_seconds", self.startup_stagger_seconds)
        _require_int("worker_min_session_seconds", self.worker_min_session_seconds)
        _require_finite_number("worker_restart_jitter_seconds", self.worker_restart_jitter_seconds)
        _require_int("schedule_poll_seconds", self.schedule_poll_seconds)
        _require_string("log_level", self.log_level)
        if self.target_mbps < 1:
            raise ValueError("target_mbps must be at least 1")
        if self.connections_per_line < 1:
            raise ValueError("connections_per_line must be at least 1")
        if self.connections_per_line > MAX_CONNECTIONS_PER_LINE:
            raise ValueError(f"connections_per_line must be at most {MAX_CONNECTIONS_PER_LINE}")
        if self.max_connections_per_line < 1:
            raise ValueError("max_connections_per_line must be at least 1")
        if self.max_connections_per_line > MAX_CONNECTIONS_PER_LINE:
            raise ValueError(f"max_connections_per_line must be at most {MAX_CONNECTIONS_PER_LINE}")
        if self.max_connections_per_line < self.connections_per_line:
            raise ValueError("max_connections_per_line must be greater than or equal to connections_per_line")
        if self.loop_pause_seconds < 0:
            raise ValueError("loop_pause_seconds must be 0 or greater")
        if self.startup_stagger_seconds < 0:
            raise ValueError("startup_stagger_seconds must be 0 or greater")
        if self.worker_min_session_seconds < 1:
            raise ValueError("worker_min_session_seconds must be at least 1")
        if self.worker_restart_jitter_seconds < 0:
            raise ValueError("worker_restart_jitter_seconds must be 0 or greater")
        if self.schedule_poll_seconds < 1:
            raise ValueError("schedule_poll_seconds must be at least 1")
        self._parse_time(self.start_time, "start_time")
        self._parse_time(self.end_time, "end_time")
        self.source_pool = validate_source_pool(self.source_pool)

    @abstractmethod
    def validate(self) -> CommonConfig:
        raise NotImplementedError

    def is_within_window(self, current: time) -> bool:
        start = self._parse_time(self.start_time, "start_time")
        end = self._parse_time(self.end_time, "end_time")
        if start == end:
            return True
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def _parse_time(value: str, name: str) -> time:
        if not isinstance(value, str) or not TIME_RE.match(value):
            raise ValueError(f"{name} must use HH:MM format")
        hour_s, minute_s = value.split(":", 1)
        hour = int(hour_s)
        minute = int(minute_s)
        if hour > 23 or minute > 59:
            raise ValueError(f"{name} must be a valid 24-hour time")
        return time(hour, minute)


@dataclass
class IkuaiLineConfig(CommonConfig):
    topology: str = field(init=False, default="ikuai_line")

    def validate(self) -> IkuaiLineConfig:
        self.validate_common()
        return self


@dataclass
class MultiIPConfig(CommonConfig):
    target_mbps: int = 800
    line_count: int = 2
    lan_ips: list[str] = field(default_factory=lambda: ["192.168.1.233", "192.168.1.234"])
    topology: str = field(init=False, default="multi_ip")

    def validate(self) -> MultiIPConfig:
        self.validate_common()
        _require_int("line_count", self.line_count)
        if not 2 <= self.line_count <= 10:
            raise ValueError("line_count must be between 2 and 10")
        self.lan_ips = validate_unique_ipv4(self.lan_ips)
        if len(self.lan_ips) != self.line_count:
            raise ValueError("lan_ips must contain exactly line_count addresses")
        return self


def validate_source_pool(values: list[str]) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("source_pool must be a list of HTTP/HTTPS URLs")
    if any(not isinstance(value, str) for value in values):
        raise ValueError("source_pool items must be strings")
    cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    if not cleaned:
        raise ValueError("source_pool must contain at least one URL")
    for url in cleaned:
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
        except ValueError as exc:
            raise ValueError(f"source_pool contains an invalid HTTP/HTTPS URL: {url}") from exc
        if any(character.isspace() for character in url) or parsed.scheme.lower() not in {"http", "https"} or not hostname:
            raise ValueError(f"source_pool contains an invalid HTTP/HTTPS URL: {url}")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("source_pool URLs must not contain credentials")
    return cleaned


def validate_unique_ipv4(values: list[str]) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("lan_ips must be a list of IPv4 addresses")
    if any(not isinstance(value, str) for value in values):
        raise ValueError("lan_ips items must be strings")
    cleaned = [value.strip() for value in values]
    validated: list[str] = []
    for index, value in enumerate(cleaned, start=1):
        try:
            parsed = ip_address(value)
        except ValueError as exc:
            raise ValueError(f"lan_ips[{index}] must be a valid IPv4 address") from exc
        if parsed.version != 4:
            raise ValueError(f"lan_ips[{index}] must be a valid IPv4 address")
        validated.append(str(parsed))
    if len(set(validated)) != len(validated):
        raise ValueError("lan_ips must not contain duplicates")
    return validated


def _parse_bool(value: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value}")


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _require_int(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")


def _require_finite_number(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")


def _require_bool(name: str, value: Any) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")


def _require_string(name: str, value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")


COMMON_ENV_MAP = {
    "TARGET_MBPS": ("target_mbps", int),
    "CONNECTIONS_PER_LINE": ("connections_per_line", int),
    "MAX_CONNECTIONS_PER_LINE": ("max_connections_per_line", int),
    "RATE_LIMIT_ENABLED": ("rate_limit_enabled", _parse_bool),
    "START_TIME": ("start_time", str),
    "END_TIME": ("end_time", str),
    "SOURCE_POOL": ("source_pool", _parse_csv),
    "LOOP_PAUSE_SECONDS": ("loop_pause_seconds", int),
    "STARTUP_STAGGER_SECONDS": ("startup_stagger_seconds", float),
    "WORKER_MIN_SESSION_SECONDS": ("worker_min_session_seconds", int),
    "WORKER_RESTART_JITTER_SECONDS": ("worker_restart_jitter_seconds", float),
    "SCHEDULE_POLL_SECONDS": ("schedule_poll_seconds", int),
    "LOG_LEVEL": ("log_level", str),
}

TOPOLOGY_ENV_MAP = {
    "ikuai_line": {},
    "multi_ip": {
        "LINE_COUNT": ("line_count", int),
        "LAN_IPS": ("lan_ips", _parse_csv),
    },
}

CONFIG_TYPES: dict[str, type[CommonConfig]] = {
    "ikuai_line": IkuaiLineConfig,
    "multi_ip": MultiIPConfig,
}

REMOVED_ENV = {"EGRESS_MODE", "LAN_IP"}
IKUAI_UNSUPPORTED_ENV = {"LINE_COUNT", "LAN_IPS"}


def load_config(
    topology_name: str,
    path: str | Path,
    env: Mapping[str, str] | None = None,
) -> CommonConfig:
    config_type = CONFIG_TYPES.get(topology_name)
    if config_type is None:
        raise ValueError(f"unsupported topology: {topology_name}")

    environment = os.environ if env is None else env
    _reject_environment(topology_name, environment)
    data = _load_environment(topology_name, environment)

    config_path = Path(path)
    if config_path.exists():
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            raise ValueError("persisted config must be a JSON object")
        data.update(_validated_saved_data(topology_name, config_type, saved))

    return config_type(**data).validate()


def save_config(path: str | Path, cfg: IkuaiLineConfig | MultiIPConfig) -> None:
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.validate().to_dict()
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(data, temporary, indent=2, ensure_ascii=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, config_path)
        temporary_path = None
        _fsync_directory(config_path.parent)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reject_environment(topology_name: str, env: Mapping[str, str]) -> None:
    forbidden = set(REMOVED_ENV)
    if topology_name == "ikuai_line":
        forbidden.update(IKUAI_UNSUPPORTED_ENV)
    for env_name in sorted(forbidden):
        if env_name in env:
            raise ValueError(f"{env_name} is not supported by {topology_name}")


def _load_environment(topology_name: str, env: Mapping[str, str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    env_map = dict(COMMON_ENV_MAP)
    env_map.update(TOPOLOGY_ENV_MAP[topology_name])
    for env_name, (field_name, converter) in env_map.items():
        if env_name in env and env[env_name] != "":
            data[field_name] = converter(env[env_name])
    return data


def _validated_saved_data(
    topology_name: str,
    config_type: type[CommonConfig],
    saved: Mapping[str, Any],
) -> dict[str, Any]:
    validated = dict(saved)
    persisted_topology = validated.pop("topology", topology_name)
    if persisted_topology != topology_name:
        raise ValueError(
            f"persisted topology {persisted_topology!r} does not match requested topology {topology_name!r}"
        )

    allowed = {name for name, config_field in config_type.__dataclass_fields__.items() if config_field.init}
    for key in validated:
        if key not in allowed:
            if topology_name == "ikuai_line" and key in {"line_count", "lan_ips"}:
                raise ValueError(f"{key} is not supported by {topology_name}")
            raise ValueError(f"unknown persisted config key: {key}")
    return validated
