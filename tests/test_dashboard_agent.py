import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from steam_pumper.config import FleetConfig
from steam_pumper.dashboard_agent import DashboardAgent
from steam_pumper.dashboard_export import export_snapshot, traffic_buckets
from steam_pumper.dashboard_protocol import config_version
from steam_pumper.fleet import FleetController, SnmpInterface, parse_accounts_csv

TZ=ZoneInfo('Asia/Shanghai')


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)
        self.environment=patch.dict('os.environ',{'APPLY_LAN_IPS':'0','FLEET_AUTOSTART':'false','DASHBOARD_URL':''})
        self.environment.start()
        self.controller=FleetController(self.path/'config.json',FleetConfig())
        self.controller.store.import_accounts(parse_accounts_csv('account_id,bind_ip,snmp_interface,weight\na,192.168.2.11,pppoe-a,1\n'))
        self.agent=self.make_agent()

    def make_agent(self):
        with patch('threading.Thread.start'):
            return DashboardAgent(self.path,{'DASHBOARD_URL':'https://cloud.example.test','DASHBOARD_NODE_NAME':'test'})

    def tearDown(self):
        self.agent.db.close()
        self.controller.shutdown()
        self.environment.stop()
        self.tmp.cleanup()

    def test_evening_override_exports_line_list_and_expires_next_day(self):
        self.controller.cfg.daily_mode_enabled = True
        now = datetime(2026, 10, 15, 20, tzinfo=TZ)
        self.controller.store._set_meta('evening_override', json.dumps({
            'date': now.date().isoformat(), 'account_ids': ['a'],
            'expires_at': '2026-10-16T00:00:00+08:00', 'max_mbps': 1000}))
        snapshot = export_snapshot(self.controller, now, {})
        upload = next(p for p in snapshot['periods'] if p['phase'] == 'upload_only')
        self.assertEqual(upload['evening_override'], ['a'])
        tomorrow = export_snapshot(self.controller, now + timedelta(days=1), {})
        self.assertTrue(all(p['evening_override'] == [] for p in tomorrow['periods']))

    def test_export_uses_daily_schedule_and_projects_source_application_status(self):
        now = datetime(2026, 10, 19, 12, tzinfo=TZ)
        with patch.object(self.controller, 'source_list_status', return_value={
                'enabled': True, 'status': 'ready', 'revision': 20261019031700,
                'active_revision': 20261018031700, 'source_count': 20,
                'origin': 'remote', 'apply_state': 'partial', 'apply_error': '',
                'url': 'https://private-config.test/manifest',
                'lines': [{'line_id': 'a', 'generation': 'old', 'pending_generation': 'new',
                           'apply_state': 'failed', 'error': 'timeout', 'sources': ['private']}]}):
            snapshot = export_snapshot(self.controller, now, {})
        self.assertEqual(snapshot['mode'], 'daily')
        self.assertEqual([p['phase'] for p in snapshot['periods']], ['daytime', 'upload_only', 'late_makeup'])
        self.assertNotIn('daily_mode_enabled', snapshot['config'])
        self.assertNotIn('per_line_mbps', snapshot['config'])
        self.assertNotIn('line_minimum_mbps', snapshot['config'])
        self.assertNotIn('url', snapshot['source_list'])
        self.assertNotIn('sources', snapshot['source_list']['lines'][0])
        self.assertEqual(snapshot['source_list']['lines'][0]['apply_state'], 'failed')

    def test_restart_identity_and_uncertain_command_never_replayed(self):
        identity=(self.agent.node_id,self.agent.token)
        command={'id':'x','action':'start','params':{},'expires_at':2000000000,'config_version':''}
        with self.agent.db:
            self.agent.db.execute("INSERT INTO commands VALUES (?,?,'executing',NULL)",('x',json.dumps(command)))
        self.agent.db.close();self.agent=self.make_agent()
        self.assertEqual(identity,(self.agent.node_id,self.agent.token))
        self.assertEqual(self.agent.db.execute("SELECT state FROM commands WHERE id='x'").fetchone()[0],'unknown')
        self.agent.incoming.put(command)
        self.agent.tick(self.controller,datetime.now(TZ),100)
        self.assertFalse(self.controller.manual_enabled)

    def test_spool_survives_network_failure_and_retries_without_duplicates(self):
        now=datetime(2026,10,12,12,tzinfo=TZ)
        start=int(now.timestamp())-600
        with self.controller.store.db:
            self.controller.store._add_traffic('a',start//60,measured_download=1000,download=700,sample_seconds=60)
        self.agent.tick(self.controller,now,100)
        self.agent.set('registered','1')
        with patch.object(self.agent,'request',side_effect=OSError('offline')):
            with self.assertRaises(OSError):self.agent.cycle()
        self.assertEqual(self.agent.db.execute('SELECT COUNT(*) FROM buckets').fetchone()[0],1)
        self.agent.db.close();self.agent=self.make_agent()
        with patch.object(self.agent,'request',return_value={'command':None}) as request:
            self.agent.cycle()
        self.assertEqual(self.agent.db.execute('SELECT COUNT(*) FROM buckets').fetchone()[0],0)
        payload=next(call.args[1] for call in request.call_args_list if call.args[0]=='/telemetry')
        self.assertEqual(payload['buckets'][0]['actual_bytes'],1000)
        self.assertEqual(payload['buckets'][0]['program_bytes'],700)

    def test_local_configuration_conflict_invalid_parameter_and_stop(self):
        now=datetime(2026,10,12,12,tzinfo=TZ)
        for i,params,version in [(1,{'target_ratio':2.1},'old'),(2,{'router_limit_mbps':6000},config_version(self.controller.cfg))]:
            cmd={'id':str(i),'action':'update_schedule','params':params,'config_version':version,'expires_at':int(now.timestamp())+300}
            with self.agent.db:self.agent.db.execute("INSERT INTO commands VALUES (?,?,'queued',NULL)",(str(i),json.dumps(cmd)))
            self.agent.incoming.put(cmd)
        self.agent.tick(self.controller,now,100)
        self.assertEqual(self.controller.cfg.target_ratio,2)
        self.assertEqual(self.controller.cfg.router_limit_mbps,5000)
        self.assertEqual([row[0] for row in self.agent.db.execute('SELECT state FROM commands')],['failed','failed'])
        self.controller.set_manual_enabled(True)
        cmd={'id':'stop','action':'stop','params':{},'config_version':'','expires_at':int(now.timestamp())+300}
        with self.agent.db:self.agent.db.execute("INSERT INTO commands VALUES (?,?,'queued',NULL)",('stop',json.dumps(cmd)))
        self.agent.incoming.put(cmd);self.agent.tick(self.controller,now,140)
        self.assertFalse(self.controller.manual_enabled)

    def test_daily_midnight_closes_actual_and_freezes_previous_config(self):
        self.controller.update_config({'daily_mode_enabled':True})
        before=datetime(2026,10,11,23,59,tzinfo=TZ)
        with self.controller.store.db:
            self.controller.store._add_traffic('a',int(before.timestamp()//60),measured_upload=100,measured_download=300,sample_seconds=60)
        self.agent.tick(self.controller,before,100)
        self.agent.tick(self.controller,before+timedelta(minutes=2),140)
        snapshot=json.loads(self.agent.get('snapshot'))
        previous=next(p for p in snapshot['periods'] if p['date']=='2026-10-11' and p['phase']=='late_makeup')
        self.assertEqual(previous['state'],'closed')
        self.assertEqual(previous['lines'][0]['target_bytes'],200)
        self.assertEqual(previous['lines'][0]['cumulative_actual_bytes'],300)
        self.assertEqual(previous['lines'][0]['completion'],'achieved')
        self.controller.update_config({'target_ratio':3})
        self.agent.tick(self.controller,before+timedelta(minutes=3),180)
        again=next(p for p in json.loads(self.agent.get('snapshot'))['periods'] if p['date']=='2026-10-11' and p['phase']=='late_makeup')
        self.assertEqual(again,previous)
        self.assertEqual(snapshot['summary']['actual_bytes'],0)

    def test_preserves_measured_estimated_unknown_and_excludes_secrets(self):
        now=datetime(2026,10,12,12,tzinfo=TZ)
        start=int(now.timestamp())-600
        with self.controller.store.db:
            self.controller.store._add_traffic('a',start//60,measured_download=100,estimated_download=50,download=80,unknown_seconds=30,sample_seconds=60)
        bucket=traffic_buckets(self.controller,start,int(now.timestamp()))[0]
        self.assertEqual(bucket['actual_bytes'],150)
        self.assertEqual(bucket['quality'],'unknown')
        snapshot=export_snapshot(self.controller,now,{})
        self.assertEqual(snapshot['summary']['measured_bytes'],100)
        self.assertEqual(snapshot['summary']['estimated_bytes'],50)
        self.assertEqual(snapshot['summary']['unknown_seconds'],30)
        self.assertNotIn('source_pool',json.dumps(snapshot))
        self.assertNotIn('SNMP_COMMUNITY',json.dumps(snapshot))

    def test_upload_totals_and_live_rates_use_snmp_not_account_capacity(self):
        now=datetime(2026,10,12,12,tzinfo=TZ)
        self.controller.store.import_accounts(parse_accounts_csv(
            'account_id,bind_ip,snmp_interface,weight,enabled\n'
            'a,192.168.2.11,pppoe-a,1,true\n'
            'b,192.168.2.12,pppoe-b,10,false\n'))
        def sample(at, a_upload, b_upload):
            self.controller.snmp_quality=self.controller.store.record_snmp([
                SnmpInterface(1,'pppoe-a','',1000,a_upload,oper_status=1),
                SnmpInterface(2,'pppoe-b','',1000,b_upload,oper_status=1)],at)
            self.controller.snmp_status='online'
        sample(now-timedelta(seconds=60),1000,2000)
        baseline=export_snapshot(self.controller,now-timedelta(seconds=60),{})
        self.assertIsNone(baseline['lines'][0]['current_upload_mbps'])
        self.assertIsNone(baseline['summary']['upload_mbps'])
        with self.controller.store.db:
            self.controller.store._add_traffic('a',int(now.timestamp()//60)-5,
                                               upload=40,estimated_upload=60,download=999)
            self.controller.store._add_traffic('a',int((now-timedelta(days=1)).timestamp()//60),upload=900)
        sample(now,37_501_000,75_002_000)
        snapshot=export_snapshot(self.controller,now,{})
        a,b=snapshot['lines']
        self.assertEqual(a['upload_mbps'],50)  # Existing profile capacity stays unchanged.
        self.assertEqual(a['current_upload_mbps'],5)
        self.assertEqual(b['current_upload_mbps'],10)
        self.assertEqual(a['upload_bytes'],37_500_100)
        self.assertEqual(snapshot['summary']['upload_bytes'],37_500_100)
        self.assertEqual(snapshot['summary']['upload_mbps'],5)
        self.assertEqual(export_snapshot(self.controller,now+timedelta(seconds=30),{})['summary']['upload_mbps'],5)
        self.assertIsNone(export_snapshot(self.controller,now+timedelta(seconds=121),{})['summary']['upload_mbps'])

    def test_upload_rate_handles_swapped_direction_zero_reset_and_collection_failure(self):
        now=datetime(2026,10,12,12,tzinfo=TZ)
        self.controller.update_config({'snmp_direction_swap':True})
        def sample(at, incoming, outgoing):
            self.controller.snmp_quality=self.controller.store.record_snmp([
                SnmpInterface(1,'pppoe-a','',incoming,outgoing,oper_status=1)],at)
            self.controller.snmp_status='online'
            return export_snapshot(self.controller,at,{})
        self.assertIsNone(sample(now,1000,1000)['summary']['upload_mbps'])
        snapshot=sample(now+timedelta(seconds=60),15_001_000,30_001_000)
        self.assertEqual(snapshot['summary']['upload_mbps'],2)
        self.assertEqual(snapshot['summary']['upload_bytes'],15_000_000)
        self.assertEqual(sample(now+timedelta(seconds=120),15_001_000,30_001_000)['summary']['upload_mbps'],0)
        self.assertIsNone(sample(now+timedelta(seconds=180),1000,1000)['summary']['upload_mbps'])
        self.assertEqual(sample(now+timedelta(seconds=240),7_501_000,1000)['summary']['upload_mbps'],1)
        self.controller.snmp_status='degraded'
        self.assertIsNone(export_snapshot(self.controller,now+timedelta(seconds=250),{})['summary']['upload_mbps'])
        self.controller.snmp_status='online'
        self.assertIsNone(export_snapshot(self.controller,now+timedelta(seconds=260),{})['summary']['upload_mbps'])
        self.assertEqual(sample(now+timedelta(seconds=300),15_001_000,1000)['summary']['upload_mbps'],1)
        self.controller.snmp_quality=self.controller.store.record_snmp([],now+timedelta(seconds=360))
        self.assertIsNone(export_snapshot(self.controller,now+timedelta(seconds=360),{})['summary']['upload_mbps'])

    def test_upload_rate_observes_each_snmp_sample_between_dashboard_exports(self):
        now=datetime(2026,10,12,12,tzinfo=TZ)
        self.controller.update_config({'snmp_poll_seconds':15})
        self.controller.dashboard_agent=self.agent
        def queue_sample(seconds, outgoing, error=None):
            observed_at=now+timedelta(seconds=seconds)
            interfaces=[SnmpInterface(1,'pppoe-a','',1000,outgoing,oper_status=1)] if error is None else None
            self.controller.snmp_results.put((interfaces,None,observed_at,error))
            self.controller.tick(monotonic_now=100+seconds,wall_time=observed_at)
        try:
            with patch.object(self.controller,'_start_snmp_poll'):
                queue_sample(0,1000)
                self.assertIsNone(json.loads(self.agent.get('snapshot'))['summary']['upload_mbps'])
                # This abnormal counter is consumed at t15, while the previous
                # cloud snapshot stays t0. The t30 observation is valid again.
                queue_sample(15,10_000_001_000)
                self.assertEqual(self.controller.snmp_quality['a'],'anomaly')
                self.assertEqual(json.loads(self.agent.get('snapshot'))['observed_at'],int(now.timestamp()))
                queue_sample(30,10_009_376_000)
                self.assertEqual(self.controller.snmp_quality['a'],'ok')
                self.assertEqual(json.loads(self.agent.get('snapshot'))['summary']['upload_mbps'],5)
                queue_sample(45,0,OSError('SNMP collection failed'))
                queue_sample(60,10_018_751_000)
                self.assertIsNone(json.loads(self.agent.get('snapshot'))['summary']['upload_mbps'])
                queue_sample(75,10_028_126_000)
                queue_sample(90,10_037_501_000)
                self.assertEqual(json.loads(self.agent.get('snapshot'))['summary']['upload_mbps'],5)
        finally:
            self.controller.dashboard_agent=None

    def test_overdelivered_upload_survives_ledger_and_dashboard_projection(self):
        self.controller.store.import_accounts(parse_accounts_csv(
            'account_id,bind_ip,snmp_interface,weight\n'
            'a,192.168.2.11,pppoe-a,1\n'
            'b,192.168.2.12,pppoe-b,10\n'))
        self.controller.update_config({'daily_mode_enabled':True,'upload_ceiling_mbps':1000})
        now=datetime(2026,10,12,18,tzinfo=TZ)
        def sample(at, small, large):
            self.controller.snmp_quality=self.controller.store.record_snmp([
                SnmpInterface(1,'pppoe-a','',1000,small,oper_status=1),
                SnmpInterface(2,'pppoe-b','',1000,large,oper_status=1)],at)
            self.controller.snmp_status='online'
            return export_snapshot(self.controller,at,{})
        sample(now,1000,1000)
        small=85 * 1_000_000 // 8 * 60
        large=627 * 1_000_000 // 8 * 60
        snapshot=sample(now+timedelta(seconds=60),1000+small,1000+large)
        self.assertEqual(snapshot['summary']['upload_bytes'],small+large)
        self.assertEqual(snapshot['summary']['upload_mbps'],712)
        self.assertEqual([line['current_upload_mbps'] for line in snapshot['lines']],[85,627])
        self.assertEqual([line['upload_mbps'] for line in snapshot['lines']],[50,500])
        self.assertEqual(snapshot['config']['target_ratio'],2)
        self.assertEqual(self.controller.snmp_quality,{'a':'ok','b':'ok'})

    def test_snmp_recovery_republishes_older_ledger_corrections(self):
        now=datetime(2026,10,12,12,tzinfo=TZ)
        start=int(now.timestamp())-7200
        with self.controller.store.db:
            self.controller.store._add_traffic('a',start//60,unknown_seconds=60)
        self.agent.tick(self.controller,now,100)
        with self.agent.db:self.agent.db.execute('DELETE FROM buckets')
        with self.controller.store.db:
            self.controller.store.db.execute('UPDATE traffic SET unknown_seconds=0, measured_download_bytes=1000,sample_seconds=60 WHERE minute=?',(start//60,))
        self.agent.invalidate_since(start)
        self.agent.tick(self.controller,now+timedelta(seconds=30),140)
        payload=json.loads(self.agent.db.execute('SELECT payload FROM buckets WHERE start=?',(start,)).fetchone()[0])
        self.assertEqual(payload['unknown_seconds'],0)
        self.assertEqual(payload['actual_bytes'],1000)

    def test_ten_minute_target_history_is_not_recomputed(self):
        now=datetime(2026,10,12,12,tzinfo=TZ)
        start=int(now.timestamp())-600
        with self.controller.store.db:
            self.controller.store._add_traffic('a',start//60,measured_upload=100,measured_download=150,sample_seconds=60)
        self.agent.tick(self.controller,now,100)
        first=json.loads(self.agent.db.execute('SELECT payload FROM buckets WHERE start=?',(start,)).fetchone()[0])
        expected = self.controller.store.daily_forecasts(now)['a']['target_download_bytes']
        self.assertEqual(first['expected_bytes'],expected)
        self.assertEqual(first['target_config_version'],config_version(self.controller.cfg))
        self.controller.update_config({'target_ratio':3})
        self.agent.invalidate_since(start)
        self.agent.tick(self.controller,now+timedelta(seconds=40),140)
        second=json.loads(self.agent.db.execute('SELECT payload FROM buckets WHERE start=?',(start,)).fetchone()[0])
        self.assertEqual(second['expected_bytes'],first['expected_bytes'])
        self.assertEqual(second['target_config_version'],first['target_config_version'])

    def test_agent_requires_https(self):
        with self.assertRaises(ValueError):DashboardAgent(self.path,{'DASHBOARD_URL':'http://example.test'})
        with self.assertRaises(ValueError):DashboardAgent(self.path,{'DASHBOARD_URL':'https://password@example.test'})


if __name__=='__main__':unittest.main()

class ComponentBootstrapTests(unittest.TestCase):
    def test_v2_bootstrap_retries_with_component_credential_and_keeps_legacy_payloads(self):
        import uuid
        from urllib.error import HTTPError
        with tempfile.TemporaryDirectory() as directory, patch('threading.Thread.start'):
            node, component = str(uuid.uuid4()), str(uuid.uuid4())
            agent=DashboardAgent(directory, {'DASHBOARD_URL':'https://local.example.test',
                    'DASHBOARD_NODE_ID':node,'DASHBOARD_COMPONENT_ID':component,'DASHBOARD_NODE_NAME':'双功能'})
            try:
                calls=[]
                def request(path,payload=None,enrollment=False,api_version=1):
                    calls.append((path,payload,enrollment,api_version))
                    if path=='/register' and not enrollment and len(calls)==1:
                        raise HTTPError('https://local.example.test',401,'not registered',{},None)
                    return {'command':None}
                with patch.object(agent,'request',side_effect=request):agent.cycle()
                self.assertEqual(calls[0][3],2)
                self.assertTrue(calls[1][2])
                self.assertEqual(calls[1][1]['component_type'],'pumper')
                self.assertEqual(calls[1][1]['node_id'],node)
                self.assertEqual(calls[-1][0],'/commands')
                self.assertEqual(calls[-1][3],1)
                self.assertEqual(agent.get('registered'),'1')
            finally:agent.db.close()
