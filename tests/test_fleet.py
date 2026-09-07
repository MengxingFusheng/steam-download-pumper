import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo


class FleetCsvTests(unittest.TestCase):
    def test_import_requires_unique_stable_mapping(self):
        from steam_pumper.fleet import parse_accounts_csv

        accounts = parse_accounts_csv(
            "account_id,bind_ip,snmp_interface,enabled\n"
            "a1,192.168.1.10,pppoe-a1,true\n"
            "a2,192.168.1.11,pppoe-a2,false\n"
        )
        self.assertEqual([item.account_id for item in accounts], ["a1", "a2"])
        self.assertFalse(accounts[1].enabled)
        with self.assertRaises(ValueError):
            parse_accounts_csv(
                "account_id,bind_ip,snmp_interface\n"
                "a1,192.168.1.10,pppoe-a1\n"
                "a1,192.168.1.11,pppoe-a2\n"
            )

    def test_167_account_inventory_uses_the_complete_new_subnet_range(self):
        from steam_pumper.fleet import parse_accounts_csv

        path = Path(__file__).resolve().parents[1] / "fleet-config" / "accounts.167.csv"
        accounts = parse_accounts_csv(path.read_text(encoding="utf-8"))
        self.assertEqual(len(accounts), 167)
        self.assertEqual(accounts[0].bind_ip, "192.168.2.11")
        self.assertEqual(accounts[-1].bind_ip, "192.168.2.177")
        self.assertEqual(len({item.account_id for item in accounts}), 167)
        self.assertEqual(len({item.snmp_interface for item in accounts}), 167)


