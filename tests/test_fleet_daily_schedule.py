"""Daily-mode allocation checks without sockets, SNMP subprocesses or engines."""
import os
import json
import tempfile
import unittest
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from steam_pumper.config import FleetConfig
from steam_pumper.fleet import ActiveLine, FleetAccount, FleetController, FleetGroup, SnmpInterface, parse_accounts_csv
from steam_pumper.fleet_schedule import WanBudget, allocate_daily, daily_phase


SHANGHAI = ZoneInfo("Asia/Shanghai")


def moment(hour=12, minute=0, second=0, day=3):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=SHANGHAI)


class WanBudgetTests(unittest.TestCase):
    def setUp(self):
        self.cfg = FleetConfig().validate()
        self.wan = WanBudget()
        self.now = moment()

    @staticmethod
    def interface(octets, *, index=24, name="wan1", alias="", discontinuity=0, oper_status=1):
        return SnmpInterface(index, name, alias, octets, octets // 2, discontinuity, oper_status)

    def observe(self, item, when=None, uptime=10_000):
        self.wan.observe([item], when or self.now, uptime, swap=False, stale_seconds=120)

    def snapshot(self, when=None, fleet=0):
        return self.wan.snapshot(when or self.now, fleet, self.cfg)

    def test_first_counter_sample_pauses_and_second_valid_sample_recovers(self):
        self.assertEqual(self.snapshot()["phase_limit_mbps"], 0)
        self.observe(self.interface(1_000))
        self.assertEqual(self.snapshot()["status"], "baseline")
        self.assertEqual(self.snapshot()["available_mbps"], 0)
        later = self.now + timedelta(seconds=60)
        self.observe(self.interface(1_000 + 1_000_000_000 * 60 // 8), later, 16_000)
        result = self.snapshot(later)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["wan_down_mbps"], 1000)
        self.assertAlmostEqual(result["available_mbps"], 3800)

    def test_named_wan_only_does_not_sum_vlan_or_pppoe_counters(self):
        start = [self.interface(1000), self.interface(1000, index=25, name="wan1.2"), self.interface(1000, index=26, name="pppoe-1")]
        finish = [self.interface(7_500_001_000), self.interface(75_000_001_000, index=25, name="wan1.2"), self.interface(3_750_001_000, index=26, name="pppoe-1")]
        self.wan.observe(start, self.now, 10_000, swap=False, stale_seconds=120)
        later = self.now + timedelta(seconds=60)
        self.wan.observe(finish, later, 16_000, swap=False, stale_seconds=120)
        self.assertEqual(self.snapshot(later)["wan_down_mbps"], 1000)

    def test_reset_or_index_change_pauses_until_next_good_counter_pair(self):
        for kind in ("rollback", "index", "discontinuity", "uptime"):
            with self.subTest(kind=kind):
                self.wan = WanBudget()
                self.observe(self.interface(1000))
                at = self.now + timedelta(seconds=60)
                self.observe(self.interface(7_500_001_000), at, 16_000)
                self.assertEqual(self.snapshot(at)["status"], "ok")
                changed = self.interface(1 if kind == "rollback" else 8_000_001_000,
                                         index=30 if kind == "index" else 24,
                                         discontinuity=100 if kind == "discontinuity" else 0)
                reset_at = at + timedelta(seconds=60)
                self.observe(changed, reset_at, 1 if kind == "uptime" else 22_000)
                self.assertEqual(self.snapshot(reset_at)["status"], "reset")
                self.assertEqual(self.snapshot(reset_at)["available_mbps"], 0)
                recovered = self.interface(changed.in_octets + 7_500_000_000,
                                           index=changed.index, discontinuity=changed.discontinuity)
                recovery_at = reset_at + timedelta(seconds=60)
                self.observe(recovered, recovery_at, 6_001 if kind == "uptime" else 28_000)
                self.assertEqual(self.snapshot(recovery_at)["status"], "ok")

    def test_stale_more_than_120_seconds_pauses_and_gap_requires_new_pair(self):
        self.observe(self.interface(1000))
        good_at = self.now + timedelta(seconds=60)
        self.observe(self.interface(7_500_001_000), good_at, 16_000)
        self.assertGreater(self.snapshot(good_at + timedelta(seconds=120))["available_mbps"], 0)
        expired_at = good_at + timedelta(seconds=121)
        self.assertEqual(self.snapshot(expired_at)["status"], "stale")
        self.assertEqual(self.snapshot(expired_at)["phase_limit_mbps"], 0)
        self.observe(self.interface(8_000_001_000), expired_at, 28_100)
        self.assertEqual(self.snapshot(expired_at)["available_mbps"], 0)
        restored_at = expired_at + timedelta(seconds=60)
        self.observe(self.interface(15_500_001_000), restored_at, 34_100)
        self.assertGreater(self.snapshot(restored_at)["available_mbps"], 0)

    def test_async_samples_and_negative_difference_do_not_pause(self):
        self.observe(self.interface(1000))
        good_at = self.now + timedelta(seconds=60)
        self.observe(self.interface(7_500_001_000), good_at, 16_000)
        # Fleet and WAN average windows deliberately need not align.
        result = self.snapshot(good_at + timedelta(seconds=17), fleet=2300)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["other_down_mbps"], 0)
        self.assertEqual(result["available_mbps"], 4800)
        self.assertEqual(self.snapshot(good_at, fleet=100)["available_mbps"], 3900)

    def test_missing_offline_and_physical_anomaly_pause(self):
        self.observe(self.interface(1000))
        self.wan.observe([], self.now, 10000, swap=False, stale_seconds=120)
        self.assertEqual(self.snapshot()["status"], "missing")
        self.assertEqual(self.snapshot()["available_mbps"], 0)
        self.observe(self.interface(1000, oper_status=2))
        self.assertEqual(self.snapshot()["available_mbps"], 0)
        self.observe(self.interface(1000))
        at = self.now + timedelta(seconds=60)
        self.observe(self.interface(1000 + 11_000_000_000 * 60 // 8), at, 16000)
        self.assertEqual(self.snapshot(at)["status"], "anomaly")
        self.assertEqual(self.snapshot(at)["available_mbps"], 0)

    def test_direction_swap_uses_out_octets(self):
        self.wan.observe([self.interface(1000)], self.now, 10000, swap=True, stale_seconds=120)
        at = self.now + timedelta(seconds=60)
        self.wan.observe([self.interface(7_500_001_000)], at, 16000, swap=True, stale_seconds=120)
        self.assertEqual(self.snapshot(at)["wan_down_mbps"], 500)

    def test_phase_boundaries_and_night_budget(self):
        self.wan.valid_at = self.now
        self.wan.status = "ok"
        for when, phase, limit in [(moment(17, 59, 59), "daytime", 4800),
                                    (moment(18), "upload_only", 0),
                                    (moment(22, 59, 59), "upload_only", 0),
                                    (moment(23), "late_makeup", 2000),
                                    (moment(23, 59, 59), "late_makeup", 2000),
                                    (moment(0, day=4), "daytime", 4800)]:
            with self.subTest(when=when):
                self.wan.valid_at = when
                self.assertEqual(daily_phase(when), phase)
                self.assertEqual(self.snapshot(when)["phase_limit_mbps"], limit)


class WholeLineAllocationTests(unittest.TestCase):
    def test_surplus_tiers_yield_to_primary_and_stop_at_three(self):
        accounts = [self.account(name) for name in ['primary', 'extra', 'higher', 'done']]
        groups = [FleetGroup('standard', '普通', 4000, 20)]
        forecasts = {
            'primary': {'remaining_download_bytes': 1},
            'extra': {'remaining_download_bytes': 0, 'predicted_upload_bytes': 1000000000, 'today_download_bytes': 2000000000},
            'higher': {'remaining_download_bytes': 0, 'predicted_upload_bytes': 1000000000, 'today_download_bytes': 2500000000},
            'done': {'remaining_download_bytes': 0, 'predicted_upload_bytes': 1000000000, 'today_download_bytes': 3000000000},
        }
        self.assertEqual(allocate_daily(accounts, groups, forecasts, 200, {}, {}, keep=['higher'], surplus_seconds=3600), {'primary': 200})
        self.assertEqual(list(allocate_daily(accounts, groups, forecasts, 600, {}, {}, surplus_seconds=3600)), ['primary', 'extra', 'higher'])
        self.assertEqual(allocate_daily(accounts, groups, forecasts, 600, {}, {}, surplus_seconds=1), {'primary': 200})
        self.assertEqual(allocate_daily(accounts, groups, forecasts, 600, {}, {}), {'primary': 200})

    @staticmethod
    def account(name, weight=1, group="standard"):
        return FleetAccount(name, "192.168.2.11", name, True, weight, group)

    @staticmethod
    def forecast(accounts, debt=1_000_000_000):
        return {item.account_id: {"remaining_download_bytes": debt} for item in accounts}

    def test_mixed_profiles_respect_whole_line_group_and_global_budgets(self):
        accounts = [self.account(f"a{i}") for i in range(30)] + [self.account(f"s{i}", 10, "symmetric500") for i in range(15)]
        groups = [FleetGroup("standard", "普通", 800, 2), FleetGroup("symmetric500", "对等", 1500, 10)]
        allocation = allocate_daily(accounts, groups, self.forecast(accounts), 2100, {}, {})
        self.assertLessEqual(sum(allocation.values()), 2100)
        self.assertEqual(sum(key.startswith("a") for key in allocation), 2)
        self.assertEqual(sum(key.startswith("s") for key in allocation), 3)
        self.assertTrue(all(rate in (200, 500) for rate in allocation.values()))

    def test_global_twenty_process_cap_with_256_accounts(self):
        accounts = [self.account(f"a{i:03}") for i in range(256)]
        groups = [FleetGroup("standard", "普通", 5000, 256)]
        result = allocate_daily(accounts, groups, self.forecast(accounts), 5000, {}, {})
        self.assertEqual(len(result), 20)
        self.assertEqual(sum(result.values()), 4000)

    def test_no_fractional_line_and_no_disabled_group(self):
        accounts = [self.account("a"), self.account("s", 10, "symmetric500")]
        groups = [FleetGroup("standard", "普通", 199, 10), FleetGroup("symmetric500", "对等", 5000, 10, False)]
        self.assertEqual(allocate_daily(accounts, groups, self.forecast(accounts), 4800, {}, {}), {})
        groups[0] = FleetGroup("standard", "普通", 4000, 20)
        self.assertEqual(allocate_daily(accounts, groups, self.forecast(accounts), 199, {}, {}), {})

    def test_priority_is_required_runtime_not_just_raw_bytes(self):
        accounts = [self.account("a"), self.account("s", 10, "symmetric500")]
        forecasts = {"a": {"remaining_download_bytes": 400_000_000}, "s": {"remaining_download_bytes": 800_000_000}}
        groups = [FleetGroup("standard", "普通", 4000, 20), FleetGroup("symmetric500", "对等", 5000, 10)]
        result = allocate_daily(accounts, groups, forecasts, 500, {}, {})
        self.assertEqual(result, {"a": 200})

    def test_equal_need_fairness_and_protected_minimum(self):
        accounts = [self.account("a"), self.account("b"), self.account("c")]
        groups = [FleetGroup("standard", "普通", 200, 1)]
        forecasts = self.forecast(accounts)
        allocated = {"a": 1800, "b": 0, "c": 0}
        last = {"a": 1, "b": 100, "c": 0}
        self.assertEqual(allocate_daily(accounts, groups, forecasts, 200, allocated, last), {"c": 200})
        self.assertEqual(allocate_daily(accounts, groups, forecasts, 200, allocated, last, keep=["a"]), {"a": 200})
        forecasts["a"]["remaining_download_bytes"] = 0
        self.assertEqual(allocate_daily(accounts, groups, forecasts, 200, allocated, last, keep=["a"]), {"c": 200})


class DailyControllerTests(unittest.TestCase):
    class FakeEngine:
        events = []

        def __init__(self, _cfg, line, _sources, _log, **kwargs):
            if "rate_limit_mbps" in kwargs:
                raise AssertionError("program rate limit must not be reintroduced")
            self.line = line
            self.state = SimpleNamespace(total_bytes=0, status="starting")

        def start(self):
            self.state.status = "downloading"
            self.events.append(("start", self.line.line_id))

        def stop(self):
            self.state.status = "stopped"
            self.events.append(("stop", self.line.line_id))

        def poll(self):
            pass

        def set_sources(self, _sources):
            pass

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {"APPLY_LAN_IPS": "0", "FLEET_AUTOSTART": "false", "FLEET_ACCOUNTS_CSV": "", "SNMP_HOST": ""}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.engines = patch("steam_pumper.fleet.EngineProcess", self.FakeEngine)
        self.engines.start()
        self.addCleanup(self.engines.stop)
        self.FakeEngine.events = []
        self.controller = FleetController(Path(self.directory.name) / "config.json", FleetConfig(daily_mode_enabled=True), snmp_reader=None)
        self.addCleanup(self.controller.shutdown)
        self.controller.manual_enabled = True
        self.now = moment()
        self.forecasts = {}
        self.predictions = patch.object(self.controller.store, "daily_forecasts", side_effect=lambda _now: self.forecasts)
        self.predictions.start()
        self.addCleanup(self.predictions.stop)

    def import_accounts(self, rows, *, weighted=True):
        header = "account_id,bind_ip,snmp_interface,weight" if weighted else "account_id,bind_ip,snmp_interface"
        csv_rows = [header]
        for number, (name, weight) in enumerate(rows, 11):
            row = f"{name},192.168.2.{number},pppoe-{name}"
            csv_rows.append(row + (f",{weight}" if weighted else ""))
        self.controller.store.import_accounts(parse_accounts_csv("\n".join(csv_rows) + "\n"))
        self.controller.snmp_quality = {name: "ok" for name, _weight in rows}
        self.forecasts = {name: {"remaining_download_bytes": 1_000_000_000, "target_download_bytes": 1_000_000_000} for name, _weight in rows}

    def set_budget(self, amount, now=None):
        when = now or self.now
        self.controller.wan_budget.valid_at = when
        self.controller.wan_budget.status = "ok"
        self.controller.wan_budget.down_mbps = 4800 - amount

    def group_limit(self, limit=200, count=1):
        self.controller.store.update_groups([{"group_id": "standard", "name": "普通", "download_limit_mbps": limit, "max_active": count, "enabled": True}])

    def schedule(self, when=None):
        self.controller._schedule(when or self.now)

    def set_override(self, when):
        with self.controller.store.db:
            self.controller.store._set_meta("evening_override", json.dumps({
                "date": when.date().isoformat(), "account_ids": ["adsl1001", "adsl1002"],
                "expires_at": "2026-10-04T00:00:00+08:00", "max_mbps": 2000,
            }))

    def test_tonight_override_only_runs_requested_lines_even_after_targets_are_met(self):
        now = moment(19)
        self.import_accounts([("adsl1001", 10), ("adsl1002", 10), ("other", 1)])
        for name in ("adsl1001", "adsl1002"):
            self.forecasts[name]["remaining_download_bytes"] = 0
        self.set_override(now)
        self.set_budget(4800, now)
        self.schedule(now)
        self.assertEqual(set(self.controller.active), {"adsl1001", "adsl1002"})
        self.assertEqual(sum(x.assigned_mbps for x in self.controller.active.values()), 1000)
        self.assertEqual(self.controller._router_snapshot(now)["phase_limit_mbps"], 2000)

    def test_tonight_override_expires_at_midnight_and_preserves_next_day_policy(self):
        now = moment(23, 59)
        self.import_accounts([("adsl1001", 10), ("adsl1002", 10)])
        self.set_override(now)
        self.set_budget(4800, now)
        self.schedule(now)
        self.assertEqual(len(self.controller.active), 2)
        next_day = moment(0, day=4)
        self.forecasts = {name: {"remaining_download_bytes": 0, "target_download_bytes": 0}
                          for name in ("adsl1001", "adsl1002")}
        self.set_budget(4800, next_day)
        self.schedule(next_day)
        self.assertIsNone(self.controller._evening_override(next_day))
        self.assertEqual(self.controller._daily_phase(next_day), "daytime")
        self.assertFalse(self.controller.active)

    def test_evening_override_cannot_bypass_wan_budget_or_manual_stop(self):
        now = moment(19)
        self.import_accounts([("adsl1001", 10), ("adsl1002", 10)])
        self.set_override(now)
        self.set_budget(800, now)
        self.schedule(now)
        self.assertEqual(len(self.controller.active), 1)
        self.controller.stop_downloads()
        self.schedule(now)
        self.assertFalse(self.controller.active)
        self.controller.manual_enabled = True
        self.controller.wan_budget.valid_at = now - timedelta(seconds=121)
        self.schedule(now)
        self.assertFalse(self.controller.active)

    def test_late_override_keeps_two_lines_prioritized_and_aggregate_below_two_g(self):
        now = moment(23)
        self.import_accounts([("adsl1001", 10), ("adsl1002", 10)] + [(f"other{i}", 1) for i in range(10)])
        self.set_override(now)
        self.set_budget(4800, now)
        self.schedule(now)
        self.assertIn("adsl1001", self.controller.active)
        self.assertIn("adsl1002", self.controller.active)
        self.assertEqual(sum(x.assigned_mbps for x in self.controller.active.values()), 2000)

    def test_unconfirmed_profile_does_not_start_and_daily_mode_cannot_be_disabled(self):
        self.import_accounts([("a", 1)], weighted=False)
        self.set_budget(4800)
        self.schedule()
        self.assertFalse(self.controller.active)
        with self.assertRaises(ValueError):
            self.controller.update_config({"daily_mode_enabled": False})
        self.assertTrue(self.controller.cfg.daily_mode_enabled)

    def test_minimum_30_minutes_then_fair_rotation(self):
        self.import_accounts([("a", 1), ("b", 1)])
        self.group_limit()
        self.set_budget(200)
        self.schedule()
        self.assertEqual(set(self.controller.active), {"a"})
        self.controller.store.record_runtime("a", self.now, self.now + timedelta(minutes=29))
        later = self.now + timedelta(minutes=29)
        self.set_budget(200, later)
        self.schedule(later)
        self.assertEqual(set(self.controller.active), {"a"})
        later = self.now + timedelta(minutes=30)
        self.set_budget(200, later)
        self.schedule(later)
        self.assertEqual(set(self.controller.active), {"b"})
        self.assertEqual(self.FakeEngine.events[-2:], [("stop", "a"), ("start", "b")])

    def test_achieved_target_releases_before_minimum_and_replaces(self):
        self.import_accounts([("a", 1), ("b", 1)])
        self.group_limit()
        self.set_budget(200)
        self.schedule()
        self.forecasts["a"]["remaining_download_bytes"] = 0
        self.schedule(self.now + timedelta(seconds=60))
        self.assertEqual(set(self.controller.active), {"b"})
        self.assertEqual(self.FakeEngine.events[-2:], [("stop", "a"), ("start", "b")])

    def test_budget_decrease_interrupts_minimum_and_never_assigns_partial_rate(self):
        self.import_accounts([("a", 1), ("s", 10)])
        self.set_budget(700)
        self.schedule()
        self.assertEqual(self.controller.daily_allocation, {"a": 200, "s": 500})
        later = self.now + timedelta(seconds=60)
        self.set_budget(500, later)
        self.schedule(later)
        self.assertEqual(self.controller.daily_allocation, {"a": 200})
        self.assertNotIn("s", self.controller.active)

    def test_fault_and_group_changes_interrupt_minimum(self):
        self.import_accounts([("a", 1), ("b", 1)])
        self.group_limit()
        self.set_budget(200)
        self.schedule()
        self.controller.snmp_quality["a"] = "offline"
        self.schedule(self.now + timedelta(seconds=60))
        self.assertEqual(set(self.controller.active), {"b"})
        self.controller.store.update_groups([{"group_id": "standard", "name": "普通", "download_limit_mbps": 200, "max_active": 1, "enabled": False}])
        self.controller.daily_groups_changed = True
        self.schedule(self.now + timedelta(seconds=61))
        self.assertFalse(self.controller.active)

    def test_18_23_midnight_transitions_with_two_profiles(self):
        self.import_accounts([("a", 1), ("s", 10)])
        before18 = moment(17, 59)
        self.set_budget(700, before18)
        self.schedule(before18)
        self.assertEqual(len(self.controller.active), 2)
        self.set_budget(700, moment(18))
        self.schedule(moment(18))
        self.assertFalse(self.controller.active)
        late = moment(23)
        self.set_budget(4800, late)
        self.schedule(late)
        self.assertEqual(sum(self.controller.daily_allocation.values()), 700)
        self.assertLessEqual(sum(self.controller.daily_allocation.values()), 2000)
        # New date has no yesterday debt, regardless of current session age.
        self.forecasts = {"a": {"remaining_download_bytes": 0, "target_download_bytes": 0}, "s": {"remaining_download_bytes": 0, "target_download_bytes": 0}}
        midnight = moment(0, day=4)
        self.set_budget(4800, midnight)
        self.schedule(midnight)
        self.assertFalse(self.controller.active)

    def test_wan_staleness_stops_existing_engines_without_waiting_rotation(self):
        self.import_accounts([("a", 1)])
        self.set_budget(200)
        self.schedule()
        self.schedule(self.now + timedelta(seconds=121))
        self.assertFalse(self.controller.active)

    def test_initial_baseline_and_failed_engine_are_not_selected(self):
        self.import_accounts([("a", 1), ("b", 1)])
        self.group_limit()
        self.set_budget(200)
        self.controller.snmp_quality["a"] = "baseline"
        self.schedule()
        self.assertEqual(set(self.controller.active), {"b"})
        self.controller._quarantine("b", self.now, "cannot connect")
        self.controller.snmp_quality["a"] = "ok"
        self.schedule(self.now + timedelta(seconds=1))
        self.assertEqual(set(self.controller.active), {"a"})

    def test_slow_notice_threshold_uses_minimum_of_50_and_quarter_allocation(self):
        account = FleetAccount("a", "192.168.2.11", "pppoe-a", True, 1, "standard")
        started = self.now - timedelta(seconds=180)
        for assigned, actual_mbps, expected in [(100, 24, "threshold=25Mbps"), (100, 26, ""), (200, 49, "threshold=50Mbps"), (500, 51, "")]:
            with self.subTest(assigned=assigned, actual=actual_mbps):
                count = int(actual_mbps * 1_000_000 / 8 * 180)
                active = ActiveLine(account, SimpleNamespace(), started, last_progress_at=self.now,
                                    rate_samples=deque([(started, 0), (self.now, count)]), assigned_mbps=assigned)
                with patch.object(self.controller, "log") as log:
                    reason = self.controller._line_health_failure(active, self.now)
                self.assertEqual(reason, "")
                if expected:
                    self.assertIn(expected, log.call_args[0][0])
                else:
                    log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
