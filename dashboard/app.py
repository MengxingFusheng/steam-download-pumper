import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials, HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from steam_pumper.dashboard_protocol import validate_command
from .models import Snapshot
from .security import verify_password
from .store import Store, token_hash


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Registration(StrictModel):
    node_id: uuid.UUID
    node_token: str = Field(min_length=40, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    external_id: str = Field(default='', max_length=200)


class NodeRename(StrictModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator('name', mode='before')
    @classmethod
    def clean_name(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise ValueError('name must not contain control characters')
        return value


class Bucket(StrictModel):
    line_id: str = Field(min_length=1, max_length=200)
    start: int = Field(ge=0)
    actual_bytes: int = Field(ge=0, le=10**18)
    program_bytes: int = Field(ge=0, le=10**18)
    measured_bytes: int = Field(ge=0, le=10**18)
    estimated_bytes: int = Field(ge=0, le=10**18)
    legacy_bytes: int = Field(ge=0, le=10**18)
    unknown_seconds: int = Field(ge=0)
    sample_seconds: int = Field(ge=0)
    observed_minutes: int = Field(ge=1, le=10)
    quality: Literal['measured', 'estimated', 'unknown', 'program_only']
    expected_bytes: int | None = Field(default=None, ge=0, le=10**18)
    target_config_version: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    target_observed_at: int | None = Field(default=None, ge=0)


class Telemetry(StrictModel):
    schema_version: Literal[1]
    buckets: list[Bucket] = Field(max_length=512)


class Heartbeat(StrictModel):
    snapshot: Snapshot


class CommandInput(StrictModel):
    action: Literal['start', 'stop', 'update_schedule']
    params: dict = Field(default_factory=dict)
    config_version: str = Field(default='', max_length=64)


class CommandResult(StrictModel):
    state: Literal['succeeded', 'failed', 'unknown']
    result: dict


def secret_setting(env, name):
    path = env.get(name + '_FILE')
    return Path(path).read_text().strip() if path else env.get(name, '')


def public_node(node):
    snapshot = json.loads(node['snapshot'])
    current = time.time()
    online = current - node['last_seen'] < 90
    if snapshot.get('mode') == 'multi_ip':
        online = online and current - snapshot.get('observed_at', 0) < 90
    return {'id': node['id'], 'name': node['name'], 'external_id': node['external_id'],
            'registered_at': node['registered_at'], 'last_seen': node['last_seen'],
            'online': online, 'snapshot': snapshot}


def create_app(env=None):
    env = os.environ if env is None else env
    store = Store(env.get('DASHBOARD_DB', '/data/dashboard.sqlite3'))
    enrollment = secret_setting(env, 'DASHBOARD_ENROLLMENT_TOKEN')
    read_token = secret_setting(env, 'DASHBOARD_READ_TOKEN')
    management_token = secret_setting(env, 'DASHBOARD_MANAGEMENT_TOKEN')
    if management_token and len(management_token) < 32:
        store.close()
        raise ValueError('management token must contain 32+ characters')
    username = env.get('DASHBOARD_ADMIN_USER', 'admin')
    password_hash = secret_setting(env, 'DASHBOARD_ADMIN_PASSWORD_HASH')
    if len(enrollment) < 32 or not password_hash.startswith('scrypt$') or len(read_token) < 32:
        store.close()
        raise ValueError('configure enrollment token, read token (32+ characters) and scrypt admin password hash')
    basic, bearer = HTTPBasic(auto_error=False), HTTPBearer(auto_error=False)

    @asynccontextmanager
    async def lifespan(app):
        yield
        store.close()

    app = FastAPI(title='Fleet Dashboard API', version='1.0.0', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store

    def admin(credentials: Annotated[HTTPBasicCredentials | None, Depends(basic)]):
        if credentials and secrets.compare_digest(credentials.username.encode(), username.encode()) and verify_password(credentials.password, password_hash):
            return username
        raise HTTPException(401, 'authentication required', headers={'WWW-Authenticate': 'Basic realm="Fleet Dashboard"'})

    def reader(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
               login: Annotated[HTTPBasicCredentials | None, Depends(basic)]):
        if credentials and (secrets.compare_digest(credentials.credentials.encode(), read_token.encode())
                            or (management_token and secrets.compare_digest(credentials.credentials.encode(), management_token.encode()))):
            return 'integration'
        return admin(login)

    def node_auth(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        node = store.node_by_token(credentials.credentials) if credentials else None
        if not node:
            raise HTTPException(401, 'invalid node credentials')
        return node

    def writing(request: Request,
                credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
                login: Annotated[HTTPBasicCredentials | None, Depends(basic)]):
        actor = (username if management_token and credentials and
                 secrets.compare_digest(credentials.credentials.encode(), management_token.encode()) else admin(login))
        if request.headers.get('x-requested-with') != 'FleetDashboard':
            raise HTTPException(403, 'write request header required')
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            raise HTTPException(403, 'origin mismatch')
        return actor

    def require_node(node_id):
        node = store.node(node_id)
        if not node:
            raise HTTPException(404, 'node not found')
        return node

    @app.middleware('http')
    async def limits(request, call_next):
        # Bound chunked as well as Content-Length bodies before JSON parsing.
        if request.method in {'POST', 'PUT', 'PATCH'}:
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 4 * 1024 * 1024:
                    return JSONResponse({'detail': 'request too large'}, status_code=413)
            body = bytes(data)
            request._body = body
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        response.headers['Referrer-Policy'] = 'same-origin'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/healthz')
    def health():
        with store.lock:
            store.db.execute('SELECT 1').fetchone()
        return {'status': 'ok', 'schema_version': 1}

    @app.get('/api/v1/openapi.json', dependencies=[Depends(reader)])
    def openapi():
        return app.openapi()

    @app.post('/api/v1/agent/register')
    def register(data: Registration, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if not credentials or not secrets.compare_digest(credentials.credentials.encode(), enrollment.encode()):
            raise HTTPException(401, 'invalid enrollment credentials')
        node_id = str(data.node_id)
        now = int(time.time())
        with store.transaction() as db:
            existing = db.execute('SELECT token_hash FROM nodes WHERE id=?', (node_id,)).fetchone()
            if existing and existing['token_hash'] != token_hash(data.node_token):
                raise HTTPException(409, 'node identity already registered')
            if not existing and db.execute('SELECT 1 FROM nodes WHERE token_hash=?', (token_hash(data.node_token),)).fetchone():
                raise HTTPException(409, 'node credentials already assigned')
            if not existing and db.execute('SELECT COUNT(*) FROM nodes').fetchone()[0] >= 100:
                raise HTTPException(409, 'node capacity reached')
            if existing:
                db.execute('UPDATE nodes SET name=CASE WHEN name_overridden=0 THEN ? ELSE name END,external_id=? WHERE id=?',
                           (data.name,data.external_id,node_id))
            else:
                db.execute('INSERT INTO nodes(id,token_hash,name,external_id,registered_at,last_seen) VALUES (?,?,?,?,?,?)',
                           (node_id, token_hash(data.node_token), data.name, data.external_id, now, now))
        return {'node_id': node_id, 'schema_version': 1}

    @app.post('/api/v1/agent/heartbeat')
    def heartbeat(data: Heartbeat, node=Depends(node_auth)):
        snapshot = data.snapshot.model_dump()
        if snapshot.get('schema_version') != 1 or snapshot.get('timezone') != 'Asia/Shanghai':
            raise HTTPException(422, 'unsupported snapshot schema/timezone')
        try:
            if abs(int(snapshot['observed_at']) - time.time()) > 120:
                raise ValueError('node clock differs by more than 120 seconds')
            date.fromisoformat(snapshot['date'])
            if snapshot['mode'] not in {'daily', 'legacy', 'multi_ip'} or len(snapshot['lines']) > 1500 or len(snapshot['periods']) > 16:
                raise ValueError('invalid snapshot size/mode')
            encoded = json.dumps(snapshot, allow_nan=False)
            for period in snapshot['periods']:
                date.fromisoformat(period['date'])
                if period['mode'] not in {'daily', 'legacy', 'multi_ip'} or not isinstance(period['phase'], str):
                    raise ValueError('invalid period')
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, 'invalid snapshot: ' + str(exc)) from exc
        now = int(time.time())
        with store.transaction() as db:
            previous = json.loads(node['snapshot'])
            if snapshot['observed_at'] >= previous.get('observed_at', 0):
                db.execute('UPDATE nodes SET last_seen=?,snapshot=? WHERE id=?', (now, encoded, node['id']))
                for period in snapshot['periods']:
                    db.execute('INSERT INTO periods VALUES (?,?,?,?,?) ON CONFLICT(node_id,date,mode,phase) DO UPDATE SET payload=excluded.payload',
                               (node['id'], period['date'], period['mode'], period['phase'], json.dumps(period)))
        store.prune(now)
        return {'ok': True}

    @app.post('/api/v1/agent/telemetry')
    def telemetry(data: Telemetry, node=Depends(node_auth)):
        now = int(time.time())
        classic = json.loads(node['snapshot']).get('mode') == 'multi_ip'
        for bucket in data.buckets:
            if classic and (bucket.actual_bytes or bucket.measured_bytes or bucket.estimated_bytes
                            or bucket.legacy_bytes or bucket.sample_seconds):
                raise HTTPException(422, 'classic telemetry must not claim SNMP measurements')
            if bucket.start % 600 or bucket.start + 600 > now or bucket.start < now - 16 * 86400:
                raise HTTPException(422, 'bucket must be a closed, aligned interval within 16 days')
            target_parts = [bucket.expected_bytes, bucket.target_config_version, bucket.target_observed_at]
            if any(value is None for value in target_parts) and not all(value is None for value in target_parts):
                raise HTTPException(422, 'target metadata must be complete or absent')
            if bucket.target_observed_at is not None and not bucket.start + 600 <= bucket.target_observed_at < bucket.start + 660:
                raise HTTPException(422, 'target must be captured at the collection boundary')
            if bucket.actual_bytes != bucket.measured_bytes + bucket.estimated_bytes + bucket.legacy_bytes:
                raise HTTPException(422, 'actual byte components do not sum')
        with store.transaction() as db:
            db.executemany('''INSERT INTO buckets(node_id,line_id,start,actual_bytes,program_bytes,measured_bytes,estimated_bytes,legacy_bytes,unknown_seconds,sample_seconds,observed_minutes,quality,expected_bytes,target_config_version,target_observed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(node_id,line_id,start) DO UPDATE SET actual_bytes=excluded.actual_bytes,
                program_bytes=excluded.program_bytes,measured_bytes=excluded.measured_bytes,
                estimated_bytes=excluded.estimated_bytes,legacy_bytes=excluded.legacy_bytes,
                unknown_seconds=excluded.unknown_seconds,sample_seconds=excluded.sample_seconds,
                observed_minutes=excluded.observed_minutes,quality=excluded.quality,
                expected_bytes=COALESCE(buckets.expected_bytes,excluded.expected_bytes),
                target_config_version=COALESCE(buckets.target_config_version,excluded.target_config_version),
                target_observed_at=COALESCE(buckets.target_observed_at,excluded.target_observed_at)''',
                [(node['id'], value.line_id, value.start, value.actual_bytes, value.program_bytes,
                  value.measured_bytes, value.estimated_bytes, value.legacy_bytes, value.unknown_seconds,
                  value.sample_seconds, value.observed_minutes, value.quality, value.expected_bytes, value.target_config_version, value.target_observed_at) for value in data.buckets])
        return {'accepted': len(data.buckets)}

    @app.get('/api/v1/nodes', dependencies=[Depends(reader)])
    def nodes(q: str = Query(default='', max_length=100), status: Literal['all', 'online', 'offline'] = 'all',
              offset: int = Query(default=0, ge=0), limit: int = Query(default=25, ge=1, le=100)):
        with store.lock:
            rows = [public_node(dict(row)) for row in store.db.execute('SELECT * FROM nodes ORDER BY name,id')]
        rows = [row for row in rows if q.casefold() in (row['name'] + row['id'] + row['external_id']).casefold()
                and (status == 'all' or row['online'] == (status == 'online'))]
        totals = {key: sum(row['snapshot'].get('summary', {}).get(key, 0) for row in rows) for key in ('actual_bytes', 'target_bytes', 'remaining_bytes')}
        return {'total': len(rows), 'online': sum(row['online'] for row in rows), 'summary': totals, 'nodes': rows[offset:offset + limit]}

    @app.get('/api/v1/nodes/{node_id}', dependencies=[Depends(reader)])
    def detail(node_id: uuid.UUID):
        return public_node(require_node(str(node_id)))

    @app.patch('/api/v1/nodes/{node_id}')
    def rename(node_id: uuid.UUID, data: NodeRename, actor=Depends(writing)):
        node = store.rename_node(str(node_id), data.name)
        if not node:
            raise HTTPException(404, 'node not found')
        return public_node(node)

    @app.get('/api/v1/nodes/{node_id}/series', dependencies=[Depends(reader)])
    def series(node_id: uuid.UUID, hours: int = Query(default=24, ge=1, le=24), line_id: str | None = Query(default=None, max_length=200)):
        node_id = str(node_id)
        node = require_node(node_id)
        snapshot = json.loads(node['snapshot'])
        measurement_basis = snapshot.get('measurement_basis', 'snmp')
        upper = int(time.time()) // 600 * 600
        lower = upper - hours * 3600
        condition = 'node_id=? AND start>=? AND start<?'
        params = [node_id, lower, upper]
        if line_id:
            condition += ' AND line_id=?'
            params.append(line_id)
        elif snapshot.get('mode') == 'multi_ip':
            condition += ' AND line_id=?'
            params.append('__classic_node__')
        else:
            # An upgraded classic node keeps its program history. Its old
            # aggregate already includes the line rows, so use it once.
            condition += ''' AND (line_id='__classic_node__' OR NOT EXISTS (
                SELECT 1 FROM buckets AS aggregate WHERE aggregate.node_id=buckets.node_id
                AND aggregate.start=buckets.start AND aggregate.line_id='__classic_node__'))'''
        with store.lock:
            rows = store.db.execute(f'''SELECT start,SUM(actual_bytes) AS actual_bytes,SUM(program_bytes) AS program_bytes,
              SUM(measured_bytes) AS measured_bytes,SUM(estimated_bytes) AS estimated_bytes,
              SUM(unknown_seconds) AS unknown_seconds,SUM(sample_seconds) AS sample_seconds,
              SUM(observed_minutes) AS observed_minutes,COUNT(*) AS reporting_lines,
              CASE WHEN COUNT(expected_bytes)=COUNT(*) THEN SUM(expected_bytes) ELSE NULL END AS expected_bytes,
              MAX(target_observed_at) AS target_observed_at,MAX(target_config_version) AS target_config_version
              FROM buckets WHERE {condition} GROUP BY start ORDER BY start''', params).fetchall()
        indexed = {row['start']: dict(row) for row in rows}
        points = []
        for start in range(lower, upper, 600):
            value = indexed.get(start)
            if value:
                value['actual_mbps'] = (value['actual_bytes'] * 8 / 600 / 1_000_000
                    if measurement_basis == 'snmp' and (value['actual_bytes'] > 0 or (value['sample_seconds'] and value['unknown_seconds'] < value['reporting_lines'] * 600)) else None)
                value['program_mbps'] = value['program_bytes'] * 8 / 600 / 1_000_000
            points.append(value or {'start': start, 'actual_mbps': None, 'program_mbps': None})
        return {'interval_seconds': 600, 'timezone': 'Asia/Shanghai', 'measurement_basis': measurement_basis, 'points': points}

    @app.get('/api/v1/nodes/{node_id}/periods', dependencies=[Depends(reader)])
    def periods(node_id: uuid.UUID, day: date | None = None):
        require_node(str(node_id))
        condition, params = 'node_id=?', [str(node_id)]
        if day:
            condition += ' AND date=?'
            params.append(day.isoformat())
        with store.lock:
            rows = store.db.execute(f'SELECT payload FROM periods WHERE {condition} ORDER BY date DESC,mode,phase LIMIT 120', params).fetchall()
        return {'periods': [json.loads(row[0]) for row in rows]}

    @app.post('/api/v1/nodes/{node_id}/commands', status_code=202)
    def command(node_id: uuid.UUID, data: CommandInput, actor=Depends(writing)):
        node_id = str(node_id)
        now = int(time.time())
        try:
            validate_command(data.action, data.params)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        with store.transaction() as db:
            store.expire(db, now)
            node = db.execute('SELECT * FROM nodes WHERE id=?', (node_id,)).fetchone()
            if not node:
                raise HTTPException(404, 'node not found')
            if now - node['last_seen'] >= 90:
                raise HTTPException(409, 'node offline')
            snapshot = json.loads(node['snapshot'])
            if not snapshot:
                raise HTTPException(409, 'waiting for node snapshot')
            if data.action not in snapshot.get('capabilities', ['start', 'stop', 'update_schedule']):
                raise HTTPException(409, 'operation is not supported by this node')
            if data.action == 'update_schedule' and data.config_version != snapshot.get('config_version'):
                raise HTTPException(409, 'configuration conflict; refresh first')
            if db.execute("SELECT 1 FROM commands WHERE node_id=? AND state IN ('queued','dispatched')", (node_id,)).fetchone():
                raise HTTPException(409, 'another command is pending')
            command_id = str(uuid.uuid4())
            db.execute('INSERT INTO commands VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (command_id, node_id, data.action, json.dumps(data.params), data.config_version, actor, now, now + 300, 'queued', None, None))
        return {'id': command_id, 'state': 'queued', 'expires_at': now + 300}

    @app.get('/api/v1/nodes/{node_id}/commands', dependencies=[Depends(reader)])
    def commands(node_id: uuid.UUID):
        require_node(str(node_id))
        with store.transaction() as db:
            store.expire(db, int(time.time()))
            rows = db.execute('SELECT * FROM commands WHERE node_id=? ORDER BY created_at DESC,rowid DESC LIMIT 50', (str(node_id),)).fetchall()
        return {'commands': [{**dict(row), 'params': json.loads(row['params']), 'result': json.loads(row['result']) if row['result'] else None} for row in rows]}

    @app.get('/api/v1/agent/commands')
    def claim(node=Depends(node_auth)):
        with store.transaction() as db:
            store.expire(db, int(time.time()))
            row = db.execute("SELECT * FROM commands WHERE node_id=? AND state IN ('queued','dispatched') ORDER BY created_at LIMIT 1", (node['id'],)).fetchone()
            if not row:
                return {'command': None}
            db.execute("UPDATE commands SET state='dispatched' WHERE id=?", (row['id'],))
        return {'command': {'id': row['id'], 'action': row['action'], 'params': json.loads(row['params']),
                            'config_version': row['config_version'], 'expires_at': row['expires_at']}}

    @app.post('/api/v1/agent/commands/{command_id}/result')
    def receipt(command_id: uuid.UUID, data: CommandResult, node=Depends(node_auth)):
        try:
            encoded_result = json.dumps(data.result, allow_nan=False)
        except ValueError as exc:
            raise HTTPException(422, 'non-finite result') from exc
        with store.transaction() as db:
            row = db.execute('SELECT * FROM commands WHERE id=? AND node_id=?', (str(command_id), node['id'])).fetchone()
            if not row:
                raise HTTPException(404, 'command not found')
            if row['state'] == 'queued':
                raise HTTPException(409, 'command not dispatched')
            # Allow a delayed durable receipt to resolve a timeout marked unknown.
            if row['state'] in {'dispatched', 'unknown'}:
                db.execute('UPDATE commands SET state=?,result=?,finished_at=? WHERE id=?',
                           (data.state, encoded_result, int(time.time()), str(command_id)))
            elif row['state'] != data.state:
                raise HTTPException(409, 'command already finalized')
        return {'ok': True}

    public = Path(__file__).parent / 'public'
    @app.get('/', dependencies=[Depends(admin)])
    def index():
        return FileResponse(public / 'index.html')
    app.mount('/assets', StaticFiles(directory=public), name='assets')
    return app
