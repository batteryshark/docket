"""Gateway path/auth contract with owned fake HOME; no broker, model or native IO."""
import ast
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


GATEWAY = Path(__file__).resolve().parents[1] / 'integrations/opencode/sandbox-gateway.py'


class GatewayPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / 'owned home #?'
        self.db_path = self.home / '.local/share/opencode/opencode.db'
        self.db_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute('CREATE TABLE credential(integration_id TEXT, active INTEGER, value TEXT)')
        self.env = patch.dict(os.environ, {'HOME': str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        tree = ast.parse(GATEWAY.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'own_access')
        namespace = {'Path': Path, 'sqlite3': sqlite3, 'json': json, 'time': time}
        # Load the actual function without starting gateway services/importing broker.
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(GATEWAY), 'exec'), namespace)
        self.own_access = namespace['own_access']

    def insert(self, *, integration='xai', active=1, kind='oauth', expires=None):
        value = {'type': kind, 'expires': expires if expires is not None else time.time()*1000+60000,
                 'access': 'dummy-owned-test-value'}
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute('INSERT INTO credential VALUES (?,?,?)', (integration, active, json.dumps(value)))

    def test_encoded_account_home_read_only_query_only_and_no_write(self):
        self.insert()
        before = self.db_path.read_bytes()
        connect = sqlite3.connect
        traces = []
        def observed(database, **kwargs):
            self.assertEqual(database, self.db_path.as_uri() + '?mode=ro')
            self.assertEqual(kwargs, {'uri': True})
            self.assertIn('%23%3F', database)
            db = connect(database, **kwargs)
            db.set_trace_callback(traces.append)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('CREATE TABLE forbidden_write(value)')
            return db
        with patch.object(sqlite3, 'connect', side_effect=observed):
            self.assertEqual(self.own_access(), 'dummy-owned-test-value')
        self.assertIn('PRAGMA query_only=ON', traces)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_missing_inactive_other_provider_wrong_type_and_expiry_refuse(self):
        for changes in (None, {'active': 0}, {'integration': 'other'}, {'kind': 'api'}, {'expires': 0}):
            with self.subTest(changes=changes):
                with closing(sqlite3.connect(self.db_path)) as db, db:
                    db.execute('DELETE FROM credential')
                if changes is not None:
                    self.insert(**changes)
                with self.assertRaises(RuntimeError):
                    self.own_access()

    def test_missing_database_is_not_created(self):
        self.db_path.unlink()  # Only this test's own dummy database.
        with self.assertRaises(sqlite3.OperationalError):
            self.own_access()
        self.assertFalse(self.db_path.exists())

    def test_real_startup_fails_closed_before_broker_or_service(self):
        control = self.home / 'control'
        control.mkdir()
        for settings in ({}, {'toolstack_root': ''}, {'toolstack_root': 'relative'},
                         {'toolstack_root': None}, {'toolstack_root': 42}):
            with self.subTest(settings=settings):
                (control / 'gateway-settings.json').write_text(json.dumps(settings))
                result = subprocess.run([sys.executable, '-B', str(GATEWAY), str(control)],
                                        capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('explicit absolute installed Tool Stack root required', result.stderr)
                self.assertNotIn('No module named', result.stderr)
                self.assertEqual(sorted(p.name for p in control.iterdir()), ['gateway-settings.json'])
