import copy
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from dashboard.app import create_app
from dashboard.security import hash_password
from steam_pumper.dashboard_export import daily_history

TZ = ZoneInfo('Asia/Shanghai')
NOW = datetime(2026, 10, 22, 12, tzinfo=TZ)


class DailyHistoryTests(unittest.TestCase):
    def test_calendar_days_keep_zero_missing_estimates_and_removed_lines(self):
        db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
        db.execute('''CREATE TABLE traffic(account_id TEXT,minute INTEGER,upload_bytes INTEGER DEFAULT 0,
            snmp_download_bytes INTEGER DEFAULT 0,measured_upload_bytes INTEGER DEFAULT 0,
            measured_download_bytes INTEGER DEFAULT 0,estimated_upload_bytes INTEGER DEFAULT 0,
            estimated_download_bytes INTEGER DEFAULT 0,upload_valid_seconds INTEGER DEFAULT 0,
            download_valid_seconds INTEGER DEFAULT 0,unknown_seconds INTEGER DEFAULT 0,
            sample_seconds INTEGER DEFAULT 0,quality TEXT DEFAULT 'ok')''')
        yesterday = NOW.replace(hour=0) - timedelta(days=1)
        minute = int(yesterday.timestamp())//60
        db.execute('INSERT INTO traffic(account_id,minute,measured_upload_bytes,measured_download_bytes,upload_valid_seconds,download_valid_seconds) VALUES (?,?,?,?,?,?)', ('removed-line',minute,123,456,60,60))
        db.execute('INSERT INTO traffic(account_id,minute,estimated_upload_bytes,estimated_download_bytes,unknown_seconds) VALUES (?,?,?,?,?)', ('a',minute+1439,10,20,5))
        db.execute('INSERT INTO traffic(account_id,minute,upload_valid_seconds,download_valid_seconds) VALUES (?,?,60,60)', ('a',minute-1440))
        db.execute('INSERT INTO traffic(account_id,minute,download_valid_seconds,sample_seconds) VALUES (?,?,NULL,60)', ('a',minute-2880))
        db.execute('INSERT INTO traffic(account_id,minute,measured_upload_bytes) VALUES (?,?,99999)', ('a',minute+1440))
        controller = SimpleNamespace(store=SimpleNamespace(db=db))
        values = daily_history(controller,NOW)
        self.assertEqual(len(values),14)
        self.assertEqual(values[0]['date'],'2026-10-08')
        self.assertEqual(values[-1]['date'],'2026-10-21')
        self.assertEqual(values[-1]['upload_bytes'],133)
        self.assertEqual(values[-1]['download_bytes'],476)
        self.assertEqual(values[-1]['estimated_upload_bytes'],10)
        self.assertEqual(values[-1]['unknown_seconds'],5)
        self.assertEqual(values[-2]['upload_bytes'],0)
        self.assertEqual(values[-2]['download_bytes'],0)
        self.assertIsNone(values[-3]['upload_bytes'])
        self.assertEqual(values[-3]['download_bytes'],0)
        self.assertIsNone(values[-4]['download_bytes'])
        # Refresh completed-day corrections, and roll dates at Beijing midnight.
        db.execute('UPDATE traffic SET measured_upload_bytes=223 WHERE account_id=?',('removed-line',))
        self.assertEqual(daily_history(controller,NOW+timedelta(minutes=10))[-1]['upload_bytes'],233)
        self.assertEqual(daily_history(controller,NOW+timedelta(days=1))[-1]['date'],'2026-10-22')
        db.close()

    def test_cloud_idempotence_correction_validation_and_old_node(self):
        with tempfile.TemporaryDirectory() as tmp, patch('dashboard.app.time.time',return_value=NOW.timestamp()):
            env={'DASHBOARD_DB':tmp+'/dashboard.db','DASHBOARD_ENROLLMENT_TOKEN':'e'*40,
                 'DASHBOARD_READ_TOKEN':'r'*40,'DASHBOARD_ADMIN_PASSWORD_HASH':hash_password('test-password')}
            app=create_app(env)
            with TestClient(app) as client:
                node=str(uuid.uuid4());agent={'Authorization':'Bearer '+'a'*40};reader={'Authorization':'Bearer '+'r'*40}
                r=client.post('/api/v1/agent/register',headers={'Authorization':'Bearer '+'e'*40},json={'node_id':node,'node_token':'a'*40,'name':'history'})
                self.assertEqual(r.status_code,200)
                snapshot={'schema_version':1,'observed_at':int(NOW.timestamp()),'date':'2026-10-22','timezone':'Asia/Shanghai','mode':'daily',
                    'manual_enabled':True,'snmp_status':'online','config':{},'config_version':'a'*64,
                    'summary':dict(actual_bytes=0,program_bytes=0,measured_bytes=0,estimated_bytes=0,legacy_bytes=0,unknown_seconds=0,target_bytes=0,remaining_bytes=0),
                    'lines':[],'groups':[],'router_budget':{},'periods':[]}
                self.assertEqual(client.post('/api/v1/agent/heartbeat',headers=agent,json={'snapshot':snapshot}).status_code,200)
                r=client.get('/api/v1/nodes/'+node+'/daily-traffic',headers=reader)
                self.assertEqual(r.status_code,200)
                self.assertEqual(len(r.json()['days']),14)
                self.assertIsNone(r.json()['days'][-1]['upload_bytes'])
                snapshot['daily_history']=[{'date':'2026-10-21','upload_bytes':0,'download_bytes':123,'estimated_upload_bytes':0,'estimated_download_bytes':3,'unknown_seconds':7,'observed_minutes':1}]
                for _ in range(2):self.assertEqual(client.post('/api/v1/agent/heartbeat',headers=agent,json={'snapshot':snapshot}).status_code,200)
                self.assertEqual(client.get('/api/v1/nodes/'+node+'/daily-traffic',headers=reader).json()['days'][-1]['download_bytes'],123)
                snapshot['daily_history'][0]['download_bytes']=456
                self.assertEqual(client.post('/api/v1/agent/heartbeat',headers=agent,json={'snapshot':snapshot}).status_code,200)
                self.assertEqual(client.get('/api/v1/nodes',headers=reader).json()['nodes'][0]['yesterday']['download_bytes'],456)
                self.assertEqual(client.get('/api/v1/nodes/'+node,headers=reader).json()['yesterday']['upload_bytes'],0)
                for bad in ['2026-10-22','2026-09-01','2026-02-30']:
                    invalid=copy.deepcopy(snapshot);invalid['daily_history'][0]['date']=bad
                    self.assertEqual(client.post('/api/v1/agent/heartbeat',headers=agent,json={'snapshot':invalid}).status_code,422)
                self.assertEqual(client.get('/api/v1/nodes/'+node+'/daily-traffic',headers=reader).json()['days'][-1]['download_bytes'],456)
                self.assertEqual(client.get('/api/v1/nodes/'+node+'/daily-traffic').status_code,401)
