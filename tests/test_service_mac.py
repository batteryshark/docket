"""Mac install privacy/alias regressions; all lifecycle calls are mocked."""
import contextlib
import io
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from docket import platform as native, service, store


def metadata(path, *, link=False):
    st = path.lstat() if link else path.stat()
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
            st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class MacInstallTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(tmp) / 'home & <雪'
        self.home.mkdir(mode=0o700)
        self.plist = self.home / 'LaunchAgents/local.docket.plist'
        self.enterContext(patch.dict(os.environ, {
            'HOME': str(self.home), 'USER': 'demo', 'PATH': '/usr/bin:/bin',
            'DOCKET_HOME': str(self.home / 'state'),
            'DOCKET_TOKEN': 'synthetic-only', 'OPENCODE_SERVER_PASSWORD': 'fake-only',
            'DOCKET_DELIVERY_HOST_MODE': 'not-a-supported-setting',
        }, clear=True))
        self.enterContext(patch.object(service.sys, 'platform', 'darwin'))
        self.enterContext(patch.object(service, 'PLIST', self.plist))
        self.calls = self.enterContext(patch.object(service.subprocess, 'run'))
        self.calls.return_value = subprocess.CompletedProcess([], 0, '', '')
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def refused(self):
        self.assertEqual(service.run('install'), 1)
        self.calls.assert_not_called()
        self.assertFalse(self.plist.exists())

    def test_invalid_configuration_precedes_state_and_native_calls(self):
        with patch.object(store, 'home', side_effect=AssertionError('state touched')):
            for key, value in (('DOCKET_HOME', ''), ('DOCKET_HOME', 'relative'),
                               ('DOCKET_DELIVERY_HOST', 'invalid')):
                with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                    self.refused()

    def test_serialization_refusal_precedes_plist_and_native_calls(self):
        with patch.object(service.plistlib, 'dumps', side_effect=ValueError('invalid XML')):
            self.refused()

    def test_environment_is_exact_and_credential_free(self):
        data = plistlib.loads(service._install_plist())
        env = data['EnvironmentVariables']
        self.assertEqual(set(env), {'PATH', 'HOME', 'USER', 'LOGNAME',
                                    'DOCKET_HOME', 'DOCKET_DELIVERY_HOST'})
        self.assertEqual(env['DOCKET_DELIVERY_HOST'], 'auto')
        self.assertNotIn(b'synthetic-only', service._install_plist())
        self.assertNotIn(b'fake-only', service._install_plist())
        self.calls.assert_not_called()

    def test_absolute_alias_pins_private_target_without_store_policy_change(self):
        target = self.home / 'target & <雪'
        target.mkdir(mode=0o700)
        sentinel = target / 'sentinel'
        sentinel.write_bytes(b'unchanged\x00fixture')
        alias = self.home / 'alias'
        alias.symlink_to(target, target_is_directory=True)
        before = (metadata(target), metadata(sentinel), metadata(alias, link=True))
        os.environ['DOCKET_HOME'] = str(alias)
        with patch.object(store, 'home', side_effect=AssertionError('old alias path')):
            self.assertEqual(service.run('install'), 0)
        data = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(data['EnvironmentVariables']['DOCKET_HOME'], str(target))
        self.assertEqual(data['StandardErrorPath'], str(target / 'daemon.err.log'))
        self.assertEqual(sentinel.read_bytes(), b'unchanged\x00fixture')
        self.assertEqual(list(target.iterdir()), [sentinel])
        self.assertEqual(before, (metadata(target), metadata(sentinel), metadata(alias, link=True)))
        with self.assertRaises(OSError):
            store.home()
        with self.assertRaises(OSError):
            native.private_directory(alias)

    def test_broad_alias_target_refused_without_permission_or_metadata_repair(self):
        target = self.home / 'broad'
        target.mkdir(mode=0o700)
        target.chmod(0o755)  # Synthetic fixture only, never production repair.
        sentinel = target / 'sentinel'
        sentinel.write_bytes(b'retain')
        alias = self.home / 'alias'
        alias.symlink_to(target, target_is_directory=True)
        before = (metadata(target), metadata(sentinel), metadata(alias, link=True))
        os.environ['DOCKET_HOME'] = str(alias)
        self.refused()
        self.assertEqual(before, (metadata(target), metadata(sentinel), metadata(alias, link=True)))
        self.assertEqual(sentinel.read_bytes(), b'retain')

    def test_dangling_relative_and_tilde_aliases_refused_unchanged(self):
        target = self.home / 'target'
        target.mkdir(mode=0o700)
        alias = self.home / 'alias'
        alias.symlink_to(target, target_is_directory=True)
        dangling = self.home / 'dangling'
        missing = self.home / 'missing'
        dangling.symlink_to(missing, target_is_directory=True)
        before = (metadata(alias, link=True), metadata(dangling, link=True), metadata(target))
        for raw in (str(dangling), 'alias', '~/alias'):
            with self.subTest(raw=raw), patch.dict(os.environ, {'DOCKET_HOME': raw}):
                self.refused()
        self.assertEqual(before, (metadata(alias, link=True), metadata(dangling, link=True), metadata(target)))
        self.assertFalse(missing.exists())

    def test_alias_to_non_directory_or_home_is_refused(self):
        file = self.home / 'file'
        file.write_bytes(b'retain')
        for name, target in (('file-alias', file), ('home-alias', self.home)):
            alias = self.home / name
            alias.symlink_to(target)
            with self.subTest(name=name), patch.dict(os.environ, {'DOCKET_HOME': str(alias)}):
                self.refused()
        self.assertEqual(file.read_bytes(), b'retain')

    def test_non_mac_routing_precedes_mac_configuration(self):
        from docket import service_linux, service_windows
        with patch.object(service, '_install_plist', side_effect=AssertionError('Mac config')):
            for platform, backend in (('linux', service_linux), ('win32', service_windows)):
                with self.subTest(platform=platform), patch.object(service.sys, 'platform', platform), \
                        patch.object(backend, 'run', return_value=7) as dispatch:
                    self.assertEqual(service.run('install'), 7)
                    dispatch.assert_called_once_with('install')
            with patch.object(service.sys, 'platform', 'freebsd13'):
                self.assertEqual(service.run('install'), 1)
        self.calls.assert_not_called()
