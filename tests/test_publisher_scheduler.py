import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, time as wall_time
from pathlib import Path
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


class PublisherSchedulerTests(unittest.TestCase):
    def test_due_is_immediate_after_missed_run_and_tomorrow_after_success(self):
        from source_publisher.scheduler import next_due

        now = datetime(2026, 7, 20, 4, 0, tzinfo=SHANGHAI)
        self.assertEqual(next_due(now, wall_time(3, 17), None), now)
        success = datetime(2026, 7, 20, 3, 18, tzinfo=SHANGHAI)
        self.assertEqual(
            next_due(now, wall_time(3, 17), success),
            datetime(2026, 7, 21, 3, 17, tzinfo=SHANGHAI),
        )
        before = datetime(2026, 12, 31, 2, 0, tzinfo=SHANGHAI)
        self.assertEqual(next_due(before, wall_time(3, 17), None).date(), before.date())

    def test_previous_day_success_is_overdue_even_before_today_publish_time(self):
        from source_publisher.scheduler import next_due

        now = datetime(2026, 7, 20, 2, 0, tzinfo=SHANGHAI)
        yesterday = datetime(2026, 7, 19, 3, 18, tzinfo=SHANGHAI)
        self.assertEqual(next_due(now, wall_time(3, 17), yesterday), now)

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


if __name__ == "__main__":
    unittest.main()
