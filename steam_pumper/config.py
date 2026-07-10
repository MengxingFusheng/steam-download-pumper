from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import time
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Mapping


TIME_RE = re.compile(r"^\d{2}:\d{2}$")
MAX_CONNECTIONS_PER_LINE_CAP = 12


@dataclass
class PumperConfig:
    line_count: int = 2
    connections_per_line: int = 12
    max_connections_per_line: int = MAX_CONNECTIONS_PER_LINE_CAP
    rate_limit_enabled: bool = True
    rate_limit_mbps: int = 900
    target_mbps: int = 900
    start_time: str = "00:00"
    end_time: str = "18:00"
    source_pool: list[str] = field(
        default_factory=lambda: [
            "http://mobile.shunicomtest.com:8080/speedtest/random4000x4000.jpg",
            "http://speedtest1.online.sh.cn:8080/speedtest/random4000x4000.jpg",
            "http://5gzhenjiang.speedtest.jsinfo.net:8080/speedtest/random4000x4000.jpg",
            "http://4gsuzhou1.speedtest.jsinfo.net:8080/speedtest/random4000x4000.jpg",
        ]
    )
    download_urls: list[str] = field(
        default_factory=lambda: [
            "http://mobile.shunicomtest.com:8080/speedtest/random4000x4000.jpg",
            "http://speedtest1.online.sh.cn:8080/speedtest/random4000x4000.jpg",
            "http://5gzhenjiang.speedtest.jsinfo.net:8080/speedtest/random4000x4000.jpg",
            "http://4gsuzhou1.speedtest.jsinfo.net:8080/speedtest/random4000x4000.jpg",
        ]
    )
    loop_pause_seconds: int = 5
    startup_stagger_seconds: float = 2.0
    worker_min_session_seconds: int = 300
    worker_restart_jitter_seconds: float = 3.0
    schedule_poll_seconds: int = 30
    stats_interval_seconds: int = 5
    lan_ip: str = "192.168.1.233"
    lan_ips: list[str] = field(default_factory=lambda: ["192.168.1.233"])
    egress_mode: str = "single_ip"
    gateway: str = "192.168.1.1"
    log_level: str = "INFO"

    def validate(self) -> "PumperConfig":
        if self.line_count < 2 or self.line_count > 10:
            raise ValueError("line_count must be between 2 and 10")
        self.egress_mode = str(self.egress_mode).strip().lower()
        if self.egress_mode in {"single", "connection_balance", "connection_count"}:
            self.egress_mode = "single_ip"
        elif self.egress_mode in {"multi", "one_to_one", "one-to-one"}:
            self.egress_mode = "multi_ip"
        if self.egress_mode not in {"single_ip", "multi_ip"}:
            raise ValueError("egress_mode must be single_ip or multi_ip")
        self.lan_ip = str(self.lan_ip).strip()
        self.lan_ips = [str(ip).strip() for ip in self.lan_ips if str(ip).strip()]
        if not self.lan_ips:
            self.lan_ips = [self.lan_ip]
        self._validate_ipv4(self.lan_ip, "lan_ip")
        for index, lan_ip in enumerate(self.lan_ips, start=1):
            self._validate_ipv4(lan_ip, f"lan_ips[{index}]")
        if len(set(self.lan_ips)) != len(self.lan_ips):
            raise ValueError("lan_ips must not contain duplicates")
        if self.egress_mode == "multi_ip":
            if len(self.lan_ips) != self.line_count:
                raise ValueError("lan_ips must contain exactly line_count addresses in multi_ip mode")
            self.lan_ip = self.lan_ips[0]
        else:
            self.lan_ips = [self.lan_ip]
        if self.connections_per_line < 1:
            raise ValueError("connections_per_line must be at least 1")
        if self.connections_per_line > MAX_CONNECTIONS_PER_LINE_CAP:
            raise ValueError(f"connections_per_line must be at most {MAX_CONNECTIONS_PER_LINE_CAP}")
        if self.max_connections_per_line < 1:
            raise ValueError("max_connections_per_line must be at least 1")
        if self.max_connections_per_line > MAX_CONNECTIONS_PER_LINE_CAP:
            self.max_connections_per_line = MAX_CONNECTIONS_PER_LINE_CAP
        if self.max_connections_per_line < self.connections_per_line:
            raise ValueError("max_connections_per_line must be greater than or equal to connections_per_line")
        if self.target_mbps < 1:
            raise ValueError("target_mbps must be at least 1")
        self.rate_limit_mbps = self.target_mbps
        if self.rate_limit_mbps < 1:
            raise ValueError("rate_limit_mbps must be at least 1")
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
        if not self.source_pool and self.download_urls:
            self.source_pool = list(self.download_urls)
        for value_name in ("start_time", "end_time"):
            self._parse_time(getattr(self, value_name), value_name)
        self.source_pool = [str(url).strip() for url in self.source_pool if str(url).strip()]
        self.download_urls = [str(url).strip() for url in self.download_urls if str(url).strip()]
        if self.source_pool:
            self.download_urls = list(self.source_pool)
        elif self.download_urls:
            self.source_pool = list(self.download_urls)
        if not self.source_pool:
            raise ValueError("source_pool must contain at least one URL")
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

    @staticmethod
    def _validate_ipv4(value: str, name: str) -> None:
        try:
            parsed = ip_address(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be a valid IPv4 address") from exc
        if parsed.version != 4:
            raise ValueError(f"{name} must be a valid IPv4 address")


ENV_MAP = {
    "LINE_COUNT": ("line_count", int),
    "CONNECTIONS_PER_LINE": ("connections_per_line", int),
    "MAX_CONNECTIONS_PER_LINE": ("max_connections_per_line", int),
    "RATE_LIMIT_ENABLED": ("rate_limit_enabled", lambda value: str(value).lower() in {"1", "true", "yes", "on"}),
    "RATE_LIMIT_MBPS": ("rate_limit_mbps", int),
    "TARGET_MBPS": ("target_mbps", int),
    "START_TIME": ("start_time", str),
    "END_TIME": ("end_time", str),
    "SOURCE_POOL": ("source_pool", lambda value: [item.strip() for item in str(value).split(",") if item.strip()]),
    "DOWNLOAD_URLS": ("download_urls", lambda value: [item.strip() for item in str(value).split(",") if item.strip()]),
    "LOOP_PAUSE_SECONDS": ("loop_pause_seconds", int),
    "STARTUP_STAGGER_SECONDS": ("startup_stagger_seconds", float),
    "WORKER_MIN_SESSION_SECONDS": ("worker_min_session_seconds", int),
    "WORKER_RESTART_JITTER_SECONDS": ("worker_restart_jitter_seconds", float),
    "SCHEDULE_POLL_SECONDS": ("schedule_poll_seconds", int),
    "STATS_INTERVAL_SECONDS": ("stats_interval_seconds", int),
    "LAN_IP": ("lan_ip", str),
    "LAN_IPS": ("lan_ips", lambda value: [item.strip() for item in str(value).split(",") if item.strip()]),
    "EGRESS_MODE": ("egress_mode", str),
    "GATEWAY": ("gateway", str),
    "LOG_LEVEL": ("log_level", str),
}


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> PumperConfig:
    env = os.environ if env is None else env
    data: dict[str, Any] = {}
    config_path = Path(path)
    if config_path.exists():
        data.update(json.loads(config_path.read_text(encoding="utf-8")))
    allowed_fields = set(PumperConfig.__dataclass_fields__)
    data = {key: value for key, value in data.items() if key in allowed_fields}
    for env_name, (field_name, converter) in ENV_MAP.items():
        if env_name in env and env[env_name] != "":
            data[field_name] = converter(env[env_name])
    if "target_mbps" not in data and "rate_limit_mbps" in data:
        data["target_mbps"] = data["rate_limit_mbps"]
    if "source_pool" not in data and "download_urls" in data:
        data["source_pool"] = list(data["download_urls"])
    return PumperConfig(**data).validate()


def save_config(path: str | Path, cfg: PumperConfig) -> None:
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(cfg.validate().to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
