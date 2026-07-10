from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


TIME_RE = re.compile(r"^\d{2}:\d{2}$")
MAX_CONNECTIONS_CAP = 12
FORBIDDEN_KEYS = {
    "line_count",
    "egress_mode",
    "lan_ips",
    "lan_ip",
    "download_mode",
    "app_ids",
    "steam_username",
    "steam_password",
    "steam_guard_code",
}
FORBIDDEN_ENV = {
    "LINE_COUNT",
    "EGRESS_MODE",
    "LAN_IPS",
    "LAN_IP",
    "DOWNLOAD_MODE",
    "APP_IDS",
    "STEAM_USERNAME",
    "STEAM_PASSWORD",
    "STEAM_GUARD_CODE",
}


def default_source_pool() -> list[str]:
    return [
        "http://mobile.shunicomtest.com:8080/speedtest/random4000x4000.jpg",
        "http://speedtest1.online.sh.cn:8080/speedtest/random4000x4000.jpg",
        "http://5gzhenjiang.speedtest.jsinfo.net:8080/speedtest/random4000x4000.jpg",
        "http://4gsuzhou1.speedtest.jsinfo.net:8080/speedtest/random4000x4000.jpg",
    ]


@dataclass
class LineConfig:
    target_mbps: int = 400
    connections: int = 8
    max_connections: int = MAX_CONNECTIONS_CAP
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

    def validate(self) -> "LineConfig":
        if self.target_mbps < 1:
            raise ValueError("target_mbps must be at least 1")
        if self.connections < 1:
            raise ValueError("connections must be at least 1")
        if self.connections > MAX_CONNECTIONS_CAP:
            raise ValueError(f"connections must be at most {MAX_CONNECTIONS_CAP}")
        if self.max_connections < 1:
            raise ValueError("max_connections must be at least 1")
        if self.max_connections > MAX_CONNECTIONS_CAP:
            self.max_connections = MAX_CONNECTIONS_CAP
        if self.max_connections < self.connections:
            raise ValueError("max_connections must be greater than or equal to connections")
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
        self.source_pool = [str(url).strip() for url in self.source_pool if str(url).strip()]
        if not self.source_pool:
            raise ValueError("source_pool must contain at least one URL")
        for url in self.source_pool:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError(f"source_pool contains an invalid HTTP/HTTPS URL: {url}")
            if parsed.username or parsed.password:
                raise ValueError("source_pool URLs must not contain credentials")
        return self

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
        if not TIME_RE.match(value):
            raise ValueError(f"{name} must use HH:MM format")
        hour_s, minute_s = value.split(":", 1)
        hour = int(hour_s)
        minute = int(minute_s)
        if hour > 23 or minute > 59:
            raise ValueError(f"{name} must be a valid 24-hour time")
        return time(hour, minute)


ENV_MAP = {
    "TARGET_MBPS": ("target_mbps", int),
    "CONNECTIONS": ("connections", int),
    "MAX_CONNECTIONS": ("max_connections", int),
    "RATE_LIMIT_ENABLED": ("rate_limit_enabled", lambda value: str(value).lower() in {"1", "true", "yes", "on"}),
    "START_TIME": ("start_time", str),
    "END_TIME": ("end_time", str),
    "SOURCE_POOL": ("source_pool", lambda value: [item.strip() for item in str(value).split(",") if item.strip()]),
    "LOOP_PAUSE_SECONDS": ("loop_pause_seconds", int),
    "STARTUP_STAGGER_SECONDS": ("startup_stagger_seconds", float),
    "WORKER_MIN_SESSION_SECONDS": ("worker_min_session_seconds", int),
    "WORKER_RESTART_JITTER_SECONDS": ("worker_restart_jitter_seconds", float),
    "SCHEDULE_POLL_SECONDS": ("schedule_poll_seconds", int),
    "LOG_LEVEL": ("log_level", str),
}


def load_line_config(path: str | Path, env: Mapping[str, str] | None = None) -> LineConfig:
    env = os.environ if env is None else env
    _reject_forbidden_env(env)
    data: dict[str, Any] = {}
    for env_name, (field_name, converter) in ENV_MAP.items():
        if env_name in env and env[env_name] != "":
            data[field_name] = converter(env[env_name])
    config_path = Path(path)
    if config_path.exists():
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        _reject_forbidden_keys(saved)
        data.update(saved)
    return LineConfig(**data).validate()


def save_line_config(path: str | Path, cfg: LineConfig) -> None:
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(cfg.validate().to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


def _reject_forbidden_env(env: Mapping[str, str]) -> None:
    for env_name in FORBIDDEN_ENV:
        if env_name in env and env[env_name] != "":
            raise ValueError(f"{env_name} is not supported by ikuai-line-pumper")


def _reject_forbidden_keys(data: Mapping[str, Any]) -> None:
    for key in FORBIDDEN_KEYS:
        if key in data:
            raise ValueError(f"{key} is not supported by ikuai-line-pumper")
