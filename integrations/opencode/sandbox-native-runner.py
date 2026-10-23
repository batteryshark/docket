#!/usr/bin/env python3
"""Finite Fleet-owned native child runner. No live host discovery/autostart."""
import json
import os
from pathlib import Path
import signal
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from docket.sandbox import run, Limits
from docket.sandbox_native import private_json, save, BINARY

root = Path(sys.argv[1]).resolve(strict=True)
allocation = private_json(root/'allocation.json')
workspace = Path(allocation['workspace']).resolve(strict=True)
config = private_json(root/'native-config.json')
installation = private_json(root/'installation.json')
read_paths = [BINARY,root/'installation.json',root/'owner.json',root/'instance.json',
              root/(allocation['session_id']+'.json')]
if (root/'native-tools.json').exists():
    read_paths.append(root/'native-tools.json')
for plugin in config['plugins']:
    package = Path(plugin['package'])
    for name in ('package.json','index.js','index.mjs','docket-events.mjs'):
        if (package/name).is_file():
            read_paths.append(package/name)
env = {f'XDG_{name.upper()}_HOME':str(workspace/name) for name in ('config','data','state','cache')}
env.update(OPENCODE_CONFIG_PROJECT_DISABLE='1',OPENCODE_DISABLE_MODELS_FETCH='1',
           OPENCODE_FILEWATCHER_DISABLE='1',OPENCODE_DISABLE_FFF='1',
           OPENCODE_CONFIG_CONTENT=json.dumps(config),
           OPENCODE_SERVER_PASSWORD=private_json(root/'server-auth.json')['password'])

def stop(signum, frame):
    raise KeyboardInterrupt()

def started(pid):
    save(root/'process.json',{'pid':os.getpid(),'sandbox_pid':pid,'deadline':allocation['deadline'],
        'argv':[str(BINARY),'serve','--hostname','127.0.0.1','--port',str(allocation['handler_port'])]})

signal.signal(signal.SIGTERM,stop)
try:
    seconds = int(allocation['deadline']-time.time())
    if not 1 <= seconds <= 3600:
        raise RuntimeError('handler lease expired or exceeds installed ceiling')
    result = run(workspace,[str(BINARY),'serve','--hostname','127.0.0.1','--port',str(allocation['handler_port'])],
        read_paths=read_paths,ports=[allocation['gateway_port']],listen_port=allocation['handler_port'],
        limits=Limits(wall_seconds=seconds,cpu_seconds=60),env=env,on_start=started)
    save(root/'process-exit.json',{'returncode':result.returncode,'at':time.time()})
except KeyboardInterrupt:
    save(root/'process-exit.json',{'interrupted':True,'at':time.time()})
