from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .candidates import load_candidates
from .config import PublisherConfig, PublisherSecrets
from .manifest import build_payload, sign_payload, verify_envelope_with_private_key
from .oss import OSSClient, OSSFailure, OSSNotFound
from .probe import ProbeResult, probe_candidates


class InsufficientSources(RuntimeError):
    pass


class PublicationInterrupted(RuntimeError):
    pass


class PublicationDeadline(RuntimeError):
    pass


PUBLICATION_TIMEOUT_SECONDS = 30 * 60


def _ensure_active(cancel_event: threading.Event, deadline: float) -> None:
    if cancel_event.is_set():
        raise PublicationInterrupted("publication interrupted")
    if time.monotonic() >= deadline:
        raise PublicationDeadline("publication deadline exceeded")


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
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
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
        deadline: float | None = None,
    ) -> PublicationResult:
        cancellation = cancel_event or threading.Event()
        absolute_deadline = (
            deadline
            if deadline is not None
            else time.monotonic() + PUBLICATION_TIMEOUT_SECONDS
        )
        _ensure_active(cancellation, absolute_deadline)
        state_path = self.config.state_dir / "state.json"
        previous = read_state(state_path)
        try:
            previous_revision = int(previous.get("last_revision", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("publisher state revision is invalid") from exc
        client: OSSClient | None = None
        private_key_path: Path | None = None
        recovered: PublicationResult | None = None
        if not validate_only:
            if self.secrets is None:
                raise ValueError("publisher secrets are required")
            client = self.oss_client or OSSClient(self.config, self.secrets)
            private_key_path = self.config.secret_dir / "source_signing_private_key"
            recovered = self._recover_remote_commit(
                client, private_key_path, absolute_deadline, cancellation
            )
            if recovered is not None and recovered.revision >= previous_revision:
                remote_generated = datetime.fromisoformat(
                    json.loads(recovered.payload)["generated_at"]
                )
                if remote_generated.astimezone(self.config.timezone).date() == now.astimezone(
                    self.config.timezone
                ).date():
                    self._write_success_state(state_path, now, recovered)
                    return recovered

        urls = load_candidates(self.config.candidates_path)
        probe_deadline = min(absolute_deadline, time.monotonic() + 25.0)
        results = self.probe_fn(
            urls,
            timeout=self.config.probe_timeout_seconds,
            concurrency=self.config.probe_concurrency,
            cancel_event=cancellation,
            deadline=probe_deadline,
        )
        _ensure_active(cancellation, absolute_deadline)
        healthy = [result for result in results if result.success]
        if len(healthy) < self.config.min_healthy_sources:
            raise InsufficientSources("fewer than the required healthy sources")
        if recovered is not None:
            previous_revision = max(previous_revision, recovered.revision)
        payload, revision = build_payload(
            healthy,
            now,
            max_sources=self.config.max_healthy_sources,
            previous_revision=previous_revision,
        )
        if validate_only:
            return PublicationResult(revision, len(healthy), payload, None)
        assert private_key_path is not None
        assert client is not None
        envelope = self.sign_fn(
            payload,
            private_key_path,
            self.config.key_id,
            self.config.manifestctl_path,
            deadline=absolute_deadline,
            cancel_event=cancellation,
        )
        _ensure_active(cancellation, absolute_deadline)
        if self.verify_fn(
            envelope,
            private_key_path,
            self.config.key_id,
            self.config.manifestctl_path,
            deadline=absolute_deadline,
            cancel_event=cancellation,
        ) != payload:
            raise OSSFailure("local manifest verification failed")

        staging = self.config.state_dir / "staging"
        release_path = staging / f"{revision}.json"
        latest_path = staging / "latest.json"
        atomic_write(release_path, envelope)
        atomic_write(latest_path, envelope)
        relative_release = f"releases/{revision}.json"
        _ensure_active(cancellation, absolute_deadline)
        try:
            existing_release = client.read_public(
                relative_release,
                deadline=absolute_deadline,
                cancel_event=cancellation,
            )
        except (OSSNotFound, KeyError):
            _ensure_active(cancellation, absolute_deadline)
            client.upload(
                release_path,
                f"pumper/v1/{relative_release}",
                overwrite=False,
                deadline=absolute_deadline,
                cancel_event=cancellation,
            )
        else:
            self._verify_public(
                existing_release,
                envelope,
                payload,
                private_key_path,
                absolute_deadline,
                cancellation,
            )
        _ensure_active(cancellation, absolute_deadline)
        self._verify_public(
            client.read_public(
                relative_release,
                deadline=absolute_deadline,
                cancel_event=cancellation,
            ),
            envelope,
            payload,
            private_key_path,
            absolute_deadline,
            cancellation,
        )
        _ensure_active(cancellation, absolute_deadline)
        client.upload(
            latest_path,
            "pumper/v1/latest.json",
            overwrite=True,
            deadline=absolute_deadline,
            cancel_event=cancellation,
        )
        _ensure_active(cancellation, absolute_deadline)
        self._verify_public(
            client.read_public(
                "latest.json",
                deadline=absolute_deadline,
                cancel_event=cancellation,
            ),
            envelope,
            payload,
            private_key_path,
            absolute_deadline,
            cancellation,
        )

        result = PublicationResult(revision, len(healthy), payload, envelope)
        _ensure_active(cancellation, absolute_deadline)
        self._write_success_state(state_path, now, result)
        return result

    def _write_success_state(
        self, state_path: Path, now: datetime, result: PublicationResult
    ) -> None:
        successful_state = {
            "last_attempt_at": now.isoformat(timespec="seconds"),
            "last_success_at": now.isoformat(timespec="seconds"),
            "last_revision": result.revision,
            "last_source_count": result.source_count,
            "last_error": "",
            "consecutive_failures": 0,
            "next_retry_at": "",
        }
        atomic_write(
            state_path,
            json.dumps(successful_state, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        )

    def _recover_remote_commit(
        self,
        client: OSSClient,
        private_key_path: Path,
        deadline: float,
        cancel_event: threading.Event,
    ) -> PublicationResult | None:
        _ensure_active(cancel_event, deadline)
        try:
            latest = client.read_public(
                "latest.json", deadline=deadline, cancel_event=cancel_event
            )
        except (OSSNotFound, KeyError):
            return None
        payload = self.verify_fn(
            latest,
            private_key_path,
            self.config.key_id,
            self.config.manifestctl_path,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        try:
            document = json.loads(payload)
            revision = int(document["revision"])
            generated = datetime.fromisoformat(document["generated_at"])
            sources = document["sources"]
            if (
                not isinstance(document, dict)
                or document.get("schema") != 1
                or len(str(revision)) != 14
                or generated.tzinfo is None
                or not isinstance(sources, list)
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OSSFailure("remote latest payload is invalid") from exc
        _ensure_active(cancel_event, deadline)
        release = client.read_public(
            f"releases/{revision}.json",
            deadline=deadline,
            cancel_event=cancel_event,
        )
        self._verify_public(
            release,
            latest,
            payload,
            private_key_path,
            deadline,
            cancel_event,
        )
        return PublicationResult(revision, len(sources), payload, latest)

    def _verify_public(
        self,
        fetched: bytes,
        expected_envelope: bytes,
        expected_payload: bytes,
        private_key_path: Path,
        deadline: float,
        cancel_event: threading.Event,
    ) -> None:
        _ensure_active(cancel_event, deadline)
        if fetched != expected_envelope:
            raise OSSFailure("public manifest differs from uploaded manifest")
        verified = self.verify_fn(
            fetched,
            private_key_path,
            self.config.key_id,
            self.config.manifestctl_path,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        if verified != expected_payload:
            raise OSSFailure("public manifest verification failed")
