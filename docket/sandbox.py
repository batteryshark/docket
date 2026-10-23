"""Fail-closed macOS Seatbelt backend for short-lived event handlers.

This is an OS boundary, not an OpenCode permission profile. Trusted callers must
copy approved inputs into the private workspace; host directories are never
implicitly mounted. Network grants are literal loopback TCP endpoints only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import resource
import signal
import subprocess
import hashlib
import time
import uuid
import fcntl
import tempfile
from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    wall_seconds: int = 300
    cpu_seconds: int = 60
    file_bytes: int = 16 * 1024 * 1024
    open_files: int = 128
    processes: int = 32

    def validate(self):
        if not (1 <= self.wall_seconds <= 3600 and 1 <= self.cpu_seconds <= 600
                and 1024 <= self.file_bytes <= 128 * 1024 * 1024
                and 16 <= self.open_files <= 512 and 1 <= self.processes <= 64):
            raise ValueError("sandbox limits outside installed ceiling")


def seatbelt_profile(workspace: Path, *, read_paths=(), ports=(), listen_port=None) -> str:
    """Generate default-deny policy. No home, DNS, Unix sockets or broad network."""
    workspace = workspace.resolve(strict=True)
    if workspace == Path('/') or workspace == Path.home():
        raise ValueError("private workspace required")
    quoted = lambda p: json.dumps(str(p))
    rules = ['(version 1)', '(deny default)', '(deny process-info*)',
             '(allow process-info* (target self))',
             '(deny sysctl-read (sysctl-name-prefix "kern.proc"))',
             '(import "dyld-support.sb")',
             '(allow process-exec process-fork)',
             '(allow sysctl-read (sysctl-name-prefix "hw.")'
             ' (sysctl-name-prefix "kern.os") (sysctl-name "kern.version" "kern.boottime" "kern.argmax"))',
             '(allow mach-lookup (global-name "com.apple.system.notification_center") (global-name "com.apple.logd"))',
             '(allow file-read-metadata)',
             '(allow file-read* (subpath "/private/var/db/timezone") (literal "/private/etc/localtime"))',
             '(allow file-read* file-map-executable (subpath "/System") (subpath "/usr/lib")'
             ' (subpath "/usr/share") (subpath "/usr/bin") (subpath "/bin")'
             ' (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))',
             '(allow file-write* (literal "/dev/null"))',
             f'(allow file-read* file-write* file-map-executable (subpath {quoted(workspace)}))']
    for path in read_paths:
        path = Path(path)
        path = path.resolve(strict=True) if path.exists() else path.parent.resolve(strict=True) / path.name
        if path.is_dir() and not str(path).startswith('/Library/Developer/CommandLineTools/'):
            raise ValueError("only exact approved runtime files may be exposed")
        kind = 'subpath' if path.is_dir() else 'literal'
        rules.append(f'(allow file-read* file-test-existence file-map-executable ({kind} {quoted(path)}))')
        # Module loaders may list the exact approved package directory. This is
        # a literal directory grant, NOT permission to read arbitrary siblings.
        rules.append(f'(allow file-read* file-test-existence (literal {quoted(path.parent)}))')
    for port in ports:
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("invalid endpoint")
        rules.append(f'(allow network-outbound (remote tcp "localhost:{port}"))')
    if listen_port is not None:
        if type(listen_port) is not int or not 1024 <= listen_port <= 65535:
            raise ValueError('invalid listener')
        rules.append(f'(allow network-bind network-inbound (local tcp "localhost:{listen_port}"))')
    return '\n'.join(rules) + '\n'


def run(workspace: Path, argv: list[str], *, read_paths=(), ports=(), listen_port=None,
        limits=Limits(), env=None, on_start=None) -> subprocess.CompletedProcess:
    """Bounded foreground runner. A service launcher must own its own deadline.

    No inherited credentials/environment. On timeout terminate the exact owned
    process group, never an arbitrary PID selected from a host process listing.
    """
    if platform.system() != 'Darwin' or not Path('/usr/bin/sandbox-exec').exists():
        raise RuntimeError('verified Seatbelt backend unavailable')
    limits.validate()
    workspace = workspace.resolve(strict=True)
    profile = seatbelt_profile(workspace, read_paths=read_paths, ports=ports, listen_port=listen_port)
    clean = {'HOME': str(workspace), 'TMPDIR': str(workspace),
             'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8'}
    for key, value in (env or {}).items():
        if key not in {'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME',
                       'XDG_CACHE_HOME', 'OPENCODE_CONFIG_CONTENT', 'OPENCODE_DB',
                       'OPENCODE_CONFIG_PROJECT_DISABLE', 'OPENCODE_CONFIG', 'OPENCODE_DISABLE_MODELS_FETCH',
                       'OPENCODE_FILEWATCHER_DISABLE', 'OPENCODE_DISABLE_FFF',
                       'OPENCODE_SERVER_PASSWORD'}:
            raise ValueError('environment key not explicitly approved')
        clean[key] = value

    def bound():
        for kind, ceiling in ((resource.RLIMIT_CPU, limits.cpu_seconds),
                              (resource.RLIMIT_FSIZE, limits.file_bytes),
                              (resource.RLIMIT_NOFILE, limits.open_files),
                              (resource.RLIMIT_NPROC, limits.processes)):
            _, hard = resource.getrlimit(kind)
            ceiling = min(ceiling, hard) if hard >= 0 else ceiling
            resource.setrlimit(kind, (ceiling, ceiling))

    # Regular output files (not pipes) make RLIMIT_FSIZE a real output ceiling,
    # avoiding an unbounded communicate() buffer under a noisy handler.
    with tempfile.TemporaryFile(mode='w+t') as stdout, tempfile.TemporaryFile(mode='w+t') as stderr, subprocess.Popen(['/usr/bin/sandbox-exec', '-p', profile, *argv],
                          cwd=workspace, env=clean, start_new_session=True,
                          stdout=stdout, stderr=stderr,
                          text=True, preexec_fn=bound) as proc:
        try:
            if on_start:
                on_start(proc.pid)
            proc.wait(timeout=limits.wall_seconds)
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            raise
        # Also reap descendants after a normal parent exit, not just timeouts.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout.seek(0); stderr.seek(0)
        out, err = stdout.read(limits.file_bytes), stderr.read(limits.file_bytes)
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)


class AllocationNoEffects(RuntimeError):
    """Installed backend proved allocation failed BEFORE creating any compute."""


class Sandbox:
    """Durable handler lifecycle, driven by the existing Docket reconciliation.

    Runtime is a trusted installed backend, NEVER selected by event data. It must
    implement allocate(workspace, generation, *, allocation_id), launch(binding, prepared, bearer),
    status(binding), stop(binding). allocate may create a session but MUST NOT run
    a model. launch transports the scoped bearer as a private capability, not text.
    No default backend is silently substituted when isolation is unavailable.
    """
    def __init__(self, root, *, runtime, broker, baseline, timeout=300):
        if not 1 <= timeout <= 3600:
            raise ValueError('invalid installed timeout')
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_mode & 0o077:
            raise ValueError('lifecycle root must be private')
        self.runtime, self.broker, self.baseline, self.timeout = runtime, broker, baseline, timeout

    def _path(self, delivery_id):
        if not isinstance(delivery_id, str) or not delivery_id:
            raise ValueError('delivery identity required')
        return self.root / (hashlib.sha256(delivery_id.encode()).hexdigest() + '.json')

    def lookup(self, delivery_id):
        """Read saved ownership only; never allocate/relaunch during recovery."""
        path = self._path(delivery_id)
        if not path.exists():
            return None
        if path.is_symlink():
            raise ValueError('untrusted lifecycle path')
        return json.loads(path.read_text())

    def _save(self, path, state):
        tmp = path.with_suffix('.new')
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(state, f, sort_keys=True)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def launch(self, prepared, delivery_id, *, lifecycle=None):
        from dataclasses import asdict
        lifecycle = lifecycle or {}
        path = self._path(delivery_id)
        lock = os.open(self.root / '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            digest = hashlib.sha256(json.dumps(prepared, sort_keys=True).encode()).hexdigest()
            if path.exists():
                old = json.loads(path.read_text())
                if old['prepared_hash'] != digest or old['admission_id'] != prepared['admission_id']:
                    raise ValueError('delivery binding conflict')
                # Intent/uncertain is returned for reconciliation, NEVER relaunched.
                return old
            now = time.time()
            deadline = min(lifecycle.get('deadline', now+self.timeout), now+self.timeout)
            if deadline <= now or lifecycle.get('delivery_id', delivery_id) != delivery_id:
                raise ValueError('expired or mismatched lifecycle')
            generation = lifecycle.get('generation') or uuid.uuid4().hex
            if not isinstance(generation, str) or not generation:
                raise ValueError('trusted generation required')
            env_id = uuid.uuid4().hex
            workspace = self.root / ('environment-' + env_id)
            state = dict(delivery_id=delivery_id, admission_id=prepared['admission_id'],
                         prepared_hash=digest, environment_id=env_id, generation=generation,
                         lease_until=min(lifecycle.get('lease_until', deadline), deadline),
                         deadline=deadline, workspace=str(workspace), phase='allocating',
                         briefing_sha256=hashlib.sha256(prepared.get('briefing','').encode()).hexdigest(),
                         output_manifest=[], consumed=False, trusted_completion=False,
                         runtime_ref={'allocation_id':env_id, 'receipt':str(workspace / '.runtime-receipt.json')})
            self._save(path, state)
            workspace.mkdir(mode=0o700)
            (workspace / '.docket-owner').write_text(env_id)
            # The durable allocation ID/receipt path precede ANY compute. Backend
            # stop/recover MUST accept this identity even if allocate never returns.
            try:
                allocated = self.runtime.allocate(workspace, generation, allocation_id=env_id,
                                                  prepared=prepared, deadline=deadline)
            except AllocationNoEffects:
                state.update(phase='allocation_failed', known_no_effects=True,
                             failure='allocation_failed_before_effects')
                self._save(path, state)
                self.prune(state)
                raise
            except BaseException:
                state.update(phase='allocation_uncertain', failure='allocation_outcome_unknown')
                self._save(path, state)
                raise
            # Only known identity fields are accepted from an installed backend.
            state.update(session_id=allocated['session_id'], runtime_ref=allocated['runtime_ref'])
            broker_profile = {'tools': {}}
            for native_tool, effect in prepared['policy'].items():
                if effect == 'deny':
                    continue
                for spec in self.baseline.get('tool_operations', {}).get(native_tool, []):
                    tool, operation = spec.split('.', 1)
                    broker_profile['tools'].setdefault(tool, {})[operation] = effect
            grant, raw = self.broker.mint(baseline=self.baseline['broker_policy'],
                profile=broker_profile, session_id=state['session_id'],
                admission_id=state['admission_id'], delivery_id=delivery_id,
                generation=generation, deadline=deadline,
                prepared=prepared if all(k in prepared for k in
                    ('owner_generation','profile_id','profile_version','baseline_version','briefing')) else None)
            state.update(grant=asdict(grant), token_ref=grant.token_ref, phase='launch_intent')
            self._save(path, state)
            try:
                self.runtime.launch(state, prepared, raw)
            except BaseException:
                # Could have admitted the exact request. Keep identity and fence token.
                self.broker.revoke(grant)
                state['phase'] = 'uncertain'
                self._save(path, state)
                raise
            finally:
                raw = None
            state['phase'] = 'admitted'
            self._save(path, state)
            return state
        finally:
            os.close(lock)

    def status(self, binding):
        path = self._path(binding['delivery_id'])
        saved = json.loads(path.read_text())
        if any(saved.get(k) != binding.get(k) for k in ('environment_id', 'generation', 'admission_id')):
            raise ValueError('stale lifecycle binding')
        if saved['phase'] == 'pruned':
            return 'handler_finished' if self._finished(saved) else 'missing'
        # Expiry is independent of host availability. Revoke before any read
        # that can fail, then recover/stop exact owned allocation (or retain it).
        if time.time() >= saved['lease_until']:
            self.prune(saved)
            return 'unavailable' if json.loads(path.read_text())['phase'] != 'pruned' else 'missing'
        try:
            observed = self.runtime.status(saved)
        except Exception as exc:
            saved['status_error'] = type(exc).__name__
            self._save(path, saved)
            return 'unavailable'
        if observed.get('consumed'):
            saved['consumed'] = True
        saved['trusted_completion'] = False
        terminal = observed.get('native_terminal', {})
        if (observed.get('finished') and observed.get('trusted_completion')
            and terminal.get('admission_id') == saved['admission_id']
            and terminal.get('session_id') == saved['session_id']
            and terminal.get('body_sha256') == saved['briefing_sha256']
            and terminal.get('terminal_id') and terminal.get('not_active') is True
            and terminal.get('empty_inbox') is True
            and terminal.get('outcome') in {'succeeded','failed','interrupted'}):
            saved.update(trusted_completion=True, native_terminal=terminal,
                         output_manifest=observed.get('output_manifest', []))
        self._save(path, saved)
        if saved['trusted_completion'] or observed.get('failed') or time.time() >= saved['lease_until']:
            self.prune(saved)
            saved = json.loads(path.read_text())
        if self._finished(saved):
            return 'handler_finished'
        state = observed.get('state', 'unavailable')
        if state not in {'admitted', 'missing', 'unavailable'}:
            state = 'unavailable'
        if state == 'unavailable':
            return state
        return 'consumed' if saved['consumed'] else state

    @staticmethod
    def _finished(saved):
        return (saved['phase'] == 'pruned' and saved['consumed']
                and saved['trusted_completion']
                and saved.get('native_terminal', {}).get('not_active') is True
                and saved.get('native_terminal', {}).get('empty_inbox') is True)

    def receipt(self, binding):
        """Detached redacted lifecycle evidence, never raw grant/credential data."""
        saved = json.loads(self._path(binding['delivery_id']).read_text())
        if any(saved.get(k) != binding.get(k) for k in ('environment_id','generation','admission_id')):
            raise ValueError('stale receipt binding')
        keys = ('environment_id','session_id','delivery_id','admission_id','generation',
                'phase','deadline','lease_until','consumed','trusted_completion',
                'native_terminal','output_manifest','failure','status_error')
        return json.loads(json.dumps({k:saved[k] for k in keys if k in saved}))

    def prune(self, binding):
        """Idempotent revoke-first cleanup; unresolved effects remain in broker DB.

        Backend stop must verify its recorded parent identity, stop parent BEFORE
        children, and prove quiet. Workspace is retained if any ownership is unknown.
        """
        from docket.toolstack import Grant
        import shutil
        path = self._path(binding['delivery_id'])
        saved = json.loads(path.read_text())
        if any(saved.get(k) != binding.get(k) for k in ('environment_id', 'generation', 'admission_id')):
            raise ValueError('stale cleanup binding')
        if saved['phase'] == 'pruned':
            return saved
        if saved.get('grant'):
            self.broker.revoke(Grant(**saved['grant']))
        saved['phase'] = 'revoked'
        self._save(path, saved)
        if not saved.get('known_no_effects') and not self.runtime.stop(saved):
            return saved  # Cannot prove old work quiet: do NOT destroy or replace.
        workspace = Path(saved['workspace'])
        if workspace.exists():
            if workspace.is_symlink() or workspace.parent != self.root or workspace.name != 'environment-' + saved['environment_id']:
                raise ValueError('workspace ownership mismatch')
            marker = workspace / '.docket-owner'
            if marker.is_symlink() or marker.read_text() != saved['environment_id']:
                raise ValueError('workspace ownership mismatch')
            # Output manifest and broker audit saved BEFORE compute removal.
            shutil.rmtree(workspace)
        saved['phase'] = 'pruned'
        self._save(path, saved)
        return saved
