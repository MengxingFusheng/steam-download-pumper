from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from functools import partial
from urllib.parse import urlsplit
from collections import Counter

from .discovery import collect_candidates
from .dns import DoHResolver
from .candidates import resolve_public_ipv4
from .config import PublisherConfig, PublisherSecrets
from .manifest import build_payload, sign_payload, verify_envelope_with_private_key
from .oss import OSSClient, OSSFailure, OSSNotFound
from .probe import ProbeResult, pinned_request, probe_candidates, rank_result
from .ookla import is_ookla_download


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
    diagnostics: dict[str, object] | None = None


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
        self.last_diagnostics: dict[str, object] = {}

    def run(
        self,
        now: datetime,
        *,
        validate_only: bool = False,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
        force_republish: bool = False,
    ) -> PublicationResult:
        run_started = time.monotonic()

        def completion_time() -> datetime:
            return now + timedelta(seconds=max(0.0, time.monotonic() - run_started))

        cancellation = cancel_event or threading.Event()
        absolute_deadline = (
            deadline
            if deadline is not None
            else run_started + PUBLICATION_TIMEOUT_SECONDS
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
                if not force_republish and remote_generated.astimezone(self.config.timezone).date() == now.astimezone(
                    self.config.timezone
                ).date():
                    self._write_success_state(state_path, completion_time(), recovered)
                    return recovered

        started = time.monotonic()
        resolver = DoHResolver(self.config.doh_url, cancel_event=cancellation, deadline=absolute_deadline) if self.config.doh_url else resolve_public_ipv4
        accepted_urls = [item["url"] for item in json.loads(recovered.payload)["sources"]] if recovered else []
        discovered = collect_candidates(
            self.config.candidates_path, self.config.state_dir,
            accepted_urls=accepted_urls, enabled=self.config.discovery_enabled,
            ookla_enabled=self.config.ookla_discovery_enabled,
            ookla_search_terms=self.config.ookla_search_terms,
            deadline=absolute_deadline, cancel_event=cancellation,
            request_fn=partial(pinned_request, resolver=resolver),
        )
        results = self.probe_fn(
            discovered.urls,
            timeout=self.config.probe_timeout_seconds,
            concurrency=self.config.probe_concurrency,
            cancel_event=cancellation,
            deadline=absolute_deadline,
            resolver=resolver,
            engine_path=self.config.discarder_path,
        )
        _ensure_active(cancellation, absolute_deadline)
        eligible = [
            result for result in results
            if result.success and math.isfinite(result.probe_mbps) and result.probe_mbps >= 0
            and result.probe_mbps >= self.config.min_probe_mbps
        ]
        healthy: list[ProbeResult] = []
        hosts: Counter[str] = Counter()
        # Give every host its fastest measured endpoint before adding redundancy.
        ranked = sorted(eligible, key=rank_result)
        best_hosts: set[str] = set()
        first, additional = [], []
        for result in ranked:
            host = urlsplit(result.url).hostname or ""
            (additional if host in best_hosts else first).append(result)
            best_hosts.add(host)
        for result in [*first, *additional]:
            host = urlsplit(result.url).hostname or ""
            if hosts[host] >= 2 or any(item.url == result.url for item in healthy):
                continue
            hosts[host] += 1
            healthy.append(result)
            if len(healthy) >= self.config.max_healthy_sources:
                break
        diagnostics = {
            **discovered.diagnostics,
            "eligible_count": len(eligible), "published_count": len(healthy),
            "ookla_published_count": sum(is_ookla_download(result.url) for result in healthy),
            "host_count": len(hosts), "quantity_target_met": len(healthy) >= 12 and len(hosts) >= 8,
            "redundancy_degraded": len(hosts) < 3,
            "bytes_read": sum(result.bytes_read for result in results),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "failures": dict(Counter(result.error for result in results if not result.success)),
            "stage_failures": dict(Counter(result.stage + ": " + result.error for result in results if not result.success)),
            "low_speed_count": sum(result.success and result.probe_mbps < self.config.min_probe_mbps for result in results),
        }
        self.last_diagnostics = diagnostics
        atomic_write(self.config.state_dir / "last-probes.json", json.dumps({
            "attempted_at": now.isoformat(timespec="seconds"), **diagnostics,
            "sources": [{"url": r.url, "success": r.success, "probe_mbps": r.probe_mbps,
                         "worst_mbps": r.worst_mbps, "success_rate": r.success_rate,
                         "bytes_read": r.bytes_read, "error": r.error, "stage": r.stage,
                         "attempts": r.attempts, "successful_probes": r.successful_probes} for r in results],
        }, separators=(",", ":")).encode())
        if len(healthy) < self.config.min_healthy_sources:
            raise InsufficientSources("fewer than the required healthy sources")
        if recovered is not None:
            previous_revision = max(previous_revision, recovered.revision)
        payload, revision = build_payload(
            healthy,
            completion_time(),
            max_sources=self.config.max_healthy_sources,
            previous_revision=previous_revision,
            preserve_order=True,
        )
        if validate_only:
            return PublicationResult(revision, len(json.loads(payload)["sources"]), payload, None, diagnostics)
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

        result = PublicationResult(revision, len(json.loads(payload)["sources"]), payload, envelope, diagnostics)
        _ensure_active(cancellation, absolute_deadline)
        self._write_success_state(state_path, completion_time(), result)
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
            "diagnostics": result.diagnostics or {},
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
