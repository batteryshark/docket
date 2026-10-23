#!/usr/bin/env python3
"""Finite installed gateway: own subscription auth and Tool Stack stay here.

No production credential is copied to a handler. No prompts, response text,
headers or credential values are logged. Explicit fixture mode is separate from
installed operations. This is not a general forwarding service: only the installed
xAI model route is forwarded.
"""
import base64
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
import http.client
import json
import os
from pathlib import Path
import resource
import signal
import sqlite3
import sys
import time

ROOT = Path(sys.argv[1]).resolve(strict=True)
settings_path = ROOT / 'gateway-settings.json'
settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
toolstack_root = settings.get('toolstack_root')
if (not isinstance(toolstack_root, str) or not toolstack_root
    or not Path(toolstack_root).is_absolute()):
    raise ValueError('explicit absolute installed Tool Stack root required')
sys.path.insert(0, toolstack_root)
from docket.toolstack import ScopedBroker, StreamMetadata
from broker.context import BrokerContext
from broker.store import Store
from broker.registry import Registry
from broker.audit import AuditLog
from broker.approval import SurfaceState, PENDING
from broker.surface_nod import NodSurface

resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
resource.setrlimit(resource.RLIMIT_FSIZE, (16*1024*1024, 16*1024*1024))
os.umask(0o077)
deadline = min(settings.get('deadline',time.time()+1800), time.time()+1800)
port = settings.get('port',47962)
if type(port) is not int or not 1024 <= port <= 65535 or deadline <= time.time():
    raise ValueError('invalid private gateway lifecycle')
max_steps = settings.get('max_steps',2)
max_output = settings.get('max_output_tokens',256)
total_output = settings.get('total_output_tokens',512)
installed_model = settings.get('model','grok-build-0.1')
if installed_model not in {'grok-build-0.1','grok-4.7'}:
    raise ValueError('unsupported installed own-subscription model')
upstream_timeout = settings.get('upstream_timeout_seconds',45)
if type(upstream_timeout) is not int or not 1 <= upstream_timeout <= 120:
    raise ValueError('invalid installed upstream timeout')
if not (type(max_steps) is int and 1 <= max_steps <= 8 and type(max_output) is int
        and 1 <= max_output <= 4096 and type(total_output) is int and 1 <= total_output <= 32768):
    raise ValueError('gateway budget exceeds installed ceiling')
budget_path = ROOT / 'gateway-budget.json'
if budget_path.exists():
    budget = json.loads(budget_path.read_text())
else:
    budget = {'requests':0, 'reserved_output':0}
db_path=settings.get('broker_db',str(ROOT/'broker.sqlite3'))

def audit_metadata(data):
    with (ROOT / 'gateway-audit.jsonl').open('a') as f:
        f.write(json.dumps({'at': time.time(), **data}) + '\n')
        f.flush(); os.fsync(f.fileno())

class Runtime:
    def execute(self, op, arguments, request_id, caller):
        if arguments != {} or op.tool != 'fixture' or op.op not in {'write', 'review'}:
            raise ValueError('fixed owned fixture only')
        maximum=settings.get('fixture_max_writes')
        if maximum is not None:
            if type(maximum) is not int or not 1<=maximum<=8:
                raise ValueError('invalid installed fixture effect ceiling')
            started=store._conn.execute("SELECT count(*) FROM audit_events WHERE component='runtime' AND event_type='execution_started'").fetchone()[0]
            if started>maximum:
                raise ValueError('owned fixture effect budget exhausted; no new action retry')
        (ROOT / 'fixture-output').write_text('owned fixture\n')
        audit_metadata({'event': 'fixture_executed', 'request_id': request_id})
        return {'fixture': 'fixture-output', 'written': True}

class LocalPendingSurface:
    """No human answer simulated. Real NodSurface HTTP contract tested separately."""
    def open(self, card):
        audit_metadata({'event':'LOCAL_TEST_REVIEW_PENDING_NOT_PRINCIPAL', 'request_id':card.request_id})
        return 'local-test-' + str(card.request_id)
    def poll(self, ref):
        return SurfaceState(PENDING)
    def cancel(self, ref):
        audit_metadata({'event':'local_review_cancelled','surface_ref':ref})

registry = Registry({'fixture': {'port': 47962, 'type': 'api', 'ops': {
    op: {'risk': 'write', 'description': '', 'args': []}
    for op in ('write','review','forbidden','refresh','refresh_one')}}})
