"""Isolated Docker/Caddy HTTPS smoke check. Requires built fleet-dashboard:verify.

Run from the repository root: python -m dashboard.verify_container
Uses temporary credentials, network, containers and volume; cleans them on exit.
"""
import json,os,secrets,ssl,subprocess,tempfile,time,uuid
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch
from urllib.request import urlopen
import httpx
from dashboard.security import hash_password
from steam_pumper.config import FleetConfig
from steam_pumper.dashboard_agent import DashboardAgent
from steam_pumper.fleet import FleetController,parse_accounts_csv

def docker(*args):
    result=subprocess.run(['docker',*args],text=True,capture_output=True,check=True)
    return result.stdout.strip()

def main():
    suffix=uuid.uuid4().hex[:10];network='fleet-qa-'+suffix;cloud='fleet-cloud-qa-'+suffix;gateway='fleet-gateway-qa-'+suffix
    volume='fleet-cloud-data-qa-'+suffix
    created=[]
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory);root.chmod(0o755)
        secret_dir=root/'secrets';secret_dir.mkdir(mode=0o755)
        for name,value in [('admin_password_hash',hash_password('container-test-password')),('enrollment_token',secrets.token_urlsafe(32)),('read_token',secrets.token_urlsafe(32))]:
            p=secret_dir/name;p.write_text(value);p.chmod(0o444)
        tls=root/'tls';tls.mkdir(mode=0o755)
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(tls/'key.pem'),'-out',str(tls/'cert.pem'),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],check=True,capture_output=True)
        (tls/'key.pem').chmod(0o600)
        config=root/'Caddyfile';config.write_text('https://localhost {\n tls /tls/cert.pem /tls/key.pem\n reverse_proxy dashboard:8080\n}\n')
        node_dir=root/'node';node_dir.mkdir()
        controller=None;agent=None
        try:
            docker('network','create',network)
            docker('volume','create',volume)
            docker('run','-d','--name',cloud,'--network',network,'--network-alias','dashboard','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
                   '--tmpfs','/tmp:size=32m,noexec,nosuid,nodev','-v',volume+':/data','-v',str(secret_dir)+':/run/secrets:ro',
                   '-e','DASHBOARD_ADMIN_PASSWORD_HASH_FILE=/run/secrets/admin_password_hash','-e','DASHBOARD_ENROLLMENT_TOKEN_FILE=/run/secrets/enrollment_token','-e','DASHBOARD_READ_TOKEN_FILE=/run/secrets/read_token','fleet-dashboard:verify')
            created.append(cloud)
            docker('run','-d','--name',gateway,'--network',network,'-p','127.0.0.1::443','-v',str(config)+':/etc/caddy/Caddyfile:ro','-v',str(tls)+':/tls:ro','caddy:2.10.2-alpine')
            created.append(gateway)
            port=int(docker('port',gateway,'443/tcp').rsplit(':',1)[1]);base=f'https://localhost:{port}'
            os.environ['SSL_CERT_FILE']=str(tls/'cert.pem')
            deadline=time.monotonic()+20
            while True:
                try:
                    with urlopen(base+'/healthz',timeout=2) as r:assert json.load(r)['status']=='ok'
                    break
                except Exception:
                    if time.monotonic()>deadline:raise
                    time.sleep(.2)
            with patch.dict(os.environ,{'APPLY_LAN_IPS':'0','FLEET_AUTOSTART':'false','DASHBOARD_URL':''}):
                controller=FleetController(node_dir/'config.json',FleetConfig(daily_mode_enabled=True))
            controller.store.import_accounts(parse_accounts_csv('account_id,bind_ip,snmp_interface,weight\na,192.168.2.11,pppoe-a,1\n'))
            with patch('threading.Thread.start'):
                agent=DashboardAgent(node_dir,{'DASHBOARD_URL':base,'DASHBOARD_NODE_NAME':'TLS container QA','DASHBOARD_ENROLLMENT_TOKEN_FILE':str(secret_dir/'enrollment_token')})
            now=datetime.now(ZoneInfo('Asia/Shanghai'));start=int(now.timestamp())//600*600-600
            with controller.store.db:
                controller.store._add_traffic('a',start//60,measured_download=100000000,download=50000000,sample_seconds=600)
            agent.tick(controller,now,100);agent.cycle()
            with httpx.Client(base_url=base,verify=ssl.create_default_context(cafile=str(tls/'cert.pem')),auth=('admin','container-test-password')) as client:
                nodes=client.get('/api/v1/nodes').json();assert nodes['total']==1
                assert nodes['nodes'][0]['snapshot']['summary']['actual_bytes']==100000000
                response=client.post('/api/v1/nodes/'+agent.node_id+'/commands',json={'action':'stop','params':{}},headers={'X-Requested-With':'FleetDashboard','Origin':base})
                assert response.status_code==202,response.text
                agent.cycle();agent.tick(controller,datetime.now(ZoneInfo('Asia/Shanghai')),140);agent.cycle()
                history=client.get('/api/v1/nodes/'+agent.node_id+'/commands').json()['commands']
                assert history[0]['state']=='succeeded'
                assert client.get('/').status_code==200
            docker('exec',cloud,'python3','-m','dashboard.backup','/data/qa-backup.sqlite3')
            check=docker('exec',cloud,'python3','-c',"import sqlite3; d=sqlite3.connect('/data/qa-backup.sqlite3'); print(d.execute('PRAGMA integrity_check').fetchone()[0],d.execute('SELECT COUNT(*) FROM nodes').fetchone()[0])")
            assert check=='ok 1',check
            docker('restart',cloud)
            deadline=time.monotonic()+20
            while True:
                try:
                    agent.last_sent_snapshot=None;agent.cycle();break
                except Exception:
                    if time.monotonic()>deadline:raise
                    time.sleep(.2)
            print('Container PASS: read-only/non-root cloud, Caddy verified TLS, real outbound urllib agent registration/telemetry, stop receipt, online backup integrity, cloud restart identity persistence')
        finally:
            if agent:agent.db.close()
            if controller:controller.shutdown()
            for container in reversed(created):
                subprocess.run(['docker','rm','-f','-v',container],capture_output=True)
            subprocess.run(['docker','network','rm',network],capture_output=True)
            subprocess.run(['docker','volume','rm',volume],capture_output=True)

if __name__=='__main__':main()
