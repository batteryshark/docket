"""One real local HTTP workload + actual NodSurface contract (NOT principal Nod)."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import sys

from docket.sandbox_native import save
from docket.toolstack_installed import InstalledBroker

SOURCE=os.environ.get('TOOLSTACK_SOURCE')


@unittest.skipUnless(SOURCE,'explicit unmodified Tool Stack source required')
class InstalledHTTPTest(unittest.TestCase):
    def test_default_rest_substitution_requires_fixed_body_and_blocks_event_templates(self):
        # No socket or real secret: reproduce only the actual request builder's
        # default-enabled interpretation, then refuse it before that builder.
        sys.path.insert(0,SOURCE)
        from broker.registry import Registry
        from toolstack_forwarder.config import load_config
        from toolstack_forwarder.request_builder import build_request
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();tool=root/'restowned';tool.mkdir()
            toml=tool/'toolyard.toml'
            toml.write_text('''id="restowned"
type="rest"
base_url="https://unused.invalid"
[entrypoint]
port=47965
[[secrets]]
name="OWN_DUMMY"
field="fixture.dummy"
[[operations]]
name="submit"
description="Submit bounded text"
verb="POST"
path="/fixture"
body_kind="text"
[[operations.args]]
name="body"
type="string"
required=true
''')
            forwarder=load_config(toml)
            self.assertTrue(forwarder.operations['submit'].body_substitution)
            class Dummy:
                calls=[]
                def get(self,name):
                    self.calls.append(name);return 'NOT-A-REAL-SECRET'
            dummy=Dummy();attack='{{secret:OWN_DUMMY}}'
            prior=build_request(forwarder,'submit',{'body':attack},dummy,4096)
            self.assertEqual(prior.body,b'NOT-A-REAL-SECRET');self.assertEqual(dummy.calls,['OWN_DUMMY'])
            dummy.calls=[]
            adapter=InstalledBroker.__new__(InstalledBroker)
            adapter.config={'policy':{'tools':{'restowned':{'submit':'allow'}}},'fixed_args':{},
                            'data_fields':{'restowned.submit':['body']}}
            registry=Registry.from_sources(str(root),())
            with self.assertRaises(ValueError): adapter.describe(registry)
            with self.assertRaises(ValueError): InstalledBroker.validate_value({'type':'string'},attack)
            with self.assertRaises(ValueError): InstalledBroker.reject_event_templates({'data':[{'nested':attack}]})
            self.assertEqual(dummy.calls,[])
            adapter.config['fixed_args']={'restowned.submit':{'body':'installed literal'}}
            adapter.config['data_fields']={'restowned.submit':[]}
            described=adapter.describe(registry)
            self.assertEqual(described[0]['schema']['properties'],{})
            fixed=build_request(forwarder,'submit',described[0]['fixed_args'],dummy,4096)
            self.assertEqual(fixed.body,b'installed literal');self.assertEqual(dummy.calls,[])

    def test_owned_resource_override_review_revocation_and_frozen_descriptor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();owned=root/'owned-output';effects=[];cards=[]
            decision={'status':'pending','decision':None}
            class Handler(BaseHTTPRequestHandler):
                def respond(self,data):
                    raw=json.dumps(data).encode();self.send_response(200)
                    self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                def do_POST(self):
                    raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
                    data=json.loads(raw) if raw else {}
                    if self.path=='/api/v1/requests':
                        if self.headers.get('Authorization')!='Bearer OWN-LOCAL-TEST-ISSUER':
                            raise ValueError('wrong local issuer')
                        cards.append(data);return self.respond({'request_id':'local-nod-contract-card'})
                    if self.path=='/api/v1/requests/local-nod-contract-card/cancel':
                        return self.respond({})
                    if self.headers.get('X-Toolstack-Secret')!='OWN-LOCAL-TOOL-CHANNEL':
                        raise ValueError('wrong owned workload channel')
                    if Path(data['arguments']['path'])!=owned:
                        raise ValueError('not the fixed owned fixture')
                    owned.write_text(data['arguments']['text']);effects.append(data['broker_request_id'])
                    self.respond({'written':True})
                def do_GET(self):
                    self.respond(decision)
                def log_message(self,*args): pass
            with HTTPServer(('127.0.0.1',0),Handler) as server:
                thread=threading.Thread(target=server.serve_forever);thread.start()
                installed=None
                try:
                    tools=root/'registry/owned';tools.mkdir(parents=True)
                    operations=''
                    for op in ('write','review'):
                        operations+='''\n[[operations]]
name = "%s"
description = "Write bounded text to the installed file"
risk = "write"
[[operations.args]]
name = "path"
type = "string"
required = true
[[operations.args]]
name = "text"
type = "string"
required = true
maxLength = 32
'''%op
                    toml=tools/'toolyard.toml'
                    toml.write_text('id="owned"\ntype="api"\n[entrypoint]\nport=%d\n'%server.server_port+operations)
                    private=root/'private';private.mkdir(mode=0o700)
                    state=private/'tool-state.json';save(state,{'owned':{'e_secret':'OWN-LOCAL-TOOL-CHANNEL'}})
                    issuer=private/'nod-issuer';issuer.write_text('OWN-LOCAL-TEST-ISSUER');issuer.chmod(0o600)
                    config={'version':2,'mode':'installed','tools_root':str(root/'registry'), 'tool_dirs':[],
                        'toolyard_state_file':str(state),'policy':{'tools':{'owned':{'write':'allow','review':'review'}}},
                        'fixed_args':{'owned.write':{'path':str(owned)},'owned.review':{'path':str(owned)}},
                        'data_fields':{'owned.write':['text'],'owned.review':['text']},
                        'tool_timeout_seconds':2,'rate_limit':30}
                    ref=private/'broker.json';save(ref,config)
                    installed=InstalledBroker(ref,toolstack_root=SOURCE,db_path=str(private/'broker.sqlite3'),
                        approval={'mode':'nod','url':'http://127.0.0.1:'+str(server.server_port),
                                  'issuer_token_file':str(issuer),'channel':'local-contract-test-not-principal'})
                    baseline={'policy':{'toolstack_owned__write':'allow','toolstack_owned__review':'allow'}}
                    definitions,policy,_=installed.native(baseline)
                    self.assertEqual(policy['tools']['owned']['review'],'review')
                    self.assertNotIn('path',definitions[0]['schema']['properties'])
                    grant,raw=installed.scoped.mint(baseline=policy,profile=policy,session_id='ses_owned',
                        admission_id='msg_owned',delivery_id='delivery-owned',generation='generation-owned',deadline=time.time()+60)
                    binding={k:getattr(grant,k) for k in ('session_id','admission_id','delivery_id','generation')}
                    headers={'Authorization':'Bearer '+raw}
                    def call(path,arguments,call_id,method='POST'):
                        return installed.handle(method,path,headers,{'arguments':arguments},binding=binding,call_id=call_id)
                    allowed=call('/v1/actions/owned.write',{'text':'owned fixture'},'write-1')
                    self.assertEqual(allowed.status,200);self.assertEqual(owned.read_text(),'owned fixture')
                    denied=call('/v1/actions/owned.write',{'path':str(root/'forbidden'),'text':'bad'},'override-1')
                    self.assertEqual(denied.status,403);self.assertFalse((root/'forbidden').exists())
                    self.assertTrue(installed.ctx.store.audit_events(request_id=denied.body['request_id']))
                    template=call('/v1/actions/owned.review',{'text':'{{secret:OWN_DUMMY}}'},'template-1')
                    self.assertEqual(template.status,403);self.assertEqual(cards,[])
                    pending=call('/v1/actions/owned.review',{'text':'review fixture'},'review-1')
                    self.assertEqual(pending.status,202);self.assertEqual(len(effects),1)
                    self.assertEqual(cards[0]['dedupe_key'],str(pending.body['request_id']))
                    path='/v1/requests/'+str(pending.body['request_id'])
                    self.assertEqual(call(path,{},'pending-1','GET').body['status'],'pending_approval')
                    self.assertEqual(len(effects),1)
                    installed.scoped.revoke(grant)
                    self.assertEqual(call('/v1/actions/owned.write',{'text':'after revoke'},'revoked-1').status,401)
                    self.assertEqual(len(effects),1)
                    toml.write_text(toml.read_text()+'\n# explicit redeployment required\n')
                    self.assertEqual(call('/v1/actions/owned.write',{'text':'changed'},'changed-1').status,409)
                    config['fixed_args']={};save(ref,config)
                    with self.assertRaises(ValueError):
                        InstalledBroker(ref,toolstack_root=SOURCE,db_path=str(private/'other.sqlite3'),approval={'mode':'refuse'})
                finally:
                    if installed: installed.ctx.store.close()
                    server.shutdown();thread.join(timeout=3)
                    self.assertFalse(thread.is_alive())
