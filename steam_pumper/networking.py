from __future__ import annotations

import os
import subprocess
from collections.abc import Callable

from .config import PumperConfig


def apply_lan_ips(cfg: PumperConfig, log: Callable[[str], None] | None = None) -> None:
    if cfg.egress_mode != "multi_ip":
        return
    if os.environ.get("APPLY_LAN_IPS", "1").lower() in {"0", "false", "no", "off"}:
        return
    interface = os.environ.get("LAN_INTERFACE", "eth0")
    prefix = _subnet_prefix(os.environ.get("LAN_SUBNET", "192.168.1.0/24"))
    existing = _existing_ipv4_addresses(interface)
    for lan_ip in cfg.lan_ips:
        if lan_ip in existing:
            continue
        _run(["ip", "addr", "add", f"{lan_ip}/{prefix}", "dev", interface])
        existing.add(lan_ip)
        if log:
            log(f"attached lan_ip={lan_ip} to {interface}")


def _subnet_prefix(subnet: str) -> str:
    if "/" not in subnet:
        return "24"
    return subnet.rsplit("/", 1)[1] or "24"


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
