import unittest

from tools.observe_daily_sources import daily_result


class DailyObservationTests(unittest.TestCase):
    def test_total_surplus_does_not_hide_an_individual_line_deficit(self):
        node = {"snapshot": {"lines": [{"line_id": "a", "enabled": True}, {"line_id": "b", "enabled": True}]}}
        periods = [{"date": "2026-10-20", "mode": "daily", "phase": "late_makeup", "state": "closed", "end": 1,
                    "lines": [{"line_id": "a", "target_bytes": 100, "cumulative_actual_bytes": 250},
                              {"line_id": "b", "target_bytes": 100, "cumulative_actual_bytes": 50}]}]
        result = daily_result(node, periods, "2026-10-20")
        self.assertEqual(result["state"], "unmet")
        self.assertEqual(result["lines"][1]["remaining_bytes"], 50)

    def test_disabled_inventory_does_not_create_a_false_deficit(self):
        node = {"snapshot": {"lines": [{"line_id": "a", "enabled": True}, {"line_id": "b", "enabled": False}]}}
        periods = [{"date": "2026-10-20", "mode": "daily", "phase": "late_makeup", "state": "closed", "end": 1,
                    "lines": [{"line_id": "a", "target_bytes": 100, "cumulative_actual_bytes": 100},
                              {"line_id": "b", "target_bytes": 100, "cumulative_actual_bytes": 0}]}]
        result = daily_result(node, periods, "2026-10-20")
        self.assertEqual(result["state"], "achieved")
        self.assertEqual(len(result["lines"]), 1)

    def test_missing_target_or_unclosed_day_is_unknown(self):
        node = {"snapshot": {"lines": [{"line_id": "a", "enabled": True}]}}
        self.assertEqual(daily_result(node, [], "2026-10-20")["state"], "unknown")
        periods = [{"date": "2026-10-20", "mode": "daily", "phase": "late_makeup", "state": "closed", "end": 1,
                    "lines": [{"line_id": "a", "target_bytes": None, "cumulative_actual_bytes": 0}]}]
        self.assertEqual(daily_result(node, periods, "2026-10-20")["state"], "unknown")
