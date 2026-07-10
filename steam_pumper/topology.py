from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from .config import IkuaiLineConfig, MultiIPConfig, validate_unique_ipv4


@dataclass(frozen=True)
class LogicalLine:
    line_id: str
    target_mbps: int
    bind_ip: str = ""


def allocate_targets(total_mbps: int, line_count: int) -> list[int]:
    if line_count < 1:
        raise ValueError("line_count must be at least 1")
    if total_mbps < 0:
        raise ValueError("total_mbps must be 0 or greater")
    base, remainder = divmod(total_mbps, line_count)
    return [base + (1 if index < remainder else 0) for index in range(line_count)]


class IkuaiLineTopology:
    name = "ikuai_line"

    def lines(self, cfg: IkuaiLineConfig) -> list[LogicalLine]:
        return [LogicalLine(line_id="line-1", target_mbps=cfg.target_mbps)]

    def apply(self, cfg: IkuaiLineConfig, log: Callable[[str], None] | None = None) -> None:
        return None


class MultiIPTopology:
    name = "multi_ip"

    def lines(self, cfg: MultiIPConfig) -> list[LogicalLine]:
        targets = allocate_targets(cfg.target_mbps, cfg.line_count)
        return [
            LogicalLine(line_id=f"line-{index + 1}", target_mbps=targets[index], bind_ip=lan_ip)
            for index, lan_ip in enumerate(cfg.lan_ips)
        ]

    def apply(self, cfg: MultiIPConfig, log: Callable[[str], None] | None = None) -> None:
        cfg.validate()
        apply_ipv4_addresses(
            cfg.lan_ips,
            os.environ.get("LAN_INTERFACE", "eth0"),
            os.environ.get("LAN_PREFIX", "24"),
            log,
        )


def topology_for(name: str) -> IkuaiLineTopology | MultiIPTopology:
    if name == "ikuai_line":
        return IkuaiLineTopology()
    if name == "multi_ip":
        return MultiIPTopology()
    raise ValueError(f"unsupported topology: {name}")


def apply_ipv4_addresses(
    lan_ips: list[str],
    interface: str,
    prefix: str,
    log: Callable[[str], None] | None = None,
) -> None:
    validated_ips = validate_unique_ipv4(lan_ips)
    normalized_prefix = _validate_prefix(prefix)
    if os.environ.get("APPLY_LAN_IPS", "1").strip().lower() in {"0", "false", "no", "off"}:
        return

    existing = _existing_ipv4_addresses(interface)
    for lan_ip in validated_ips:
        if lan_ip in existing:
            continue
        try:
            _run(["ip", "addr", "add", f"{lan_ip}/{normalized_prefix}", "dev", interface])
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"failed to add IPv4 address {lan_ip} to {interface}") from exc
        existing.add(lan_ip)
        if log is not None:
            log(f"attached lan_ip={lan_ip} to {interface}")


def _validate_prefix(prefix: str) -> str:
    try:
        value = int(prefix)
    except (TypeError, ValueError) as exc:
        raise ValueError("LAN_PREFIX must be an IPv4 prefix between 0 and 32") from exc
    if not 0 <= value <= 32:
        raise ValueError("LAN_PREFIX must be an IPv4 prefix between 0 and 32")
    return str(value)


def _existing_ipv4_addresses(interface: str) -> set[str]:
    result = _run(["ip", "-4", "-o", "addr", "show", "dev", interface])
    addresses: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if "inet" not in parts:
            continue
        inet_index = parts.index("inet")
        if inet_index + 1 < len(parts):
            addresses.add(parts[inet_index + 1].split("/", 1)[0])
    return addresses


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)
