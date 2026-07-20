from __future__ import annotations

import fcntl
import json
import os
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
    local_success_date = (
        last_success.astimezone(now.tzinfo).date() if last_success is not None else None
    )
    succeeded_today = local_success_date == now.date()
    if succeeded_today:
        return datetime.combine(now.date() + timedelta(days=1), publish_time, tzinfo=now.tzinfo)
    if local_success_date is not None and local_success_date < now.date():
        return now
    if now >= today_due:
        return now
    return today_due


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
    if started is not None and now - started > timedelta(minutes=30):
        return False
    if success is not None:
        return now - success <= timedelta(hours=36) and success - now <= timedelta(minutes=1)
    return first_due is not None and now <= first_due + timedelta(hours=2)


def _write_health(path: Path, value: dict[str, object]) -> None:
    atomic_write(path, json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def run_scheduler(
    config: PublisherConfig,
    service: PublicationService,
    stop_event: threading.Event,
    *,
    now_fn: Callable[[], datetime] | None = None,
) -> int:
    clock = now_fn or (lambda: datetime.now(config.timezone))
    state = read_state(config.state_dir / "state.json")
    last_success = _parse_datetime(state.get("last_success_at"))
    started_at = clock()
    due = next_due(started_at, config.publish_time, last_success)
    health_path = config.state_dir / "health.json"
    health: dict[str, object] = {
        "heartbeat_at": started_at.isoformat(timespec="seconds"),
        "process_started_at": started_at.isoformat(timespec="seconds"),
        "first_due_at": due.isoformat(timespec="seconds"),
        "publication_started_at": "",
        "last_success_at": last_success.isoformat(timespec="seconds") if last_success else "",
    }
    failures = 0
    while not stop_event.is_set():
        now = clock()
        health["heartbeat_at"] = now.isoformat(timespec="seconds")
        _write_health(health_path, health)
        remaining = (due - now).total_seconds()
        if remaining > 0:
            interruptible_sleep(stop_event, min(remaining, 60))
            continue
        health["publication_started_at"] = now.isoformat(timespec="seconds")
        _write_health(health_path, health)
        try:
            service.run(now, cancel_event=stop_event)
        except Exception:
            if stop_event.is_set():
                break
            failures += 1
            due = clock() + timedelta(seconds=retry_delay(failures, config.retry_seconds))
        else:
            failures = 0
            last_success = clock()
            health["last_success_at"] = last_success.isoformat(timespec="seconds")
            due = next_due(last_success, config.publish_time, last_success)
        finally:
            health["publication_started_at"] = ""
    health["heartbeat_at"] = clock().isoformat(timespec="seconds")
    health["publication_started_at"] = ""
    _write_health(health_path, health)
    return 0
