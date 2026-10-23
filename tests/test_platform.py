"""Foundation tests. Native Windows proof is still required; no host discovery."""

import ctypes
import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from docket import platform
from docket.api import DocketServer
from docket.bounds import _absolute_path
from docket.checks import Checks, read_process_start, stat_file
from docket.client import ClientError, request
from docket.clock import FakeClock
from docket.delivery import OpenCodeAdapter
from docket.host import OpenChamberAdapter
from docket.parse import local_zone, parse_alarm
from docket.pairing import PairingError, atomic_private
from docket.scheduler import Scheduler
from docket.store import Item, Store, load_token
from tests.support import FakeAdapter, RecordingOpenCode, utc


class PathTests(unittest.TestCase):
    def test_windows_fully_qualified_only(self):
        for path in (r'C:\work\file', 'C:/work/file', r'\\server\share\file', r'\\server\share'):
            self.assertTrue(platform.fully_qualified_path(path, windows=True), path)
        for path in ('C:file', 'C:', r'\file', '/file', 'file', r'\\server',
                     r'\\?\C:\file', r'\\.\pipe\file', 'C:/bad\0'):
            self.assertFalse(platform.fully_qualified_path(path, windows=True), path)
        with patch('docket.platform.is_windows', return_value=True):
            self.assertEqual(_absolute_path('C:/work/file'), 'C:/work/file')
            with self.assertRaises(ValueError):
                _absolute_path('/work/file')

    def test_pinning_uses_identity_not_lowercase(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / 'a'
            b = Path(tmp) / 'b'
            a.mkdir()
            b.mkdir()
            self.assertTrue(platform.same_directory(str(a), str(a / '..' / 'a')))
            self.assertFalse(platform.same_directory(str(a), str(b)))
            if os.name != 'nt':  # Windows symlink privilege is not assumed.
                link = Path(tmp) / 'link'
                link.symlink_to(a, target_is_directory=True)
                self.assertTrue(platform.same_directory(str(a), str(link)))
            else:
                case_dir = Path(tmp) / 'case-dir'
                case_dir.mkdir()
                fsutil = subprocess.run(
                    [
                        r'C:\Windows\System32\fsutil.exe',
                        'file',
                        'setCaseSensitiveInfo',
                        str(case_dir),
                        'enable',
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    **platform.child_options(),
                )
                if fsutil.returncode == 0:
                    upper = case_dir / 'Alpha'
                    lower = case_dir / 'alpha'
                    upper.mkdir()
                    lower.mkdir()
                    self.assertTrue(platform.same_directory(str(upper), str(upper)))
                    self.assertFalse(platform.same_directory(str(upper), str(lower)))
            self.assertFalse(platform.same_directory(str(a), str(Path(tmp) / 'A-missing')))
            self.assertFalse(platform.same_directory(str(Path(tmp) / 'A-missing'), str(a)))
            self.assertTrue(platform.same_directory(str(Path(tmp) / 'missing-a'), str(Path(tmp) / 'missing-a')))
            self.assertFalse(platform.same_directory(str(Path(tmp) / 'missing-a'), str(Path(tmp) / 'missing-b')))
            self.assertFalse(
                platform.same_directory(str(Path(tmp) / 'MissingCase'), str(Path(tmp) / 'missingcase'))
            )
            self.assertFalse(platform.same_directory('relative', 'relative'))
            for err in (
                PermissionError('denied'),
                OSError(errno.EIO, 'input/output error'),
                OSError(errno.ESTALE, 'stale file handle'),
            ):
                with patch('docket.platform.os.path.samefile', side_effect=err):
                    with self.assertRaises(OSError):
                        platform.same_directory(str(a), str(a))
            with patch('docket.platform.os.path.samefile', side_effect=FileNotFoundError), \
                    patch('docket.platform.os.stat', side_effect=[FileNotFoundError(), PermissionError('denied')]):
                with self.assertRaises(PermissionError):
                    platform.same_directory(str(Path(tmp) / 'missing'), str(a))

    def test_pinned_directory_outage_retries_then_delivers_once_and_mismatch_drops(self):
        fake = RecordingOpenCode()
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ, {'DOCKET_HOME': str(Path(tmp) / 'state')}
            ):
                base = Path(tmp)
                root = base / 'state'
                pinned = base / 'pinned'
                other = base / 'other'
                pinned.mkdir()
                other.mkdir()
                fake.sessions['ses_live0001'] = {
                    'id': 'ses_live0001',
                    'time': {},
                    'location': {'directory': str(pinned)},
                }
                fake.sessions['ses_moved0001'] = {
                    'id': 'ses_moved0001',
                    'time': {},
                    'location': {'directory': str(other)},
                }
                adapter = OpenCodeAdapter(discovery=lambda: (fake.url, 'Basic dGVzdA=='))
                for err in (
                    PermissionError('denied'),
                    OSError(errno.EIO, 'io error'),
                    OSError(errno.ESTALE, 'stale handle'),
                ):
                    with patch('docket.platform.os.path.samefile', side_effect=err):
                        validation = adapter.validate({'session_id': 'ses_live0001', 'directory': str(pinned)})
                        self.assertFalse(validation.ok)
                        self.assertEqual(validation.code, 'unavailable')
                mismatch = adapter.validate({'session_id': 'ses_moved0001', 'directory': str(pinned)})
                self.assertFalse(mismatch.ok)
                self.assertEqual(mismatch.code, 'wrong_target')
                self.assertTrue(adapter.validate({'session_id': 'ses_live0001', 'directory': str(pinned)}).ok)

                paired_root = root / 'openchamber'
                atomic_private(paired_root / 'client.json', {
                    'version': 1,
                    'clientId': 'docket-own-id',
                    'clientToken': 'oc_client_test_token',
                    'enrolledOrigin': fake.url,
                    'dedupeKey': 'docket:' + '0' * 32,
                })
                paired = OpenChamberAdapter(paired=True, root=paired_root)
                with patch.object(paired, '_request', side_effect=adapter._request):
                    with patch('docket.platform.os.path.samefile', side_effect=OSError(errno.EIO, 'io error')):
                        self.assertEqual(
                            paired.validate({'session_id': 'ses_live0001', 'directory': str(pinned)}).code,
                            'unavailable',
                        )
                    self.assertEqual(
                        paired.validate({'session_id': 'ses_moved0001', 'directory': str(pinned)}).code,
                        'wrong_target',
                    )
                    self.assertTrue(paired.validate({'session_id': 'ses_live0001', 'directory': str(pinned)}).ok)

                store = Store(root / 'docket.db')
                clock = FakeClock(utc(2026, 10, 16, 12, 0, 0))
                sched = Scheduler(store, adapter, clock)
                server = DocketServer(store, sched, token='test-token', port=0)
                server.serve_in_thread()
                try:
                    with patch.dict(os.environ, {'DOCKET_URL': server.url, 'DOCKET_TOKEN': 'test-token'}):
                        with patch('docket.platform.os.path.samefile', side_effect=PermissionError('denied')):
                            with self.assertRaises(ClientError) as unavailable_err:
                                request('POST', '/api/v1/timers', {
                                    'in': 10,
                                    'reason': 'pinned api check',
                                    'session_id': 'ses_live0001',
                                    'directory': str(pinned),
                                })
                            self.assertEqual(unavailable_err.exception.status, 503)
                            self.assertEqual(unavailable_err.exception.code, 'unavailable')
                        with self.assertRaises(ClientError) as wrong_err:
                            request('POST', '/api/v1/timers', {
                                'in': 10,
                                'reason': 'wrong dir api check',
                                'session_id': 'ses_moved0001',
                                'directory': str(pinned),
                            })
                        self.assertEqual(wrong_err.exception.status, 400)
                        self.assertEqual(wrong_err.exception.code, 'wrong_target')
                finally:
                    server.stop()

                now = clock.now()
                for item_id, session_id in (
                    ('dkt_pinned', 'ses_live0001'),
                    ('dkt_mismatch', 'ses_moved0001'),
                ):
                    store.insert(Item(
                        id=item_id,
                        kind='timer',
                        status='pending',
                        fire_at=now,
                        created_at=now,
                        updated_at=now,
                        reason=item_id,
                        context={},
                        target={'adapter': 'opencode', 'session_id': session_id, 'directory': str(pinned)},
                        urgent=False,
                        created_by=None,
                        late=False,
                        late_by_seconds=None,
                        delivered_at=None,
                        cancelled_at=None,
                        error=None,
                        receipt=None,
                        attempts=0,
                        next_attempt_at=None,
                        firing_at=None,
                        spec=None,
                    ))
                real_samefile = os.path.samefile

                def flaky_samefile(first, second):
                    if str(other) in (str(first), str(second)):
                        return real_samefile(first, second)
                    raise OSError(errno.ESTALE, 'stale file handle')

                with patch('docket.platform.os.path.samefile', side_effect=flaky_samefile):
                    sched.tick()

                survived = store.get('dkt_pinned')
                dropped = store.get('dkt_mismatch')
                self.assertEqual(survived.status, 'pending')
                self.assertEqual(survived.attempts, 1)
                self.assertIsNotNone(survived.next_attempt_at)
                self.assertEqual(dropped.status, 'undeliverable')
                self.assertTrue((dropped.error or '').startswith('wrong_target:'))
                self.assertEqual(fake.inbox.get('ses_live0001', {}), {})

                clock.advance(seconds=5)
                sched.tick()
                admitted = store.get('dkt_pinned')
                self.assertEqual(admitted.status, 'admitted')
                inbox_items = list(fake.inbox.get('ses_live0001', {}).values())
                self.assertEqual(len(inbox_items), 1)
                aid = inbox_items[0]['id']
                fake.inbox['ses_live0001'].pop(aid, None)
                frozen = store.get_request('dkt_pinned')['frozen']
                fake.messages.setdefault('ses_live0001', {})[aid] = {
                    'id': aid, 'type': 'user', 'text': json.loads(frozen['body'])['text'], 'time': {},
                }

                sched.tick()
                delivered = store.get('dkt_pinned')
                self.assertEqual(delivered.status, 'delivered')
                sched.tick()
                post_prompts = [c for c in fake.calls if c[0] == 'POST' and c[1].endswith('/prompt')]
                self.assertEqual(len(post_prompts), 1)
                store.close()
        finally:
            fake.stop()


class ProcessTests(unittest.TestCase):
    def test_posix_absence_is_not_error(self):
        with patch('docket.checks.is_windows', return_value=False):
            for code, out, err in ((2, '', ''), (1, '', 'denied'), (0, '', ''),
                                   (0, 'garbage', ''), (0, '51 other start', ''),
                                   (0, '50 garbage', ''), (0, '50', '')):
                with patch('docket.checks.subprocess.run', return_value=subprocess.CompletedProcess([], code, out, err)):
                    with self.assertRaises(OSError):
                        read_process_start(50)
            with patch('docket.checks.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', '')):
                self.assertIsNone(read_process_start(50))
            with patch('docket.checks.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '50 Fri Oct 16 12:00:00 2026', '')) as run:
                self.assertEqual(read_process_start(50), 'Fri Oct 16 12:00:00 2026')
                self.assertEqual(run.call_args.kwargs['timeout'], 2.0)
                self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)

    def test_windows_native_contract(self):
        kernel = types.SimpleNamespace(**{name: Mock() for name in
            ('OpenProcess', 'GetProcessTimes', 'WaitForSingleObject', 'CloseHandle')})
        kernel.OpenProcess.return_value = 123
        kernel.WaitForSingleObject.return_value = 258
        def times(_handle, creation, *_rest):
            creation.dwLowDateTime = 42
            creation.dwHighDateTime = 1
            return True
        kernel.GetProcessTimes.side_effect = times
        with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
                patch.object(ctypes, 'get_last_error', return_value=5, create=True) as error:
            self.assertEqual(platform.windows_process_start(50), 'win:50:4294967338')
            kernel.CloseHandle.assert_called_with(123)
            kernel.WaitForSingleObject.assert_called_with(123, 0)
            kernel.GetProcessTimes.side_effect = None
            kernel.GetProcessTimes.return_value = False
            with self.assertRaises(OSError):
                platform.windows_process_start(50)
            kernel.GetProcessTimes.side_effect = times
            kernel.WaitForSingleObject.return_value = 0
            self.assertIsNone(platform.windows_process_start(50))
            kernel.WaitForSingleObject.return_value = 0xffffffff
            with self.assertRaises(OSError):
                platform.windows_process_start(50)
            kernel.OpenProcess.return_value = None
            with self.assertRaises(OSError):
                platform.windows_process_start(50)
            error.return_value = 87
            self.assertIsNone(platform.windows_process_start(50))

    def test_native_errors_do_not_fire_watch(self):
        with patch('docket.checks.is_windows', return_value=True), \
                patch('docket.checks.windows_process_start', side_effect=PermissionError):
            self.assertEqual(Checks().evaluate({'check': 'process_ended', 'pid': 50,
                'process_start': 'win:50:1'}, utc(2026, 1, 1)).kind, 'unavailable')

    def test_persisted_legacy_locale_process_start_and_reuse_upgrade(self):
        now = utc(2026, 10, 16, 12, 0, 0)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {'DOCKET_HOME': str(Path(tmp) / 'state')}
        ):
            root = Path(tmp) / 'state'
            store = Store(root / 'docket.db')
            adapter = FakeAdapter()
            clock = FakeClock(now)
            starts: dict[int, str | None] = {
                4242: 'Fri Oct 16 12:00:00 2026',
                4243: 'Fri Oct 6 12:00:00 2026',
            }
            checks = Checks(adapter, process_reader=lambda pid: starts[pid])
            sched = Scheduler(store, adapter, clock, checks=checks)
            for item_id, pid, saved_start in (
                ('dkt_legacy_locale', 4242, 'ven. 16 oct. 12:00:00 2026'),
                ('dkt_legacy_en', 4243, 'Fri Oct  6 12:00:00 2026'),
            ):
                store.insert(Item(
                    id=item_id,
                    kind='watch',
                    status='pending',
                    fire_at=now,
                    created_at=now,
                    updated_at=now,
                    reason=item_id,
                    context={},
                    target={'adapter': 'opencode', 'session_id': 'ses_knownsession0001'},
                    urgent=False,
                    created_by=None,
                    late=False,
                    late_by_seconds=None,
                    delivered_at=None,
                    cancelled_at=None,
                    error=None,
                    receipt=None,
                    attempts=0,
                    next_attempt_at=None,
                    firing_at=None,
                    spec={
                        'check': 'process_ended',
                        'pid': pid,
                        'process_start': saved_start,
                        'every': 15,
                        'until': (now + timedelta(minutes=10)).isoformat(),
                        'checks': 0,
                        'latched': False,
                        'open_occurrence_id': None,
                        'next_seq': 1,
                    },
                ))
            self.assertEqual(checks.evaluate(store.get('dkt_legacy_locale').spec, now).kind, 'unavailable')
            self.assertEqual(checks.evaluate(store.get('dkt_legacy_en').spec, now).kind, 'waiting')

            sched.tick()
            self.assertEqual(adapter.sent, [])
            locale_row = store.get('dkt_legacy_locale')
            en_row = store.get('dkt_legacy_en')
            self.assertEqual(locale_row.status, 'pending')
            self.assertEqual(en_row.status, 'pending')
            # Persisted rows are never rewritten or rebound on evaluation.
            self.assertEqual(locale_row.spec['process_start'], 'ven. 16 oct. 12:00:00 2026')
            self.assertEqual(en_row.spec['process_start'], 'Fri Oct  6 12:00:00 2026')

            starts[4242] = None
            starts[4243] = 'Fri Oct 6 12:05:00 2026'
            clock.advance(seconds=15)
            sched.tick()
            self.assertEqual(store.get('dkt_legacy_locale').status, 'delivered')
            self.assertEqual(store.get('dkt_legacy_en').status, 'delivered')
            messages = {row['message'] for row in adapter.sent}
            self.assertEqual(len(messages), 2)
            self.assertTrue(any('exited' in msg and 'ven. 16 oct. 12:00:00 2026' in msg for msg in messages))
            self.assertTrue(any('pid_reused' in msg and 'Fri Oct  6 12:00:00 2026' in msg for msg in messages))
            store.close()

        win_same = Checks(process_reader=lambda _pid: 'win:50:4294967338').evaluate(
            {'check': 'process_ended', 'pid': 50, 'process_start': 'win:50:4294967338'}, now,
        )
        self.assertEqual(win_same.kind, 'waiting')
        win_reused = Checks(process_reader=lambda _pid: 'win:50:4294999999').evaluate(
            {'check': 'process_ended', 'pid': 50, 'process_start': 'win:50:4294967338'}, now,
        )
        self.assertEqual(win_reused.kind, 'met')
        self.assertEqual(win_reused.summary['ended'], 'pid_reused')
        win_mismatch_pid = Checks(process_reader=lambda _pid: 'win:51:4294967338').evaluate(
            {'check': 'process_ended', 'pid': 50, 'process_start': 'win:50:4294967338'}, now,
        )
        self.assertEqual(win_mismatch_pid.kind, 'unavailable')

    def test_no_console_options(self):
        with patch('docket.platform.is_windows', return_value=True), \
                patch.object(subprocess, 'CREATE_NO_WINDOW', 0x08000000, create=True):
            self.assertEqual(platform.child_options()['creationflags'], 0x08000000)
            self.assertTrue(platform.child_options()['close_fds'])

    def test_stat_timeout_and_failed_child_are_unavailable(self):
        path = str(Path(tempfile.gettempdir()) / 'docket-foundation-stat')
        with patch('docket.checks.subprocess.run', side_effect=subprocess.TimeoutExpired('stat', 1)):
            self.assertEqual(stat_file(path).state, 'unavailable')
        with patch('docket.checks.subprocess.run', return_value=subprocess.CompletedProcess([], 1, 'missing', '')):
            self.assertEqual(stat_file(path).state, 'unavailable')

    @unittest.skipUnless(os.name == 'nt', 'native Windows process proof')
    def test_windows_live_process_exit_reuse_and_access_denied(self):
        now = utc(2026, 10, 19, 12, 0, 0)
        own = read_process_start(os.getpid())
        self.assertIsNotNone(own)
        self.assertTrue(own.startswith(f'win:{os.getpid()}:'))
        self.assertEqual(read_process_start(os.getpid()), own)
        reused = Checks().evaluate(
            {'check': 'process_ended', 'pid': os.getpid(), 'process_start': f'win:{os.getpid()}:1'},
            now,
        )
        self.assertEqual(reused.kind, 'met')
        self.assertEqual(reused.summary['ended'], 'pid_reused')

        child = subprocess.Popen(
            [sys.executable, '-I', '-B', '-c', 'import time; time.sleep(30)'],
            **platform.child_options(),
        )
        try:
            child_start = read_process_start(child.pid)
            self.assertIsNotNone(child_start)
            self.assertEqual(
                Checks().evaluate(
                    {'check': 'process_ended', 'pid': child.pid, 'process_start': child_start}, now
                ).kind,
                'waiting',
            )
            # Impersonate the built-in anonymous token on the current thread so the real
            # Windows kernel OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE)
            # returns ERROR_ACCESS_DENIED (5) against the live child process.
            from ctypes import wintypes

            kernel, advapi = platform._windows_apis()
            kernel.GetCurrentThread.argtypes = []
            kernel.GetCurrentThread.restype = wintypes.HANDLE
            advapi.ImpersonateAnonymousToken.argtypes = [wintypes.HANDLE]
            advapi.ImpersonateAnonymousToken.restype = wintypes.BOOL
            advapi.RevertToSelf.argtypes = []
            advapi.RevertToSelf.restype = wintypes.BOOL

            self.assertTrue(advapi.ImpersonateAnonymousToken(kernel.GetCurrentThread()))
            try:
                with self.assertRaises(OSError) as denied_exc:
                    read_process_start(child.pid)
                self.assertEqual(denied_exc.exception.errno, 5)
                denied = Checks().evaluate(
                    {'check': 'process_ended', 'pid': child.pid, 'process_start': child_start}, now
                )
                self.assertEqual(denied.kind, 'unavailable')
            finally:
                self.assertTrue(advapi.RevertToSelf())

            child.terminate()
            child.wait(timeout=5)
            self.assertIsNone(read_process_start(child.pid))
            exited = Checks().evaluate(
                {'check': 'process_ended', 'pid': child.pid, 'process_start': child_start}, now
            )
            self.assertEqual(exited.kind, 'met')
            self.assertEqual(exited.summary['ended'], 'exited')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def _apply_windows_test_sddl(path: Path, sddl: str) -> None:
    """Apply an explicit owner/DACL SDDL to a newly created test fixture on Windows."""
    from ctypes import wintypes

    kernel, advapi = platform._windows_apis()
    sd_ptr = wintypes.LPVOID()
    sd_size = wintypes.ULONG(0)
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(sd_ptr), ctypes.byref(sd_size)
    ):
        raise OSError(ctypes.get_last_error(), 'ConvertStringSecurityDescriptorToSecurityDescriptorW')
    try:
        owner_sid = wintypes.LPVOID()
        owner_defaulted = wintypes.BOOL(0)
        advapi.GetSecurityDescriptorOwner.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.BOOL),
        ]
        advapi.GetSecurityDescriptorOwner.restype = wintypes.BOOL
        if not advapi.GetSecurityDescriptorOwner(
            sd_ptr.value, ctypes.byref(owner_sid), ctypes.byref(owner_defaulted)
        ):
            raise OSError(ctypes.get_last_error(), 'GetSecurityDescriptorOwner')
        present = wintypes.BOOL(0)
        dacl_ptr = wintypes.LPVOID()
        defaulted = wintypes.BOOL(0)
        if not advapi.GetSecurityDescriptorDacl(
            sd_ptr.value, ctypes.byref(present), ctypes.byref(dacl_ptr), ctypes.byref(defaulted)
        ):
            raise OSError(ctypes.get_last_error(), 'GetSecurityDescriptorDacl')
        control = wintypes.WORD(0)
        revision = wintypes.DWORD(0)
        advapi.GetSecurityDescriptorControl(
            sd_ptr.value, ctypes.byref(control), ctypes.byref(revision)
        )
        sec_info = platform._DACL_SECURITY_INFORMATION
        if owner_sid.value:
            sec_info |= platform._OWNER_SECURITY_INFORMATION
        if control.value & platform._SE_DACL_PROTECTED:
            sec_info |= platform._PROTECTED_DACL_SECURITY_INFORMATION
        else:
            sec_info |= 0x20000000  # UNPROTECTED_DACL_SECURITY_INFORMATION
        handle = platform._windows_raw_open(
            str(path),
            platform._READ_CONTROL | platform._WRITE_DAC | platform._WRITE_OWNER | platform._SYNCHRONIZE,
            platform._OPEN_EXISTING,
            platform._FILE_FLAG_BACKUP_SEMANTICS | platform._FILE_FLAG_OPEN_REPARSE_POINT,
        )
        with platform._win_handle(handle):
            rc = advapi.SetSecurityInfo(
                handle,
                platform._SE_FILE_OBJECT,
                sec_info,
                owner_sid.value,
                None,
                dacl_ptr.value,
                None,
            )
            if rc != 0:
                raise OSError(rc, 'SetSecurityInfo failed')
    finally:
        if sd_ptr.value:
            kernel.LocalFree(sd_ptr.value)


