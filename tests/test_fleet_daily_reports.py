import csv
import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from steam_pumper.config import FleetConfig, load_config
from steam_pumper.fleet import FleetController, parse_accounts_csv


class DailyReportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"APPLY_LAN_IPS": "0", "FLEET_AUTOSTART": "false"})
        self.environment.start()
        self.controller = FleetController(Path(self.directory.name) / "config.json", FleetConfig())
        self.controller.store.import_accounts(parse_accounts_csv(
            "account_id,bind_ip,snmp_interface,weight\na,192.168.2.11,pppoe-a,10\n"))

    def tearDown(self):
        self.controller.shutdown()
        self.environment.stop()
        self.directory.cleanup()

    def test_completed_day_report_uses_stored_unknown_capacity_and_pre18_snapshot(self):
        store = self.controller.store
        at = datetime(2026, 9, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        with store.db:
            store._add_traffic("a", int(at.timestamp() // 60), measured_upload=100, measured_download=500,
                               unknown_upload_seconds=1, unknown_upload_capacity_bytes=50)
            store._set_meta("daily_pre18:2026-09-01", json.dumps({"a": 400}))
        row = list(csv.DictReader(io.StringIO(self.controller.report_csv("daily", "2026-09-01"))))[0]
        self.assertEqual(row["target_download_bytes"], "2700000000000")
        self.assertEqual(row["minimum_download_bytes"], "2700000000000")
        self.assertEqual(row["maximum_download_bytes"], "2700000000000")
        self.assertEqual(row["minimum_met"], "0")
        self.assertEqual(row["pre18_met"], "1")
        self.assertEqual(row["full_day_met"], "0")
        self.assertEqual(row["upload_bytes"], "100")  # Unknown budget is not actual upload.

    def test_views_are_cached_by_minute_and_invalidated_after_configuration(self):
        now = datetime(2026, 9, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        with patch.object(self.controller.store, "daily_forecasts", wraps=self.controller.store.daily_forecasts) as calculate:
            self.controller._daily_snapshot(now)
            self.controller._daily_snapshot(now.replace(second=50))
            self.assertEqual(calculate.call_count, 1)
            self.controller.update_config({"target_ratio": 2.1})
            self.controller._daily_snapshot(now)
            self.assertEqual(calculate.call_count, 2)
            self.controller._daily_snapshot(now.replace(minute=1))
            self.assertEqual(calculate.call_count, 3)

    def test_report_and_csv_share_upper_prediction_and_fixed_floor(self):
        store = self.controller.store
        at = datetime(2026, 9, 1, 23, tzinfo=ZoneInfo("Asia/Shanghai"))
        with store.db:
            store._add_traffic('a', int(at.timestamp() // 60),
                               measured_upload=2_000_000_000_000, measured_download=3_000_000_000_000)
        before = store.daily_totals(at.date())
        report = store.daily_report(at.date())['a']
        row = list(csv.DictReader(io.StringIO(self.controller.report_csv('daily', '2026-09-01'))))[0]
        self.assertEqual(report['target_download_bytes'], 4_000_000_000_000)
        self.assertEqual(report['minimum_download_bytes'], 2_700_000_000_000)
        self.assertTrue(report['minimum_achieved'])
        self.assertFalse(report['final_achieved'])
        self.assertEqual(int(row['target_download_bytes']), report['maximum_download_bytes'])
        self.assertEqual(row['minimum_met'], '1')
        self.assertEqual(store.daily_totals(at.date()), before)

    def test_legacy_csv_cannot_replace_confirmed_inventory_and_daily_mode_cannot_be_disabled(self):
        with self.assertRaises(ValueError):
            self.controller.import_accounts_csv("account_id,bind_ip,snmp_interface\na,192.168.2.11,pppoe-a\n")
        self.assertEqual(self.controller.store.accounts()[0].weight, 10)
        with self.assertRaises(ValueError):
            self.controller.update_config({"daily_mode_enabled": False})
        self.assertTrue(self.controller.cfg.daily_mode_enabled)
        self.assertEqual(self.controller.store.accounts()[0].weight, 10)

    def test_profile_backfill_is_bounded_resumable_and_preserves_actual_counters(self):
        store = self.controller.store
        with store.db:
            store._add_traffic("a", 100, measured_upload=123, sample_seconds=60, quality="ok")
            store._add_traffic("a", 280, measured_upload=456, unknown_seconds=30, quality="missing")
            store.db.execute("DELETE FROM meta WHERE key='daily_profile_schema_at'")
        statements = []
        store.db.set_trace_callback(statements.append)
        store._migrate_daily_profiles()
        rows = store.db.execute("SELECT * FROM traffic ORDER BY minute").fetchall()
        self.assertEqual(rows[0]["measured_upload_bytes"], 123)
        self.assertEqual(rows[0]["upload_valid_seconds"], 60)
        self.assertEqual(rows[0]["upload_capacity_bytes"], 375_000_000)
        self.assertEqual(rows[1]["measured_upload_bytes"], 456)
        self.assertEqual(rows[1]["unknown_upload_capacity_bytes"], 187_500_000)
        self.assertGreaterEqual(sum(s == "COMMIT" for s in statements), 4)
        store._migrate_daily_profiles()
        repeated = store.db.execute("SELECT * FROM traffic ORDER BY minute").fetchall()
        self.assertEqual([tuple(row) for row in rows], [tuple(row) for row in repeated])
        store.db.set_trace_callback(None)


class DailyConfigurationTests(unittest.TestCase):
    def test_default_enabled_and_invalid_budget_fields_rejected(self):
        self.assertTrue(FleetConfig().validate().daily_mode_enabled)
        for options in ({"daily_mode_enabled": "true"}, {"daily_mode_enabled": False}, {"router_limit_mbps": 6000},
                        {"router_reserve_mbps": -1}, {"late_download_limit_mbps": 2500},
                        {"wan_stale_seconds": 121}, {"router_snmp_interface": " "}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                FleetConfig(**options).validate()
