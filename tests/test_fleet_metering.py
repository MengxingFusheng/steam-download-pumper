import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from steam_pumper.config import FleetConfig
from steam_pumper.fleet import FleetAccount, FleetStore, SnmpInterface


class FleetMeteringTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "fleet.sqlite3"
        self.store = FleetStore(self.path, FleetConfig().validate())
        self.store.import_accounts([FleetAccount("a", "192.168.2.11", "pppoe-a", True, 10, "symmetric500")])
        self.start = datetime(2026, 10, 21, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.down = self.up = 0
        self.sample(0)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def sample(self, seconds, down=0, up=0):
        self.down += down
        self.up += up
        return self.store.record_snmp(
            [SnmpInterface(2, "pppoe-a", "", self.down, self.up)],
            self.start + timedelta(seconds=seconds),
        )["a"]

    @staticmethod
    def amount(mbps, seconds=60):
        return mbps * 1_000_000 // 8 * seconds

    def test_552m_download_is_measured_without_changing_500m_profile_or_target_floor(self):
        amount = self.amount(552)
        self.assertEqual(self.sample(60, amount), "ok")
        totals = self.store.daily_totals(self.start.date())["a"]
        self.assertEqual(totals["measured_download"], amount)
        self.assertEqual(totals["estimated_download"], 0)
        account = self.store.accounts()[0]
        self.assertEqual((account.upload_mbps, account.download_mbps), (500, 500))
        forecast = self.store.daily_forecasts(self.start + timedelta(seconds=60))["a"]
        self.assertEqual(forecast["minimum_download_bytes"], 2_700_000_000_000)

    def test_repeated_download_anomalies_do_not_dilute_recent_measured_rate(self):
        valid = self.amount(552)
        self.sample(60, valid)
        for seconds in (120, 180, 240):
            self.assertEqual(self.sample(seconds, self.amount(2000)), "anomaly")
            totals = self.store.daily_totals(self.start.date())["a"]
            self.assertEqual(totals["estimated_download"], valid * (seconds // 60 - 1))
            self.assertEqual(totals["measured_download"], valid)
            self.assertEqual(totals["unknown_seconds"], 0)

    def test_upload_anomalies_do_not_dilute_upload_rate_or_valid_download(self):
        valid = self.amount(20)
        self.sample(60, self.amount(300), valid)
        for seconds in (120, 180):
            self.assertEqual(self.sample(seconds, self.amount(300), self.amount(2000)), "anomaly")
        totals = self.store.daily_totals(self.start.date())["a"]
        self.assertEqual(totals["estimated_upload"], valid * 2)
        self.assertEqual(totals["measured_upload"], valid)
        self.assertEqual(totals["measured_download"], self.amount(300) * 3)
        self.assertEqual(totals["estimated_download"], 0)

    def test_anomalies_without_valid_history_remain_unknown(self):
        for seconds in (60, 120):
            self.assertEqual(self.sample(seconds, self.amount(2000)), "anomaly")
        now = self.start + timedelta(seconds=120)
        self.assertIsNone(self.store._recent_measured_rate("a", now, "download", minutes=30))
        totals = self.store.daily_totals(self.start.date())["a"]
        self.assertEqual(totals["unknown_seconds"], 120)
        self.assertEqual(totals["estimated_download"], 0)

    def test_valid_zero_download_is_a_real_zero_rate(self):
        self.sample(60)
        self.assertEqual(self.sample(120, self.amount(2000)), "anomaly")
        now = self.start + timedelta(seconds=120)
        self.assertEqual(self.store._recent_measured_rate("a", now, "download", minutes=30), 0)
        self.assertEqual(self.store.daily_totals(self.start.date())["a"]["unknown_seconds"], 0)

    def test_mixed_valid_and_invalid_intervals_within_one_minute_keep_valid_duration(self):
        self.sample(30, self.amount(300, 30))
        self.assertEqual(self.sample(60, self.amount(2000, 30)), "anomaly")
        rate = self.store._recent_measured_rate("a", self.start + timedelta(seconds=60), "download", minutes=30)
        self.assertEqual(rate, self.amount(300, 1))
        row = self.store.db.execute("SELECT * FROM traffic WHERE account_id='a'").fetchone()
        self.assertEqual(row["sample_seconds"], 60)
        self.assertEqual(row["download_valid_seconds"], 30)
        self.assertEqual(row["estimated_download_bytes"], self.amount(300, 30))

    def test_reset_estimates_do_not_dilute_later_estimates(self):
        valid = self.amount(300)
        self.sample(60, valid)
        for seconds, index in ((120, 3), (180, 4), (240, 5)):
            self.store.record_snmp([SnmpInterface(index, "pppoe-a", "", 1, 1)],
                                   self.start + timedelta(seconds=seconds))
        totals = self.store.daily_totals(self.start.date())["a"]
        self.assertEqual(totals["measured_download"], valid)
        self.assertEqual(totals["estimated_download"], valid * 3)

    def test_legacy_schema_preserves_bytes_and_uses_only_unambiguous_valid_minutes(self):
        minute = int(self.start.timestamp() // 60)
        with self.store.db:
            self.store._add_traffic("a", minute, measured_download=self.amount(300), sample_seconds=60)
            self.store._add_traffic("a", minute + 1, measured_download=self.amount(100),
                                    estimated_download=123, sample_seconds=60, quality="anomaly")
            self.store.db.execute("ALTER TABLE traffic DROP COLUMN download_valid_seconds")
        original = [tuple(r) for r in self.store.db.execute("SELECT * FROM traffic ORDER BY minute")]
        self.store.close()
        self.store = FleetStore(self.path, FleetConfig().validate())
        migrated = self.store.db.execute("SELECT * FROM traffic ORDER BY minute").fetchall()
        self.assertEqual([tuple(r)[:-1] for r in migrated], original)
        self.assertTrue(all(r["download_valid_seconds"] is None for r in migrated))
        now = self.start + timedelta(minutes=2)
        self.assertEqual(self.store._recent_measured_rate("a", now, "download", minutes=30), self.amount(300, 1))
        # A program-only update in a legacy minute must not erase its inferred
        # valid duration or change measured/estimated counters.
        self.store.record_download("a", 999, self.start)
        self.assertEqual(self.store._recent_measured_rate("a", now, "download", minutes=30), self.amount(300, 1))


if __name__ == "__main__":
    unittest.main()
