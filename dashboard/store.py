from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL, external_id TEXT NOT NULL, registered_at INTEGER NOT NULL,
            last_seen INTEGER NOT NULL, snapshot TEXT NOT NULL DEFAULT '{}');
          CREATE TABLE IF NOT EXISTS buckets(node_id TEXT, line_id TEXT, start INTEGER,
            actual_bytes INTEGER NOT NULL, program_bytes INTEGER NOT NULL, measured_bytes INTEGER NOT NULL,
            estimated_bytes INTEGER NOT NULL, legacy_bytes INTEGER NOT NULL, unknown_seconds INTEGER NOT NULL,
            sample_seconds INTEGER NOT NULL, observed_minutes INTEGER NOT NULL, quality TEXT NOT NULL,
            expected_bytes INTEGER, target_config_version TEXT, target_observed_at INTEGER,
            PRIMARY KEY(node_id,line_id,start));
          CREATE INDEX IF NOT EXISTS buckets_window ON buckets(node_id,start);
          CREATE INDEX IF NOT EXISTS buckets_retention ON buckets(start);
          CREATE TABLE IF NOT EXISTS periods(node_id TEXT, date TEXT, mode TEXT, phase TEXT, payload TEXT NOT NULL,
            PRIMARY KEY(node_id,date,mode,phase));
          CREATE TABLE IF NOT EXISTS daily_traffic(node_id TEXT, date TEXT, observed_at INTEGER NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(node_id,date));
          CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,node_id TEXT NOT NULL,action TEXT NOT NULL,
            params TEXT NOT NULL,config_version TEXT NOT NULL,actor TEXT NOT NULL,created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,state TEXT NOT NULL,result TEXT,finished_at INTEGER);
          CREATE INDEX IF NOT EXISTS commands_node ON commands(node_id,created_at);
          CREATE UNIQUE INDEX IF NOT EXISTS commands_pending ON commands(node_id)
            WHERE state IN ('queued','dispatched');
          CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
          INSERT OR IGNORE INTO schema_version VALUES(1);
        ''')
        # Additive upgrades preserve pre-existing v1 telemetry.
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(buckets)')}
        for name, sql_type in [('expected_bytes', 'INTEGER'), ('target_config_version', 'TEXT'), ('target_observed_at', 'INTEGER')]:
            if name not in columns:
                self.db.execute(f'ALTER TABLE buckets ADD COLUMN {name} {sql_type}')
        node_columns = {row[1] for row in self.db.execute('PRAGMA table_info(nodes)')}
        if 'name_overridden' not in node_columns:
            self.db.execute('ALTER TABLE nodes ADD COLUMN name_overridden INTEGER NOT NULL DEFAULT 0')
        self.db.commit()
        self.last_prune = 0

    @contextmanager
    def transaction(self):
        with self.lock, self.db:
            yield self.db

    def node_by_token(self, token):
        with self.lock:
            row = self.db.execute('SELECT * FROM nodes WHERE token_hash=?', (token_hash(token),)).fetchone()
            return dict(row) if row else None

    def node(self, node_id):
        with self.lock:
            row = self.db.execute('SELECT * FROM nodes WHERE id=?', (node_id,)).fetchone()
            return dict(row) if row else None

    def rename_node(self, node_id, name):
        with self.transaction() as db:
            result = db.execute('UPDATE nodes SET name=?,name_overridden=1 WHERE id=?', (name, node_id))
            if not result.rowcount:
                return None
            return dict(db.execute('SELECT * FROM nodes WHERE id=?', (node_id,)).fetchone())

    def expire(self, db, now):
        db.execute("UPDATE commands SET state='expired',finished_at=? WHERE state='queued' AND expires_at<=?", (now, now))
        db.execute("UPDATE commands SET state='unknown',finished_at=?,result=? WHERE state='dispatched' AND expires_at<=?",
                   (now, json.dumps({'error': 'no execution receipt before deadline'}), now))

    def prune(self, now=None):
        now = int(time.time()) if now is None else now
        if now - self.last_prune < 3600:
            return
        with self.transaction() as db:
            db.execute('DELETE FROM buckets WHERE start<?', (now - 15 * 86400,))
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo
            date = (datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai')) - timedelta(days=15)).date().isoformat()
            db.execute('DELETE FROM periods WHERE date<?', (date,))
            db.execute('DELETE FROM daily_traffic WHERE date<?', (date,))
            db.execute('DELETE FROM commands WHERE created_at<? AND state NOT IN (?,?)', (now - 90 * 86400, 'queued', 'dispatched'))
            self.expire(db, now)
        self.last_prune = now

    def close(self):
        self.db.close()
