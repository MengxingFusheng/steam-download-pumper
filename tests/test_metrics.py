import unittest

from steam_pumper.config import PumperConfig
from steam_pumper.metrics import ThroughputTracker, next_worker_count, theoretical_window_bytes


class MetricsTests(unittest.TestCase):
    def test_tracker_computes_current_and_window_averages(self):
        tracker = ThroughputTracker()

        tracker.record(100.0, 1_000_000)
        tracker.record(101.0, 11_000_000)
        tracker.record(102.0, 21_000_000)

        self.assertEqual(round(tracker.current_mbps, 1), 80.0)
        self.assertEqual(round(tracker.average_mbps(10), 1), 80.0)
        self.assertEqual(tracker.today_bytes, 20_000_000)

    def test_tracker_resets_daily_counter_on_new_day(self):
        tracker = ThroughputTracker()

        tracker.record(100.0, 1_000_000, day="2026-07-06")
        tracker.record(101.0, 11_000_000, day="2026-07-06")
        tracker.record(102.0, 21_000_000, day="2026-07-07")

        self.assertEqual(tracker.today_bytes, 0)

    def test_autoscaler_grows_below_ninety_percent_target(self):
        cfg = PumperConfig(line_count=2, connections_per_line=6, max_connections_per_line=10, target_mbps=800)

        self.assertEqual(next_worker_count(cfg, current_workers=12, avg60_mbps=700), 14)
        self.assertEqual(next_worker_count(cfg, current_workers=20, avg60_mbps=700), 20)

    def test_autoscaler_can_shrink_when_over_upper_bound(self):
        cfg = PumperConfig(line_count=2, connections_per_line=6, max_connections_per_line=10, target_mbps=800)

        self.assertEqual(next_worker_count(cfg, current_workers=14, avg60_mbps=950), 12)
        self.assertEqual(next_worker_count(cfg, current_workers=12, avg60_mbps=950), 12)

    def test_theoretical_window_bytes_uses_target_mbps_and_runtime_window(self):
        cfg = PumperConfig(target_mbps=800, start_time="00:00", end_time="18:00")

        self.assertEqual(theoretical_window_bytes(cfg), 6_480_000_000_000)


if __name__ == "__main__":
    unittest.main()