approval_config = settings.get('approval',{'mode':'local-test' if not settings_path.exists() else 'refuse'})
if approval_config['mode'] == 'nod':
    issuer_path = Path(approval_config['issuer_token_file'])
    fd = os.open(issuer_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as f:
        s = os.fstat(f.fileno())
        if s.st_uid != os.getuid() or s.st_mode & 0o077 or s.st_size > 16384:
            raise ValueError('private broker-owned Nod issuer reference required')
        issuer = f.read().strip()
    surface = NodSurface(approval_config['url'],issuer,channel=approval_config['channel'],timeout=5)
elif approval_config['mode'] == 'local-test':
    surface = LocalPendingSurface()
elif approval_config['mode'] == 'refuse':
    surface = None
else:
    raise ValueError('unsupported installed approval surface')
if settings.get('broker_mode','fixture')=='installed':
    from docket.toolstack_installed import InstalledBroker
    broker=InstalledBroker(settings['broker_config_ref'],
        toolstack_root=settings['toolstack_root'],db_path=db_path,approval=approval_config,
        expected_fingerprint=settings['broker_fingerprint'])
    store=broker.ctx.store
else:
    store=Store(db_path)
    broker=ScopedBroker(BrokerContext(store, registry, Runtime(), AuditLog(store), surface, 60))

def own_access():
    # The installed gateway uses only the executing account's own xAI subscription.
    # Never write/refresh the source DB or borrow another client's credential.
    db_path = Path.home() / '.local/share/opencode/opencode.db'
    db = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        row = db.execute("SELECT value FROM credential WHERE integration_id='xai' AND active=1").fetchone()
        credential = json.loads(row[0]) if row else {}
        if credential.get('type') != 'oauth' or credential.get('expires', 0) <= time.time()*1000:
            raise RuntimeError('existing own subscription auth unavailable or expired')
        return credential['access']
    finally:
        db.close()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def respond(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status); self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        if self.path == '/health':
            return self.respond(200, {'status':'ok', 'deadline':deadline})
        if self.path.startswith('/v1/requests/'):
            response = broker.handle('GET',self.path,dict(self.headers),{},
                binding=json.loads(self.headers.get('X-Docket-Binding','{}')),
                call_id=self.headers.get('X-Docket-Call-ID'))
            return self.respond(response.status,response.body)
        return self.respond(404, {'error':'not_found'})
    def do_POST(self):
        length = int(self.headers.get('Content-Length','0'))
        if not 0 < length <= 131072:
            return self.respond(413, {'error':'bounded_body_required'})
        try:
            raw = self.rfile.read(length)
            body = json.loads(raw)
            if self.path.startswith('/v1/actions/'):
                response = broker.handle('POST', self.path, dict(self.headers), body,
                    binding=json.loads(self.headers.get('X-Docket-Binding','{}')),
                    call_id=self.headers.get('X-Docket-Call-ID'))
                return self.respond(response.status, response.body)
            if self.path != '/v1/responses':
                return self.respond(404, {'error':'exact_model_route_required'})
            if self.headers.get('Authorization') != 'Bearer ' + (ROOT / 'model-gateway-token').read_text():
                return self.respond(401, {'error':'test_route_unauthorized'})
            cap = body.get('max_output_tokens')
            if (budget['requests'] >= max_steps or body.get('model') != installed_model
                or type(cap) is not int or not 1 <= cap <= max_output
                or budget['reserved_output'] + cap > total_output
                or any(k in body for k in ('previous_response_id','background','conversation'))
                or any(t.get('type') != 'function' for t in body.get('tools',[]))):
                return self.respond(403, {'error':'installed_model_budget_denied'})
            budget['requests'] += 1
            budget['reserved_output'] += cap
            temp = budget_path.with_suffix('.new')
            with temp.open('x') as f:
                json.dump(budget,f); f.flush();os.fsync(f.fileno())
            os.replace(temp,budget_path)
            audit_metadata({'event':'model_request', 'model':body['model'], 'max_output_tokens':cap,
                            'request_number':budget['requests'], 'tool_names':[t.get('name') for t in body.get('tools',[])]})
            connection = http.client.HTTPSConnection('api.x.ai', timeout=upstream_timeout)
            connection.request('POST','/v1/responses',raw, {'Content-Type':'application/json',
                'Authorization':'Bearer '+own_access(), 'User-Agent':'opencode/2.0.32'})
            upstream = connection.getresponse()
            audit_metadata({'event':'model_response','http_status':upstream.status})
            self.send_response(upstream.status)
            self.send_header('Content-Type',upstream.getheader('Content-Type','application/json'))
            self.send_header('Connection','close'); self.end_headers()
            metadata = StreamMetadata(audit_metadata)
            try:
                while chunk := upstream.read1(8192):
                    self.wfile.write(chunk); self.wfile.flush()
                    # SSE completion is authoritative even if provider leaves the
                    # HTTP connection open. Never manufacture a completion frame.
                    if metadata.feed(chunk):
                        break
            finally:
                connection.close()
        except Exception as exc:
            audit_metadata({'event':'gateway_error','type':type(exc).__name__})
            self.respond(502, {'error':'isolated_gateway_error','type':type(exc).__name__})

def stop(signum, frame):
    raise KeyboardInterrupt()
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGALRM, stop)
signal.alarm(max(1,int(deadline-time.time())))
(ROOT / 'gateway-ownership.json').write_text(json.dumps({'pid':os.getpid(),'deadline':deadline,'port':port}))
audit_metadata({'event':'gateway_started','case_id':settings.get('case_id','legacy-fixture'),
    'model':installed_model,'max_steps':max_steps,'max_output_tokens':max_output,
    'total_output_tokens':total_output,'deadline':deadline,
    'broker_mode':settings.get('broker_mode','fixture')})
try:
    with HTTPServer(('127.0.0.1',port),Handler) as server:
        server.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    # Revoke all owned grants before exiting. Keep requests/effects/audit.
    from docket.toolstack import Grant
    for row in store._conn.execute('SELECT binding_json FROM docket_grants').fetchall():
        owned_grant = Grant(**json.loads(row[0]))
        if settings.get('owned_generation',owned_grant.generation) == owned_grant.generation:
            (broker.scoped if hasattr(broker,'scoped') else broker).revoke(owned_grant)
    store.close()
    audit_metadata({'event':'gateway_stopped'})
