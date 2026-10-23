import concurrent.futures
from contextlib import closing
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from docket.event_journal import EventJournal, PATH, MAX_BODY_BYTES

SOURCE=os.environ.get('TOOLSTACK_SOURCE')


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.db=self.root/'docket.sqlite3'
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('CREATE TABLE items(id TEXT PRIMARY KEY,body TEXT)')
            db.execute("INSERT INTO items VALUES('ordinary','must remain unchanged')")
            db.commit()
        self.token=secrets.token_urlsafe(32)
        self.channel=self.root/'journal-channel.json'
        self.channel.write_text(json.dumps({'version':1,'namespace':'broker-deployment-v1',
            'source':'docket-event-summary-v1','channel_token_sha256':hashlib.sha256(self.token.encode()).hexdigest()}))
        self.channel.chmod(0o600)
        self.journal=EventJournal(self.db,self.channel)

    def tearDown(self):
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT * FROM items').fetchall(),[('ordinary','must remain unchanged')])
        self.tmp.cleanup()

    def envelope(self,request=1,text='bounded event summary',caller=None):
        return {'arguments':{'source':'docket-event-summary-v1','text':text},
                'broker_request_id':request,'caller':{'name':caller or 'docket-'+format(request,'032x')}}

    def call(self,envelope=None,*,raw=None,token=None,method='POST',path=PATH):
        raw=raw if raw is not None else json.dumps(envelope or self.envelope()).encode()
        headers={'Content-Type':'application/json','Content-Length':str(len(raw)),
                 'X-Toolstack-Secret':self.token if token is None else token}
        return self.journal.handle(method,path,headers,raw)

    def test_record_replay_conflict_handler_quota_and_admin_projection(self):
        original=self.envelope();first=self.call(original)
        self.assertEqual(first.status,200)
        self.assertEqual(set(first.body),{'status','row_id','sha256'})
        self.assertNotIn('bounded event summary',json.dumps(first.body))
        self.assertEqual(self.call(original),first)
        self.assertEqual(self.call(self.envelope(text='changed same request')).status,409)
        self.assertEqual(self.call(self.envelope(2,caller=original['caller']['name'])).status,409)
        # A distinct authenticated instance may record another row; this is not
        # the earlier test workload's global one-effect-forever restriction.
        self.assertEqual(self.call(self.envelope(2,'another handler')).status,200)
        rows=self.journal.list_outcomes(limit=1)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['text'],'another handler')
        self.assertEqual(set(rows[0]),{'row_id','source','text','text_sha256','created_at_ms'})
        self.assertEqual(len(self.journal.list_outcomes()),2)
        self.assertEqual(len(self.journal.list_outcomes(before_id=rows[0]['row_id'])),1)
        for limit in (True,0,101):
            with self.assertRaises(ValueError):self.journal.list_outcomes(limit=limit)

    def test_auth_bounds_unknown_fields_and_no_channel_read(self):
        with patch('docket.event_journal.json.loads',side_effect=AssertionError('must not parse')):
            self.assertEqual(self.call(raw=b'not json',token='wrong').status,401)
            self.assertEqual(self.call(raw=b'x'*(MAX_BODY_BYTES+1)).status,413)
        cases=[]
        e=self.envelope();e['arguments']['path']='/not-authorized';cases.append(e)
        e=self.envelope();e['arguments']['source']='principal';cases.append(e)
        e=self.envelope();e['caller']['caller_id']='admin';cases.append(e)
        e=self.envelope();e['caller']['name']='admin';cases.append(e)
        e=self.envelope();e['caller_id']=1;cases.append(e)
        e=self.envelope();e['broker_request_id']=True;cases.append(e)
        e=self.envelope(text='x'*513);cases.append(e)
        e=self.envelope(text='😀'*129);cases.append(e)
        for invalid in cases:
            self.assertEqual(self.call(invalid).status,400)
        self.assertEqual(self.call(raw=b'{"arguments":{},"arguments":{}}').status,400)
        self.assertEqual(self.call(method='GET').status,405)
        self.assertEqual(self.call(method='GET',path='/api/v1/events/outcomes').status,404)
        self.assertEqual(self.call(method='GET',path='/admin',token='').status,404)
        headers={'X-Toolstack-Secret':self.token,'Content-Type':'application/json','Content-Length':'9'*100}
        self.assertEqual(self.journal.preflight('POST',PATH,headers).status,413)
        self.assertEqual(self.journal.list_outcomes(),[])
        self.assertEqual(self.call(self.envelope(text='x'*512)).status,200)

    def test_concurrent_idempotence_and_restart(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.call(self.envelope(12)),range(8)))
        self.assertTrue(all(r==results[0] and r.status==200 for r in results))
        self.assertEqual(len(self.journal.list_outcomes()),1)
        restarted=EventJournal(self.db,self.channel)
        self.journal=restarted
        self.assertEqual(self.call(self.envelope(12)),results[0])
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda n:self.call(self.envelope(n)),range(20,28)))
        self.assertTrue(all(r.status==200 for r in results))
        self.assertEqual(len(self.journal.list_outcomes()),9)

    @unittest.skipUnless(SOURCE,'explicit unmodified Tool Stack source required')
    def test_actual_registry_http_runtime_and_explicit_toolstate(self):
        sys.path.insert(0,SOURCE)
        from broker.registry import Registry
        from broker.runtime import HttpRuntime
        from broker.tool_state import ToolState
        journal=self.journal;seen=[]
        class Handler(BaseHTTPRequestHandler):
            def respond(self,response):
                raw=json.dumps(response.body).encode();self.send_response(response.status)
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_POST(self):
                self.connection.settimeout(2)
                seen.append(self.path)
                rejected=journal.preflight(self.command,self.path,dict(self.headers),self.headers.get('Content-Length'))
                if rejected:
                    self.close_connection=True;return self.respond(rejected)
                self.respond(journal.handle(self.command,self.path,dict(self.headers),self.rfile.read(int(self.headers['Content-Length']))))
            def do_GET(self):
                self.respond(journal.handle(self.command,self.path,dict(self.headers),b''))
            def log_message(self,*args):pass
        with HTTPServer(('127.0.0.1',0),Handler) as server:
            thread=threading.Thread(target=server.serve_forever);thread.start()
            try:
                tool=self.root/'tools/docket';tool.mkdir(parents=True)
                (tool/'toolyard.toml').write_text('''id="docket"
type="api"
[entrypoint]
port=%d
[[operations]]
name="record"
description="Record a bounded Docket event summary"
risk="write"
[[operations.args]]
name="source"
type="string"
required=true
[[operations.args]]
name="text"
type="string"
required=true
maxLength=512
'''%server.server_port)
                state=self.root/'toolyard-state.json';state.write_text(json.dumps({'docket':{'e_secret':self.token}}));state.chmod(0o600)
                runtime=HttpRuntime(timeout=2,tool_secret=ToolState(state).e_secret_for)
                operation=Registry.from_sources(str(self.root/'tools'),()).lookup('docket','record')
                caller='docket-'+'a'*32;args={'source':'docket-event-summary-v1','text':'actual normal HTTP transport'}
                first=runtime.execute(operation,args,101,caller)
                self.assertEqual(first['status'],'recorded')
                self.assertEqual(runtime.execute(operation,args,101,caller),first)
                with self.assertRaisesRegex(RuntimeError,'HTTP 409'):
                    runtime.execute(operation,{**args,'text':'changed'},101,caller)
                with self.assertRaisesRegex(RuntimeError,'HTTP 409'):
                    runtime.execute(operation,args,102,caller)
                with self.assertRaisesRegex(RuntimeError,'HTTP 400'):
                    runtime.execute(operation,{**args,'text':'x'*513},103,'docket-'+'b'*32)
                with self.assertRaisesRegex(RuntimeError,'HTTP 400'):
                    runtime.execute(operation,{**args,'db_path':'untrusted'},103,'docket-'+'b'*32)
                with self.assertRaisesRegex(RuntimeError,'HTTP 401'):
                    HttpRuntime(timeout=2,tool_secret=lambda _: 'wrong').execute(operation,args,104,caller)
                self.assertEqual(set(seen),{PATH})
                self.assertEqual(len(self.journal.list_outcomes()),1)
            finally:
                server.shutdown();thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
