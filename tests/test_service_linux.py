"""Source-only Linux service contract. Never invoke a native service manager."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from docket import service, service_linux as linux
from tests.support import block_source_service_processes


def setUpModule():
    guard = block_source_service_processes()
    unittest.addModuleCleanup(guard.close)


def shown(active="inactive", enabled="disabled", load="loaded", fragment=""):
    return {"LoadState": load, "UnitFileState": enabled, "ActiveState": active,
            "FragmentPath": fragment, "DropInPaths": ""}


class LinuxServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.unit = self.home / ".config/systemd/user/docket.service"
        self.env = patch.dict(os.environ, {"HOME": str(self.home)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        # Universal guard: tests may mock _call, but can never launch a real process.
        self.popen = patch.object(linux.subprocess, "Popen", side_effect=AssertionError("real process forbidden"))
        self.popen.start()
        self.addCleanup(self.popen.stop)

    def own(self):
        self.unit.parent.mkdir(parents=True, exist_ok=True)
        self.unit.write_text(linux.OWNER + "test unit\n")

    def test_render_fixed_command_and_hardening(self):
        text = linux.render_unit(Path('/opt/Docket space/%py$"\\thon'), Path('/opt/Docket space/%root'),
                                 Path('/home/example'), Path('/home/example/.docket'))
        for expected in ('ExecStart="/opt/Docket space/%%py$$\\"\\\\thon" -B -m docket daemon',
                         'WorkingDirectory=/opt/Docket space/%%root\n',
                         'UMask=0077', 'Restart=on-failure', 'RestartSec=5',
                         'StartLimitBurst=5', 'StartLimitIntervalSec=60',
                         'TimeoutStopSec=20', 'StandardOutput=journal', 'StandardError=journal',
                         'DOCKET_HOME=/home/example/.docket'):
            self.assertIn(expected, text)
        self.assertNotIn('WorkingDirectory="', text)
        self.assertNotIn('shell', text)

    def test_path_rendering_and_escaping(self):
        self.assertEqual(linux._path('/opt/docket/app'), '/opt/docket/app')
        self.assertEqual(linux._path('/opt/Docket space/app'), '/opt/Docket space/app')
        self.assertEqual(linux._path('/opt/%root/foo%20bar'), '/opt/%%root/foo%%20bar')
        for bad in ('', 'relative', '/opt/app\\', '/opt/a\nb', '/opt/a\tb', '/opt/a\x00b', '/opt/\ud800'):
            with self.subTest(bad=repr(bad)), self.assertRaises(linux.ServiceError):
                linux._path(bad)

    def test_path_whitespace_policy(self):
        for value in ('/opt/docket/app', '/opt/Docket space/app', '/opt/Docket\u2003space/app'):
            with self.subTest(accepted=repr(value)):
                self.assertEqual(linux._path(value), value)
                text = linux.render_unit(Path('/usr/bin/python3'), Path(value),
                                         Path('/home/example'), Path('/home/example/.docket'))
                self.assertIn(f'WorkingDirectory={value}\n', text)
        for suffix in (' ', '\u00a0', '\u2003', '\u3000'):
            value = '/opt/docket/app' + suffix
            with self.subTest(refused=repr(value)):
                with self.assertRaises(linux.ServiceError):
                    linux._path(value)
                with self.assertRaises(linux.ServiceError):
                    linux.render_unit(Path('/usr/bin/python3'), Path(value),
                                      Path('/home/example'), Path('/home/example/.docket'))

    def test_reject_unrepresentable_paths(self):
        for value in ('relative', '/opt/a\nb', '/opt/a\tb', '/opt/a\x00b', '/opt/a\x7fb', '/opt/a\x85b', '/opt/\ud800'):
            with self.subTest(value=repr(value)), self.assertRaises(linux.ServiceError):
                linux.render_unit(Path(value), Path('/opt/docket'), Path('/home/example'), Path('/home/example/.docket'))
        for value in ('relative', '/opt/a\nb', '/opt/a\tb', '/opt/a\x00b', '/opt/a\x7fb', '/opt/a\x85b', '/opt/\ud800', '/opt/trailing\\'):
            with self.subTest(root_value=repr(value)), self.assertRaises(linux.ServiceError):
                linux.render_unit(Path('/usr/bin/python3'), Path(value), Path('/home/example'), Path('/home/example/.docket'))

    def test_explicit_persistent_deployment_required(self):
        with self.assertRaises(linux.ServiceError):
            linux.installation()
        for root in (str(self.home), '/opt/docket/wt/linux-service', '/opt/docket/../other', '/opt/docket/trailing\\'):
            with patch.dict(os.environ, DOCKET_SERVICE_PYTHON='/usr/bin/python3', DOCKET_SERVICE_ROOT=root):
                with self.assertRaises(linux.ServiceError):
                    linux.installation()

    def test_installation_keeps_venv_and_explicit_state(self):
        with patch.dict(os.environ, DOCKET_SERVICE_PYTHON='/opt/docket/venv/bin/python',
                        DOCKET_SERVICE_ROOT='/opt/docket/app', DOCKET_HOME='/home/example/private state'), \
                patch.object(os, 'access', return_value=True):
            # Check worktree marker and package/executable existence separately.
            with patch.object(Path, 'is_file', autospec=True,
                              side_effect=lambda p: p.name != '.git'):
                text = linux.installation()
        self.assertIn('ExecStart="/opt/docket/venv/bin/python"', text)
        self.assertIn('WorkingDirectory=/opt/docket/app\n', text)
        self.assertNotIn('WorkingDirectory="', text)
        self.assertIn('DOCKET_HOME=/home/example/private state', text)

    def test_install_idempotent_and_uninstall_retains_state(self):
        state = self.home / '.docket'
        state.mkdir()
        for name in ('database', 'token', 'config', 'history'):
            (state / name).write_text('private fixture')
        values = shown(load='not-found')
        with patch.object(linux, '_show', return_value=values), \
                patch.object(linux, '_call', return_value=(0, '')) as call, \
                patch.object(linux, 'installation', return_value=linux.OWNER + 'test unit\n'):
            self.assertEqual(linux.run('install'), 0)
            self.assertEqual(linux.run('install'), 0)
            self.assertEqual(self.unit.stat().st_mode & 0o777, 0o600)
            self.assertEqual(linux.run('uninstall'), 0)
            self.assertEqual(call.call_args_list[-3:],
                             [unittest.mock.call('stop', 'docket.service'),
                              unittest.mock.call('disable', 'docket.service'),
                              unittest.mock.call('daemon-reload')])
            self.assertEqual(linux.run('uninstall'), 0)
        self.assertFalse(self.unit.exists())
        self.assertEqual(len(list(state.iterdir())), 4)

    def test_refuse_unknown_symlink_override_or_foreign_registration(self):
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text('[Service]\nExecStart=/bin/false\n')
        with patch.object(linux, '_show', return_value=shown()), patch.object(linux, '_call') as call:
            for action in ('install', 'uninstall', 'start', 'stop', 'restart'):
                self.assertEqual(linux.run(action), 1)
            call.assert_not_called()
        self.unit.unlink()
        self.unit.symlink_to(self.home / 'missing')
        self.assertEqual(linux.run('install'), 1)
        self.unit.unlink()
        self.own()
        for values in (shown(fragment='/etc/systemd/user/docket.service'),
                       dict(shown(fragment=str(self.unit)), DropInPaths='/etc/override.conf')):
            with patch.object(linux, '_show', return_value=values), patch.object(linux, '_call') as call:
                self.assertEqual(linux.run('uninstall'), 1)
                call.assert_not_called()

    def test_changed_owned_unit_refused(self):
        self.own()
        with patch.object(linux, '_show', return_value=shown(fragment=str(self.unit))), \
                patch.object(linux, 'installation', return_value=linux.OWNER + 'different'), \
                patch.object(linux, '_call') as call:
            self.assertEqual(linux.run('install'), 1)
            call.assert_not_called()

    def test_actions_and_missing(self):
        self.own()
        with patch.object(linux, '_show', return_value=shown(fragment=str(self.unit))), \
                patch.object(linux, '_call', return_value=(0, '')) as call:
            for action in ('start', 'stop', 'restart'):
                self.assertEqual(linux.run(action), 0)
                call.assert_called_with(action, linux.UNIT)
        self.unit.unlink()
        with patch.object(linux, '_show', return_value=shown(load='not-found')), \
                patch.object(linux, '_call') as call:
            self.assertEqual(linux.run('start'), 1)
            call.assert_not_called()

    def test_status_inactive_is_not_error_and_failed_is_reported(self):
        self.own()
        for active, enabled, code in (('inactive', 'disabled', 0), ('active', 'enabled', 0), ('failed', 'enabled', 1)):
            with patch.object(linux, '_show', return_value=shown(active, enabled)):
                self.assertEqual(linux.run('status'), code)
                self.assertIn(f'enabled={enabled}; active={active}', self.output.getvalue())
        self.unit.unlink()
        with patch.object(linux, '_show', return_value=shown(load='not-found')):
            self.assertEqual(linux.run('status'), 1)
            self.assertIn('missing', self.output.getvalue())

    def test_show_accepts_normal_not_found_but_not_bus_failure(self):
        with patch.object(linux, '_call', return_value=(4, 'LoadState=not-found\nActiveState=inactive\n')):
            self.assertEqual(linux._show()['LoadState'], 'not-found')
        with patch.object(linux, '_call', return_value=(1, 'secret manager diagnostic')):
            with self.assertRaises(linux.ServiceError):
                linux._show()

    def test_failures_redact_output(self):
        self.own()
        with patch.object(linux, '_show', return_value=shown(fragment=str(self.unit))), \
                patch.object(linux, '_call', return_value=(1, 'secret-token')):
            self.assertEqual(linux.run('start'), 1)
        self.assertNotIn('secret-token', self.output.getvalue())

    def test_uninstall_failure_keeps_unit_and_unknown_action_does_nothing(self):
        self.own()
        with patch.object(linux, '_show', return_value=shown(fragment=str(self.unit))), \
                patch.object(linux, '_call', return_value=(1, 'private diagnostic')) as command:
            self.assertEqual(linux.run('uninstall'), 1)
            command.assert_called_once_with('stop', linux.UNIT)
            self.assertTrue(self.unit.exists())
        with patch.object(linux, '_show') as show:
            self.assertEqual(linux.run('arbitrary'), 1)
            show.assert_not_called()

    def test_xdg_path_and_relative_state_refusal(self):
        config = self.home / 'config space'
        with patch.dict(os.environ, XDG_CONFIG_HOME=str(config)), \
                patch.object(linux, '_show', return_value=shown(load='not-found')), \
                patch.object(linux, '_call', return_value=(0, '')), \
                patch.object(linux, 'installation', return_value=linux.OWNER + 'test unit\n'):
            self.assertEqual(linux.run('install'), 0)
            self.assertTrue((config / 'systemd/user/docket.service').is_file())
        with patch.dict(os.environ, XDG_CONFIG_HOME='relative'), patch.object(linux, '_call') as command:
            self.assertEqual(linux.run('status'), 1)
            command.assert_not_called()
        with self.assertRaises(linux.ServiceError):
            linux.render_unit(Path('/usr/bin/python3'), Path('/opt/docket'), self.home, Path('relative'))

    def test_missing_systemctl_is_bounded_error(self):
        with patch.object(linux.subprocess, 'Popen', side_effect=FileNotFoundError):
            with self.assertRaises(linux.ServiceError):
                linux._call('show', linux.UNIT)

    def test_bounded_subprocess_argv_and_output(self):
        process = MagicMock()
        process.__enter__.return_value = process
        process.wait.return_value = 0
        process.poll.return_value = 0
        selector = MagicMock()
        selector.__enter__.return_value = selector
        selector.select.return_value = [object()]
        with patch.object(linux.subprocess, 'Popen', return_value=process) as popen, \
                patch.object(linux.selectors, 'DefaultSelector', return_value=selector), \
                patch.object(linux.os, 'read', side_effect=[b'LoadState=loaded\n', b'']):
            self.assertEqual(linux._call('show', linux.UNIT), (0, 'LoadState=loaded\n'))
            self.assertEqual(popen.call_args.args[0], ['systemctl', '--user', '--no-pager', 'show', linux.UNIT])
            self.assertEqual(popen.call_args.kwargs['stderr'], subprocess.DEVNULL)
            self.assertNotIn('shell', popen.call_args.kwargs)
            self.assertLessEqual(process.wait.call_args.kwargs['timeout'], 9)
        for chunks, events in (([b'x' * 4096, b'x' * 4096, b'x'], [object()]), ([], [])):
            process.poll.return_value = None
            selector.select.return_value = events
            with patch.object(linux.subprocess, 'Popen', return_value=process), \
                    patch.object(linux.selectors, 'DefaultSelector', return_value=selector), \
                    patch.object(linux.os, 'read', side_effect=chunks):
                with self.assertRaises(linux.ServiceError):
                    linux._call('show', linux.UNIT)
                process.kill.assert_called()

    def test_dispatch_and_cli_contract(self):
        from docket.cli import main
        with patch.object(service.sys, 'platform', 'linux'), patch.object(linux, 'run', return_value=0) as run:
            for action in ('install', 'status', 'start', 'stop', 'restart', 'uninstall'):
                self.assertEqual(main(['service', action]), 0)
                run.assert_called_with(action)
        with patch.object(service.sys, 'platform', 'freebsd13'):
            self.assertEqual(service.run('status'), 1)
        with patch.object(service.sys, 'platform', 'darwin'), \
                patch.object(service.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'state = running\n', '')) as launch:
            self.assertEqual(service.run('status'), 0)
            self.assertEqual(launch.call_args.args[0][0], 'launchctl')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(['service', 'logs'])
        self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
