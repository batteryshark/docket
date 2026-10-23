import json
from pathlib import Path
import platform
import socket
import tempfile
import subprocess
import unittest

from docket.sandbox import run, seatbelt_profile


@unittest.skipUnless(platform.system() == 'Darwin', 'Seatbelt requires macOS')
class SeatbeltTests(unittest.TestCase):
    def test_real_file_and_network_denials(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            root = Path(root).resolve()
            secret = Path(outside).resolve() / 'unapproved-input'
            secret.write_text('fixture-not-a-real-secret')
            with socket.socket() as allowed, socket.socket() as forbidden:
                allowed.bind(('127.0.0.1', 0)); allowed.listen()
                forbidden.bind(('127.0.0.1', 0)); forbidden.listen()
                ap, fp = allowed.getsockname()[1], forbidden.getsockname()[1]
                host_fixture = subprocess.Popen(['/bin/sleep','3'],env={'SANDBOX_FAKE_SECRET':'host-only-fixture'})
                probe = root / 'probe.py'
                probe.write_text('''import json, socket, pathlib, os, ctypes
result = {}
def check(name, fn):
    try: fn(); result[name] = 'allowed'
    except OSError as e: result[name] = 'denied:' + str(e.errno)
check('private_write', lambda: pathlib.Path('output').write_text('ok'))
check('outside_read', lambda: pathlib.Path(%r).read_text())
check('outside_write', lambda: pathlib.Path(%r).write_text('bad'))
check('home_read', lambda: pathlib.Path(%r).read_bytes())
check('allowed_endpoint', lambda: socket.create_connection(('127.0.0.1', %d), 1))
check('other_loopback', lambda: socket.create_connection(('127.0.0.1', %d), 1))
check('external_network', lambda: socket.create_connection(('1.1.1.1', 443), 1))
libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
mib = (ctypes.c_int * 3)(1,49,%d)
buffer = ctypes.create_string_buffer(65536)
size = ctypes.c_size_t(len(buffer))
ret = libc.sysctl(mib,3,buffer,ctypes.byref(size),None,0)
result['host_process_arguments'] = 'denied:' + str(ctypes.get_errno()) if ret != 0 else 'allowed'
result['ambient_secret'] = 'absent' if 'SANDBOX_FAKE_SECRET' not in os.environ else 'present'
print(json.dumps(result))
''' % (str(secret), str(secret.parent / 'bad'), str(Path.home() / '.chief-of-staff/principal.md'), ap, fp, host_fixture.pid))
                runtime = Path('/Library/Developer/CommandLineTools')
                try:
                    result = run(root, [str(runtime / 'usr/bin/python3'), str(probe)], ports=[ap], read_paths=[runtime / 'usr/bin/python3', runtime / 'Library/Frameworks/Python3.framework'])
                finally:
                    host_fixture.terminate();host_fixture.wait(timeout=3)
                self.assertEqual(result.returncode, 0, result.stderr)
                evidence = json.loads(result.stdout)
                print(json.dumps(evidence, sort_keys=True))
                for name in ('outside_read', 'outside_write', 'home_read', 'other_loopback', 'external_network', 'host_process_arguments'):
                    self.assertEqual(evidence[name], 'denied:1', evidence)
                self.assertEqual(evidence['private_write'], 'allowed')
                self.assertEqual(evidence['allowed_endpoint'], 'allowed')
                self.assertEqual(evidence['ambient_secret'], 'absent')

    def test_no_directory_runtime_grants(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                seatbelt_profile(Path(root), read_paths=[Path.home()])
