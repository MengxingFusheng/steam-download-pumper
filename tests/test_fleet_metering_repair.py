import tempfile
import unittest
from pathlib import Path

from steam_pumper.config import FleetConfig
from steam_pumper.fleet import FleetAccount, FleetStore
from tools.repair_fleet_metering import repair_estimates


class FleetMeteringRepairTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = FleetStore(Path(self.directory.name) / "fleet.sqlite3", FleetConfig())
        self.store.import_accounts([FleetAccount("a", "192.168.2.11", "pppoe-a", True, 10, "symmetric500")])

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def minute(self, minute, *, measured=0, estimated=1_000_000_000, program=4_000_000_000,
               quality="anomaly", seconds=60, legacy=True, upload_capacity=3_750_000_000):
        with self.store.db:
            self.store._add_traffic("a", minute, measured_download=measured, estimated_download=estimated,
                                    download=program, quality=quality, sample_seconds=seconds,
                                    upload_valid_seconds=seconds, upload_capacity_bytes=upload_capacity)
            if legacy:
                self.store.db.execute("UPDATE traffic SET download_valid_seconds=NULL WHERE minute=?", (minute,))

    def test_dry_run_is_read_only_and_apply_preserves_measured_and_program_bytes(self):
        self.minute(100)
        before = dict(self.store.db.execute("SELECT * FROM traffic").fetchone())
        preview = repair_estimates(self.store.db, 200)
        self.assertEqual(preview["added_estimated_bytes"], 2_750_000_000)
        self.assertEqual(dict(self.store.db.execute("SELECT * FROM traffic").fetchone()), before)
        self.assertIsNone(self.store.db.execute("SELECT name FROM sqlite_master WHERE name='metering_download_repairs'").fetchone())
        result = repair_estimates(self.store.db, 200, apply=True)
        self.assertEqual(result["changed_minutes"], 1)
        after = dict(self.store.db.execute("SELECT * FROM traffic").fetchone())
        self.assertEqual(after.pop("estimated_download_bytes"), 3_750_000_000)
        before.pop("estimated_download_bytes")
        self.assertEqual(after, before)
        audit = self.store.db.execute("SELECT * FROM metering_download_repairs").fetchone()
        self.assertEqual((audit["old_estimated_bytes"], audit["new_estimated_bytes"]), (1_000_000_000, 3_750_000_000))
        self.assertEqual(repair_estimates(self.store.db, 200, apply=True)["changed_minutes"], 0)

    def test_slow_or_stalled_program_is_not_estimated_as_full_speed(self):
        self.minute(100, program=2_000_000_000)
        self.minute(101, program=0)
        repair_estimates(self.store.db, 200, apply=True)
        values = [r[0] for r in self.store.db.execute("SELECT estimated_download_bytes FROM traffic ORDER BY minute")]
        self.assertEqual(values, [2_000_000_000, 1_000_000_000])

    def test_mixed_valid_and_invalid_samples_do_not_count_measured_bytes_twice(self):
        self.minute(100, measured=2_000_000_000, estimated=100_000_000)
        repair_estimates(self.store.db, 200, apply=True)
        row = self.store.db.execute("SELECT * FROM traffic").fetchone()
        self.assertEqual(row["measured_download_bytes"], 2_000_000_000)
        self.assertEqual(row["estimated_download_bytes"], 1_750_000_000)

    def test_unrelated_unknown_new_and_unconfirmed_historical_profiles_are_excluded(self):
        self.minute(100, quality="ok")
        self.minute(101, quality="reset")
        self.minute(102, estimated=0)
        self.minute(103, legacy=False)
        self.minute(104, upload_capacity=375_000_000)
        self.minute(105, seconds=120)
        self.minute(200)
        self.assertEqual(repair_estimates(self.store.db, 200, apply=True)["changed_minutes"], 0)

    def test_standard_accounts_are_excluded(self):
        self.minute(100)
        with self.store.db:
            self.store.db.execute("UPDATE accounts SET weight=1")
        self.assertEqual(repair_estimates(self.store.db, 200, apply=True)["changed_minutes"], 0)


if __name__ == "__main__":
    unittest.main()
