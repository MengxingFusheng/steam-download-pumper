"""Read-only Dashboard sidecar for the existing multi-IP HTTP controller.

Run ``python -m steam_pumper.dashboard_classic`` with CLASSIC_STATUS_URL set to
the complete internal /api/status URL and CLASSIC_STATE_DIR on persistent storage.
An optional private CLASSIC_SEED_FILE contains {captured_at: epoch, status: ...}
captured immediately before migrating to a fresh controller instance. It is
projected into numeric counters once; raw configuration and source URLs are
never retained in the agent database or transmitted to the cloud.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo

from .dashboard_agent import DashboardAgent

SHANGHAI = ZoneInfo('Asia/Shanghai')
AGGREGATE_LINE = '__classic_node__'
SAFE_CONFIG_FIELDS = ('start_time', 'end_time', 'target_mbps', 'line_count',
                      'connections_per_line', 'max_connections_per_line', 'reduce_above_target', 'rate_limit_enabled')


def byte_count(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10**18:
        raise ValueError('invalid program byte counter')
    return value


def speed(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100_000:
        raise ValueError('invalid program speed')
    return float(value)


def project_status(status):
    """Validate the small public projection before changing persistent counters."""
    metrics, config = status['metrics'], status['config']
    lines = metrics['lines']
    if not isinstance(lines, list) or not 1 <= len(lines) <= 1500:
        raise ValueError('invalid classic lines')
    safe_config = {}
    for key in SAFE_CONFIG_FIELDS:
        value = config.get(key)
        if value is not None:
            if not isinstance(value, (str, int, float, bool)) or isinstance(value, str) and len(value) > 100:
                raise ValueError('invalid classic configuration')
            if key in {'start_time', 'end_time'}:
                datetime.strptime(value, '%H:%M')
            elif key in {'rate_limit_enabled', 'reduce_above_target'}:
                if not isinstance(value, bool):
                    raise ValueError('invalid classic switch')
            elif key == 'target_mbps':
                speed(value)
            elif isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1500:
                raise ValueError('invalid classic count')
            safe_config[key] = value
    # Only known scheduling fields are included in this hash and projection.
    version = hashlib.sha256(json.dumps(safe_config, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    result = {'config': safe_config, 'config_version': version,
              'today_bytes': byte_count(metrics['today_bytes']),
              'target_bytes': byte_count(metrics['theoretical_window_bytes']),
              'minimum_accept_bytes': byte_count(metrics['minimum_accept_bytes']),
              'manual_enabled': bool(status['manual_enabled']), 'running': bool(status['running']), 'lines': []}
    identities = set()
    for line in lines:
        identity, bind_ip = line['line_id'], line['bind_ip']
        if (not isinstance(identity, str) or not 1 <= len(identity) <= 200 or identity in identities
                or identity == AGGREGATE_LINE or not isinstance(bind_ip, str) or len(bind_ip) > 45):
            raise ValueError('invalid classic line identity')
        identities.add(identity)
        result['lines'].append({'line_id': identity, 'bind_ip': bind_ip,
            'today_bytes': byte_count(line['today_bytes']), 'target_mbps': speed(line['target_mbps']),
            'current_mbps': speed(line.get('current_mbps', 0)), 'avg60_mbps': speed(line.get('avg60_mbps', 0)),
            'status': str(line.get('status', 'unknown'))[:100], 'metrics_available': bool(line.get('metrics_available', False))})
    result['stats_source'] = 'program' if all(line['metrics_available'] for line in result['lines']) else 'interface_fallback'
    return result


class ClassicAgent(DashboardAgent):
    """Reuse v1 network/identity/outbox handling without claiming upstream liveness."""
    def cycle(self):
        snapshot = json.loads(self.get('snapshot') or 'null')
        if not snapshot or not 0 <= time.time() - snapshot['observed_at'] < 60:
            return
        super().cycle()


class ClassicCollector:
    def __init__(self, data_dir, env=None):
        self.env = os.environ if env is None else env
        self.status_url = self.env.get('CLASSIC_STATUS_URL', 'http://127.0.0.1:80/api/status')
        parsed = urlparse(self.status_url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError('configure an internal status URL without credentials or query')
        self.poll_seconds = int(self.env.get('CLASSIC_POLL_SECONDS', '15'))
        if not 5 <= self.poll_seconds <= 30:
            raise ValueError('CLASSIC_POLL_SECONDS must be between 5 and 30')
        Path(data_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.agent = ClassicAgent(data_dir, self.env)
        with self.agent.lock, self.agent.db:
            self.agent.db.row_factory = sqlite3.Row
            self.agent.db.executescript('''
                CREATE TABLE IF NOT EXISTS classic_counters(day TEXT,key TEXT,raw INTEGER NOT NULL,
                    total INTEGER NOT NULL,offset INTEGER NOT NULL,basis TEXT NOT NULL,
                    observed_at INTEGER NOT NULL,available INTEGER NOT NULL,PRIMARY KEY(day,key));
                CREATE TABLE IF NOT EXISTS classic_minutes(line_id TEXT,minute INTEGER,program_bytes INTEGER NOT NULL,
                    observed_seconds INTEGER NOT NULL,PRIMARY KEY(line_id,minute));
            ''')
        if self.agent.get('classic_seed') is None:
            seed = {'date': None, 'totals': {}}
            seed_path = self.env.get('CLASSIC_SEED_FILE')
            if seed_path:
                payload = json.loads(Path(seed_path).read_text())
                if 'status' in payload:
                    projected = project_status(payload['status'])
                    seed['date'] = datetime.fromtimestamp(int(payload['captured_at']), SHANGHAI).date().isoformat()
                else:
                    projected = project_status(payload)
                    seed['date'] = self.env.get('CLASSIC_SEED_DATE')
                    if not seed['date']:
                        raise ValueError('a raw status seed requires CLASSIC_SEED_DATE')
                    datetime.strptime(seed['date'], '%Y-%m-%d')
                seed['totals'] = {line['line_id']: line['today_bytes'] for line in projected['lines']}
                seed['totals'][AGGREGATE_LINE] = projected['today_bytes']
            self.agent.set('classic_seed', json.dumps(seed))
        self.seed = json.loads(self.agent.get('classic_seed'))

    def fetch_status(self):
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(Request(self.status_url, headers={'Accept': 'application/json'}), timeout=5) as response:
            body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise ValueError('classic status response too large')
            return json.loads(body)

    def unavailable(self):
        # Preserve the last snapshot timestamp and stop sending fresh heartbeats.
        # Recovery must not invent bucket samples for the failed interval.
        with self.agent.lock, self.agent.db:
            self.agent.db.execute('UPDATE classic_counters SET available=0')

    def _interval(self, db, identity, left, right, amount):
        elapsed = right - left
        cursor, remaining = left, amount
        while cursor < right:
            minute = cursor // 60
            end = min(right, (minute + 1) * 60)
            seconds = end - cursor
            allocated = remaining if end == right else amount * seconds // elapsed
            db.execute('''INSERT INTO classic_minutes VALUES (?,?,?,?)
                ON CONFLICT(line_id,minute) DO UPDATE SET program_bytes=program_bytes+excluded.program_bytes,
                    observed_seconds=observed_seconds+excluded.observed_seconds''', (identity, minute, allocated, seconds))
            remaining -= allocated
            cursor = end

    def _counter(self, db, day, identity, raw, basis, observed_at, available=True):
        previous = db.execute('SELECT * FROM classic_counters WHERE day=? AND key=?', (day, identity)).fetchone()
        if not available:
            if previous:
                db.execute('UPDATE classic_counters SET available=0 WHERE day=? AND key=?', (day, identity))
                return previous['total']
            # No helper metrics means no per-line traffic can be attributed.
            return self.seed['totals'].get(identity, 0) if self.seed['date'] == day else raw
        offset = previous['offset'] if previous else self.seed['totals'].get(identity, 0) if self.seed['date'] == day else 0
        if previous:
            if previous['basis'] != basis:
                # Interface counters and helper sums overlap but have no common
                # baseline. Keep the known total and start measuring future
                # deltas from the new source; do not add its whole raw total.
                offset = previous['total'] - raw
            elif raw < previous['raw']:
                offset = previous['total']
        total = byte_count(offset + raw)
        if previous:
            elapsed = observed_at - previous['observed_at']
            if (previous['available'] and previous['basis'] == basis and raw >= previous['raw']
                    and 0 < elapsed <= self.poll_seconds * 2):
                self._interval(db, identity, previous['observed_at'], observed_at, raw - previous['raw'])
        db.execute('''INSERT INTO classic_counters VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(day,key) DO UPDATE SET raw=excluded.raw,total=excluded.total,offset=excluded.offset,
                basis=excluded.basis,observed_at=excluded.observed_at,available=excluded.available''',
            (day, identity, raw, total, offset, basis, observed_at, 1))
        return total

    def observe(self, status, now=None):
        now = datetime.now(SHANGHAI) if now is None else now.astimezone(SHANGHAI)
        projected = project_status(status)
        day, observed_at = now.date().isoformat(), int(now.timestamp())
        cfg, target = projected['config'], projected['target_bytes']
        weights = [line['target_mbps'] for line in projected['lines']]
        weight_total = sum(weights)
        targets = [int(target * value / weight_total) for value in weights] if weight_total else [target // len(weights)] * len(weights)
        targets[-1] += target - sum(targets)
        with self.agent.lock, self.agent.db:
            db = self.agent.db
            actual = self._counter(db, day, AGGREGATE_LINE, projected['today_bytes'], projected['stats_source'], observed_at)
            lines = []
            for line, goal in zip(projected['lines'], targets):
                count = self._counter(db, day, line['line_id'], line['today_bytes'], 'program', observed_at, line['metrics_available'])
                lines.append({'line_id': line['line_id'], 'bind_ip': line['bind_ip'], 'snmp_interface': '',
                    'enabled': True, 'active': projected['running'] and line['status'] == 'downloading',
                    'weight': None, 'group_id': 'multi_ip', 'upload_mbps': None, 'upload_bytes': None,
                    'current_upload_mbps': None, 'download_mbps': line['target_mbps'],
                    'assigned_mbps': line['target_mbps'] if projected['running'] else 0,
                    'status': line['status'], 'avg60_mbps': line['avg60_mbps'], 'profile_confirmed': False,
                    'forecast_quality': 'configured_speed', 'metrics_available': line['metrics_available'],
                    'actual_bytes': count, 'program_bytes': count, 'measured_bytes': 0, 'estimated_bytes': 0,
                    'legacy_bytes': 0, 'unknown_seconds': 0, 'target_bytes': goal})
            snapshot = {'schema_version': 1, 'observed_at': observed_at, 'date': day, 'timezone': 'Asia/Shanghai',
                'mode': 'multi_ip', 'measurement_basis': 'program', 'target_basis': 'configured_speed',
                'stats_source': projected['stats_source'], 'capabilities': [],
                'manual_enabled': projected['manual_enabled'], 'snmp_status': 'not_configured',
                'config': cfg, 'config_version': projected['config_version'],
                'summary': {'actual_bytes': actual, 'program_bytes': actual, 'target_bytes': target,
                    'remaining_bytes': max(0, target - actual), 'measured_bytes': 0, 'estimated_bytes': 0,
                    'legacy_bytes': 0, 'unknown_seconds': 0, 'upload_bytes': None, 'upload_mbps': None,
                    'minimum_accept_bytes': projected['minimum_accept_bytes']},
                'lines': lines, 'groups': [], 'router_budget': {}, 'periods': [],
                'rolling': None}
            db.execute('INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       ('snapshot', json.dumps(snapshot, allow_nan=False)))
            upper = observed_at // 600 * 600
            last = db.execute("SELECT value FROM state WHERE key='classic_last_bucket'").fetchone()
            if not last or int(last[0]) != upper:
                lower = max(upper - 15 * 86400, int(last[0]) - 600 if last else upper - 86400)
                rows = db.execute('''SELECT line_id,CAST(minute/10 AS INTEGER)*600 AS start,
                    SUM(program_bytes) AS program_bytes,SUM(observed_seconds) AS observed_seconds,COUNT(*) AS minutes
                    FROM classic_minutes WHERE minute>=? AND minute<? GROUP BY line_id,start''', (lower // 60, upper // 60)).fetchall()
                for row in rows:
                    bucket = {'line_id': row['line_id'], 'start': row['start'], 'actual_bytes': 0,
                        'program_bytes': row['program_bytes'], 'measured_bytes': 0, 'estimated_bytes': 0,
                        'legacy_bytes': 0, 'unknown_seconds': max(0, 600-row['observed_seconds']),
                        'sample_seconds': 0, 'observed_minutes': row['minutes'], 'quality': 'program_only'}
                    if row['start'] == upper - 600 and observed_at - upper < 60:
                        goals = {line['line_id']: line['target_bytes'] for line in lines}
                        goals[AGGREGATE_LINE] = target
                        if row['line_id'] in goals:
                            metadata = {'expected_bytes': goals[row['line_id']],
                                'target_config_version': snapshot['config_version'], 'target_observed_at': observed_at}
                            db.execute('INSERT OR IGNORE INTO bucket_targets VALUES (?,?,?)',
                                (row['line_id'], row['start'], json.dumps(metadata)))
                    captured = db.execute('SELECT payload FROM bucket_targets WHERE line_id=? AND start=?',
                        (row['line_id'], row['start'])).fetchone()
                    if captured:
                        bucket.update(json.loads(captured[0]))
                    db.execute('INSERT INTO buckets VALUES (?,?,?) ON CONFLICT(line_id,start) DO UPDATE SET payload=excluded.payload',
                               (row['line_id'], row['start'], json.dumps(bucket)))
                db.execute('INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('classic_last_bucket', str(upper)))
            cutoff = (now-timedelta(days=15)).date().isoformat()
            db.execute('DELETE FROM classic_counters WHERE day<?', (cutoff,))
            db.execute('DELETE FROM classic_minutes WHERE minute<?', ((upper-15*86400)//60,))
            db.execute('DELETE FROM buckets WHERE start<?', (upper-15*86400,))
            db.execute('DELETE FROM bucket_targets WHERE start<?', (upper-15*86400,))
        self.reject_commands()
        return snapshot

    def reject_commands(self):
        while not self.agent.incoming.empty():
            command = self.agent.incoming.get_nowait()
            with self.agent.lock, self.agent.db:
                self.agent.db.execute("UPDATE commands SET state='failed',result=? WHERE id=? AND state='queued'",
                    (json.dumps({'error': 'classic collector is read-only; use the original node console'}), command['id']))

    def shutdown(self):
        self.agent.shutdown()


def main():
    env = os.environ
    collector = ClassicCollector(env.get('CLASSIC_STATE_DIR', '/data'), env)
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    failed = False
    try:
        while not stopped.is_set():
            try:
                collector.observe(collector.fetch_status())
                failed = False
            except Exception:
                collector.unavailable()
                if not failed:
                    print('classic upstream unavailable; collection will retry', flush=True)
                failed = True
            collector.reject_commands()
            stopped.wait(collector.poll_seconds)
    finally:
        collector.shutdown()


if __name__ == '__main__':
    main()
