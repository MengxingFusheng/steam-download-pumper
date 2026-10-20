import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from steam_pumper.dashboard_classic import AGGREGATE_LINE, ClassicCollector

TZ = ZoneInfo('Asia/Shanghai')


def status(total=0, first=0, second=0, available=True):
    return {'manual_enabled': True, 'running': available, 'within_window': available,
            'config': {'start_time': '00:00', 'end_time': '18:00', 'target_mbps': 800, 'line_count': 2,
                       'connections_per_line': 8, 'max_connections_per_line': 12, 'rate_limit_enabled': True,
                       'source_pool': ['https://private-source.example/secret'], 'password': 'not-for-cloud'},
            'logs': ['private'], 'metrics': {'today_bytes': total, 'theoretical_window_bytes': 6_480_000_000_000,
                'minimum_accept_bytes': 5_184_000_000_000, 'current_mbps': 800,
                'lines': [{'line_id': f'line-{index}', 'bind_ip': f'192.168.1.{232+index}',
                           'today_bytes': amount, 'target_mbps': 400, 'current_mbps': 400 if available else 0,
                           'avg60_mbps': 400 if available else 0, 'status': 'downloading' if available else 'stopped',
                           'metrics_available': available, 'connections': 8}
                          for index, amount in enumerate((first, second), 1)]}}


class ClassicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.env = {'DASHBOARD_URL': 'https://dashboard.example.test', 'CLASSIC_POLL_SECONDS': '15'}
        self.collectors = []
        self.now = datetime(2026, 10, 12, 12, tzinfo=TZ)

    def collector(self, env=None):
        with patch('threading.Thread.start'):
            value = ClassicCollector(self.directory, {**self.env, **(env or {})})
        self.collectors.append(value)
        return value

    def tearDown(self):
        for collector in self.collectors:
            collector.agent.db.close()
        self.tmp.cleanup()

    def test_seed_offset_and_identity_survive_collector_restart_without_double_counting(self):
        seed = self.directory/'seed.json'
        seed.write_text(json.dumps({'captured_at': int(self.now.timestamp()), 'status': status(4000, 1000, 3000)}))
        collector = self.collector({'CLASSIC_SEED_FILE': str(seed)})
        first = collector.observe(status(50, 20, 30), self.now)
        self.assertEqual(first['summary']['actual_bytes'], 4050)
        self.assertEqual([line['actual_bytes'] for line in first['lines']], [1020, 3030])
        second = collector.observe(status(100, 40, 60), self.now+timedelta(seconds=15))
        self.assertEqual(second['summary']['actual_bytes'], 4100)
        identity = (collector.agent.node_id, collector.agent.token)
        collector.agent.db.close(); self.collectors.remove(collector)
        seed.write_text(json.dumps({'captured_at': int(self.now.timestamp()), 'status': status(9000, 3000, 6000)}))
        restarted = self.collector({'CLASSIC_SEED_FILE': str(seed)})
        self.assertEqual((restarted.agent.node_id, restarted.agent.token), identity)
        self.assertEqual(restarted.observe(status(100, 40, 60), self.now+timedelta(seconds=30))['summary']['actual_bytes'], 4100)
        self.assertEqual(restarted.observe(status(120, 50, 70), self.now+timedelta(seconds=45))['summary']['actual_bytes'], 4120)
        self.assertEqual(restarted.observe(status(3, 1, 2), self.now+timedelta(days=1))['summary']['actual_bytes'], 3)
        self.assertNotIn('private-source', json.dumps(second))
        self.assertNotIn('not-for-cloud', json.dumps(second))
        self.assertNotIn('source_pool', restarted.agent.get('classic_seed'))

    def test_program_counter_reset_and_new_day_have_distinct_accounting(self):
        collector = self.collector()
        collector.observe(status(500, 200, 300), self.now)
        reset = collector.observe(status(10, 4, 6), self.now+timedelta(seconds=15))
        self.assertEqual(reset['summary']['actual_bytes'], 510)
        self.assertEqual([line['actual_bytes'] for line in reset['lines']], [204, 306])
        self.assertEqual(collector.observe(status(100, 40, 60), self.now+timedelta(seconds=30))['summary']['actual_bytes'], 600)
        tomorrow = collector.observe(status(20, 8, 12), self.now+timedelta(days=1))
        self.assertEqual(tomorrow['summary']['actual_bytes'], 20)
        self.assertEqual([line['actual_bytes'] for line in tomorrow['lines']], [8, 12])

    def test_fallback_is_not_fabricated_as_line_traffic_or_upload(self):
        collector = self.collector()
        snapshot = collector.observe(status(160_000, 0, 0, available=False), self.now)
        self.assertEqual(snapshot['summary']['actual_bytes'], 160_000)
        self.assertEqual(sum(line['actual_bytes'] for line in snapshot['lines']), 0)
        self.assertEqual(snapshot['stats_source'], 'interface_fallback')
        self.assertEqual(snapshot['summary']['target_bytes'], 6_480_000_000_000)
        self.assertEqual(snapshot['summary']['minimum_accept_bytes'], 5_184_000_000_000)
        self.assertEqual([line['target_bytes'] for line in snapshot['lines']], [3_240_000_000_000]*2)
        self.assertEqual(snapshot['measurement_basis'], 'program')
        self.assertEqual(snapshot['target_basis'], 'configured_speed')
        self.assertEqual(snapshot['capabilities'], [])
        self.assertEqual(snapshot['periods'], [])
        self.assertIsNone(snapshot['summary']['upload_bytes'])
        self.assertIsNone(snapshot['summary']['upload_mbps'])
        self.assertTrue(all(line['upload_bytes'] is None and line['current_upload_mbps'] is None for line in snapshot['lines']))
        # Upstream changes its aggregate from interface fallback to helper sums.
        # A source switch is not a controller restart, so no old total is added.
        program = collector.observe(status(100_000, 40_000, 60_000), self.now+timedelta(seconds=15))
        self.assertEqual(program['summary']['actual_bytes'], 160_000)
        self.assertEqual(program['stats_source'], 'program')
        later = collector.observe(status(150_000, 60_000, 90_000), self.now+timedelta(seconds=30))
        self.assertEqual(later['summary']['actual_bytes'], 210_000)
        self.assertIsNone(later['rolling'])

    def test_telemetry_uses_program_deltas_with_real_sampling_gaps(self):
        collector = self.collector()
        start = self.now.replace(minute=0, second=0)
        collector.observe(status(100, 40, 60), start)
        collector.observe(status(200, 80, 120), start+timedelta(seconds=15))
        collector.unavailable()
        collector.observe(status(900, 360, 540), start+timedelta(minutes=11))
        payloads = [json.loads(row[0]) for row in collector.agent.db.execute('SELECT payload FROM buckets')]
        self.assertEqual(len(payloads), 3)
        node = next(value for value in payloads if value['line_id'] == AGGREGATE_LINE)
        self.assertEqual(node['program_bytes'], 100)
        self.assertEqual(node['actual_bytes'], 0)
        self.assertEqual(node['measured_bytes'], 0)
        self.assertEqual(node['sample_seconds'], 0)
        self.assertEqual(node['quality'], 'program_only')
        self.assertEqual(node['unknown_seconds'], 585)
        self.assertTrue(all(value['start'] == int(start.timestamp()) for value in payloads))
        collector.observe(status(1000, 400, 600), start+timedelta(minutes=11, seconds=15))
        collector.observe(status(1100, 440, 660), start+timedelta(minutes=20))
        aggregate = [json.loads(row[0]) for row in collector.agent.db.execute('SELECT payload FROM buckets WHERE line_id=?', (AGGREGATE_LINE,))]
        self.assertEqual([value['start'] for value in aggregate], [int(start.timestamp()), int(start.timestamp())+600])
        self.assertEqual([value['program_bytes'] for value in aggregate], [100, 100])

    def test_upstream_failure_expires_heartbeats_and_commands_are_read_only(self):
        collector = self.collector()
        now = datetime.fromtimestamp(time.time(), TZ)
        collector.observe(status(0, 0, 0), now)
        with patch.object(collector.agent, 'request', return_value={'command': None}) as request:
            collector.agent.cycle()
            self.assertTrue(any(call.args[0] == '/heartbeat' for call in request.call_args_list))
            collector.unavailable()
            previous = collector.agent.get('snapshot')
            with patch('time.time', return_value=now.timestamp()+61):
                request.reset_mock(); collector.agent.cycle(); request.assert_not_called()
            self.assertEqual(collector.agent.get('snapshot'), previous)
        command = {'id': 'classic-stop', 'action': 'stop', 'params': {}, 'expires_at': int(now.timestamp())+300}
        with collector.agent.lock, collector.agent.db:
            collector.agent.db.execute("INSERT INTO commands VALUES (?,?,'queued',NULL)", (command['id'], json.dumps(command)))
        collector.agent.incoming.put(command); collector.reject_commands()
        self.assertEqual(collector.agent.db.execute('SELECT state FROM commands').fetchone()[0], 'failed')
        self.assertEqual(collector.agent.get('snapshot'), previous)

    def test_closed_bucket_targets_survive_outage_and_later_configuration_change(self):
        collector = self.collector()
        collector.observe(status(100, 40, 60), self.now+timedelta(seconds=570))
        collector.observe(status(200, 80, 120), self.now+timedelta(seconds=585))
        collector.observe(status(300, 120, 180), self.now+timedelta(seconds=600))
        start=int(self.now.timestamp())
        def bucket():
            return json.loads(collector.agent.db.execute('SELECT payload FROM buckets WHERE line_id=? AND start=?', (AGGREGATE_LINE,start)).fetchone()[0])
        first=bucket()
        self.assertEqual(first['expected_bytes'],6_480_000_000_000)
        changed=status(400,160,240)
        changed['config']['target_mbps']=900
        changed['metrics']['theoretical_window_bytes']=7_290_000_000_000
        collector.observe(changed,self.now+timedelta(seconds=1200))
        self.assertEqual(bucket()['expected_bytes'],first['expected_bytes'])
        self.assertEqual(bucket()['target_config_version'],first['target_config_version'])
        self.assertEqual(bucket()['target_observed_at'],first['target_observed_at'])

    def test_bad_status_does_not_mutate_counters_or_transmit_raw_configuration(self):
        collector = self.collector()
        snapshot = collector.observe(status(100, 40, 60), self.now)
        for change in ({'today_bytes': -1}, {'today_bytes': True}, {'theoretical_window_bytes': 'secret'}):
            invalid = status(200, 80, 120); invalid['metrics'].update(change)
            with self.assertRaises(ValueError): collector.observe(invalid, self.now+timedelta(seconds=15))
        bad = status(); bad['config']['start_time'] = 'https://secret.invalid/token'
        with self.assertRaises(ValueError): collector.observe(bad, self.now)
        self.assertEqual(json.loads(collector.agent.get('snapshot')), snapshot)
        self.assertEqual(collector.agent.db.execute('SELECT total FROM classic_counters WHERE key=?', (AGGREGATE_LINE,)).fetchone()[0], 100)


if __name__ == '__main__':
    unittest.main()
