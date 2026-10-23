"""Integration with an explicitly supplied, unmodified Tool Stack checkout.

Run with TOOLSTACK_SOURCE=<checkout> python3.13 -m unittest tests.test_toolstack.
The temporary surface is a local test authority, NOT a principal Nod answer.
"""
import os
import sys
import tempfile
from pathlib import Path
import time
import unittest
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

from docket.toolstack import ScopedBroker, narrow_policy

SOURCE = os.environ.get('TOOLSTACK_SOURCE')
if SOURCE:
    sys.path.insert(0, SOURCE)


@unittest.skipUnless(SOURCE, 'explicit Tool Stack source required')
class ToolStackTests(unittest.TestCase):
    def setUp(self):
        from broker.store import Store
        from broker.registry import Registry
        from broker.audit import AuditLog
        from broker.context import BrokerContext
        from broker.approval import SurfaceState, PENDING
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / 'broker.sqlite3'))
        self.executions = []
        self.cards = []
        self.decision = SurfaceState(PENDING)
        surface = SimpleNamespace(open=self.open_card, poll=lambda ref: self.decision, cancel=lambda ref: None)
        registry = Registry({'fixture': {'port': 47963, 'type': 'api', 'ops':
            {op: {'risk': 'write', 'description': '', 'args': []}
             for op in ('write', 'review', 'forbidden', 'refresh', 'refresh_one')}}})
        runtime = SimpleNamespace(execute=self.execute)
        ctx = BrokerContext(self.store, registry, runtime, AuditLog(self.store), surface, 60)
        self.broker = ScopedBroker(ctx)
        policy = {'tools': {'fixture': {'write': 'allow', 'review': 'review'}}}
        self.grant, self.raw = self.broker.mint(baseline=policy, profile=policy,
            session_id='ses_fixture', admission_id='admission-1', delivery_id='delivery-1',
            generation='generation-1', deadline=time.time() + 60)
        self.binding = {k: getattr(self.grant, k) for k in ('session_id','admission_id','delivery_id','generation')}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def open_card(self, card):
        self.cards.append(card)
        return 'local-test-nod-ref'

    def execute(self, op, args, request_id, caller):
        # Only this owned fixture is writable. No host path comes from arguments.
        path = Path(self.tmp.name) / 'fixture-output'
        path.write_text('owned fixture')
        self.executions.append(request_id)
        return {'written': 'fixture-output'}

    def call(self, path, call_id='call-1', method='POST'):
        return self.broker.handle(method, path, {'Authorization': 'Bearer ' + self.raw},
            {'arguments': {}}, binding=self.binding, call_id=call_id)

    def test_allow_deny_no_replay_revoke(self):
        out = self.call('/v1/actions/fixture.write')
        self.assertEqual(out.status, 200)
        self.assertEqual(self.call('/v1/actions/fixture.write').body, out.body)
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.call('/v1/actions/fixture.forbidden', 'deny').status, 403)
        for op in ('refresh', 'refresh_one'):
            self.assertEqual(self.call('/v1/actions/fixture.' + op, op).status, 403)
        self.broker.revoke(self.grant)
        self.assertEqual(self.call('/v1/actions/fixture.write', 'after-revoke').status, 401)
        self.assertEqual(len(self.executions), 1)
        self.assertTrue(self.store.audit_events(request_id=out.body['request_id']))

    def test_review_parks_exact_request_and_reject_never_executes(self):
        from broker.approval import SurfaceState, REJECTED
        out = self.call('/v1/actions/fixture.review')
        self.assertEqual(out.status, 202)
        self.assertEqual(len(self.cards), 1)
        self.assertEqual(self.cards[0].request_id, out.body['request_id'])
        self.assertEqual(self.executions, [])
        path = '/v1/requests/' + str(out.body['request_id'])
        self.assertEqual(self.call(path, 'poll-pending', 'GET').body['status'], 'pending_approval')
        self.decision = SurfaceState(REJECTED, 'LOCAL-TEST-PROVIDER-NOT-PRINCIPAL')
        self.assertEqual(self.call(path, 'poll-rejected', 'GET').body['status'], 'denied')
        self.assertEqual(self.executions, [])

    def test_binding_and_lease(self):
        self.binding['generation'] = 'new-generation'
        self.assertEqual(self.call('/v1/actions/fixture.write').status, 403)
        self.assertEqual(self.executions, [])

    def test_distinct_callers_and_review_narrowing(self):
        p = {'tools': {'fixture': {'write': 'allow'}}}
        grant, _ = self.broker.mint(baseline=p, profile=p, session_id='s2', admission_id='a2',
            delivery_id='d2', generation='g2', deadline=time.time()+60)
        self.assertNotEqual(grant.caller_id, self.grant.caller_id)
        self.assertEqual(narrow_policy({'tools': {'f': {'o': 'review'}}}, {'tools': {'f': {'o': 'allow'}}}),
                         {'tools': {'f': {'o': 'review'}}})

    def test_actual_nod_surface_http_contract_pending_rejected(self):
        """Actual unmodified NodSurface; local test issuer, NOT real Nod/human."""
        from broker.surface_nod import NodSurface
        cards = []
        decision = {'request_id': 'fixture-nod-1', 'status': 'pending', 'decision': None}
        case = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                case.assertEqual(self.headers.get('Authorization'), 'Bearer LOCAL-TEST-ISSUER')
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path == '/api/v1/requests':
                    cards.append(payload)
                    response = {'request_id': 'fixture-nod-1', 'deduped': False}
                else:
                    response = {}
                self.respond(response)

            def do_GET(self):
                case.assertEqual(self.path, '/api/v1/requests/fixture-nod-1/decision')
                case.assertEqual(self.headers.get('Authorization'), 'Bearer LOCAL-TEST-ISSUER')
                self.respond(decision)

            def respond(self, response):
                encoded = json.dumps(response).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args):
                pass

        with HTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                self.broker.ctx.surface = NodSurface('http://127.0.0.1:' + str(server.server_port),
                                                     'LOCAL-TEST-ISSUER', timeout=2)
                out = self.call('/v1/actions/fixture.review')
                self.assertEqual(out.status, 202)
                rid = out.body['request_id']
                self.assertEqual(cards[0]['dedupe_key'], str(rid))
                self.assertEqual(cards[0]['notification'], {'redact': True})
                self.assertEqual(self.executions, [])
                path = '/v1/requests/' + str(rid)
                self.assertEqual(self.call(path, 'nod-pending', 'GET').body['status'], 'pending_approval')
                decision.update(status='resolved', decision={
                    'option_kind': 'reject', 'actor_user_id': 'LOCAL-TEST-NOT-PRINCIPAL',
                    'resolved_at': '2026-10-22T20:00:00Z'})
                self.assertEqual(self.call(path, 'nod-reject', 'GET').body['status'], 'denied')
                self.assertEqual(self.executions, [])
                self.assertEqual(self.store.approval_for_request(rid)['status'], 'rejected')
            finally:
                server.shutdown()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())

    def test_lifecycle_idempotence_completion_and_revoke_first_prune(self):
        from docket.sandbox import Sandbox
        from broker.identity import hash_token
        calls = []
        observed = {'state': 'admitted', 'consumed': False}
        runtime = SimpleNamespace(
            allocate=lambda workspace, generation, **kw: {'session_id': 'ses_handler', 'runtime_ref': 'owned-parent'},
            launch=lambda binding, prepared, raw: calls.append(('launch', binding['environment_id'])),
            status=lambda binding: observed)
        def stop(binding):
            self.assertIsNone(self.store.caller_by_token_hash(binding['token_ref']))
            calls.append(('stop-after-revoke', binding['environment_id']))
            return True
        runtime.stop = stop
        manager = Sandbox(Path(self.tmp.name) / 'lifecycle', runtime=runtime, broker=self.broker,
            baseline={'broker_policy': {'tools': {'fixture': {'write': 'allow'}}},
                      'tool_operations': {'docket_fixture': ['fixture.write']}})
        prepared = {'admission_id': 'msg_exact', 'policy': {'docket_fixture': 'allow'}}
        binding = manager.launch(prepared, 'delivery-exact', lifecycle={
            'delivery_id': 'delivery-exact', 'generation': 'trusted-runtime-generation',
            'deadline': time.time()+60, 'lease_until': time.time()+30})
        self.assertEqual(manager.launch(prepared, 'delivery-exact'), binding)
        self.assertEqual(len(calls), 1)
        self.assertEqual(manager.status(binding), 'admitted')
        observed.update(consumed=True, finished=True, trusted_completion=True, output_manifest=[],
                        native_terminal={'admission_id':binding['admission_id'], 'session_id':binding['session_id'],
                            'body_sha256':binding['briefing_sha256'], 'terminal_id':'msg_terminal',
                            'outcome':'succeeded','not_active':True,'empty_inbox':True})
        self.assertEqual(manager.status(binding), 'handler_finished')
        self.assertFalse(Path(binding['workspace']).exists())
        self.assertEqual(manager.status(binding), 'handler_finished')
        self.assertEqual(calls[-1][0], 'stop-after-revoke')
        with self.assertRaises(ValueError):
            manager.prune({**binding, 'generation': 'different-generation'})
        self.assertNotIn('grant', manager.receipt(binding))
        self.assertNotIn('token_ref', manager.receipt(binding))

    def test_completion_never_finished_until_backend_proves_stop_quiet(self):
        from docket.sandbox import Sandbox
        observed = {}
        runtime = SimpleNamespace(allocate=lambda *a, **kw: {'session_id':'ses_busy','runtime_ref':'busy'},
            launch=lambda *a: None, status=lambda b: observed, stop=lambda b: False)
        manager = Sandbox(Path(self.tmp.name)/'busy', runtime=runtime, broker=self.broker,
                          baseline={'broker_policy':{}, 'tool_operations':{}})
        binding = manager.launch({'admission_id':'msg_busy','policy':{}}, 'delivery-busy')
        observed.update(consumed=True, finished=True, trusted_completion=True,
            native_terminal={'admission_id':binding['admission_id'], 'session_id':binding['session_id'],
                'body_sha256':binding['briefing_sha256'],'terminal_id':'msg_terminal',
                'outcome':'succeeded','not_active':True,'empty_inbox':True})
        self.assertNotEqual(manager.status(binding), 'handler_finished')
        self.assertEqual(manager.receipt(binding)['phase'], 'revoked')
        self.assertTrue(Path(binding['workspace']).exists())

    def test_lifecycle_failed_launch_never_replays_uncertain_effect(self):
        from docket.sandbox import Sandbox
        calls = []
        def fail(binding, prepared, raw):
            calls.append('uncertain-launch')
            raise TimeoutError('local test uncertain admission')
        runtime = SimpleNamespace(
            allocate=lambda w, g, **kw: {'session_id': 'ses_failed', 'runtime_ref': 'owned-failed'},
            launch=fail, stop=lambda b: True, status=lambda b: {'failed': True})
        manager = Sandbox(Path(self.tmp.name) / 'failed', runtime=runtime, broker=self.broker,
                          baseline={'broker_policy': {}, 'tool_operations': {}})
        prepared = {'admission_id':'msg_failed', 'policy':{}}
        with self.assertRaises(TimeoutError):
            manager.launch(prepared, 'delivery-failed')
        binding = manager.launch(prepared, 'delivery-failed')
        self.assertEqual(binding['phase'], 'uncertain')
        self.assertEqual(calls, ['uncertain-launch'])
        self.assertIsNone(self.store.caller_by_token_hash(binding['token_ref']))
        self.assertEqual(manager.prune(binding)['phase'], 'pruned')

    def test_allocation_failure_and_unknown_allocation_deadline(self):
        from docket.sandbox import Sandbox, AllocationNoEffects
        root = Path(self.tmp.name) / 'allocation'
        def no_effects(*args, **kw):
            raise AllocationNoEffects('proved no compute started')
        runtime = SimpleNamespace(allocate=no_effects, stop=lambda b: False)
        manager = Sandbox(root, runtime=runtime, broker=self.broker,
                          baseline={'broker_policy': {}, 'tool_operations': {}})
        prepared = {'admission_id':'msg_allocate', 'policy':{}}
        with self.assertRaises(AllocationNoEffects):
            manager.launch(prepared, 'delivery-allocate')
        binding = manager.launch(prepared, 'delivery-allocate')
        self.assertEqual(binding['phase'], 'pruned')
        self.assertFalse(Path(binding['workspace']).exists())
        def uncertain(*args, **kw):
            raise TimeoutError('compute may have started')
        runtime.allocate = uncertain
        with self.assertRaises(TimeoutError):
            manager.launch(prepared, 'delivery-unknown')
        binding = manager.launch(prepared, 'delivery-unknown')
        self.assertEqual(binding['phase'], 'allocation_uncertain')
        self.assertEqual(binding['runtime_ref']['allocation_id'], binding['environment_id'])
        path = manager._path('delivery-unknown')
        binding['lease_until'] = time.time()-1
        manager._save(path, binding)
        self.assertEqual(manager.status(binding), 'unavailable')
        self.assertTrue(Path(binding['workspace']).exists())  # unknown live compute retained
        runtime.stop = lambda b: True  # installed backend now proves exact allocation quiet
        self.assertEqual(manager.status(binding), 'missing')
        self.assertFalse(Path(binding['workspace']).exists())

    def test_expired_status_outage_revokes_before_recover_stop(self):
        from docket.sandbox import Sandbox
        def outage(b):
            raise ConnectionError('offline')
        def stop(b):
            self.assertIsNone(self.store.caller_by_token_hash(b['token_ref']))
            return False
        runtime = SimpleNamespace(allocate=lambda *a, **kw: {'session_id':'ses_outage','runtime_ref':'exact-parent'},
            launch=lambda *a: None, status=outage, stop=stop)
        manager = Sandbox(Path(self.tmp.name)/'outage', runtime=runtime, broker=self.broker,
                          baseline={'broker_policy':{},'tool_operations':{}})
        binding = manager.launch({'admission_id':'msg_outage','policy':{}}, 'delivery-outage')
        self.assertEqual(manager.status(binding), 'unavailable')
        binding['lease_until'] = time.time()-1
        manager._save(manager._path(binding['delivery_id']), binding)
        self.assertEqual(manager.status(binding), 'unavailable')
        self.assertIsNone(self.store.caller_by_token_hash(binding['token_ref']))
        self.assertTrue(Path(binding['workspace']).exists())
