#!/usr/bin/env python3
"""Finite Fleet-owned isolated OpenCode test service. No provider auth copied."""
import json
import os
from pathlib import Path
import signal
import secrets
import sys
import time
import subprocess
import resource

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from docket.sandbox import run, Limits

ROOT = Path(sys.argv[1]).resolve(strict=True)
BINARY = Path('/Applications/OpenChamber.app/Contents/Resources/opencode-cli/opencode')
WORK = ROOT / 'runtime'
WORK.mkdir(mode=0o700, exist_ok=True)
case_path=ROOT/'active-case.json'
deadline=min(time.time()+1800,json.loads(case_path.read_text())['deadline']) if case_path.exists() else time.time()+1800
if not time.time()<deadline<=time.time()+1800:
    raise RuntimeError('test owner case expired or exceeds installed lifetime')
env = {f'XDG_{name.upper()}_HOME': str(WORK / name) for name in ('config', 'data', 'state', 'cache')}
env.update(OPENCODE_CONFIG_PROJECT_DISABLE='1', OPENCODE_DISABLE_MODELS_FETCH='1',
           OPENCODE_FILEWATCHER_DISABLE='1', OPENCODE_DISABLE_FFF='1')
auth_path = ROOT / 'server-auth'
if not auth_path.exists():
    fd = os.open(auth_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as auth:
        auth.write(secrets.token_urlsafe(32))
env['OPENCODE_SERVER_PASSWORD'] = auth_path.read_text()
config_path = ROOT / 'config.json'
if config_path.exists():
    private_config = WORK / 'isolated-opencode.json'
    fd = os.open(private_config, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as config:
        config.write(config_path.read_text())
        config.flush(); os.fsync(config.fileno())
    env['OPENCODE_CONFIG'] = str(private_config)
else:
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps({'$schema': 'https://opencode.ai/config.json',
        'snapshots': False, 'plugins': [], 'permissions': [{'action': '*', 'resource': '*', 'effect': 'deny'}]})

def stop(signum, frame):
    raise KeyboardInterrupt()

def started(pid):
    record = {'wrapper_pid': os.getpid(), 'sandbox_pid': pid, 'started_at': time.time(),
              'deadline': deadline, 'binary': str(BINARY), 'port': 47961,
              'argv': [str(BINARY), 'serve', '--hostname', '127.0.0.1', '--port', '47961']}
    (ROOT / 'ownership.json').write_text(json.dumps(record, indent=2))

signal.signal(signal.SIGTERM, stop)
try:
    read_paths = [BINARY]
    if (ROOT / 'sandbox-read-paths.json').exists():
        read_paths += [Path(p) for p in json.loads((ROOT / 'sandbox-read-paths.json').read_text())]
    argv = [str(BINARY), 'serve', '--hostname', '127.0.0.1', '--port', '47961']
    diagnostic = ROOT / 'loader-ab-unconfined-once'
    if diagnostic.exists():
        # Explicit lead-authorized A/B only; consume marker so a later restart
        # restores the sandbox. Model gateway MUST be stopped for this test.
        diagnostic.rename(ROOT / 'loader-ab-unconfined-used')
        resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16*1024*1024, 16*1024*1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        clean = {'HOME':str(WORK), 'TMPDIR':str(WORK), 'PATH':'/usr/bin:/bin', 'LANG':'en_US.UTF-8', **env}
        (ROOT / 'UNCONFINED-DIAGNOSTIC.json').write_text(json.dumps({'at':time.time(),'models':'FORBIDDEN'}))
        with subprocess.Popen(argv, cwd=WORK, env=clean, start_new_session=True) as p:
            started(p.pid)
            try:
                p.wait(timeout=120)
            finally:
                if p.poll() is None:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()
        result = subprocess.CompletedProcess(argv, p.returncode, '', '')
    else:
        result = run(WORK, argv, read_paths=read_paths, listen_port=47961, ports=[47962],
                     limits=Limits(wall_seconds=max(1,int(deadline-time.time())), cpu_seconds=120), env=env, on_start=started)
    print(result.stdout, flush=True)
    print(result.stderr, file=sys.stderr, flush=True)
    sys.exit(result.returncode)
except KeyboardInterrupt:
    pass
finally:
    (ROOT / 'stopped.json').write_text(json.dumps({'stopped_at': time.time()}))