class FleetLedgerTests(unittest.TestCase):
    def _store(self, directory):
        from steam_pumper.config import FleetConfig
        from steam_pumper.fleet import FleetStore, parse_accounts_csv

        cfg = FleetConfig().validate()
        store = FleetStore(Path(directory) / "fleet.sqlite3", cfg)
        store.import_accounts(parse_accounts_csv("account_id,bind_ip,snmp_interface\na1,192.168.1.10,pppoe-a1\n"))
        return store

    def test_normal_snmp_up_and_down_are_authoritative_and_program_download_is_separate(self):
        from steam_pumper.fleet import SnmpInterface

        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            start = datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)
            store.record_snmp([SnmpInterface(3, "pppoe-a1", "", 1000, 2000)], start)
            store.record_snmp([SnmpInterface(3, "pppoe-a1", "", 5000, 9000)], start + timedelta(seconds=60))
            store.record_download("a1", 1234, start + timedelta(seconds=60))
            totals = store.rolling(start + timedelta(seconds=60))["a1"]
            self.assertEqual(totals["upload"], 7000)
            self.assertEqual(totals["snmp_download"], 4000)
            self.assertEqual(totals["download"], 4000)
            self.assertEqual(totals["program_download"], 1234)
            self.assertEqual(totals["measured_upload"], 7000)
            self.assertEqual(totals["measured_download"], 4000)
            store.close()

    def test_counter_reset_uses_recent_measured_rates(self):
        from steam_pumper.fleet import SnmpInterface

        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            start = datetime(2026, 8, 1, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            store.record_snmp([SnmpInterface(3, "pppoe-a1", "", 5000, 5000)], start)
            store.record_snmp([SnmpInterface(3, "pppoe-a1", "", 9000, 12000)], start + timedelta(seconds=60))
            store.record_snmp([SnmpInterface(4, "pppoe-a1", "", 1, 1)], start + timedelta(seconds=120))
            totals = store.rolling(start + timedelta(seconds=120))["a1"]
            self.assertEqual(totals["upload"], 14000)
            self.assertEqual(totals["download"], 8000)
            self.assertEqual(totals["estimated_upload"], 7000)
            self.assertEqual(totals["estimated_download"], 4000)
            self.assertEqual(totals["interruptions"], 1)
            store.close()

    def test_today_uses_shanghai_midnight_and_excludes_previous_day(self):
        zone = ZoneInfo("Asia/Shanghai")
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            with store.db:
                store._add_traffic("a1", int(datetime(2026, 9, 3, 23, 59, tzinfo=zone).timestamp() // 60), measured_download=111)
                store._add_traffic("a1", int(datetime(2026, 9, 4, 0, 1, tzinfo=zone).timestamp() // 60), measured_download=222)

            today = store.today(datetime(2026, 9, 4, 12, 0, tzinfo=zone))["a1"]

            self.assertEqual(today["download"], 222)
            store.close()

    def test_missing_sample_is_reconciled_when_counters_remain_continuous(self):
        from steam_pumper.fleet import SnmpInterface

        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            start = datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)
            store.record_snmp([SnmpInterface(3, "pppoe-a1", "", 1000, 2000)], start)
            store.record_snmp([], start + timedelta(seconds=60))
            pending = store.rolling(start + timedelta(seconds=60))["a1"]
            self.assertEqual(pending["unknown_seconds"], 60)

            result = store.record_snmp(
                [SnmpInterface(3, "pppoe-a1", "", 9000, 16000)],
                start + timedelta(seconds=120),
            )
            totals = store.rolling(start + timedelta(seconds=120))["a1"]
            self.assertEqual(result["a1"], "recovered")
            self.assertEqual(totals["upload"], 14000)
            self.assertEqual(totals["download"], 8000)
            self.assertEqual(totals["unknown_seconds"], 0)
            self.assertEqual(totals["interruptions"], 0)
            store.close()

    def test_download_anomaly_does_not_discard_valid_upload(self):
        from steam_pumper.config import FleetConfig
        from steam_pumper.fleet import FleetStore, SnmpInterface, parse_accounts_csv

        with tempfile.TemporaryDirectory() as directory:
            cfg = FleetConfig(download_ceiling_mbps=1).validate()
            store = FleetStore(Path(directory) / "fleet.sqlite3", cfg)
            store.import_accounts(parse_accounts_csv("account_id,bind_ip,snmp_interface\na1,192.168.1.10,pppoe-a1\n"))
            start = datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)
            store.record_snmp([SnmpInterface(3, "pppoe-a1", "", 1000, 2000)], start)
            store.record_snmp([SnmpInterface(3, "pppoe-a1", "", 5000, 9000)], start + timedelta(seconds=60))
            result = store.record_snmp(
                [SnmpInterface(3, "pppoe-a1", "", 100_000_000, 16000)],
                start + timedelta(seconds=120),
            )
            totals = store.rolling(start + timedelta(seconds=120))["a1"]
            self.assertEqual(result["a1"], "anomaly")
            self.assertEqual(totals["measured_upload"], 14000)
            self.assertEqual(totals["measured_download"], 4000)
            self.assertEqual(totals["estimated_download"], 4000)
            store.close()

    def test_reconnect_prefers_one_live_interface_over_stale_duplicate_names(self):
        from steam_pumper.fleet import SnmpInterface

        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            start = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
            result = store.record_snmp([
                SnmpInterface(3, "pppoe-a1", "", 1000, 2000, oper_status=2),
                SnmpInterface(4, "pppoe-a1", "", 5000, 6000, oper_status=1),
                SnmpInterface(5, "pppoe-a1", "", 7000, 8000, oper_status=2),
            ], start)
            self.assertEqual(result["a1"], "baseline")
            result = store.record_snmp([
                SnmpInterface(3, "pppoe-a1", "", 1000, 2000, oper_status=2),
                SnmpInterface(4, "pppoe-a1", "", 9000, 12000, oper_status=1),
            ], start + timedelta(seconds=60))
            self.assertEqual(result["a1"], "ok")
            self.assertEqual(store.rolling(start + timedelta(seconds=60))["a1"]["measured_upload"], 6000)
            store.close()

    def test_duplicate_down_reconnect_artifacts_are_offline_not_ambiguous(self):
        from steam_pumper.fleet import SnmpInterface

        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            start = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
            result = store.record_snmp([
                SnmpInterface(3, "pppoe-a1", "", 1000, 2000, oper_status=2),
                SnmpInterface(4, "pppoe-a1", "", 3000, 4000, oper_status=2),
            ], start)
            self.assertEqual(result["a1"], "offline")
            store.close()

    def test_old_database_columns_are_reused_as_legacy_snmp_totals(self):
        from steam_pumper.config import FleetConfig
        from steam_pumper.fleet import FleetStore

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fleet.sqlite3"
            database = sqlite3.connect(path)
            database.executescript("""
            CREATE TABLE accounts(account_id TEXT PRIMARY KEY,bind_ip TEXT UNIQUE,snmp_interface TEXT UNIQUE,enabled INTEGER,last_started INTEGER DEFAULT 0);
            CREATE TABLE traffic(account_id TEXT,minute INTEGER,upload_bytes INTEGER DEFAULT 0,download_bytes INTEGER DEFAULT 0,snmp_download_bytes INTEGER DEFAULT 0,quality TEXT DEFAULT 'ok',PRIMARY KEY(account_id,minute));
            CREATE TABLE cursors(account_id TEXT PRIMARY KEY,if_index INTEGER,in_octets INTEGER,out_octets INTEGER,discontinuity INTEGER,sampled_at INTEGER NOT NULL,valid INTEGER DEFAULT 0);
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO accounts VALUES('a1','192.168.2.11','pppoe-a1',1,0);
            """)
            minute = int(datetime.now(timezone.utc).timestamp() // 60)
            database.execute("INSERT INTO traffic VALUES('a1',?,100,125,150,'ok')", (minute,))
            database.commit()
            database.close()

            store = FleetStore(path, FleetConfig().validate())
            totals = store.rolling(datetime.now(timezone.utc))["a1"]
            self.assertEqual(totals["upload"], 100)
            self.assertEqual(totals["download"], 150)
            self.assertEqual(totals["program_download"], 125)
            self.assertEqual(totals["legacy_upload"], 100)
            store.close()
            FleetStore(path, FleetConfig().validate()).close()


class FleetScheduleTests(unittest.TestCase):
    def test_metrics_restore_today_totals_and_ratio_target_fields(self):
        from steam_pumper.config import FleetConfig
        from steam_pumper.fleet import FleetController, parse_accounts_csv

        zone = ZoneInfo("Asia/Shanghai")
        now = datetime.now(zone)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPLY_LAN_IPS": "0"}, clear=False):
            controller = FleetController(Path(directory) / "config.json", FleetConfig().validate(), snmp_reader=None)
            controller.store.import_accounts(parse_accounts_csv("account_id,bind_ip,snmp_interface\na1,192.168.2.11,pppoe-a1\n"))
            minute = int(now.timestamp() // 60)
            with controller.store.db:
                controller.store._add_traffic("a1", minute, measured_upload=100, measured_download=150, download=125)
            controller.rate_samples.extend([(1.0, 0), (2.0, 25_000_000)])

            metrics = controller.metrics()

            self.assertEqual(metrics["today_bytes"], 150)
            self.assertEqual(metrics["today_upload_bytes"], 100)
            self.assertEqual(metrics["rolling_program_download_bytes"], 125)
            self.assertEqual(metrics["rolling_target_download_bytes"], 200)
            self.assertEqual(metrics["rolling_debt_bytes"], 50)
            self.assertAlmostEqual(metrics["rolling_ratio"], 1.5)
            self.assertAlmostEqual(metrics["rolling_ratio_progress_percent"], 75.0)
            self.assertGreater(metrics["target_percent"], 0)
            controller.shutdown()

    def test_local_source_status_reports_the_effective_pool(self):
        from steam_pumper.config import FleetConfig
        from steam_pumper.fleet import FleetController

        sources = ["https://one.example/speedtest.bin", "https://two.example/speedtest.bin"]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPLY_LAN_IPS": "0"}, clear=False):
            controller = FleetController(
                Path(directory) / "config.json",
                FleetConfig(source_pool=sources).validate(),
                snmp_reader=None,
            )
            status = controller.source_list_status()
            self.assertFalse(status["enabled"])
            self.assertEqual(status["status"], "local")
            self.assertEqual(status["source_count"], 2)
            self.assertEqual(status["origin"], "local-fallback")
            controller.shutdown()

    def test_local_source_pool_change_is_hot_reloaded_for_active_lines(self):
        from steam_pumper.config import FleetConfig
        from steam_pumper.fleet import FleetController

        class FakeEngine:
            def __init__(self):
                self.applied = []

            def set_sources(self, sources):
                self.applied.append(list(sources))

            def stop(self):
                pass

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPLY_LAN_IPS": "0"}, clear=False):
            controller = FleetController(Path(directory) / "config.json", FleetConfig().validate(), snmp_reader=None)
            engine = FakeEngine()
            controller.active["a1"] = SimpleNamespace(engine=engine)
            sources = ["https://example.net/speedtest/random4000x4000.jpg"]
            controller.update_config({"source_pool": sources})
            self.assertEqual(controller.effective_source_pool, sources)
            self.assertEqual(controller.effective_source_origin, "local-config")
            self.assertEqual(engine.applied, [sources])
            controller.shutdown()

    def test_empty_bootstrap_inventory_waits_for_web_import(self):
        from steam_pumper.config import FleetConfig
        from steam_pumper.fleet import FleetController

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPLY_LAN_IPS": "0", "FLEET_ACCOUNTS_CSV": "", "FLEET_AUTOSTART": "false"}, clear=False):
            controller = FleetController(Path(directory) / "config.json", FleetConfig().validate(), snmp_reader=None)
            self.assertEqual(controller.store.accounts(), [])
            controller.shutdown()

    def test_pcdn_deployment_is_stopped_by_default_but_can_autostart_explicitly(self):
        from steam_pumper.config import FleetConfig
        from steam_pumper.fleet import FleetController

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPLY_LAN_IPS": "0", "FLEET_AUTOSTART": "false"}, clear=False):
            controller = FleetController(Path(directory) / "config.json", FleetConfig().validate(), snmp_reader=None)
            self.assertFalse(controller.manual_enabled)
            controller.shutdown()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPLY_LAN_IPS": "0", "FLEET_AUTOSTART": "true"}, clear=False):
            controller = FleetController(Path(directory) / "config.json", FleetConfig().validate(), snmp_reader=None)
            self.assertTrue(controller.manual_enabled)
            controller.shutdown()

    def test_schedule_uses_ten_twenty_ten_base_slots(self):
        from steam_pumper.config import FleetConfig
        from steam_pumper.fleet import FleetController, parse_accounts_csv

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPLY_LAN_IPS": "0"}, clear=False):
            controller = FleetController(Path(directory) / "config.json", FleetConfig().validate(), snmp_reader=None)
            controller.store.import_accounts(parse_accounts_csv("account_id,bind_ip,snmp_interface\na1,192.168.1.10,pppoe-a1\n"))
            account = controller.store.accounts()
            zone = ZoneInfo("Asia/Shanghai")
            self.assertEqual(controller._desired_count(datetime(2026, 8, 1, 0, 30, tzinfo=zone), account), 10)
            self.assertEqual(controller._desired_count(datetime(2026, 8, 1, 2, 0, tzinfo=zone), account), 20)
            self.assertEqual(controller._desired_count(datetime(2026, 8, 1, 12, 0, tzinfo=zone), account), 10)
            controller.shutdown()

    def test_evening_slots_use_recent_actual_upload_rate(self):
        from steam_pumper.config import FleetConfig
        from steam_pumper.fleet import FleetController, parse_accounts_csv

        zone = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 8, 1, 20, 0, tzinfo=zone)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPLY_LAN_IPS": "0"}, clear=False):
            controller = FleetController(Path(directory) / "config.json", FleetConfig().validate(), snmp_reader=None)
            controller.store.import_accounts(parse_accounts_csv("account_id,bind_ip,snmp_interface\na1,192.168.2.11,pppoe-a1\n"))
            totals = {"a1": {"upload": 0, "download": 0}}
            with patch.object(controller.store, "rolling", return_value=totals), patch.object(
                controller.store, "recent_upload_rates", return_value={"a1": 50_000_000 / 8},
            ):
                self.assertEqual(controller._desired_count(now, controller.store.accounts()), 1)
            with patch.object(controller.store, "rolling", return_value=totals), patch.object(
                controller.store, "recent_upload_rates", return_value={"a1": 0.0},
            ):
                self.assertEqual(controller._desired_count(now, controller.store.accounts()), 0)
            controller.shutdown()

    def test_unusable_snmp_account_is_removed_from_candidates(self):
        from steam_pumper.config import FleetConfig
        from steam_pumper.fleet import FleetController, parse_accounts_csv

        zone = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 8, 1, 12, 0, tzinfo=zone)
        csv_text = (
            "account_id,bind_ip,snmp_interface\n"
            "a1,192.168.2.11,pppoe-a1\n"
            "a2,192.168.2.12,pppoe-a2\n"
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPLY_LAN_IPS": "0"}, clear=False):
            controller = FleetController(Path(directory) / "config.json", FleetConfig().validate(), snmp_reader=None)
            controller.store.import_accounts(parse_accounts_csv(csv_text))
            controller.manual_enabled = True
            for unusable in ("offline", "missing", "ambiguous"):
                controller.snmp_quality = {"a1": unusable, "a2": "ok"}
                started = []
                with patch.object(controller, "_start", side_effect=lambda account, _now: started.append(account.account_id)):
                    controller._schedule(now)
                self.assertEqual(started, ["a2"])
            controller.shutdown()

    def test_stalled_line_is_cooled_down_and_replaced_without_waiting_for_rotation(self):
        from steam_pumper.config import FleetConfig
        from steam_pumper.fleet import ActiveLine, FleetController, parse_accounts_csv

        class FakeEngine:
            def __init__(self):
                self.state = SimpleNamespace(total_bytes=0, status="downloading")
                self.stopped = False

            def poll(self):
                pass

            def stop(self):
                self.stopped = True
                self.state.status = "stopped"

        zone = ZoneInfo("Asia/Shanghai")
        started = datetime(2026, 8, 1, 12, 0, tzinfo=zone)
        now = started + timedelta(seconds=91)
        csv_text = (
            "account_id,bind_ip,snmp_interface\n"
            "a1,192.168.2.11,pppoe-a1\n"
            "a2,192.168.2.12,pppoe-a2\n"
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPLY_LAN_IPS": "0"}, clear=False):
            controller = FleetController(Path(directory) / "config.json", FleetConfig().validate(), snmp_reader=None)
            controller.store.import_accounts(parse_accounts_csv(csv_text))
            first = controller.store.accounts()[0]
            engine = FakeEngine()
            controller.active[first.account_id] = ActiveLine(first, engine, started, last_progress_at=started)

            self.assertTrue(controller._poll_engines(now))
            self.assertNotIn("a1", controller.active)
            self.assertTrue(engine.stopped)
            self.assertGreater(controller.account_cooldowns["a1"], now)

            started_accounts = []
            controller.manual_enabled = True
            with patch.object(controller, "_start", side_effect=lambda account, _now: started_accounts.append(account.account_id)):
                controller._schedule(now)
            self.assertEqual(started_accounts, ["a2"])
            controller.shutdown()

    def test_sustained_slow_line_is_unhealthy_even_when_it_makes_progress(self):
        from collections import deque

        from steam_pumper.config import FleetConfig
        from steam_pumper.fleet import ActiveLine, FleetAccount, FleetController

        zone = ZoneInfo("Asia/Shanghai")
        started = datetime(2026, 8, 1, 12, 0, tzinfo=zone)
        now = started + timedelta(seconds=180)
        active = ActiveLine(
            FleetAccount("a1", "192.168.2.11", "pppoe-a1"),
            SimpleNamespace(),
            started,
            last_bytes=100_000_000,
            last_progress_at=now,
            rate_samples=deque([(started, 0), (now, 100_000_000)], maxlen=400),
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPLY_LAN_IPS": "0"}, clear=False):
            controller = FleetController(Path(directory) / "config.json", FleetConfig().validate(), snmp_reader=None)
            reason = controller._line_health_failure(active, now)
            self.assertIn("below 50Mbps", reason)
            controller.shutdown()
