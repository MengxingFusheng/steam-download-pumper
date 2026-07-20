from __future__ import annotations

import fcntl
import json
import os
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable, Iterator

from .config import PublisherConfig
from .service import PublicationService, atomic_write, read_state


class LockHeld(RuntimeError):
    pass


def next_due(now: datetime, publish_time: time, last_success: datetime | None) -> datetime:
    if now.tzinfo is None:
        raise ValueError("scheduler clock must be timezone-aware")
    today_due = datetime.combine(now.date(), publish_time, tzinfo=now.tzinfo)
    if now < today_due:
        most_recent_due = datetime.combine(
            now.date() - timedelta(days=1), publish_time, tzinfo=now.tzinfo
        )
        next_scheduled = today_due
    else:
        most_recent_due = today_due
        next_scheduled = datetime.combine(
            now.date() + timedelta(days=1), publish_time, tzinfo=now.tzinfo
        )
    if last_success is None:
        return next_scheduled if now < today_due else now
    if last_success.astimezone(now.tzinfo) < most_recent_due:
        return now
    return next_scheduled


def retry_delay(failure_count: int, delays: tuple[int, int, int]) -> int:
    if failure_count < 1:
        raise ValueError("failure count must be positive")
    return delays[min(failure_count - 1, len(delays) - 1)]


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockHeld("publisher lock is already held") from exc
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def interruptible_sleep(stop_event: threading.Event, seconds: float) -> bool:
    return stop_event.wait(max(0.0, seconds))


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def health_is_healthy(health_path: Path, now: datetime) -> bool:
    try:
        raw = health_path.read_bytes()
        if len(raw) > 64 * 1024:
            return False
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    heartbeat = _parse_datetime(value.get("heartbeat_at"))
    first_due = _parse_datetime(value.get("first_due_at"))
    started = _parse_datetime(value.get("publication_started_at"))
    success = _parse_datetime(value.get("last_success_at"))
    if heartbeat is None or now - heartbeat > timedelta(minutes=5) or heartbeat - now > timedelta(minutes=1):
        return False
    if started is not None:
        active_for = now - started
        return timedelta(0) <= active_for <= timedelta(minutes=30)
    if success is not None:
        return now - success <= timedelta(hours=36) and success - now <= timedelta(minutes=1)
    return first_due is not None and now <= first_due + timedelta(hours=2)


def _write_health(path: Path, value: dict[str, object]) -> None:
    atomic_write(path, json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _write_state(path: Path, updates: dict[str, object]) -> dict[str, object]:
    state = read_state(path)
    state.update(updates)
    atomic_write(
        path,
        json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8"),
    )
    return state


def _log_event(event: str, **fields: object) -> None:
    document = {"event": event, **fields}
    print(json.dumps(document, separators=(",", ":"), sort_keys=True), file=sys.stderr)


def run_scheduler(
    config: PublisherConfig,
    service: PublicationService,
    stop_event: threading.Event,
    *,
    now_fn: Callable[[], datetime] | None = None,
    sleep_fn: Callable[[threading.Event, float], bool] = interruptible_sleep,
    heartbeat_interval_seconds: float = 60.0,
) -> int:
    clock = now_fn or (lambda: datetime.now(config.timezone))
    state_path = config.state_dir / "state.json"
    state = read_state(state_path)
    last_success = _parse_datetime(state.get("last_success_at"))
    started_at = clock()
    due = next_due(started_at, config.publish_time, last_success)
    try:
        failures = max(0, int(state.get("consecutive_failures", 0)))
    except (TypeError, ValueError):
        failures = 0
    persisted_retry = _parse_datetime(state.get("next_retry_at"))
    if failures and persisted_retry is not None:
        due = max(started_at, persisted_retry.astimezone(started_at.tzinfo))
    health_path = config.state_dir / "health.json"
    health: dict[str, object] = {
        "heartbeat_at": started_at.isoformat(timespec="seconds"),
        "process_started_at": started_at.isoformat(timespec="seconds"),
        "first_due_at": due.isoformat(timespec="seconds"),
        "publication_started_at": "",
        "last_success_at": last_success.isoformat(timespec="seconds") if last_success else "",
    }
    health_lock = threading.Lock()

    def write_health() -> None:
        with health_lock:
            _write_health(health_path, dict(health))

    while not stop_event.is_set():
        now = clock()
        health["heartbeat_at"] = now.isoformat(timespec="seconds")
        write_health()
        remaining = (due - now).total_seconds()
        if remaining > 0:
            sleep_fn(stop_event, min(remaining, 60))
            continue
        health["publication_started_at"] = now.isoformat(timespec="seconds")
        write_health()
        publication_done = threading.Event()

        def pulse_heartbeat() -> None:
            while not publication_done.wait(max(0.01, heartbeat_interval_seconds)):
                with health_lock:
                    health["heartbeat_at"] = clock().isoformat(timespec="seconds")
                    _write_health(health_path, dict(health))

        heartbeat_thread = threading.Thread(
            target=pulse_heartbeat,
            name="publisher-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            service.run(now, cancel_event=stop_event)
        except Exception:
            if not stop_event.is_set():
                failures += 1
                failed_at = clock()
                due = failed_at + timedelta(
                    seconds=retry_delay(failures, config.retry_seconds)
                )
                _write_state(state_path, {
                    "last_attempt_at": failed_at.isoformat(timespec="seconds"),
                    "last_error": "publication failed",
                    "consecutive_failures": failures,
                    "next_retry_at": due.isoformat(timespec="seconds"),
                })
                _log_event(
                    "publication_failed",
                    consecutive_failures=failures,
                    next_retry_at=due.isoformat(timespec="seconds"),
                )
        else:
            failures = 0
            last_success = clock()
            health["last_success_at"] = last_success.isoformat(timespec="seconds")
            _write_state(state_path, {
                "last_attempt_at": last_success.isoformat(timespec="seconds"),
                "last_success_at": last_success.isoformat(timespec="seconds"),
                "last_error": "",
                "consecutive_failures": 0,
                "next_retry_at": "",
            })
            due = next_due(last_success, config.publish_time, last_success)
        finally:
            publication_done.set()
            heartbeat_thread.join()
            with health_lock:
                health["publication_started_at"] = ""
        if stop_event.is_set():
            break
    health["heartbeat_at"] = clock().isoformat(timespec="seconds")
    health["publication_started_at"] = ""
    write_health()
    return 0
