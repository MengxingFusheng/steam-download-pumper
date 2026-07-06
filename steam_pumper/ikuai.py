from __future__ import annotations

import json
import os
import ssl
import urllib.request
from typing import Any


def parse_interfaces_status(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [row for row in payload.get("iface_stream", []) if str(row.get("interface", "")).startswith("wan")]
    total_download = sum(_to_int(row.get("download")) for row in rows)
    parsed: list[dict[str, Any]] = []
    for row in rows:
        download = _to_int(row.get("download"))
        parsed.append(
            {
                "interface": row.get("interface", ""),
                "ip_addr": row.get("ip_addr", ""),
                "download_mbps": download * 8 / 1_000_000,
                "upload_mbps": _to_int(row.get("upload")) * 8 / 1_000_000,
                "connect_num": _to_int(row.get("connect_num")),
                "share_percent": (download / total_download * 100) if total_download else 0.0,
            }
        )
    return parsed


def fetch_interfaces_status() -> dict[str, Any]:
    base_url = os.environ.get("IKUAI_BASE_URL", "").rstrip("/")
    token = os.environ.get("IKUAI_TOKEN", "")
    if not base_url or not token:
        return {"enabled": False, "interfaces": [], "error": ""}
    request = urllib.request.Request(
        f"{base_url}/api/v4.0/monitoring/interfaces-status",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(request, timeout=2, context=context) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {"enabled": True, "interfaces": parse_interfaces_status(payload), "error": ""}
    except Exception as exc:
        return {"enabled": True, "interfaces": [], "error": str(exc)}


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