def _apply_windows_owner_attack_fixture_or_skip(
    testcase: unittest.TestCase, path: Path, sddl: str
) -> None:
    """Apply wrong-owner fixture SDDL, skipping only if unprivileged environment returns ERROR_INVALID_OWNER (1307)."""
    try:
        _apply_windows_test_sddl(path, sddl)
    except OSError as exc:
        if exc.errno == 1307 or getattr(exc, 'winerror', None) == 1307:
            testcase.skipTest(
                'assigning Administrators owner to test fixture requires ownership privilege (ERROR_INVALID_OWNER 1307)'
            )
        raise


class StorageTests(unittest.TestCase):
    def test_windows_refuses_before_any_storage_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'state'
            with patch('docket.platform.is_windows', return_value=True), patch(
                'docket.platform._windows_user_sid',
                side_effect=platform.PrivateStorageError(
                    'Windows private Docket storage unavailable: native user SID, ACL token unavailable'
                ),
            ):
                with self.assertRaisesRegex(OSError, 'SID, ACL'):
                    Store(root / 'docket.db')
                with self.assertRaisesRegex(PairingError, 'SID, ACL'):
                    atomic_private(root / 'client.json', {'token': 'fake'})
            self.assertFalse(root.exists())

    def test_state_root_uses_localappdata_on_windows_and_requires_qualified_path(self):
        from docket.pairing import state_dir
        from docket.store import home

        with tempfile.TemporaryDirectory() as tmp:
            if os.name == 'nt':
                fake_local = Path(tmp) / 'LocalAppData'
                fake_local.mkdir()
                with patch.dict(os.environ, {'DOCKET_HOME': '', 'LOCALAPPDATA': str(fake_local)}):
                    resolved = home()
                    self.assertEqual(resolved, fake_local / 'Docket')
                    self.assertTrue(resolved.is_dir())
                    self.assertEqual(state_dir(), fake_local / 'Docket' / 'openchamber')
            else:
                win_local = r'C:\Users\tester\AppData\Local'
                with patch('docket.platform.is_windows', return_value=True), patch.dict(
                    os.environ, {'DOCKET_HOME': '', 'LOCALAPPDATA': win_local}
                ):
                    self.assertEqual(platform.state_root(), Path(win_local) / 'Docket')
                    self.assertEqual(state_dir(), Path(win_local) / 'Docket' / 'openchamber')

            for bad_local in ('', 'relative\\appdata', '/posix/only'):
                with patch('docket.platform.is_windows', return_value=True), patch.dict(
                    os.environ, {'DOCKET_HOME': '', 'LOCALAPPDATA': bad_local}
                ):
                    with self.assertRaises(OSError):
                        platform.state_root()

    def test_no_chmod_or_reacl_of_home_or_existing_nonprivate_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'unrelated'
            root.mkdir(mode=0o755)
            root.chmod(0o755)  # owned fixture; mkdir's requested mode is masked by umask
            with self.assertRaises(OSError):
                platform.private_directory(root)
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)
            with patch('docket.platform.Path.home', return_value=Path(tmp)):
                with self.assertRaises(OSError):
                    platform.private_directory(Path(tmp))

    @unittest.skipIf(os.name == 'nt', 'POSIX security proof only')
    def test_tokens_private_and_db_links_refused_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DOCKET_HOME': tmp}):
            token = load_token()
            self.assertEqual(load_token(), token)
            self.assertEqual(stat.S_IMODE((Path(tmp) / 'token').stat().st_mode), 0o600)
            victim = Path(tmp) / 'unrelated'
            victim.write_text('untouched')
            victim.chmod(0o644)
            db = Path(tmp) / 'docket.db'
            db.symlink_to(victim)
            with self.assertRaises(OSError):
                Store(db)
            self.assertEqual(victim.read_text(), 'untouched')
            self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o644)
            (Path(tmp) / 'token').chmod(0o644)
            with self.assertRaises(OSError):
                load_token()

    @unittest.skipIf(os.name == 'nt', 'POSIX security proof only')
    def test_db_hardlink_and_wal_symlink_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            victim = root / 'unrelated'
            victim.write_text('untouched')
            victim.chmod(0o644)
            db = root / 'docket.db'
            os.link(victim, db)
            with self.assertRaises(OSError):
                Store(db)
            db.unlink()
            (root / 'docket.db-wal').symlink_to(victim)
            with self.assertRaises(OSError):
                Store(db)
            self.assertEqual(victim.read_text(), 'untouched')
            self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o644)

    def test_protection_failure_rolls_back_before_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'state'
            platform.private_directory(root)
            db_path = root / 'docket.db'
            store = Store(db_path)
            try:
                store._db.execute('CREATE TABLE recovery_fixture (value TEXT)')
                store._commit()
                for phase in ('before', 'after'):
                    from docket.store import _seal_db

                    def fail_protection(path):
                        if phase == 'after':
                            _seal_db(path)
                        raise platform.PrivateStorageError('injected protection failure')

                    store._db.execute('INSERT INTO recovery_fixture VALUES (?)', (phase,))
                    with patch('docket.store._seal_db', side_effect=fail_protection):
                        with self.assertRaisesRegex(platform.PrivateStorageError, 'injected'):
                            store._commit()
                    self.assertFalse(store._db.in_transaction)
                    self.assertEqual(store._db.execute('SELECT * FROM recovery_fixture').fetchall(), [])
                # A later successful operation cannot accidentally commit either failed write.
                store._db.execute("INSERT INTO recovery_fixture VALUES ('success')")
                store._commit()
            finally:
                store.close()
            reopened = Store(db_path)
            try:
                self.assertEqual([r[0] for r in reopened._db.execute('SELECT * FROM recovery_fixture')], ['success'])
            finally:
                reopened.close()

    @unittest.skipUnless(os.name == 'nt', 'native Windows inherited SQLite crash recovery')
    def test_windows_controlled_crash_recovers_private_inherited_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'state'
            platform.private_directory(root)
            db_path = root / 'docket.db'
            initial = Store(db_path)
            initial._db.execute('CREATE TABLE recovery_fixture (value TEXT)')
            initial._commit()
            initial.close()
            # Stock SQLite can create sidecars after the last protection check.
            # Use its actual inherited ACLs, not a copied snapshot or ACL fixture.
            code = (
                'import sys,os,sqlite3;sys.path.insert(0,' + repr(str(Path(__file__).resolve().parents[1])) + ');'
                'from docket import platform;platform.require_private_storage();'
                'db=sqlite3.connect(' + repr(str(db_path)) + ');'
                'db.execute("INSERT INTO recovery_fixture VALUES (\'committed-before-crash\')");'
                'db.commit();os._exit(73)'
            )
            child = subprocess.Popen(
                [sys.executable, '-I', '-B', '-c', code],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **platform.child_options(),
            )
            try:
                print('OWN_CRASH_CHILD', child.pid, platform.windows_process_start(child.pid), flush=True)
                stdout, stderr = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 73, (stdout, stderr))
                for suffix in ('-wal', '-shm'):
                    sidecar = Path(str(db_path) + suffix)
                    self.assertTrue(sidecar.exists())
                    with self.assertRaises(platform.PrivateStorageError):
                        platform.seal_file(sidecar)  # strict verification must reject inheritance
                import sqlite3
                connect = sqlite3.connect

                def checked_connect(*args, **kwargs):
                    # Store must finish protecting both handles before SQLite opens.
                    for suffix in ('-wal', '-shm'):
                        platform.seal_file(Path(str(db_path) + suffix))
                    return connect(*args, **kwargs)

                with patch('docket.store.sqlite3.connect', side_effect=checked_connect):
                    reopened = Store(db_path)
                try:
                    self.assertEqual([r[0] for r in reopened._db.execute('SELECT * FROM recovery_fixture')], ['committed-before-crash'])
                    self.assertEqual(reopened._db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                finally:
                    reopened.close()
            finally:
                if child.poll() is None:
                    child.kill()  # exact Popen-owned child only
                    child.communicate(timeout=5)
                print('OWN_CRASH_CHILD_REAPED', child.pid, child.returncode, flush=True)

    def test_narrow_storage_api_bounded_reads_atomic_replace_and_crash_reopen(self):
        import shutil
        from docket.pairing import read_private

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'state'
            with patch.dict(os.environ, {'DOCKET_HOME': str(root)}):
                token = load_token()
                self.assertEqual(load_token(), token)

                f = root / 'secret.bin'
                platform.create_private_file(f, b'initial')
                with self.assertRaises(FileExistsError):
                    platform.create_private_file(f, b'second')
                self.assertEqual(platform.read_private_bytes(f, max_bytes=16), b'initial')
                with self.assertRaises(platform.PrivateStorageError):
                    platform.read_private_bytes(f, max_bytes=4)
                with self.assertRaises(platform.PrivateStorageError):
                    platform.replace_private_bytes(f, b'x' * 32, max_bytes=8)
                self.assertEqual(platform.read_private_bytes(f, max_bytes=16), b'initial')

                with patch('docket.platform.os.replace', side_effect=OSError('replace boom')):
                    with self.assertRaises(OSError):
                        platform.replace_private_bytes(f, b'updated', max_bytes=16)
                self.assertEqual(platform.read_private_bytes(f, max_bytes=16), b'initial')
                self.assertEqual(list(root.glob('.secret-*')), [])

                platform.replace_private_bytes(f, b'updated', max_bytes=16)
                self.assertEqual(platform.read_private_bytes(f, max_bytes=16), b'updated')

                client_path = root / 'openchamber' / 'client.json'
                atomic_private(client_path, {'version': 1, 'ok': True})
                self.assertEqual(read_private(client_path), {'version': 1, 'ok': True})

                # Hardlink refusal on token, pairing, and DB/WAL files.
                other = root / 'other-link'
                os.link(f, other)
                try:
                    with self.assertRaises(OSError):
                        platform.read_private_bytes(f, max_bytes=16)
                    with self.assertRaises(OSError):
                        platform.seal_file(f)
                    with self.assertRaises(OSError):
                        platform.replace_private_bytes(f, b'nope', max_bytes=16)
                finally:
                    other.unlink()
                self.assertEqual(platform.read_private_bytes(f, max_bytes=16), b'updated')

                # DB + WAL + SHM creation, verification, snapshot reopen, and handle cleanup.
                db_path = root / 'docket.db'
                now = utc(2026, 10, 19, 12, 0, 0)
                store = Store(db_path)
                try:
                    store.insert(Item(
                        id='dkt_persist',
                        kind='timer',
                        status='pending',
                        fire_at=now,
                        created_at=now,
                        updated_at=now,
                        reason='persisted',
                        context={},
                        target={'adapter': 'fake', 'session_id': 'ses_knownsession0001'},
                        urgent=False,
                        created_by=None,
                        late=False,
                        late_by_seconds=None,
                        delivered_at=None,
                        cancelled_at=None,
                        error=None,
                        receipt=None,
                        attempts=0,
                        next_attempt_at=None,
                        firing_at=None,
                        spec=None,
                    ))
                    wal_path = Path(str(db_path) + '-wal')
                    shm_path = Path(str(db_path) + '-shm')
                    self.assertTrue(db_path.exists())
                    self.assertTrue(wal_path.exists())
                    self.assertTrue(shm_path.exists())
                    platform.seal_file(db_path)
                    platform.seal_file(wal_path)
                    platform.seal_file(shm_path)

                    # Snapshot live DB + WAL + SHM into a fresh private directory.
                    # This is a reopen fixture, not controlled-crash or power-loss proof.
                    crash_root = Path(tmp) / 'crash-state'
                    platform.private_directory(crash_root)
                    for name in ('docket.db', 'docket.db-wal', 'docket.db-shm'):
                        src = root / name
                        platform.create_private_file(crash_root / name, src.read_bytes())
                finally:
                    store.close()

                reopened = Store(crash_root / 'docket.db')
                try:
                    recovered = reopened.get('dkt_persist')
                    self.assertIsNotNone(recovered)
                    self.assertEqual(recovered.reason, 'persisted')
                finally:
                    reopened.close()

                # Clean handle release allows immediate directory tree deletion on Windows and POSIX.
                shutil.rmtree(root)
                shutil.rmtree(crash_root)

    @unittest.skipUnless(os.name == 'nt', 'native Windows ACL, SID, and reparse proof')
    def test_windows_native_acl_sid_reparse_and_sidecar_attacks_refused(self):
        from docket.pairing import read_private

        user_sid, _, user_sid_ptr = platform._windows_user_sid()
        self.assertTrue(user_sid.startswith('S-1-5-21-'))
        self.assertNotEqual(user_sid, 'S-1-5-32-544')

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / 'private-state'
            platform.private_directory(root)

            # Verify root directory handle security directly.
            dir_h = platform._windows_raw_open(
                str(root),
                platform._READ_CONTROL | platform._FILE_READ_ATTRIBUTES | platform._SYNCHRONIZE,
                platform._OPEN_EXISTING,
                platform._FILE_FLAG_BACKUP_SEMANTICS | platform._FILE_FLAG_OPEN_REPARSE_POINT,
            )
            with platform._win_handle(dir_h):
                _, _, _, is_protected = platform._windows_verify_handle(
                    dir_h,
                    user_sid_str=user_sid,
                    user_sid_ptr=user_sid_ptr,
                    is_directory=True,
                )
                self.assertTrue(is_protected)

            # 1. Existing directory with broad trustee ACEs (Everyone, Users, Authenticated Users, Administrators)
            # or unprotected inheritance is refused.
            for idx, bad_dir_sddl in enumerate((
                f'O:{user_sid}D:AI(A;OICIID;FA;;;{user_sid})(A;OICIID;FA;;;SY)',
                f'O:{user_sid}D:P(A;OICI;FA;;;{user_sid})(A;OICI;FR;;;WD)',
                f'O:{user_sid}D:P(A;OICI;FA;;;{user_sid})(A;OICI;FR;;;BU)',
                f'O:{user_sid}D:P(A;OICI;FA;;;{user_sid})(A;OICI;FR;;;AU)',
                f'O:{user_sid}D:P(A;OICI;FA;;;{user_sid})(A;OICI;FA;;;BA)',
                f'O:{user_sid}D:P(D;OICI;FW;;;WD)(A;OICI;FA;;;{user_sid})',
                f'O:{user_sid}D:P(A;OICI;FR;;;{user_sid})(A;OICI;FA;;;SY)',
            )):
                bad_dir = base / f'bad-dir-{idx}'
                bad_dir.mkdir()
                _apply_windows_test_sddl(bad_dir, bad_dir_sddl)
                with self.assertRaises(platform.PrivateStorageError):
                    platform.private_directory(bad_dir)

            # 2. File ACL attack cases inside a valid private directory.
            client_file = root / 'client.json'
            atomic_private(client_file, {'version': 1, 'ok': True})
            for bad_file_sddl in (
                f'O:{user_sid}D:AI(A;ID;FA;;;{user_sid})(A;ID;FA;;;SY)',
                f'O:{user_sid}D:P(A;;FA;;;{user_sid})(A;;FR;;;WD)',
                f'O:{user_sid}D:P(A;;FA;;;{user_sid})(A;;FR;;;BU)',
                f'O:{user_sid}D:P(A;;FA;;;{user_sid})(A;;FR;;;AU)',
                f'O:{user_sid}D:P(A;;FA;;;{user_sid})(A;;FA;;;BA)',
                f'O:{user_sid}D:P(D;;FW;;;WD)(A;;FA;;;{user_sid})',
                f'O:{user_sid}D:P(A;;FR;;;{user_sid})(A;;FA;;;SY)',
            ):
                _apply_windows_test_sddl(client_file, bad_file_sddl)
                with self.assertRaises(PairingError):
                    read_private(client_file)
                with self.assertRaises(PairingError):
                    atomic_private(client_file, {'version': 1, 'ok': False})
                with self.assertRaises(platform.PrivateStorageError):
                    platform.seal_file(client_file)
                # Restore valid ACL and ensure no silent mutation happened during refusal.
                _apply_windows_test_sddl(
                    client_file, f'O:{user_sid}D:P(A;;FA;;;{user_sid})(A;;FA;;;SY)'
                )
                self.assertEqual(read_private(client_file), {'version': 1, 'ok': True})

            # 3. Kernel-enforced access denial when another principal (anonymous impersonation token)
            # attempts to open our private directory or private client file.
            from ctypes import wintypes

            kernel, advapi = platform._windows_apis()
            kernel.GetCurrentThread.argtypes = []
            kernel.GetCurrentThread.restype = wintypes.HANDLE
            advapi.ImpersonateAnonymousToken.argtypes = [wintypes.HANDLE]
            advapi.ImpersonateAnonymousToken.restype = wintypes.BOOL
            advapi.RevertToSelf.argtypes = []
            advapi.RevertToSelf.restype = wintypes.BOOL
            self.assertTrue(advapi.ImpersonateAnonymousToken(kernel.GetCurrentThread()))
            try:
                with self.assertRaises(PermissionError):
                    client_file.read_bytes()
                with self.assertRaises(PermissionError):
                    list(root.iterdir())
            finally:
                self.assertTrue(advapi.RevertToSelf())

            # 4. Pre-planted unsealed or bad-DACL WAL sidecar is refused before SQLite opens.
            bad_db_root = base / 'bad-db-state'
            platform.private_directory(bad_db_root)
            bad_db = bad_db_root / 'docket.db'
            platform.create_private_file(bad_db, b'')
            bad_wal = bad_db_root / 'docket.db-wal'
            platform.create_private_file(bad_wal, b'')
            _apply_windows_test_sddl(
                bad_wal, f'O:{user_sid}D:P(A;;FA;;;{user_sid})(A;;FR;;;WD)'
            )
            with self.assertRaises(platform.PrivateStorageError):
                Store(bad_db)
            bad_wal.unlink()

            # 5. Reparse point (NTFS directory junction) refusal on directory, ancestor, DB, WAL, token, and pairing files.
            junction_target = base / 'junction-target'
            junction_target.mkdir()
            victim_marker = junction_target / 'untouched.txt'
            victim_marker.write_text('safe')

            def make_junction(link_path: Path, target_path: Path) -> None:
                done = subprocess.run(
                    [
                        r'C:\Windows\System32\cmd.exe',
                        '/d',
                        '/c',
                        'mklink',
                        '/J',
                        str(link_path),
                        str(target_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    **platform.child_options(),
                )
                self.assertEqual(done.returncode, 0, done.stderr or done.stdout)

            def remove_junction(link_path: Path) -> None:
                os.rmdir(link_path)

            junc_dir = base / 'junc-dir'
            make_junction(junc_dir, junction_target)
            try:
                with self.assertRaises(OSError) as ctx:
                    platform.private_directory(junc_dir)
                self.assertEqual(ctx.exception.errno, errno.ELOOP)
                with self.assertRaises(OSError) as ctx2:
                    platform.private_directory(junc_dir / 'nested')
                self.assertEqual(ctx2.exception.errno, errno.ELOOP)
            finally:
                remove_junction(junc_dir)

            for name in ('client.json', 'docket.db', 'docket.db-wal', 'token'):
                target_path = root / name
                if os.path.lexists(target_path):
                    target_path.unlink()
                make_junction(target_path, junction_target)
                try:
                    if name == 'client.json':
                        with self.assertRaises(OSError) as c1:
                            read_private(target_path)
                        self.assertEqual(c1.exception.errno, errno.ELOOP)
                        with self.assertRaises(OSError) as c2:
                            atomic_private(target_path, {'x': 1})
                        self.assertEqual(c2.exception.errno, errno.ELOOP)
                    elif name == 'token':
                        with patch.dict(os.environ, {'DOCKET_HOME': str(root)}):
                            with self.assertRaises(OSError) as c3:
                                load_token()
                            self.assertEqual(c3.exception.errno, errno.ELOOP)
                    else:
                        with self.assertRaises(OSError) as c4:
                            Store(root / 'docket.db')
                        self.assertEqual(c4.exception.errno, errno.ELOOP)
                finally:
                    remove_junction(target_path)
            self.assertEqual(victim_marker.read_text(), 'safe')

            sym_file = root / 'sym-client.json'
            try:
                os.symlink(victim_marker, sym_file)
            except OSError as exc:
                if getattr(exc, 'winerror', None) != 1314:
                    raise
            else:
                try:
                    with self.assertRaises(OSError) as s1:
                        read_private(sym_file)
                    self.assertEqual(s1.exception.errno, errno.ELOOP)
                    with self.assertRaises(OSError) as s2:
                        atomic_private(sym_file, {'x': 1})
                    self.assertEqual(s2.exception.errno, errno.ELOOP)
                finally:
                    sym_file.unlink()
            self.assertEqual(victim_marker.read_text(), 'safe')

            # 6. Directory vs regular file type confusion refusal.
            dir_as_file = root / 'dir_instead_of_file.json'
            platform.private_directory(dir_as_file)
            with self.assertRaises(platform.PrivateStorageError):
                platform.read_private_bytes(dir_as_file)
            with self.assertRaises(platform.PrivateStorageError):
                platform.seal_file(dir_as_file)
            os.rmdir(dir_as_file)

            file_as_dir = root / 'file_instead_of_dir'
            platform.create_private_file(file_as_dir, b'x')
            with self.assertRaises(platform.PrivateStorageError):
                platform.private_directory(file_as_dir)
            file_as_dir.unlink()

    @unittest.skipUnless(os.name == 'nt', 'native Windows ACL, SID, and reparse proof')
    def test_windows_native_privileged_wrong_owner_fixtures_refused(self):
        from docket.pairing import read_private

        user_sid, _, _ = platform._windows_user_sid()
        self.assertTrue(user_sid.startswith('S-1-5-21-'))
        self.assertNotEqual(user_sid, 'S-1-5-32-544')

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / 'private-state'
            platform.private_directory(root)

            # 1. Existing directory owned by BUILTIN\Administrators (S-1-5-32-544) is refused and not re-ACLed.
            admin_dir = base / 'admin-owned-dir'
            admin_dir.mkdir()
            _apply_windows_owner_attack_fixture_or_skip(
                self, admin_dir, f'O:BAD:P(A;OICI;FA;;;{user_sid})(A;OICI;FA;;;SY)'
            )
            try:
                with self.assertRaisesRegex(platform.PrivateStorageError, 'owner'):
                    platform.private_directory(admin_dir)
            finally:
                _apply_windows_test_sddl(
                    admin_dir, f'O:{user_sid}D:P(A;OICI;FA;;;{user_sid})(A;OICI;FA;;;SY)'
                )

            # 2. File ACL/owner attack cases inside a valid private directory.
            client_file = root / 'client.json'
            atomic_private(client_file, {'version': 1, 'ok': True})
            _apply_windows_test_sddl(
                client_file, f'O:BAD:P(A;;FA;;;{user_sid})(A;;FA;;;SY)'
            )
            try:
                with self.assertRaises(PairingError):
                    read_private(client_file)
                with self.assertRaises(PairingError):
                    atomic_private(client_file, {'version': 1, 'ok': False})
                with self.assertRaises(platform.PrivateStorageError):
                    platform.seal_file(client_file)
            finally:
                # Restore valid ACL and ensure no silent mutation happened during refusal.
                _apply_windows_test_sddl(
                    client_file, f'O:{user_sid}D:P(A;;FA;;;{user_sid})(A;;FA;;;SY)'
                )
            self.assertEqual(read_private(client_file), {'version': 1, 'ok': True})

            # 3. Pre-planted wrong-owner SQLite DB is refused before SQLite opens.
            bad_db_root = base / 'bad-db-state'
            platform.private_directory(bad_db_root)
            bad_db = bad_db_root / 'docket.db'
            platform.create_private_file(bad_db, b'')
            _apply_windows_test_sddl(bad_db, f'O:BAD:P(A;;FA;;;{user_sid})(A;;FA;;;SY)')
            try:
                with self.assertRaises(platform.PrivateStorageError):
                    Store(bad_db)
            finally:
                _apply_windows_test_sddl(
                    bad_db, f'O:{user_sid}D:P(A;;FA;;;{user_sid})(A;;FA;;;SY)'
                )

    def test_windows_privileged_wrong_owner_fixture_skip_policy_narrow_1307(self):
        class DummyTest(unittest.TestCase):
            def runTest(self):
                pass

        dummy = DummyTest()
        fake_path = Path('/tmp/fake-admin-dir')

        # 1. Exact errno 1307 triggers self.skipTest.
        with patch('tests.test_platform._apply_windows_test_sddl', side_effect=OSError(1307, 'SetSecurityInfo failed')):
            with self.assertRaises(unittest.SkipTest):
                _apply_windows_owner_attack_fixture_or_skip(dummy, fake_path, 'O:BA...')

        # 2. Other OSError codes (e.g. 5 ERROR_ACCESS_DENIED) propagate rather than skip.
        with patch('tests.test_platform._apply_windows_test_sddl', side_effect=OSError(5, 'Access is denied')):
            with self.assertRaises(OSError) as ctx:
                _apply_windows_owner_attack_fixture_or_skip(dummy, fake_path, 'O:BA...')
            self.assertEqual(ctx.exception.errno, 5)

        # 3. Non-OSError exceptions propagate.
        with patch('tests.test_platform._apply_windows_test_sddl', side_effect=RuntimeError('unexpected error')):
            with self.assertRaises(RuntimeError):
                _apply_windows_owner_attack_fixture_or_skip(dummy, fake_path, 'O:BA...')


class TimezoneTests(unittest.TestCase):
    def test_windows_local_calendar_keeps_dst_rules(self):
        zone = ZoneInfo('America/New_York')
        with patch.dict(sys.modules, {'tzlocal': types.SimpleNamespace(get_localzone=lambda: zone)}), \
                patch.dict(os.environ, {'DOCKET_TIMEZONE': ''}), \
                patch('docket.parse.is_windows', return_value=True):
            self.assertIs(local_zone(), zone)
            # 09:00 after spring transition is 13:00 UTC, not winter's 14:00.
            self.assertEqual(parse_alarm('09:00', now=utc(2026, 3, 7, 15)), utc(2026, 3, 8, 13))
            self.assertEqual(parse_alarm('09:00', now=utc(2026, 10, 31, 15)), utc(2026, 11, 1, 14))
        if os.name == 'nt':
            with patch.dict(os.environ, {'DOCKET_TIMEZONE': ''}):
                live_zone = local_zone()
                self.assertIsInstance(live_zone, ZoneInfo)

    def test_missing_rules_fail_explicitly(self):
        with patch.dict(os.environ, {'DOCKET_TIMEZONE': 'Invalid/DocketZone'}):
            with self.assertRaisesRegex(ValueError, 'IANA'):
                local_zone()
        with patch.dict(sys.modules, {'tzlocal': None}), \
                patch.dict(os.environ, {'DOCKET_TIMEZONE': ''}), \
                patch('docket.parse.is_windows', return_value=True):
            with self.assertRaisesRegex(ValueError, 'tzlocal/tzdata'):
                local_zone()


class BoundaryTests(unittest.TestCase):
    def test_windows_bind_is_exclusive_before_bind(self):
        import socket
        from docket.api import DocketServer
        from http.server import ThreadingHTTPServer
        server = object.__new__(DocketServer)
        server.socket = Mock()
        with patch('docket.platform.is_windows', return_value=True), \
                patch.object(socket, 'SO_EXCLUSIVEADDRUSE', -5, create=True), \
                patch.object(ThreadingHTTPServer, 'server_bind') as bind:
            DocketServer.server_bind(server)
            server.socket.setsockopt.assert_called_once_with(socket.SOL_SOCKET, -5, 1)
            self.assertFalse(server.allow_reuse_address)
            bind.assert_called_once()

    @unittest.skipUnless(os.name == 'nt', 'native Windows exclusive bind contention proof')
    def test_windows_live_exclusive_bind_rejects_second_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'state'
            store = Store(root / 'docket.db')
            clock = FakeClock(utc(2026, 10, 19, 12, 0, 0))
            sched = Scheduler(store, FakeAdapter(), clock)
            first = DocketServer(store, sched, token='t1', host='127.0.0.1', port=0)
            try:
                self.assertFalse(first.allow_reuse_address)
                with self.assertRaisesRegex(SystemExit, 'cannot bind'):
                    DocketServer(store, sched, token='t2', host='127.0.0.1', port=first.server_port)
            finally:
                first.server_close()
                store.close()

    def test_other_platform_service_refuses_without_actions(self):
        from docket import service
        with patch.object(service.sys, 'platform', 'freebsd13'), \
                patch.object(service.subprocess, 'run') as run, patch('builtins.print'):
            self.assertEqual(service.run('install'), 1)
            run.assert_not_called()
