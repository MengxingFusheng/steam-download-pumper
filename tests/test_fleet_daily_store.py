import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from steam_pumper.config import FleetConfig
from steam_pumper.fleet import FleetAccount, FleetStore, SnmpInterface, parse_accounts_csv


ZONE = ZoneInfo("Asia/Shanghai")


class FleetProfileCsvTests(unittest.TestCase):
    def test_legacy_and_profile_defaults(self):
        old = parse_accounts_csv("account_id,bind_ip,snmp_interface\na,192.168.2.11,pppoe-a\n")[0]
        self.assertFalse(old.profile_confirmed)
        self.assertEqual(old.group_id, "standard")
        accounts = parse_accounts_csv(
            "account_id,bind_ip,snmp_interface,weight,group_id\n"
            "a,192.168.2.11,pppoe-a,1,\n"
            "b,192.168.2.12,pppoe-b,10,\n"
        )
        self.assertEqual((accounts[0].upload_mbps, accounts[0].download_mbps), (50, 200))
        self.assertEqual((accounts[1].upload_mbps, accounts[1].download_mbps), (500, 500))
        self.assertEqual(accounts[1].group_id, "symmetric500")

    def test_invalid_profiles_headers_or_overlong_row_rejected(self):
        for payload in (
            "account_id,bind_ip,snmp_interface,weight\na,192.168.2.11,pppoe-a,2\n",
            "account_id,bind_ip,snmp_interface,weight,weight\na,192.168.2.11,pppoe-a,1,10\n",
            "account_id,bind_ip,snmp_interface\na,192.168.2.11,pppoe-a,extra\n",
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_accounts_csv(payload)


class FleetDailyStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = FleetStore(Path(self.directory.name) / "fleet.sqlite3", FleetConfig().validate())
        self.store.import_accounts([
            FleetAccount("a", "192.168.2.11", "pppoe-a", True, 1, "standard"),
            FleetAccount("b", "192.168.2.12", "pppoe-b", True, 10, "symmetric500"),
        ])

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def _minute(self, when, **values):
        with self.store.db:
            self.store._add_traffic("a", int(when.timestamp() // 60), **values)

    def _history_bin(self, when, utilization, ceiling=50):
        capacity = int(ceiling * 1_000_000 / 8 * 60)
        with self.store.db:
            for offset in range(15):
                self.store._add_traffic(
                    "a", int((when + timedelta(minutes=offset)).timestamp() // 60),
                    measured_upload=int(capacity * utilization), sample_seconds=60,
                    upload_valid_seconds=60, upload_capacity_bytes=capacity,
                )

    def test_group_validation_is_atomic_and_bulk_assign_rejects_unknown_account(self):
        original = self.store.groups()
        with self.assertRaises(ValueError):
            self.store.update_groups([
                {"group_id": "standard", "name": "changed", "download_limit_mbps": 1000, "max_active": 3},
                {"group_id": "bad", "name": "bad", "download_limit_mbps": 2000, "max_active": 21},
            ])
        self.assertEqual(self.store.groups(), original)
        with self.assertRaises(ValueError):
            self.store.assign_group("standard", ["b", "missing"])
        self.assertEqual(self.store.accounts()[1].group_id, "symmetric500")
        self.store.update_groups([{"group_id": "custom", "name": "天津", "download_limit_mbps": 2500, "max_active": 5}])
        self.store.assign_group("custom", ["a", "b"])
        self.assertTrue(all(account.group_id == "custom" for account in self.store.accounts()))

    def test_invalid_csv_group_does_not_change_inventory(self):
        original = self.store.accounts()
        with self.assertRaises(ValueError):
            self.store.import_accounts([FleetAccount("c", "192.168.2.13", "pppoe-c", True, 1, "missing")])
        self.assertEqual(self.store.accounts(), original)

    def test_import_preserves_history_and_last_started_but_requires_profile_confirmation(self):
        now = datetime(2026, 10, 3, 12, tzinfo=ZONE)
        self.store.mark_started("a", now)
        self._minute(now, measured_upload=1234)
        self.store.import_accounts(parse_accounts_csv("account_id,bind_ip,snmp_interface\na,192.168.2.11,pppoe-a\n"))
        self.assertEqual(self.store.last_started()["a"], int(now.timestamp()))
        self.assertEqual(self.store.daily_totals(now.date())["a"]["upload"], 1234)
        with self.assertRaises(ValueError):
            self.store.validate_daily_ready()

    def test_500m_upload_is_valid_and_download_validation_remains_physical(self):
        start = datetime(2026, 10, 3, 18, tzinfo=ZONE)
        self.store.record_snmp([SnmpInterface(2, "pppoe-b", "", 0, 0)], start)
        amount = 500 * 1_000_000 // 8 * 60
        result = self.store.record_snmp([SnmpInterface(2, "pppoe-b", "", amount, amount)], start + timedelta(minutes=1))
        self.assertEqual(result["b"], "ok")
        totals = self.store.daily_totals(start.date())["b"]
        self.assertEqual(totals["measured_upload"], amount)
        self.assertEqual(totals["measured_download"], amount)

    def test_isp_overdelivery_keeps_actual_bytes_and_500m_forecast_profile(self):
        self.store.cfg.upload_ceiling_mbps = 1000
        self.store.cfg.download_ceiling_mbps = 1000
        start = datetime(2026, 10, 3, 18, tzinfo=ZONE)
        self.store.record_snmp([SnmpInterface(2, 'pppoe-b', '', 0, 0)], start)
        amount = 900 * 1_000_000 // 8 * 60
        now = start + timedelta(minutes=1)
        result = self.store.record_snmp([SnmpInterface(2, 'pppoe-b', '', amount, amount)], now)
        self.assertEqual(result['b'], 'ok')
        totals = self.store.daily_totals(start.date())['b']
        self.assertEqual(totals['measured_upload'], amount)
        self.assertEqual(totals['measured_download'], amount)
        view = self.store.daily_forecasts(now)['b']
        self.assertEqual(view['future_upload_bytes'], 500 * 1_000_000 // 8 * (6 * 3600 - 60))
        # Raising a calibration ceiling must not admit a physically impossible
        # increment beyond that ceiling.
        bad = amount + 2000 * 1_000_000 // 8 * 60
        result = self.store.record_snmp([SnmpInterface(2, 'pppoe-b', '', bad, bad)], now + timedelta(minutes=1))
        self.assertEqual(result['b'], 'anomaly')

    def test_standard_line_overdelivery_uses_physical_limit_without_changing_profile(self):
        self.store.cfg.upload_ceiling_mbps = 1000
        start = datetime(2026, 10, 3, 18, tzinfo=ZONE)
        self.store.record_snmp([SnmpInterface(1, 'pppoe-a', '', 0, 0)], start)
        amount = 85 * 1_000_000 // 8 * 60
        now = start + timedelta(minutes=1)
        result = self.store.record_snmp([SnmpInterface(1, 'pppoe-a', '', 0, amount)], now)
        self.assertEqual(result['a'], 'ok')
        totals = self.store.daily_totals(start.date())['a']
        self.assertEqual(totals['measured_upload'], amount)
        self.assertEqual(totals['estimated_upload'], 0)
        self.assertEqual(totals['unknown_upload_seconds'], 0)
        self.assertEqual(self.store.accounts()[0].upload_mbps, 50)
        prediction = self.store.daily_forecasts(now)['a']
        self.assertEqual(prediction['future_upload_bytes'], 50 * 1_000_000 // 8 * (6 * 3600 - 60))

    def test_day_bounds_and_runtime_midnight_split(self):
        self._minute(datetime(2026, 10, 2, 23, 59, tzinfo=ZONE), measured_upload=111)
        self._minute(datetime(2026, 10, 3, 0, tzinfo=ZONE), measured_upload=222)
        self._minute(datetime(2026, 10, 4, 0, tzinfo=ZONE), measured_upload=333)
        self.assertEqual(self.store.daily_totals(date(2026, 10, 3))["a"]["upload"], 222)
        self.store.record_runtime("a", datetime(2026, 10, 2, 23, 59, 30, tzinfo=ZONE), datetime(2026, 10, 3, 0, 0, 30, tzinfo=ZONE))
        self.assertEqual(self.store.allocated_today(datetime(2026, 10, 3, 12, tzinfo=ZONE))["a"], 30)

    def test_cold_start_forecast_and_unknown_are_not_booked_as_actual(self):
        now = datetime(2026, 10, 3, 12, tzinfo=ZONE)
        self._minute(now, measured_upload=100, measured_download=150, unknown_seconds=60,
                     unknown_upload_seconds=60, unknown_upload_capacity_bytes=375_000_000)
        prediction = self.store.daily_forecasts(now)["a"]
        expected_future = 50 * 1_000_000 // 8 * 6 * 3600
        self.assertEqual(prediction["future_upload_bytes"], expected_future)
        self.assertEqual(prediction["predicted_upload_bytes"], expected_future + 375_000_000 + 100)
        self.assertEqual(prediction["forecast_quality"], "cold-start")
        self.assertEqual(self.store.daily_totals(now.date())["a"]["upload"], 100)
        self.assertEqual(self.store.daily_forecasts(now + timedelta(days=1))["a"]["today_upload_bytes"], 0)

    def test_historical_median_is_normalized_and_only_complete_days_qualify(self):
        now = datetime(2026, 10, 3, 23, 45, tzinfo=ZONE)
        for day, util, ceiling in ((1, 0.2, 50), (2, 0.8, 500), (3, 0.5, 50)):
            self._history_bin(now - timedelta(days=day), util, ceiling)
        self._history_bin(now, 1.0, 50)  # Current day must not enter the model.
        prediction = self.store.daily_forecasts(now)["a"]
        expected = int(50 * 1_000_000 / 8 * 900 * 0.5 * 1.2)
        self.assertEqual(prediction["future_upload_bytes"], expected)
        self.assertEqual(prediction["forecast_quality"], "historical")
        # Profile changes rescale historical utilization instead of old Mbps.
        self.store.import_accounts([FleetAccount("a", "192.168.2.11", "pppoe-a", True, 10, "symmetric500")])
        self.assertEqual(self.store.daily_forecasts(now)["a"]["future_upload_bytes"], expected * 10)

    def test_fewer_than_three_valid_days_falls_back_and_margin_is_capped(self):
        now = datetime(2026, 10, 3, 23, 45, tzinfo=ZONE)
        self._history_bin(now - timedelta(days=1), 0.9)
        self._history_bin(now - timedelta(days=2), 0.9)
        maximum = 50 * 1_000_000 // 8 * 900
        self.assertEqual(self.store.daily_forecasts(now)["a"]["future_upload_bytes"], maximum)
        self._history_bin(now - timedelta(days=3), 0.9)
        self.store._history_cache_key = None
        self.assertEqual(self.store.daily_forecasts(now)["a"]["future_upload_bytes"], maximum)

    def test_reset_keeps_previous_measured_bytes_and_recovery_clears_pending_unknown(self):
        start = datetime(2026, 10, 3, 18, tzinfo=ZONE)
        self.store.record_snmp([SnmpInterface(2, "pppoe-a", "", 1000, 1000)], start)
        self.store.record_snmp([], start + timedelta(minutes=1))
        self.assertEqual(self.store.daily_totals(start.date())["a"]["unknown_upload_seconds"], 60)
        self.store.record_snmp([SnmpInterface(2, "pppoe-a", "", 2000, 3000)], start + timedelta(minutes=2))
        totals = self.store.daily_totals(start.date())["a"]
        self.assertEqual(totals["upload"], 2000)
        self.assertEqual(totals["unknown_upload_capacity"], 0)
        self.store.record_snmp([SnmpInterface(3, "pppoe-a", "", 1, 1)], start + timedelta(minutes=3))
        self.assertEqual(self.store.daily_totals(start.date())["a"]["measured_upload"], 2000)

    def test_finished_daily_report_has_no_future_prediction_and_keeps_18_target(self):
        day = date(2026, 9, 1)
        self._minute(datetime(2026, 9, 1, 17, tzinfo=ZONE), measured_upload=100, measured_download=200)
        self._minute(datetime(2026, 9, 1, 23, tzinfo=ZONE), measured_upload=20, measured_download=60)
        with self.store.db:
            self.store._set_meta(f"daily_pre18:{day.isoformat()}", json.dumps({"a": 220}))
        report = self.store.daily_report(day)["a"]
        self.assertEqual(report["future_upload_bytes"], 0)
        self.assertEqual(report["predicted_download_bytes"], 240)
        self.assertEqual(report["target_download_bytes"], 270_000_000_000)
        self.assertEqual(report["before18_download_bytes"], 200)
        self.assertFalse(report["before18_achieved"])
        self.assertFalse(report["final_achieved"])


if __name__ == "__main__":
    unittest.main()
