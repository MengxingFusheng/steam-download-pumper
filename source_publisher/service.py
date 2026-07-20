from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .candidates import load_candidates
from .config import PublisherConfig, PublisherSecrets
from .manifest import build_payload, sign_payload, verify_envelope_with_private_key
from .oss import OSSClient, OSSFailure
from .probe import ProbeResult, probe_candidates


class InsufficientSources(RuntimeError):
    pass


class PublicationInterrupted(RuntimeError):
    pass


@dataclass(frozen=True)
class PublicationResult:
    revision: int
    source_count: int
    payload: bytes
    envelope: bytes | None


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_state(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError("publisher state is unreadable") from exc
    if len(raw) > 64 * 1024:
        raise ValueError("publisher state is too large")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("publisher state is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("publisher state is invalid")
    return value


class PublicationService:
    def __init__(
        self,
        config: PublisherConfig,
        secrets: PublisherSecrets | None = None,
        *,
        oss_client: OSSClient | None = None,
        probe_fn: Callable[..., list[ProbeResult]] = probe_candidates,
        sign_fn: Callable[..., bytes] = sign_payload,
        verify_fn: Callable[..., bytes] = verify_envelope_with_private_key,
    ) -> None:
        self.config = config
        self.secrets = secrets
        self.oss_client = oss_client
        self.probe_fn = probe_fn
        self.sign_fn = sign_fn
        self.verify_fn = verify_fn

    def run(
        self,
        now: datetime,
        *,
        validate_only: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> PublicationResult:
        cancellation = cancel_event or threading.Event()
        urls = load_candidates(self.config.candidates_path)
        results = self.probe_fn(
            urls,
            timeout=self.config.probe_timeout_seconds,
            concurrency=self.config.probe_concurrency,
            cancel_event=cancellation,
        )
        if cancellation.is_set():
            raise PublicationInterrupted("publication interrupted")
        healthy = [result for result in results if result.success]
        if len(healthy) < self.config.min_healthy_sources:
            raise InsufficientSources("fewer than the required healthy sources")
        state_path = self.config.state_dir / "state.json"
        previous = read_state(state_path)
        try:
            previous_revision = int(previous.get("last_revision", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("publisher state revision is invalid") from exc
        payload, revision = build_payload(
            healthy,
            now,
            max_sources=self.config.max_healthy_sources,
            previous_revision=previous_revision,
        )
        if validate_only:
            return PublicationResult(revision, len(healthy), payload, None)
        if self.secrets is None:
            raise ValueError("publisher secrets are required")

        private_key_path = self.config.secret_dir / "source_signing_private_key"
        envelope = self.sign_fn(
            payload, private_key_path, self.config.key_id, self.config.manifestctl_path
        )
        if self.verify_fn(
            envelope, private_key_path, self.config.key_id, self.config.manifestctl_path
        ) != payload:
            raise OSSFailure("local manifest verification failed")

        staging = self.config.state_dir / "staging"
        release_path = staging / f"{revision}.json"
        latest_path = staging / "latest.json"
        atomic_write(release_path, envelope)
        atomic_write(latest_path, envelope)
        client = self.oss_client or OSSClient(self.config, self.secrets)
        relative_release = f"releases/{revision}.json"
        client.upload(release_path, f"pumper/v1/{relative_release}")
        self._verify_public(client.read_public(relative_release), envelope, payload, private_key_path)
        client.upload(latest_path, "pumper/v1/latest.json")
        self._verify_public(client.read_public("latest.json"), envelope, payload, private_key_path)

        successful_state = {
            "last_attempt_at": now.isoformat(timespec="seconds"),
            "last_success_at": now.isoformat(timespec="seconds"),
            "last_revision": revision,
            "last_source_count": len(healthy),
            "last_error": "",
            "consecutive_failures": 0,
        }
        atomic_write(
            state_path,
            json.dumps(successful_state, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        )
        return PublicationResult(revision, len(healthy), payload, envelope)

    def _verify_public(
        self,
        fetched: bytes,
        expected_envelope: bytes,
        expected_payload: bytes,
        private_key_path: Path,
    ) -> None:
        if fetched != expected_envelope:
            raise OSSFailure("public manifest differs from uploaded manifest")
        verified = self.verify_fn(
            fetched, private_key_path, self.config.key_id, self.config.manifestctl_path
        )
        if verified != expected_payload:
            raise OSSFailure("public manifest verification failed")
