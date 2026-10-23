"""Trusted, instance-scoped Tool Stack adapter (broker imported unmodified).

The operator supplies a BrokerContext from the installed Tool Stack package.
Never point this at an ambient caller or reuse an existing client's credential.
All ingress to this context MUST use ScopedBroker.handle (not broker.gateway).
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import secrets
import threading
import time


def narrow_policy(baseline: dict, profile: dict) -> dict:
    """Intersect exact operations; review cannot become allow."""
    result = {}
    for tool, ops in profile.get('tools', {}).items():
        for op, requested in ops.items():
            installed = baseline.get('tools', {}).get(tool, {}).get(op, 'deny')
            if requested not in {'allow', 'review', 'deny'}:
                raise ValueError('invalid policy effect')
            effect = ('deny' if 'deny' in (installed, requested)
                      else 'review' if 'review' in (installed, requested) else 'allow')
            result.setdefault(tool, {})[op] = effect
    return {'tools': result}


@dataclass(frozen=True)
class Grant:
    caller_id: int
    caller_name: str
    token_ref: str  # hash only, never the raw bearer
    session_id: str
    admission_id: str
    delivery_id: str
    generation: str
    deadline: float
    prepared_hash: str | None = None
    body_sha256: str | None = None
    owner_generation: str | None = None
    profile_id: str | None = None
    profile_version: str | None = None
    baseline_version: str | None = None


class StreamMetadata:
    """Bounded SSE metadata only; never retain/log prompt or reasoning text."""
    TERMINAL = {'response.completed','response.failed','response.incomplete','[DONE]'}
    KNOWN = TERMINAL | {'response.created','response.in_progress','response.output_item.added',
        'response.output_item.done','response.content_part.added','response.content_part.done',
        'response.output_text.delta','response.output_text.done','response.reasoning_text.delta',
        'response.reasoning_text.done','response.reasoning_summary_text.delta',
        'response.reasoning_summary_text.done','response.reasoning_summary_part.added',
        'response.reasoning_summary_part.done','response.function_call_arguments.delta',
        'response.function_call_arguments.done'}

    def __init__(self, audit):
        self.audit, self.pending, self.event = audit, b'', None
        self.counts = {}
        self.terminal_recorded = False

    def feed(self, chunk):
        self.pending += chunk
        if len(self.pending) > 131072:
            raise ValueError('upstream SSE metadata frame exceeds installed bound')
        terminal = False
        while b'\n' in self.pending:
            line,self.pending = self.pending.split(b'\n',1)
            line = line.rstrip(b'\r')
            if not line:
                terminal = terminal or self.event in self.TERMINAL
                if self.event in self.TERMINAL and not self.terminal_recorded:
                    self.audit({'event':'upstream_terminal','type':self.event,'event_counts':dict(self.counts)})
                self.event = None
                self.terminal_recorded = False
            elif line.startswith(b'event: '):
                named=line[7:].decode('ascii',errors='replace')
                if named in self.KNOWN:
                    self.event=named
            elif line.startswith(b'data: '):
                if line[6:] == b'[DONE]':
                    self.event='[DONE]'
                    self.audit({'event':'upstream_stream_end','marker':'[DONE]'})
                    self.terminal_recorded=True
                    continue
                try:
                    data=json.loads(line[6:])
                except (ValueError,UnicodeError):
                    continue
                if not isinstance(data,dict):
                    continue
                event=data.get('type',self.event)
                if event in self.KNOWN:
                    self.counts[event]=self.counts.get(event,0)+1
                    if self.counts[event]==1:
                        self.audit({'event':'upstream_event_type','type':event})
                if event in self.TERMINAL:
                    self.event=event
                    usage=data.get('response',{}).get('usage',{})
                    self.audit({'event':'upstream_terminal','type':event,
                        'event_counts':dict(self.counts),
                        'input_tokens':usage.get('input_tokens') if type(usage.get('input_tokens')) is int else None,
                        'output_tokens':usage.get('output_tokens') if type(usage.get('output_tokens')) is int else None})
                    self.terminal_recorded=True
        return terminal


class ScopedBroker:
    """One constrained caller per generation, finite lease, exact effect journal.

    The raw bearer is returned ONCE to the trusted launcher, not put in Grant,
    prompts, events or audit. Its authenticated holder is bound to the session,
    admission and generation; those labels alone are never authentication.
    """
    def __init__(self, ctx):
        self.ctx = ctx
        self.lock = threading.RLock()
        ctx.store._conn.executescript('''
          CREATE TABLE IF NOT EXISTS docket_grants (
            token_ref TEXT PRIMARY KEY, binding_json TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS docket_effects (
            token_ref TEXT NOT NULL, call_id TEXT NOT NULL, digest TEXT NOT NULL,
            state TEXT NOT NULL, response_json TEXT,
            PRIMARY KEY(token_ref, call_id));
        ''')
        ctx.store._conn.commit()

    def mint(self, *, baseline, profile, session_id, admission_id, delivery_id,
             generation, deadline, prepared=None):
        from broker.identity import hash_token
        if not all(isinstance(x, str) and x for x in
                   (session_id, admission_id, delivery_id, generation)):
            raise ValueError('complete lifecycle identity required')
        if not time.time() < deadline <= time.time() + 3600:
            raise ValueError('finite lease required')
        policy = narrow_policy(baseline, profile)
        raw = secrets.token_urlsafe(32)
        ref = hash_token(raw)
        name = 'docket-' + secrets.token_hex(16)
        with self.lock:
            caller_id = self.ctx.store.add_caller(name)
            self.ctx.store.set_policy(caller_id, policy)
            self.ctx.store.add_token(caller_id, ref)
            preparation={}
            if prepared is not None:
                if prepared['admission_id']!=admission_id:
                    raise ValueError('preparation admission mismatch')
                preparation={k:prepared[k] for k in ('owner_generation','profile_id','profile_version','baseline_version')}
                preparation.update(prepared_hash=hashlib.sha256(json.dumps(prepared,sort_keys=True,
                    separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest(),
                    body_sha256=hashlib.sha256(prepared['briefing'].encode()).hexdigest())
            grant = Grant(caller_id, name, ref, session_id, admission_id,
                          delivery_id, generation, deadline,**preparation)
            self.ctx.store._conn.execute('INSERT INTO docket_grants VALUES (?, ?)',
                                        (ref, json.dumps(grant.__dict__)))
            self.ctx.store._conn.commit()
        return grant, raw

    def revoke(self, grant):
        """Revoke before stopping compute. Retain all effect/request audit."""
        from broker.approval import CANCELLED
        with self.lock:
            self.ctx.store.revoke_token(grant.token_ref)
            self.ctx.store.revoke_caller(grant.caller_name)
            for row in self.ctx.store.pending_approvals_for_caller(grant.caller_id):
                self.ctx.store.update_approval(row['id'], status=CANCELLED)
                self.ctx.store.update_request(row['request_id'], status='expired', arguments_json=None)
                if self.ctx.surface:
                    self.ctx.surface.cancel(row['surface_ref'])

    def handle(self, method, path, headers, body, *, binding, call_id=None):
        """Only ingress. Mutating/approval-resolving calls require stable call ID.

        An interrupted intent is uncertain forever until explicit reconciliation;
        it is never resubmitted, even with identical arguments. GET request status
        is effectful; callers give each authoritative resolution attempt its own
        persisted identity, never reinterpret a transport timeout as a new action.
        """
        from broker.gateway import Response, handle
        from broker.identity import authenticate, bearer_token, hash_token
        headers = {k.lower(): v for k, v in headers.items()}
        with self.lock:
            caller = authenticate(self.ctx.store, headers.get('authorization'))
            raw = bearer_token(headers.get('authorization'))
            ref = hash_token(raw) if raw else ''
            row = self.ctx.store._conn.execute(
                'SELECT binding_json FROM docket_grants WHERE token_ref=?', (ref,)).fetchone()
            if caller is None:
                return handle(method, path, headers, body, self.ctx)
            if row is None:
                return Response(403, {'error': 'not_a_docket_grant'}, 'docket')
            grant = Grant(**json.loads(row[0]))
            expected = {k: getattr(grant, k) for k in
                        ('session_id', 'admission_id', 'delivery_id', 'generation')}
            for key in ('prepared_hash','body_sha256','owner_generation','profile_id','profile_version','baseline_version'):
                if getattr(grant,key) is not None:
                    expected[key]=getattr(grant,key)
            if binding != expected or time.time() >= grant.deadline:
                if time.time() >= grant.deadline:
                    self.revoke(grant)
                return Response(403, {'error': 'binding_or_lease_denied'}, 'docket')
            # The upstream auto-allow exception is NOT accepted by this boundary.
            # Even explicit profile allow needs matching installed baseline (mint).
            if path.startswith('/v1/actions/') and path.rsplit('.', 1)[-1] in {'refresh', 'refresh_one'}:
                spec = path[len('/v1/actions/'):].split('.')
                if len(spec) != 2 or self.ctx.store.policy_for(caller.id).get('tools', {}).get(spec[0], {}).get(spec[1], 'deny') != 'allow':
                    rid = self.ctx.store.create_request(call_id or 'docket', caller.id, spec[0], spec[-1], 'denied')
                    self.ctx.audit.record('policy', 'docket_refresh_denied', 'denied', call_id or 'docket', request_id=rid, details={})
                    return Response(403, {'status': 'denied', 'request_id': rid}, call_id or 'docket')
            # Do not expose upstream MCP (would bypass the refresh gate above).
            if path == '/mcp':
                return Response(403, {'error': 'use_exact_action_endpoint'}, 'docket')
            effectful = method != 'GET' or path.startswith('/v1/requests/')
            if not effectful:
                return handle(method, path, headers, body, self.ctx)
            if not isinstance(call_id, str) or not 1 <= len(call_id) <= 128:
                return Response(400, {'error': 'stable_call_id_required'}, 'docket')
            digest = hashlib.sha256(json.dumps([method, path, body], sort_keys=True).encode()).hexdigest()
            db = self.ctx.store._conn
            old = db.execute('SELECT digest,state,response_json FROM docket_effects WHERE token_ref=? AND call_id=?', (ref, call_id)).fetchone()
            if old:
                if old[0] != digest:
                    return Response(409, {'error': 'call_id_conflict'}, call_id)
                if old[2]:
                    saved = json.loads(old[2])
                    return Response(**saved)
                return Response(409, {'error': 'uncertain_effect_no_replay'}, call_id)
            db.execute('INSERT INTO docket_effects VALUES (?,?,?,?,NULL)', (ref, call_id, digest, 'intent'))
            db.commit()
            headers['x-correlation-id'] = call_id
            response = handle(method, path, headers, body, self.ctx)
            db.execute('UPDATE docket_effects SET state=?, response_json=? WHERE token_ref=? AND call_id=?',
                       ('recorded', json.dumps(response.__dict__), ref, call_id))
            db.commit()
            return response
