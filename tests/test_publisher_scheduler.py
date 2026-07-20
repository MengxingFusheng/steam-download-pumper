import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, time as wall_time
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch

from tests.test_publisher_config import BASE_ENV


SHANGHAI = ZoneInfo("Asia/Shanghai")


class PublisherSchedulerTests(unittest.TestCase):
    def _config(self, root, now):
        from source_publisher.config import PublisherConfig

        config = PublisherConfig.from_env({**BASE_ENV, "STATE_DIR": str(root)})
        return replace(
            config,
            publish_time=(now - timedelta(minutes=1)).timetz().replace(tzinfo=None),
        )

    def test_before_publish_time_waits_when_yesterdays_run_succeeded(self):
        from source_publisher.scheduler import next_due

        now = datetime(2026, 7, 20, 2, 0, tzinfo=SHANGHAI)
        yesterday = datetime(2026, 7, 19, 3, 18, tzinfo=SHANGHAI)
        self.assertEqual(
            next_due(now, wall_time(3, 17), yesterday),
            datetime(2026, 7, 20, 3, 17, tzinfo=SHANGHAI),
        )

    def test_after_publish_time_runs_immediately_unless_today_succeeded(self):
        from source_publisher.scheduler import next_due

        now = datetime(2026, 7, 20, 4, 0, tzinfo=SHANGHAI)
        yesterday = datetime(2026, 7, 19, 3, 18, tzinfo=SHANGHAI)
        self.assertEqual(next_due(now, wall_time(3, 17), yesterday), now)
        today = datetime(2026, 7, 20, 3, 18, tzinfo=SHANGHAI)
        self.assertEqual(
            next_due(now, wall_time(3, 17), today),
            datetime(2026, 7, 21, 3, 17, tzinfo=SHANGHAI),
        )

    def test_never_succeeded_waits_before_publish_time_and_runs_after(self):
        from source_publisher.scheduler import next_due

        before = datetime(2026, 7, 20, 2, 0, tzinfo=SHANGHAI)
        after = datetime(2026, 7, 20, 4, 0, tzinfo=SHANGHAI)
        self.assertEqual(
            next_due(before, wall_time(3, 17), None),
            datetime(2026, 7, 20, 3, 17, tzinfo=SHANGHAI),
        )
        self.assertEqual(next_due(after, wall_time(3, 17), None), after)

    def test_recent_due_comparison_handles_year_boundary(self):
        from source_publisher.scheduler import next_due

        now = datetime(2027, 1, 1, 2, 0, tzinfo=SHANGHAI)
        covered = datetime(2026, 12, 31, 3, 18, tzinfo=SHANGHAI)
        stale = datetime(2026, 12, 30, 3, 18, tzinfo=SHANGHAI)
        self.assertEqual(
            next_due(now, wall_time(3, 17), covered),
            datetime(2027, 1, 1, 3, 17, tzinfo=SHANGHAI),
        )
        self.assertEqual(next_due(now, wall_time(3, 17), stale), now)

    def test_retry_schedule_is_capped(self):
        from source_publisher.scheduler import retry_delay

        delays = (900, 3600, 21600)
        self.assertEqual([retry_delay(i, delays) for i in range(1, 6)], [900, 3600, 21600, 21600, 21600])

    def test_exclusive_lock_rejects_second_owner(self):
        from source_publisher.scheduler import LockHeld, exclusive_lock

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "publish.lock"
            with exclusive_lock(path):
                with self.assertRaises(LockHeld):
                    with exclusive_lock(path):
                        pass

    def test_interruptible_sleep_reacts_within_five_seconds(self):
        from source_publisher.scheduler import interruptible_sleep

        stop = threading.Event()
        started = time.monotonic()
        thread = threading.Thread(target=interruptible_sleep, args=(stop, 60))
        thread.start()
        stop.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 1)

    def test_health_allows_first_due_grace_then_requires_recent_success(self):
        from source_publisher.scheduler import health_is_healthy

        with tempfile.TemporaryDirectory() as directory:
            health_path = Path(directory) / "health.json"
            due = datetime(2026, 7, 20, 3, 17, tzinfo=SHANGHAI)
            now = due + timedelta(hours=1)
            health_path.write_text(json.dumps({
                "heartbeat_at": now.isoformat(),
                "process_started_at": (due - timedelta(minutes=1)).isoformat(),
                "first_due_at": due.isoformat(),
                "publication_started_at": "",
            }), encoding="utf-8")
            self.assertTrue(health_is_healthy(health_path, now))
            self.assertFalse(health_is_healthy(health_path, due + timedelta(hours=3)))
            value = json.loads(health_path.read_text(encoding="utf-8"))
            value["heartbeat_at"] = (due + timedelta(hours=3)).isoformat()
            value["last_success_at"] = (due + timedelta(hours=2, minutes=30)).isoformat()
            health_path.write_text(json.dumps(value), encoding="utf-8")
            self.assertTrue(health_is_healthy(health_path, due + timedelta(hours=3)))

    def test_active_publication_has_thirty_minute_health_grace(self):
        from source_publisher.scheduler import health_is_healthy

        with tempfile.TemporaryDirectory() as directory:
            health_path = Path(directory) / "health.json"
            now = datetime(2026, 7, 20, 4, 0, tzinfo=SHANGHAI)
            value = {
                "heartbeat_at": now.isoformat(),
                "process_started_at": (now - timedelta(hours=3)).isoformat(),
                "first_due_at": (now - timedelta(hours=3)).isoformat(),
                "publication_started_at": (now - timedelta(minutes=29)).isoformat(),
                "last_success_at": (now - timedelta(days=3)).isoformat(),
            }
            health_path.write_text(json.dumps(value), encoding="utf-8")
            self.assertTrue(health_is_healthy(health_path, now))
            value["publication_started_at"] = (now - timedelta(minutes=31)).isoformat()
            health_path.write_text(json.dumps(value), encoding="utf-8")
            self.assertFalse(health_is_healthy(health_path, now))

    def test_scheduler_refreshes_heartbeat_during_publication(self):
        from source_publisher import scheduler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(SHANGHAI)
            config = self._config(root, now)
            stop = threading.Event()
            health_writes = []
            real_write = scheduler._write_health

            def record_health(path, value):
                health_writes.append(dict(value))
                real_write(path, value)

            class SlowService:
                def run(self, _now, **_kwargs):
                    time.sleep(0.18)
                    stop.set()

            with patch(
                "source_publisher.scheduler._write_health", side_effect=record_health
            ):
                scheduler.run_scheduler(
                    config,
                    SlowService(),
                    stop,
                    heartbeat_interval_seconds=0.03,
                )
            active_writes = [
                item for item in health_writes if item.get("publication_started_at")
            ]
            self.assertGreaterEqual(len(active_writes), 3)

    def test_scheduler_persists_failure_and_restart_respects_backoff(self):
        from source_publisher.scheduler import run_scheduler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failed_at = datetime(2026, 7, 20, 4, 0, tzinfo=SHANGHAI)
            config = self._config(root, failed_at)
            stop = threading.Event()

            class FailingService:
                def run(self, _now, **_kwargs):
                    stop.set()
                    raise RuntimeError("SENSITIVE FAILURE DETAIL")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                run_scheduler(config, FailingService(), stop, now_fn=lambda: failed_at)
            state_path = root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["last_error"], "publication failed")
            self.assertEqual(state["consecutive_failures"], 1)
            retry_at = datetime.fromisoformat(state["next_retry_at"])
            self.assertEqual(retry_at, failed_at + timedelta(seconds=900))
            self.assertIn("publication_failed", stderr.getvalue())
            self.assertNotIn("SENSITIVE", stderr.getvalue())

            calls = []
            restart_stop = threading.Event()

            class RecordingService:
                def run(self, run_at, **_kwargs):
                    calls.append(run_at)

            def stop_sleep(_event, _seconds):
                restart_stop.set()
                return True

            run_scheduler(
                config,
                RecordingService(),
                restart_stop,
                now_fn=lambda: failed_at + timedelta(minutes=1),
                sleep_fn=stop_sleep,
            )
            self.assertEqual(calls, [])

    def test_scheduler_clears_persisted_failure_after_retry_success(self):
        from source_publisher.scheduler import run_scheduler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            retry_at = datetime(2026, 7, 20, 4, 15, tzinfo=SHANGHAI)
            config = self._config(root, retry_at)
            root.mkdir(parents=True, exist_ok=True)
            (root / "state.json").write_text(json.dumps({
                "last_success_at": (retry_at - timedelta(days=2)).isoformat(),
                "last_revision": 20260718031700,
                "last_source_count": 3,
                "last_error": "publication failed",
                "consecutive_failures": 1,
                "next_retry_at": retry_at.isoformat(),
            }), encoding="utf-8")
            stop = threading.Event()

            class SuccessfulService:
                def run(self, _now, **_kwargs):
                    stop.set()

            run_scheduler(
                config,
                SuccessfulService(),
                stop,
                now_fn=lambda: retry_at + timedelta(seconds=1),
            )
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["consecutive_failures"], 0)
            self.assertEqual(state["next_retry_at"], "")
            self.assertEqual(state["last_error"], "")
            self.assertEqual(state["last_revision"], 20260718031700)


if __name__ == "__main__":
    unittest.main()
