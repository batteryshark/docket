"""Offline installer contracts only. No daemon, host, pairing or launchctl runs."""
import contextlib
import io
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from docket import service, store


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home & <雪 space'
        self.home.mkdir()
        self.plist = self.home / 'Library/LaunchAgents/local.docket.plist'
        self.env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'USER': 'demo & <雪',
                    'DOCKET_HOME': str(self.home / 'state & <雪 space'),
                    'DOCKET_DELIVERY_HOST': 'openchamber-paired',
                    'DOCKET_TOKEN': 'synthetic-not-a-credential',
                    'UNRELATED_SETTING': 'must-not-copy'}
        self.enterContext(patch.dict(os.environ, self.env, clear=True))
        self.enterContext(patch.object(service, 'PLIST', self.plist))
        self.run_mock = self.enterContext(patch.object(service.subprocess, 'run'))
        self.run_mock.return_value = subprocess.CompletedProcess([], 0, '', '')
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))

    def install(self):
        self.assertEqual(service.run('install'), 0)
        return plistlib.loads(self.plist.read_bytes())

    def test_roundtrip_and_startup_environment(self):
        data = self.install()
        env = data['EnvironmentVariables']
        self.assertEqual(set(env), {'HOME', 'PATH', 'USER', 'LOGNAME',
                                    'DOCKET_HOME', 'DOCKET_DELIVERY_HOST'})
        self.assertEqual(env['HOME'], str(self.home))
        self.assertEqual(env['USER'], self.env['USER'])
        self.assertEqual(env['LOGNAME'], self.env['USER'])
        self.assertEqual(env['DOCKET_DELIVERY_HOST'], 'openchamber-paired')
        with patch.dict(os.environ, env, clear=True):
            state = store.home()
            self.assertEqual(state, Path(self.env['DOCKET_HOME']).resolve())
            self.assertEqual(data['StandardOutPath'], str(state / 'daemon.out.log'))
            self.assertEqual(data['StandardErrorPath'], str(state / 'daemon.err.log'))
        self.assertTrue(data['RunAtLoad'])
        self.assertTrue(data['KeepAlive'])
        self.assertEqual(self.run_mock.call_args_list[0].args[0][1], 'bootout')
        self.assertEqual(self.run_mock.call_args_list[1].args[0],
                         ['launchctl', 'bootstrap', service._domain(), str(self.plist)])

    def test_defaults_and_tilde_are_pinned(self):
        os.environ.pop('DOCKET_HOME')
        os.environ.pop('DOCKET_DELIVERY_HOST')
        data = self.install()
        self.assertEqual(data['EnvironmentVariables']['DOCKET_HOME'], str(self.home / '.docket'))
        self.assertEqual(data['EnvironmentVariables']['DOCKET_DELIVERY_HOST'], 'auto')
        os.environ['DOCKET_HOME'] = '~/custom & <雪'
        self.assertEqual(self.install()['EnvironmentVariables']['DOCKET_HOME'],
                         str(self.home / 'custom & <雪'))

    def test_interpreter_and_module_root_not_launcher_default(self):
        exe = self.home / 'python & <雪 space'
        exe.write_text('not executed')
        exe.chmod(0o700)
        source = self.home / 'source & <雪 space/docket/service.py'
        os.environ['DOCKET_PYTHON'] = 'ignored-by-module-invocation'
        with patch.object(sys, 'executable', str(exe)), patch.object(service, '__file__', str(source)):
            data = self.install()
        self.assertEqual(data['ProgramArguments'], [str(exe), '-m', 'docket', 'daemon'])
        self.assertEqual(data['WorkingDirectory'], str(source.parents[1]))

    def test_invalid_interpreter_fails_before_service_calls(self):
        for exe in ('', 'python3', str(self.home / 'absent')):
            with self.subTest(exe=exe), patch.object(sys, 'executable', exe):
                self.assertEqual(service.run('install'), 1)
        exe = self.home / 'not-executable'
        exe.write_text('not executed')
        with patch.object(sys, 'executable', str(exe)):
            self.assertEqual(service.run('install'), 1)
        with patch.object(sys, 'version_info', (3, 10)):
            self.assertEqual(service.run('install'), 1)
        self.run_mock.assert_not_called()
        self.assertFalse(self.plist.exists())

    def test_interpreter_symlink_is_not_dereferenced(self):
        exe = self.home / 'venv & <雪/bin/python'
        exe.parent.mkdir(parents=True)
        exe.symlink_to(sys.executable)
        with patch.object(sys, 'executable', str(exe)):
            self.assertEqual(self.install()['ProgramArguments'][0], str(exe))

    def test_supported_modes_are_preserved(self):
        for mode in ('auto', 'opencode', 'openchamber-paired', 'openchamber-unpaired'):
            with self.subTest(mode=mode), patch.dict(os.environ, {'DOCKET_DELIVERY_HOST': mode}):
                self.assertEqual(self.install()['EnvironmentVariables']['DOCKET_DELIVERY_HOST'], mode)

    def test_unrepresentable_environment_fails_without_service_calls(self):
        os.environ['USER'] = 'invalid\x01xml'
        self.assertEqual(service.run('install'), 1)
        self.run_mock.assert_not_called()
        self.assertFalse(self.plist.exists())

    def test_resolved_state_keeps_existing_contents_in_place(self):
        state = self.home / 'actual state & <雪'
        state.mkdir()
        sentinel = state / 'existing-state'
        sentinel.write_bytes(b'unchanged state fixture')
        alias = self.home / 'state alias'
        alias.symlink_to(state, target_is_directory=True)
        os.environ['DOCKET_HOME'] = str(alias)
        data = self.install()
        self.assertEqual(data['EnvironmentVariables']['DOCKET_HOME'], str(state))
        self.assertEqual(data['StandardOutPath'], str(state / 'daemon.out.log'))
        self.assertEqual(sentinel.read_bytes(), b'unchanged state fixture')
        self.assertEqual(list(state.iterdir()), [sentinel])

    def test_unsupported_settings_fail_closed(self):
        for key, value in (('DOCKET_HOME', ''), ('DOCKET_HOME', 'relative'),
                           ('DOCKET_DELIVERY_HOST', 'unknown')):
            with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                self.assertEqual(service.run('install'), 1)
        self.run_mock.assert_not_called()
        self.assertFalse(self.plist.exists())

    def test_bootstrap_failure_never_claims_install(self):
        self.run_mock.side_effect = [subprocess.CompletedProcess([], 1, '', ''),
                                    subprocess.CompletedProcess([], 5, '', 'mock bootstrap failure')]
        self.assertEqual(service.run('install'), 5)
        self.assertNotIn('installed and started', self.output.getvalue())
        self.assertTrue(self.plist.exists())
