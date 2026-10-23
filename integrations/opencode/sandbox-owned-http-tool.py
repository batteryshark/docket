#!/usr/bin/env python3
"""Finite OWNED test workload for normal Tool Stack HttpRuntime; no model calls.

The channel credential exists only in the explicit private ToolState file.
There is one fixed output resource and one durably reserved effect, never a
generic file/exec endpoint. Register in Fleet before starting; preparation alone
leaves deadline0, which deliberately refuses to start.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import hashlib
import hmac
import json
import os
from pathlib import Path
import resource
import signal
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from docket.sandbox_native import private_json,save

path=Path(sys.argv[1]).resolve(strict=True);config=private_json(path)
required={'version','port','deadline','tool_id','toolyard_state_file','output','audit_root','text_nonce'}
if set(config)!=required or config['version']!=1 or config['tool_id']!='owned':
    raise ValueError('fixed owned HTTP fixture configuration required')
remaining=config['deadline']-time.time()
if not 1<=remaining<=900 or type(config['port']) is not int or not 1024<=config['port']<=65535:
    raise ValueError('explicit finite workload deadline <=15 minutes required')
root=path.parent.parent.resolve();output=Path(config['output']);audit=Path(config['audit_root'])
if (output.parent.resolve()!=root/'outputs' or audit.resolve()!=root/'workload'
    or Path(config['toolyard_state_file']).resolve()!=path.parent/'toolyard-state.json'):
    raise ValueError('owned fixture paths do not match the installed case')
resource.setrlimit(resource.RLIMIT_CPU,(30,30))
resource.setrlimit(resource.RLIMIT_NOFILE,(64,64))
resource.setrlimit(resource.RLIMIT_FSIZE,(1024*1024,1024*1024))
os.umask(0o077);audit.mkdir(mode=0o700,parents=True,exist_ok=True)
ledger=audit/'effect.json'

def record(event,**data):
    with (audit/'audit.jsonl').open('a') as f:
        f.write(json.dumps({'at':time.time(),'event':event,**data})+'\n');f.flush();os.fsync(f.fileno())

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def respond(self,code,data):
        raw=json.dumps(data).encode();self.send_response(code);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if self.path!='/health': return self.respond(404,{'error':'not_found'})
        return self.respond(200,{'status':'ok','deadline':config['deadline'],'effect_reserved':ledger.exists()})
    def do_POST(self):
        if self.path!='/v1/actions/write': return self.respond(404,{'error':'not_found'})
        state=private_json(config['toolyard_state_file'])
        expected=state['owned']['e_secret']
        if not hmac.compare_digest(self.headers.get('X-Toolstack-Secret',''),expected):
            return self.respond(401,{'error':'owned_channel_required'})
        length=int(self.headers.get('Content-Length','0'))
        if not 0<length<=8192: return self.respond(413,{'error':'bounded_body_required'})
        try:
            body=json.loads(self.rfile.read(length));args=body['arguments'];request=body['broker_request_id'];caller=body['caller']['name']
            if (set(args)!={'path','text'} or args['path']!=str(output) or args['text']!=config['text_nonce']
                or not isinstance(args['text'],str) or len(args['text'].encode())>64 or type(request) is not int):
                raise ValueError('outside installed owned resource or data contract')
            text_hash=hashlib.sha256(args['text'].encode()).hexdigest()
            if ledger.exists():
                old=private_json(ledger)
                if old.get('phase')=='completed' and all(old.get(k)==v for k,v in
                    {'broker_request_id':request,'caller':caller,'text_sha256':text_hash}.items()):
                    return self.respond(200,old['result'])
                return self.respond(409,{'error':'owned_effect_already_reserved_no_new_id_retry'})
            intent={'phase':'intent','broker_request_id':request,'caller':caller,'text_sha256':text_hash}
            save(ledger,intent);record('effect_intent',broker_request_id=request)
            fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as f: f.write(args['text']);f.flush();os.fsync(f.fileno())
            result={'written':True,'output_sha256':text_hash,'broker_request_id':request}
            save(ledger,{**intent,'phase':'completed','result':result});record('effect_completed',broker_request_id=request)
            return self.respond(200,result)
        except (ValueError,KeyError,TypeError,OSError):
            return self.respond(403,{'error':'owned_fixture_scope_or_effect_refused'})

def stop(signum,frame): raise KeyboardInterrupt()
signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGALRM,stop);signal.alarm(max(1,int(remaining)))
save(audit/'ownership.json',{'pid':os.getpid(),'deadline':config['deadline'],'port':config['port'],
    'argv':[sys.executable,str(Path(__file__).resolve()),str(path)]})
record('started')
try:
    with HTTPServer(('127.0.0.1',config['port']),Handler) as server:server.serve_forever()
except KeyboardInterrupt: pass
finally:record('stopped')
