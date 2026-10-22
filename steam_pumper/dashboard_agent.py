"""Optional outbound dashboard agent. Network I/O never runs in Fleet.tick."""
from __future__ import annotations

import json
import os
import queue
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request

from .dashboard_export import export_snapshot, traffic_buckets
from .dashboard_protocol import config_version, validate_command


class DashboardAgent:
    def __init__(self, data_dir, env=None):
        env = os.environ if env is None else env
        self.url = env.get('DASHBOARD_URL', '').rstrip('/')
        if urlparse(self.url).scheme != 'https' or not urlparse(self.url).hostname or urlparse(self.url).username or urlparse(self.url).query or urlparse(self.url).fragment:
            raise ValueError('DASHBOARD_URL must be an HTTPS URL without credentials, query or fragment')
        self.enrollment_file = env.get('DASHBOARD_ENROLLMENT_TOKEN_FILE', '/run/secrets/dashboard_enrollment_token')
        self.name = env.get('DASHBOARD_NODE_NAME', '').strip()[:100] or os.uname().nodename
        self.external_id = env.get('DASHBOARD_EXTERNAL_NODE_ID', '').strip()[:200]
        self.lock = threading.RLock()
        self.db = sqlite3.connect(Path(data_dir) / 'dashboard-agent.sqlite3', check_same_thread=False)
        os.chmod(Path(data_dir) / 'dashboard-agent.sqlite3', 0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS buckets(line_id TEXT, start INTEGER, payload TEXT NOT NULL, PRIMARY KEY(line_id,start));
          CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, command TEXT NOT NULL, state TEXT NOT NULL, result TEXT);
          CREATE TABLE IF NOT EXISTS bucket_targets(line_id TEXT, start INTEGER, payload TEXT NOT NULL, PRIMARY KEY(line_id,start));
          CREATE INDEX IF NOT EXISTS outbox_window ON buckets(start,line_id);
          CREATE INDEX IF NOT EXISTS target_window ON bucket_targets(start);
        ''')
        os.chmod(Path(data_dir) / 'dashboard-agent.sqlite3', 0o600)
        configured_id = env.get('DASHBOARD_NODE_ID', '').strip()
        stored_id = self.get('node_id')
        if configured_id and stored_id and configured_id != stored_id:
            self.db.close()
            raise ValueError('DASHBOARD_NODE_ID differs from persisted identity')
        self.node_id = stored_id or (str(uuid.UUID(configured_id)) if configured_id else str(uuid.uuid4()))
        self.component_id = env.get('DASHBOARD_COMPONENT_ID', '').strip()
        if self.component_id:
            self.component_id = str(uuid.UUID(self.component_id))
        self.token = self.get('token') or secrets.token_urlsafe(32)
        if self.component_id:
            saved_component = self.get('component_id')
            if saved_component and saved_component != self.component_id:
                self.db.close()
                raise ValueError('DASHBOARD_COMPONENT_ID differs from persisted identity')
            if not saved_component:
                self.set('registered', '0')
                self.set('component_id', self.component_id)
        self.set('node_id', self.node_id)
        self.set('token', self.token)
        # A crash between dispatch and receipt is uncertain. Never replay it.
        with self.db:
            self.db.execute("UPDATE commands SET state='unknown',result=? WHERE state='executing'",
                            (json.dumps({'error': 'node restarted during command execution'}),))
        self.incoming = queue.Queue(maxsize=32)
        self.stop_event = threading.Event()
        self.last_snapshot = 0.0
        self.last_bucket = None
        self.last_sent_snapshot = None
        self.last_heartbeat = 0.0
        self.thread = threading.Thread(target=self.run, name='fleet-dashboard', daemon=True)
        self.thread.start()

    def get(self, key):
        with self.lock:
            row = self.db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
            return row[0] if row else None

    def set(self, key, value):
        with self.lock, self.db:
            self.db.execute('INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    def invalidate_since(self, timestamp):
        # SNMP recovery may replace unknown/estimated ledger intervals older than
        # the ordinary overlap. Persist the correction boundary for re-export.
        previous = self.get('dirty_since')
        self.set('dirty_since', str(min(timestamp, int(previous)) if previous else timestamp))

    def tick(self, controller, now, monotonic_now):
        while not self.incoming.empty():
            command = self.incoming.get_nowait()
            with self.lock:
                row = self.db.execute('SELECT state FROM commands WHERE id=?', (command['id'],)).fetchone()
            if not row or row[0] != 'queued':
                continue
            with self.lock, self.db:
                self.db.execute("UPDATE commands SET state='executing' WHERE id=?", (command['id'],))
            try:
                if now.timestamp() >= command['expires_at']:
                    raise ValueError('command expired')
                validate_command(command['action'], command['params'])
                if command['action'] == 'update_schedule':
                    if command['config_version'] != config_version(controller.cfg):
                        raise ValueError('configuration conflict; refresh and retry')
                    controller.update_config(command['params'])
                else:
                    controller.set_manual_enabled(command['action'] == 'start')
                state, result = 'succeeded', {'config_version': config_version(controller.cfg), 'manual_enabled': controller.manual_enabled}
            except ValueError as exc:
                state, result = 'failed', {'error': str(exc)}
            except Exception:
                state, result = 'unknown', {'error': 'operation interrupted; inspect node before retrying'}
            with self.lock, self.db:
                self.db.execute('UPDATE commands SET state=?,result=? WHERE id=?', (state, json.dumps(result), command['id']))
            self.last_snapshot = 0
        if monotonic_now - self.last_snapshot < 30:
            return
        targets = json.loads(self.get('targets') or '{}')
        previous = json.loads(self.get('snapshot') or 'null')
        if self.get('dirty_since'):
            controller._dashboard_daily_history = None
        snapshot = export_snapshot(controller, now, targets)
        if previous and previous['date'] != snapshot['date']:
            # Finish the previous date from its actual ledger and captured config.
            periods = [p for p in previous['periods'] if p['date'] == previous['date']]
            for period in periods:
                interval = controller.store._totals_between(period['start'] // 60, period['end'] // 60)
                midnight = period['start'] - (period['start'] + 8 * 3600) % 86400
                cumulative = controller.store._totals_between(midnight // 60, period['end'] // 60)
                period['state'] = 'closed'
                for line in period['lines']:
                    value = interval.get(line['line_id'], {})
                    total = cumulative.get(line['line_id'], {})
                    line.update(actual_bytes=value.get('download', 0), program_bytes=value.get('program_download', 0),
                                unknown_seconds=value.get('unknown_seconds', 0), cumulative_actual_bytes=total.get('download', 0))
                    if previous['mode'] == 'daily' and period['phase'] == 'late_makeup':
                        line['target_bytes'] = int((total.get('upload', 0) + total.get('unknown_upload_capacity', 0)) * previous['config']['target_ratio'])
                    target = line['target_bytes']
                    line['completion'] = 'not_applicable' if period['phase'] == 'upload_only' else 'unknown' if target is None else 'no_target' if not target else 'achieved' if line['cumulative_actual_bytes'] >= target else 'unmet'
            self.set('archived_periods', json.dumps(periods))
        archived = json.loads(self.get('archived_periods') or '[]')
        dirty = self.get('dirty_since')
        if dirty:
            for period in archived:
                if period['end'] <= int(dirty):
                    continue
                interval = controller.store._totals_between(period['start'] // 60, period['end'] // 60)
                midnight = period['start'] - (period['start'] + 8 * 3600) % 86400
                cumulative = controller.store._totals_between(midnight // 60, period['end'] // 60)
                for line in period['lines']:
                    value = interval.get(line['line_id'], {})
                    total = cumulative.get(line['line_id'], {})
                    line.update(actual_bytes=value.get('download', 0), program_bytes=value.get('program_download', 0),
                                unknown_seconds=value.get('unknown_seconds', 0), cumulative_actual_bytes=total.get('download', 0))
                    target = line['target_bytes']
                    line['completion'] = 'not_applicable' if period['phase'] == 'upload_only' else 'unknown' if target is None else 'no_target' if not target else 'achieved' if line['cumulative_actual_bytes'] >= target else 'unmet'
            self.set('archived_periods', json.dumps(archived))
        snapshot['periods'] = archived + snapshot['periods']
        # Keep bounded state even after extended cloud outages.
        cutoff_date = (now - timedelta(days=15)).date().isoformat()
        self.set('targets', json.dumps({key: value for key, value in targets.items() if key[:10] >= cutoff_date}))
        self.set('snapshot', json.dumps(snapshot, allow_nan=False))
        self.last_snapshot = monotonic_now
        upper = int(now.timestamp()) // 600 * 600
        dirty = self.get('dirty_since')
        if upper != self.last_bucket or dirty:
            lower = int(self.get('last_enqueued') or upper - 86400) - 1800
            if dirty:
                lower = min(lower, int(dirty) // 600 * 600)
            lower = max(upper - 15 * 86400, lower)
            values = traffic_buckets(controller, lower, upper)
            with self.lock, self.db:
                # Capture the existing daily target at each live collection boundary.
                # Backfilled actuals have no invented historical forecast.
                if int(now.timestamp()) - upper < 60:
                    self.db.executemany('INSERT OR IGNORE INTO bucket_targets VALUES (?,?,?)',
                        [(line['line_id'], upper - 600, json.dumps({'expected_bytes': line['target_bytes'],
                          'target_config_version': snapshot['config_version'], 'target_observed_at': snapshot['observed_at']}))
                         for line in snapshot['lines']])
                recorded = {(row[0], row[1]): json.loads(row[2]) for row in self.db.execute(
                    'SELECT line_id,start,payload FROM bucket_targets WHERE start>=? AND start<?', (lower, upper))}
                for value in values:
                    value.update(recorded.get((value['line_id'], value['start']), {
                        'expected_bytes': None, 'target_config_version': None, 'target_observed_at': None}))
                self.db.executemany('INSERT INTO buckets VALUES (?,?,?) ON CONFLICT(line_id,start) DO UPDATE SET payload=excluded.payload',
                                    [(value['line_id'], value['start'], json.dumps(value)) for value in values])
                self.db.execute('DELETE FROM buckets WHERE start<?', (upper - 15 * 86400,))
                self.db.execute('DELETE FROM bucket_targets WHERE start<?', (upper - 15 * 86400,))
                self.db.execute("DELETE FROM commands WHERE state='acknowledged' AND CAST(json_extract(command, '$.expires_at') AS INTEGER)<?", (upper - 90 * 86400,))
            self.set('dirty_since', '')
            self.set('last_enqueued', str(upper))
            self.last_bucket = upper

    def request(self, path, payload=None, enrollment=False, api_version=1):
        token = Path(self.enrollment_file).read_text().strip() if enrollment else self.token
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.url + '/api/v' + str(api_version) + '/agent' + path, data=body,
                          headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        # Do not follow redirects with a credential-bearing request.
        from urllib.request import HTTPRedirectHandler, build_opener
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        with build_opener(NoRedirect()).open(request, timeout=8) as response:
            data = response.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise ValueError('dashboard response too large')
            return json.loads(data)

    def cycle(self):
        if self.get('registered') != '1':
            if self.component_id:
                payload = {'node_id': self.node_id, 'component_id': self.component_id,
                           'component_type': 'pumper', 'component_token': self.token, 'name': self.name}
                try:
                    self.request('/register', payload, api_version=2)
                except HTTPError as exc:
                    if exc.code != 401:
                        raise
                    self.request('/register', payload, api_version=2, enrollment=True)
                self.set('component_registered', '1')
            else:
                self.request('/register', {'node_id': self.node_id, 'node_token': self.token,
                                          'name': self.name, 'external_id': self.external_id}, enrollment=True)
            self.set('registered', '1')
        snapshot = self.get('snapshot')
        if snapshot and (snapshot != self.last_sent_snapshot or time.monotonic() - self.last_heartbeat >= 30):
            self.request('/heartbeat', {'snapshot': json.loads(snapshot)})
            self.last_sent_snapshot = snapshot
            self.last_heartbeat = time.monotonic()
        # Limit each network cycle; commands are not starved by historical backfill.
        with self.lock:
            rows = self.db.execute('SELECT line_id,start,payload FROM buckets ORDER BY start LIMIT 512').fetchall()
        if rows:
            self.request('/telemetry', {'schema_version': 1, 'buckets': [json.loads(row[2]) for row in rows]})
            with self.lock, self.db:
                self.db.executemany('DELETE FROM buckets WHERE line_id=? AND start=? AND payload=?', rows)
        with self.lock:
            results = self.db.execute("SELECT id,state,result FROM commands WHERE state IN ('succeeded','failed','unknown')").fetchall()
        for command_id, state, result in results:
            self.request('/commands/' + command_id + '/result', {'state': state, 'result': json.loads(result)})
            with self.lock, self.db:
                self.db.execute("UPDATE commands SET state='acknowledged' WHERE id=?", (command_id,))
        command = self.request('/commands').get('command')
        if command:
            with self.lock, self.db:
                self.db.execute("INSERT OR IGNORE INTO commands VALUES (?,?,'queued',NULL)", (command['id'], json.dumps(command)))
                state = self.db.execute('SELECT state FROM commands WHERE id=?', (command['id'],)).fetchone()[0]
            if state == 'queued' and not self.incoming.full():
                self.incoming.put(command)
        self.set('connection', 'online')

    def run(self):
        delay = 15
        while not self.stop_event.is_set():
            try:
                self.cycle()
                delay = 15
            except HTTPError as exc:
                if exc.code == 401:
                    self.set('registered', '0')
                    self.last_sent_snapshot = None
                # Never log request URLs, headers, credentials or response bodies.
                self.set('connection', 'http_' + str(exc.code))
                delay = min(120, delay * 2)
            except Exception:
                self.set('connection', 'unavailable')
                delay = min(120, delay * 2)
            self.stop_event.wait(delay)

    def shutdown(self):
        self.stop_event.set()
        self.thread.join(timeout=10)
        if not self.thread.is_alive():
            self.db.close()
