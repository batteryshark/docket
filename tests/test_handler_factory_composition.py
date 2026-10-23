"""Actual fixed factory/Sandbox/ScopedBroker; fake process seam, NO native/model.

Requires explicit unmodified Tool Stack source. Does not start Fleet or sockets.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from docket.event_host import configured_event_adapter
from docket.sandbox_native import NativeRuntime, save, INTEGRATION
from docket.wake_profiles import bind_handler_instance
from tests.test_wake_profiles import fixture


@unittest.skipUnless(os.environ.get('TOOLSTACK_SOURCE'), 'explicit installed Tool Stack required')
class FactoryCompositionTests(unittest.TestCase):
    def test_real_factory_scoped_grant_frozen_identity_and_revoke_first_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            lifecycle_root = root/'lifecycle'; lifecycle_root.mkdir(mode=0o700)
            binding_root = root/'bindings'; binding_root.mkdir(mode=0o700)
            b,p,budget = fixture()
            b['target']={'handler_class':'owned-fixture'}
            b['role']=p['role']='docket-test-handler'
            del b['goal_id'],b['goal_generation']
            b['models']=['xai/grok-build-0.1']; p['model']=b['models'][0]
            b['policy']=p['policy']={'docket_fixture':'allow'}
            p['required_tools']=['docket_fixture']
            installation={'baseline':b,'profile':p,'available_tools':['docket_fixture'],'remaining_budget':budget}
            save(binding_root/'installation.json',installation)
            save(binding_root/'owner.json',{'target':b['target'],'owner_generation':b['owner_generation'],
                                          'stopped':False,'revoked':False})
            config={'version':1,'backend':'seatbelt-opencode','state_root':str(lifecycle_root),
                    'toolstack_root':os.environ['TOOLSTACK_SOURCE'],'broker_db':str(lifecycle_root/'broker.db'),
                    'handler_port':47971,'gateway_port':47972,'events_plugin':str(INTEGRATION),
                    'installation':installation,'timeout':30,'approval':{'mode':'refuse'}}
            save(root/'sandbox-config.json',config)
            host=configured_event_adapter({'version':1,'url':'http://127.0.0.1:47961',
                                           'binding_root':str(binding_root),'sandbox_config':str(root/'sandbox-config.json')})
            prepared=host.prepare({'action':'handler','baseline':b,'profile':p},{'result':'fixture'},'msg_factorycomposition')
            frozen=json.dumps(prepared,sort_keys=True)
            calls=[]
            def allocate(runtime,workspace,generation,*,allocation_id,prepared,deadline):
                calls.append('allocate')
                instance=bind_handler_instance(prepared,b,session_id='ses_'+allocation_id,directory=str(workspace),
                    environment_id=allocation_id,generation=generation,goal_id='fixture-goal',goal_generation='created')
                self.assertEqual(instance['handler_class'],'owned-fixture')
                self.assertLessEqual(deadline,time.time()+30)
                return {'session_id':instance['target']['session_id'],'runtime_ref':{'allocation_id':allocation_id}}
            def launch(runtime,binding,prepared,bearer):
                calls.append('launch')
                self.assertGreaterEqual(len(bearer),32)  # Never retain or print capability.
                self.assertEqual(json.dumps(prepared,sort_keys=True),frozen)
            def status(runtime,binding):
                calls.append('status')
                output=lifecycle_root/'retained-output.json'; save(output,{'fixture':'no model was run'})
                return {'state':'consumed','consumed':True,'finished':True,'trusted_completion':True,
                    'native_terminal':{'session_id':binding['session_id'],'admission_id':prepared['admission_id'],
                        'body_sha256':hashlib.sha256(prepared['briefing'].encode()).hexdigest(),
                        'terminal_id':'msg_fixtureterminal','outcome':'succeeded','not_active':True,'empty_inbox':True},
                    'output_manifest':[{'path':str(output),'kind':'fixture'}]}
            def stop(runtime,binding):
                calls.append('stop')
                self.assertEqual(host.sandbox.lookup(binding['delivery_id'])['phase'],'revoked')
                return True
            try:
                with patch.object(NativeRuntime,'allocate',allocate),patch.object(NativeRuntime,'launch',launch), \
                     patch.object(NativeRuntime,'status',status),patch.object(NativeRuntime,'stop',stop):
                    now=time.time()
                    receipt=host.dispatch(prepared,prepared['admission_id'],lifecycle={'delivery_id':'evd_composition',
                        'generation':'fixture-runtime','deadline':now+30,'lease_until':now+29})
                    self.assertEqual(receipt.state,'admitted')
                    self.assertEqual(host.reconcile(prepared,prepared['admission_id']),'handler_finished')
                    binding=host.sandbox.lookup('evd_composition')
                    self.assertEqual(binding['phase'],'pruned')
                    self.assertFalse(Path(binding['workspace']).exists())
                    self.assertTrue((lifecycle_root/'retained-output.json').exists())
                    self.assertEqual(calls,['allocate','launch','status','stop'])
                    self.assertEqual(json.dumps(prepared,sort_keys=True),frozen)
                    self.assertNotIn('grant',host.sandbox.receipt(binding))
            finally:
                host.sandbox.broker.ctx.store.close()
