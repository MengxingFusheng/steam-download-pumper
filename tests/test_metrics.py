import unittest

from steam_pumper.metrics import ThroughputTracker, next_connection_count, theoretical_window_bytes


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

    def test_tracker_reports_sample_span(self):
        tracker = ThroughputTracker()
        tracker.record(10.0, 0)
        tracker.record(14.5, 10)

        self.assertEqual(tracker.sample_span_seconds(), 4.5)

    def test_autoscaler_grows_below_ninety_percent_target(self):
        self.assertEqual(next_connection_count(6, 10, 6, 350, 400, True), 7)
        self.assertEqual(next_connection_count(6, 10, 10, 350, 400, True), 10)

    def test_autoscaler_can_shrink_when_over_upper_bound(self):
        self.assertEqual(next_connection_count(6, 10, 8, 500, 400, True), 7)
        self.assertEqual(next_connection_count(6, 10, 6, 500, 400, True), 6)
        self.assertEqual(next_connection_count(6, 10, 8, 500, 400, False), 8)

    def test_theoretical_window_bytes_uses_target_mbps_and_runtime_window(self):
        self.assertEqual(theoretical_window_bytes(800, "00:00", "18:00"), 6_480_000_000_000)


if __name__ == "__main__":
    unittest.main()
