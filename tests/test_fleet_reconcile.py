import tempfile
import unittest
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from steam_pumper.config import FleetConfig
from steam_pumper.fleet import FleetAccount, FleetController, FleetStore, SnmpInterface, SnmpV2Reader, SYSTEM_UPTIME_OID, SYS_UPTIME_OID
from steam_pumper.fleet_reconcile import TIMETICKS_MODULUS, router_rebooted


class RouterUptimeTests(unittest.TestCase):
    def test_normal_progress_and_collection_jitter_are_not_reboots(self):
        for current in (106000, 105900, 106100):
            self.assertFalse(router_rebooted(100000, current, 60))

    def test_wrap_is_not_reboot(self):
        self.assertFalse(router_rebooted(TIMETICKS_MODULUS - 3000, 3000, 60))

    def test_reboot_can_have_greater_uptime_than_previous_sample(self):
        self.assertTrue(router_rebooted(360000, 720000, 6 * 3600))

    def test_invalid_or_incomparable_uptimes_do_not_prove_offline(self):
        for old, new, elapsed in ((1, 0, 0), (1, 0, -1), (-1, 0, 60), (1, -1, 60),
                                  (TIMETICKS_MODULUS, 0, 60), (1, 0, TIMETICKS_MODULUS / 100)):
            self.assertFalse(router_rebooted(old, new, elapsed))

    def test_reader_collects_kernel_and_agent_uptime_separately(self):
        reader = SnmpV2Reader('192.0.2.1', '/unused')
        columns = [{2: 'pppoe-a'}, {}, {2: '100'}, {2: '200'}, {2: '0'}, {2: '1'}]
        with patch.object(reader, '_config_file', return_value=Path('/unused')), \
                patch.object(reader, '_walk', side_effect=columns), \
                patch.object(reader, '_get_int', side_effect=lambda oid, config: 12000 if oid == SYS_UPTIME_OID else 360000) as get:
            interfaces = reader.read()
        self.assertEqual(len(interfaces), 1)
        self.assertEqual(reader.last_sys_uptime, 12000)
        self.assertEqual(reader.last_system_uptime, 360000)
        self.assertEqual(get.call_args_list[-1].args[0], SYSTEM_UPTIME_OID)


class FleetRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'fleet.sqlite3'
        self.cfg = FleetConfig(upload_ceiling_mbps=1000).validate()
        self.store = FleetStore(self.path, self.cfg)
        self.store.import_accounts([FleetAccount('a', '192.168.2.11', 'pppoe-a', True, 10, 'symmetric500')])
        self.start = datetime(2026, 10, 21, 18, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.sample(0, 1000, 2000, system=360000)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def sample(self, seconds, down=1000, up=2000, *, system=None, agent=100000, oper=1, missing=False):
        interfaces = [] if missing else [SnmpInterface(2, 'pppoe-a', '', down, up, oper_status=oper)]
        return self.store.record_snmp(interfaces, self.start + timedelta(seconds=seconds),
                                      device_uptime=agent, system_uptime=system)['a']

    def totals(self):
        return self.store.daily_totals(self.start.date())['a']

    def test_collector_offline_but_router_continuous_keeps_actual_counter_deltas(self):
        self.sample(7200, 5_000_001_000, 1_000_002_000, system=1_080_000, agent=820000)
        total = self.totals()
        self.assertEqual(total['measured_upload'], 1_000_000_000)
        self.assertEqual(total['measured_download'], 5_000_000_000)
        self.assertEqual(total['estimated_upload'], 0)
        self.assertEqual(total['unknown_upload_capacity'], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM router_reboots').fetchone()[0], 0)

    def test_snmp_agent_restart_does_not_discard_continuous_interface_counters(self):
        self.assertEqual(self.sample(60, 5000, 9000, system=366000, agent=1000), 'ok')
        self.assertEqual(self.totals()['measured_upload'], 7000)
        self.assertEqual(self.totals()['estimated_upload'], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM router_reboots').fetchone()[0], 0)

    def test_kernel_reboot_clears_pending_upload_budget_and_counts_only_postboot_bytes(self):
        self.sample(60, missing=True)
        self.assertGreater(self.totals()['unknown_upload_capacity'], 0)
        self.assertEqual(self.sample(600, 50000, 40000, system=12000, agent=12000), 'router_reboot')
        total = self.totals()
        self.assertEqual(total['measured_upload'], 40000)
        self.assertEqual(total['measured_download'], 50000)
        self.assertEqual(total['estimated_upload'], 0)
        self.assertEqual(total['unknown_upload_seconds'], 0)
        self.assertEqual(total['unknown_upload_capacity'], 0)
        self.assertEqual(total['unknown_seconds'], 480)
        self.sample(660, 60000, 47000, system=18000, agent=18000)
        self.assertEqual(self.totals()['measured_upload'], 47000)
        self.assertEqual(self.totals()['measured_download'], 60000)

    def test_reboot_with_uptime_larger_than_old_sample_still_excludes_old_gap(self):
        self.assertEqual(self.sample(21600, 50000, 40000, system=720000), 'router_reboot')
        self.assertEqual(self.totals()['unknown_upload_capacity'], 0)
        self.assertEqual(self.totals()['measured_upload'], 40000)
        self.assertEqual(self.totals()['unknown_seconds'], 14400)

    def test_reboot_evidence_survives_collector_restart(self):
        self.store.close()
        self.store = FleetStore(self.path, self.cfg)
        self.assertEqual(self.sample(600, 50000, 40000, system=12000), 'router_reboot')
        self.assertEqual(self.totals()['unknown_upload_capacity'], 0)

    def test_missing_first_counter_preserves_gap_start_until_reboot_recovery(self):
        self.store.import_accounts(self.store.accounts() + [FleetAccount('b', '192.168.2.12', 'pppoe-b', True, 10, 'symmetric500')])
        self.sample(60, system=366000)
        self.sample(120, system=372000)
        cursor = self.store.db.execute("SELECT * FROM cursors WHERE account_id='b'").fetchone()
        self.assertEqual(cursor['baseline_at'], int((self.start + timedelta(seconds=60)).timestamp()))
        self.sample(600, system=12000)
        total = self.store.daily_totals(self.start.date())['b']
        self.assertEqual(total['unknown_upload_capacity'], 0)
        self.assertEqual(total['unknown_upload_seconds'], 0)
        self.assertEqual(total['unknown_seconds'], 540)

    def test_reboot_across_midnight_does_not_allocate_upload_to_previous_day(self):
        self.sample(7 * 3600, 50000, 40000, system=180000)
        old = self.store.daily_totals(self.start.date())['a']
        new = self.store.daily_totals(self.start.date() + timedelta(days=1))['a']
        self.assertEqual(old['upload'], 0)
        self.assertEqual(old['unknown_upload_capacity'], 0)
        self.assertEqual(new['measured_upload'], 40000)
        self.assertEqual(new['unknown_upload_capacity'], 0)

    def test_missing_line_after_reboot_is_reconciled_when_it_returns_later(self):
        self.assertEqual(self.sample(600, system=12000, missing=True), 'reboot_gap')
        self.assertEqual(self.totals()['unknown_upload_capacity'], 0)
        self.assertEqual(self.sample(1200, 50000, 40000, system=72000), 'router_reboot')
        self.assertEqual(self.totals()['measured_upload'], 40000)
        self.assertEqual(self.totals()['unknown_seconds'], 480)
        self.assertEqual(self.totals()['unknown_upload_capacity'], 0)

    def test_explicit_down_interface_uses_real_counters_without_upload_estimates(self):
        self.assertEqual(self.sample(60, 5000, 9000, system=366000, oper=2), 'offline')
        self.assertEqual(self.sample(120, 5000, 9000, system=372000, oper=2), 'offline')
        total = self.totals()
        self.assertEqual(total['measured_upload'], 7000)
        self.assertEqual(total['measured_download'], 4000)
        self.assertEqual(total['estimated_upload'], 0)
        self.assertEqual(total['unknown_upload_capacity'], 0)
        self.assertEqual(self.sample(180, 6000, 10000, system=378000), 'recovered')
        self.assertEqual(self.totals()['measured_upload'], 8000)

    def test_down_interface_counter_reset_does_not_charge_upload(self):
        self.sample(60, 5000, 9000, system=366000)
        self.assertEqual(self.sample(120, 0, 0, system=372000, oper=2), 'offline')
        self.assertEqual(self.totals()['estimated_upload'], 0)
        self.assertEqual(self.totals()['unknown_upload_capacity'], 0)

    def test_absent_uptime_does_not_turn_an_unverified_gap_into_confirmed_offline(self):
        self.sample(60, missing=True)
        self.assertEqual(self.sample(120, 1, 1), 'reset')
        self.assertGreater(self.totals()['unknown_upload_capacity'], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM router_reboots').fetchone()[0], 0)

    def test_impossible_postboot_counters_are_not_imported_as_measured_upload(self):
        self.sample(600, 10**15, 10**15, system=12000)
        total = self.totals()
        self.assertEqual(total['measured_upload'], 0)
        self.assertEqual(total['estimated_upload'], 0)
        self.assertEqual(total['unknown_upload_seconds'], 120)
        self.assertEqual(total['unknown_upload_capacity'], 120 * 62_500_000)

    def test_previous_measured_traffic_is_preserved_and_recovery_is_not_double_counted(self):
        self.sample(60, 5000, 9000, system=366000)
        self.sample(600, 50000, 40000, system=12000)
        before = self.totals()
        self.sample(600, 50000, 40000, system=12000)
        self.assertEqual(self.totals(), before)
        self.assertEqual(before['measured_upload'], 47000)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM router_reboots').fetchone()[0], 1)

    def test_missing_line_after_router_reboot_is_not_scheduled_for_download(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'APPLY_LAN_IPS': '0', 'DASHBOARD_URL': '', 'SNMP_HOST': '', 'FLEET_AUTOSTART': 'false'}):
            controller = FleetController(Path(directory) / 'config.json', FleetConfig())
            try:
                controller.store.import_accounts(self.store.accounts())
                now = self.start.replace(hour=12)
                controller.manual_enabled = True
                controller.snmp_quality = {'a': 'reboot_gap'}
                controller.wan_budget.valid_at = now
                controller.wan_budget.status = 'ok'
                with patch.object(controller, '_start') as start:
                    controller._schedule_daily(now)
                start.assert_not_called()
                self.assertEqual(controller.daily_allocation, {})
                self.assertEqual(controller._completion_diagnostics(now)[0]['reason'], 'snmp_unavailable')
            finally:
                controller.shutdown()


if __name__ == '__main__':
    unittest.main()
