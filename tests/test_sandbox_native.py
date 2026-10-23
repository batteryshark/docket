"""Factory/known-no-effect tests. NOT real process/model E2E claims."""
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest

from docket.sandbox import AllocationNoEffects
from docket.sandbox_native import configured_sandbox, save, private_json, INTEGRATION, NativeRuntime
import hashlib
from docket.wake_profiles import prepare

SOURCE = os.environ.get('TOOLSTACK_SOURCE')


class PrivateConfigTests(unittest.TestCase):
    def test_intervening_native_input_cannot_complete_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime=NativeRuntime({'state_root':tmp})
            allocation='a'*32;generation='generation'
            save(runtime.control(allocation)/'allocation.json',{'allocation_id':allocation,
                'generation':generation,'session_id':'ses_actual','workspace':tmp})
            context=[{'id':'msg_exact','type':'user','text':'briefing'},
                     {'id':'msg_other','type':'synthetic','text':'foreign input'},
                     {'id':'msg_idle','type':'idle','outcome':'succeeded'}]
            runtime.api=lambda receipt,method,path: {'data':context if path.endswith('/context') else {} if path.endswith('/active') else []}
            binding={'environment_id':allocation,'generation':generation,'admission_id':'msg_exact',
                     'briefing_sha256':hashlib.sha256(b'briefing').hexdigest()}
            result=runtime.status(binding)
            self.assertEqual(result,{'state':'unavailable','consumed':True})
            self.assertFalse((runtime.control(allocation)/'native-output.json').exists())

    def test_private_config_refuses_permissions_or_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'config.json'
            save(p,{'version':1})
            self.assertEqual(private_json(p),{'version':1})
            p.chmod(0o644)
            with self.assertRaises(ValueError):
                private_json(p)
            link=Path(root)/'symlink';link.symlink_to(p)
            with self.assertRaises(OSError):
                private_json(link)

    def test_no_backend_or_command_selector_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'config.json'
            save(p,{'version':1,'backend':'process','command':'anything'})
            with self.assertRaises(ValueError):
                configured_sandbox(p)

    @unittest.skipUnless(SOURCE,'explicit unmodified Tool Stack installation required')
    def test_real_factory_busy_endpoint_proves_no_effects_and_prunes(self):
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as occupied, socket.socket() as other:
            occupied.bind(('127.0.0.1',0));occupied.listen()
            other.bind(('127.0.0.1',0))
            root=Path(tmp)/'private';root.mkdir(mode=0o700)
            budget={'max_steps':2,'max_output_tokens':256,'total_output_tokens':512}
            baseline={'version':'handler-b1','target':{'handler_class':'owned-fixture'},
                'owner_generation':'installed-v1','role':'docket-test-handler','authority':[],
                'policy':{'docket_fixture':'allow'},'secrets':[], 'models':['xai/grok-build-0.1'],
                'budget':budget,'context_paths':[]}
            profile={'id':'fixture','version':'p1','baseline_version':'handler-b1',
                'role':'docket-test-handler','authority':[],'policy':{'docket_fixture':'allow'},
                'secrets':[],'model':'xai/grok-build-0.1','budget':budget,'context_paths':[],
                'template':'Assess the owned fixture.','fields':['result'],'required_tools':['docket_fixture']}
            installation={'baseline':baseline,'profile':profile,'available_tools':['docket_fixture'],
                          'remaining_budget':budget}
            config={'version':1,'backend':'seatbelt-opencode','state_root':str(root),
                'toolstack_root':SOURCE,'broker_db':str(root/'broker.sqlite3'),
                'handler_port':occupied.getsockname()[1],'gateway_port':other.getsockname()[1],
                'events_plugin':str(INTEGRATION),'installation':installation,'timeout':30,
                'approval':{'mode':'refuse'}}
            path=Path(tmp)/'config.json';save(path,config)
            manager=configured_sandbox(path)
            prepared=prepare(baseline,profile,{'result':'fixture'},available_tools=['docket_fixture'],
                remaining_budget=budget,action='handler')
            prepared['admission_id']='msg_factory_busy'
            with self.assertRaises(AllocationNoEffects):
                manager.launch(prepared,'delivery-factory-busy')
            binding=manager.lookup('delivery-factory-busy')
            self.assertEqual(binding['phase'],'pruned')
            self.assertFalse(Path(binding['workspace']).exists())
            self.assertIsNone(manager.lookup('missing-delivery'))
