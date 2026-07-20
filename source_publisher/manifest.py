from __future__ import annotations

import base64
import json
import math
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from .probe import ProbeResult


MAX_MANIFEST_BYTES = 512 * 1024
SHANGHAI = ZoneInfo("Asia/Shanghai")


class ManifestError(RuntimeError):
    pass


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("manifest timestamps must be timezone-aware")
    return value.astimezone(SHANGHAI).isoformat(timespec="seconds")


def build_payload(
    results: Iterable[ProbeResult],
    generated_at: datetime,
    *,
    max_sources: int = 100,
    previous_revision: int = 0,
) -> tuple[bytes, int]:
    generated = generated_at.astimezone(SHANGHAI)
    revision = int(generated.strftime("%Y%m%d%H%M%S"))
    if revision <= previous_revision:
        revision = previous_revision + 1
    healthy = [
        result
        for result in results
        if result.success and math.isfinite(result.probe_mbps) and result.probe_mbps >= 0
    ]
    healthy.sort(key=lambda result: (-result.probe_mbps, result.url))
    sources = [
        {
            "url": result.url,
            "checked_at": _timestamp(result.checked_at),
            "probe_mbps": round(result.probe_mbps, 3),
        }
        for result in healthy[:max_sources]
    ]
    document = {
        "schema": 1,
        "revision": revision,
        "generated_at": _timestamp(generated),
        "expires_at": _timestamp(generated + timedelta(hours=72)),
        "sources": sources,
    }
    payload = json.dumps(document, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest payload exceeds 512 KiB")
    return payload, revision


def _child_environment() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}


def _parse_envelope(envelope: bytes, expected_key_id: str) -> None:
    if not envelope or len(envelope) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest envelope is empty or too large")
    try:
        value = json.loads(envelope)
        if not isinstance(value, dict) or set(value) != {
            "key_id", "algorithm", "payload", "signature"
        }:
            raise ValueError
        if value["key_id"] != expected_key_id or value["algorithm"] != "Ed25519":
            raise ValueError
        base64.b64decode(value["payload"], validate=True)
        signature = base64.b64decode(value["signature"], validate=True)
        if len(signature) != 64:
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ManifestError("manifest envelope is invalid") from exc


def sign_payload(
    payload: bytes, private_key_path: Path, key_id: str, manifestctl_path: str
) -> bytes:
    command = [
        manifestctl_path,
        "sign",
        "--private-key",
        str(private_key_path),
        "--key-id",
        key_id,
    ]
    try:
        completed = subprocess.run(
            command,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_child_environment(),
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManifestError("manifest signing failed") from exc
    envelope = completed.stdout.strip()
    if completed.returncode != 0:
        raise ManifestError("manifest signing failed")
    _parse_envelope(envelope, key_id)
    return envelope


def verify_envelope(
    envelope: bytes, public_key_base64: str, key_id: str, manifestctl_path: str
) -> bytes:
    command = [
        manifestctl_path,
        "verify",
        "--public-key-base64",
        public_key_base64,
        "--key-id",
        key_id,
        "--max-bytes",
        str(MAX_MANIFEST_BYTES),
    ]
    _parse_envelope(envelope, key_id)
    try:
        completed = subprocess.run(
            command,
            input=envelope,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_child_environment(),
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManifestError("manifest verification failed") from exc
    if completed.returncode != 0 or len(completed.stdout) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest verification failed")
    return completed.stdout


def verify_envelope_with_private_key(
    envelope: bytes, private_key_path: Path, key_id: str, manifestctl_path: str
) -> bytes:
    command = [
        manifestctl_path,
        "verify",
        "--private-key",
        str(private_key_path),
        "--key-id",
        key_id,
        "--max-bytes",
        str(MAX_MANIFEST_BYTES),
    ]
    _parse_envelope(envelope, key_id)
    try:
        completed = subprocess.run(
            command,
            input=envelope,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_child_environment(),
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManifestError("manifest verification failed") from exc
    if completed.returncode != 0 or len(completed.stdout) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest verification failed")
    return completed.stdout
