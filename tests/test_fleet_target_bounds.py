import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from steam_pumper.fleet import FleetAccount, FleetGroup
from steam_pumper.fleet_forecast import daily_prediction, download_targets
from steam_pumper.fleet_schedule import allocate_daily


class DailyTargetBoundsTests(unittest.TestCase):
    def bounds(self, upload=500, prediction=1_800_000_000_000, downloaded=0, ratio=2):
        return download_targets(upload_mbps=upload, predicted_upload_bytes=prediction,
                                download_bytes=downloaded, target_ratio=ratio)

    def test_nominal_six_hours_is_daily_floor_for_each_profile_and_ratio(self):
        for rate, ratio, expected in [(500, 2, 2_700_000_000_000), (50, 2, 270_000_000_000),
                                      (500, 2.5, 3_375_000_000_000)]:
            with self.subTest(rate=rate, ratio=ratio):
                view = self.bounds(upload=rate, prediction=1, ratio=ratio)
                self.assertEqual(view['minimum_download_bytes'], expected)
                self.assertEqual(view['maximum_download_bytes'], expected)
                self.assertEqual(view['predicted_download_bytes'], int(ratio))

    def test_prediction_above_floor_is_the_upper_goal(self):
        view = self.bounds(downloaded=2_800_000_000_000)
        self.assertEqual(view['minimum_download_bytes'], 2_700_000_000_000)
        self.assertEqual(view['maximum_download_bytes'], 3_600_000_000_000)
        self.assertEqual(view['target_download_bytes'], view['maximum_download_bytes'])
        self.assertEqual(view['remaining_minimum_download_bytes'], 0)
        self.assertEqual(view['remaining_download_bytes'], 800_000_000_000)
        self.assertEqual(self.bounds(downloaded=3_600_000_000_001)['remaining_download_bytes'], 0)

    def test_floor_does_not_shrink_as_day_passes_or_history_predicts_low(self):
        for hour in (0, 12, 18, 23):
            now = datetime(2026, 10, 21, hour, 59, tzinfo=ZoneInfo('Asia/Shanghai'))
            view = daily_prediction(now, upload_mbps=500, totals={'upload': 123},
                                    historical_utilization=dict.fromkeys(range(96), 0), target_ratio=2)
            self.assertEqual(view['predicted_upload_bytes'], 123)
            self.assertEqual(view['minimum_download_bytes'], 2_700_000_000_000)
            self.assertEqual(view['target_download_bytes'], 2_700_000_000_000)
            self.assertEqual(view['today_upload_bytes'], 123)

    def test_unknown_measurement_increases_upper_without_becoming_actual_or_floor(self):
        now = datetime(2026, 10, 21, 23, 59, tzinfo=ZoneInfo('Asia/Shanghai'))
        view = daily_prediction(now, upload_mbps=500,
            totals={'upload': 100, 'unknown_upload_capacity': 2_000_000_000_000},
            historical_utilization=dict.fromkeys(range(96), 0), target_ratio=2)
        self.assertEqual(view['today_upload_bytes'], 100)
        self.assertEqual(view['minimum_download_bytes'], 2_700_000_000_000)
        self.assertEqual(view['maximum_download_bytes'], 4_000_000_000_200)

    def test_shared_budget_prioritizes_unfinished_floor_over_protected_upper(self):
        accounts = [FleetAccount(n, '192.168.2.'+str(i), n, True, 10, 'symmetric500')
                    for i, n in enumerate(['floor', 'upper'], 11)]
        forecasts = {'floor': self.bounds(downloaded=2_600_000_000_000),
                     'upper': self.bounds(downloaded=2_800_000_000_000)}
        groups = [FleetGroup('symmetric500', '500M', 500, 1)]
        allocation = allocate_daily(accounts, groups, forecasts, 500, {}, {}, keep=['upper'])
        self.assertEqual(allocation, {'floor': 500})
        forecasts['floor'] = self.bounds(downloaded=3_600_000_000_000)
        self.assertEqual(allocate_daily(accounts, groups, forecasts, 500, {}, {}), {'upper': 500})

