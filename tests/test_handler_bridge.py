"""EventHost composition contract tests; fixture backend is NOT OS/native proof."""
import copy
import hashlib
import json
import time
import unittest

from docket.event_host import EventHost
from tests import test_wake_profiles as owner_fixture


class LifecycleFixture:
    def __init__(self):
        self.bindings, self.launches, self.reply = {}, 0, 'admitted'
        self.terminal, self.lost_ack = {}, False

    def lookup(self, delivery_id):
        return copy.deepcopy(self.bindings.get(delivery_id))

    def launch(self, prepared, delivery_id, *, lifecycle):
        self.launches += 1
        binding = {'delivery_id':delivery_id, 'admission_id':prepared['admission_id'],
                   'generation':lifecycle['generation'], 'environment_id':'fixture-env', 'session_id':'ses_fixture',
                   'prepared_hash':hashlib.sha256(json.dumps(prepared,sort_keys=True).encode()).hexdigest()}
        self.bindings[delivery_id] = binding
        if self.lost_ack:
            raise OSError('lost acknowledgement after exact allocation')
        return copy.deepcopy(binding)

    def status(self, binding):
        return self.reply

    def receipt(self, binding):
        return copy.deepcopy(self.terminal)


class HandlerBridgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = owner_fixture.EventHostTest(); self.fixture.setUp()
        f = self.fixture
        b = copy.deepcopy(f.b); b['target'] = {'handler_class':'installed-fixture'}
        del b['goal_id'], b['goal_generation']
        f.write('installation.json', {'baseline':b,'profile':f.p,'available_tools':['read','act'],'remaining_budget':b['budget']})
        f.write('owner.json', {'target':b['target'],'owner_generation':b['owner_generation'],'stopped':False,'revoked':False})
        self.backend = LifecycleFixture()
        self.host = EventHost({'version':1,'url':'http://127.0.0.1:47961','binding_root':str(f.root)},
                              request=f.request, sandbox=self.backend)
        self.prepared = self.host.prepare({'action':'handler','baseline':b,'profile':f.p}, {'result':'fixture'}, 'msg_handler')
        now = time.time()
        self.lifecycle = {'delivery_id':'evd_handler','generation':'runtime-gen','deadline':now+300,'lease_until':now+60}

    def tearDown(self):
        self.fixture.tearDown()

    def test_frozen_class_dispatch_and_restart_lookup_never_relaunch(self):
        original = copy.deepcopy(self.prepared)
        receipt = self.host.dispatch(self.prepared,'msg_handler',lifecycle=self.lifecycle)
        self.assertEqual(receipt.state,'admitted')
        self.assertEqual(self.prepared,original)
        self.assertEqual(self.host.reconcile(self.prepared,'msg_handler'),'admitted')
        self.host.dispatch(self.prepared,'msg_handler',lifecycle=self.lifecycle)
        self.assertEqual(self.backend.launches,1)
        self.assertEqual(self.prepared['target'],{'handler_class':'installed-fixture'})
        self.assertFalse(any(path.endswith('/prompt') for _,path,_ in self.fixture.calls))

    def test_lost_launch_ack_uses_readonly_lookup(self):
        self.backend.lost_ack = True
        with self.assertRaises(OSError):
            self.host.dispatch(self.prepared,'msg_handler',lifecycle=self.lifecycle)
        self.assertEqual(self.host.reconcile(self.prepared,'msg_handler'),'admitted')
        self.assertEqual(self.backend.launches,1)

    def test_finished_requires_exact_native_terminal_consumption_and_quiet(self):
        self.host.dispatch(self.prepared,'msg_handler',lifecycle=self.lifecycle)
        self.backend.reply = 'handler_finished'
        binding = self.backend.lookup('evd_handler')
        receipt = {**binding,'phase':'revoked','consumed':True,'trusted_completion':True,'output_manifest':[],
                   'native_terminal':{'session_id':'ses_fixture','admission_id':'msg_handler',
                    'body_sha256':hashlib.sha256(self.prepared['briefing'].encode()).hexdigest(),
                    'terminal_id':'msg_terminal','outcome':'succeeded','not_active':True,'empty_inbox':True}}
        self.backend.terminal = receipt
        self.assertEqual(self.host.reconcile(self.prepared,'msg_handler'),'unavailable')
        receipt['phase'] = 'pruned'
        self.assertEqual(self.host.reconcile(self.prepared,'msg_handler'),'handler_finished')
        for key,value in [('admission_id','msg_other'),('body_sha256','wrong'),('not_active',False)]:
            bad = copy.deepcopy(receipt); bad['native_terminal'][key] = value
            self.backend.terminal = bad
            self.assertEqual(self.host.reconcile(self.prepared,'msg_handler'),'unavailable')
