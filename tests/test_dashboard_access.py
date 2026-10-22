import tempfile
import unittest
import uuid
from fastapi.testclient import TestClient
from dashboard.app import create_app
from dashboard.security import hash_password


class DashboardAccessTests(unittest.TestCase):
    def test_viewer_read_access_and_write_denial_after_admin_rename(self):
        with tempfile.TemporaryDirectory() as root:
            app=create_app({'DASHBOARD_DB':root+'/dashboard.db','DASHBOARD_ADMIN_USER':'mengxing',
                'DASHBOARD_ADMIN_PASSWORD_HASH':hash_password('administrator-fixture'),
                'DASHBOARD_VIEWER_USER':'admin','DASHBOARD_VIEWER_PASSWORD_HASH':hash_password('viewer-fixture'),
                'DASHBOARD_ENROLLMENT_TOKEN':'e'*40,'DASHBOARD_READ_TOKEN':'r'*40})
            with TestClient(app) as client:
                identity=str(uuid.uuid4())
                r=client.post('/api/v1/agent/register',headers={'Authorization':'Bearer '+'e'*40},json={'node_id':identity,'node_token':'a'*40,'name':'before'})
                self.assertEqual(r.status_code,200)
                viewer=('admin','viewer-fixture');admin=('mengxing','administrator-fixture')
                for path in ['/api/v1/nodes','/api/v1/nodes/'+identity,'/api/v1/nodes/'+identity+'/daily-traffic','/api/v1/nodes/'+identity+'/commands']:
                    self.assertEqual(client.get(path,auth=viewer).status_code,200,path)
                self.assertEqual(client.get('/api/v1/nodes',auth=('admin','administrator-fixture')).status_code,401)
                headers={'X-Requested-With':'FleetDashboard'}
                self.assertEqual(client.patch('/api/v1/nodes/'+identity,auth=viewer,headers=headers,json={'name':'unauthorized'}).status_code,403)
                self.assertEqual(client.post('/api/v1/nodes/'+identity+'/commands',auth=viewer,headers=headers,json={'action':'stop','params':{}}).status_code,403)
                self.assertEqual(client.patch('/api/v1/nodes/'+identity,auth=admin,headers=headers,json={'name':'authorized'}).status_code,200)
                self.assertEqual(client.get('/api/v1/nodes/'+identity,auth=viewer).json()['name'],'authorized')

    def test_duplicate_account_names_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                create_app({'DASHBOARD_DB':root+'/dashboard.db','DASHBOARD_ADMIN_USER':'same',
                    'DASHBOARD_ADMIN_PASSWORD_HASH':hash_password('administrator-fixture'),
                    'DASHBOARD_VIEWER_USER':'same','DASHBOARD_VIEWER_PASSWORD_HASH':hash_password('viewer-fixture'),
                    'DASHBOARD_ENROLLMENT_TOKEN':'e'*40,'DASHBOARD_READ_TOKEN':'r'*40})
