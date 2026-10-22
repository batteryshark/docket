"""Temporary private rosters only; no live milestones or host calls."""
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from docket.checkins import roster
from docket.milestone import update_own

SID = "ses_fixtureown0001"
AT = "2026-10-22T12:00:00+00:00"


class MilestoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.path = self.root / "lead-checkins.jsonl"
        self.own = {"session": SID, "directory": "/fixture/own", "state": "hold", "extra": {"keep": True}}
        self.foreign = b'{"session":"ses_fixtureother01", "directory":"/fixture/other", "state":"done"}\n'
        self.raw = json.dumps(self.own).encode() + b"\n\n" + self.foreign
        self.path.write_bytes(self.raw)
        self.path.chmod(0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def update(self, **changes):
        args = dict(session_id=SID, directory="/fixture/own", milestone_at=AT,
                    milestone_ref="fixture accepted outcome", expected=self.raw,
                    now=datetime(2026, 10, 22, 13, tzinfo=timezone.utc))
        args.update(changes)
        return update_own(self.path, **args)

    def test_own_preserves_state_fields_other_lines_modes_and_duplicate(self):
        self.assertEqual(self.update()["status"], "recorded")
        row = roster(self.path)[SID]
        for key, value in self.own.items():
            self.assertEqual(row[key], value)
        self.assertEqual(row["milestone_at"], AT)
        self.assertEqual(row["milestone_ref"], "fixture accepted outcome")
        self.assertTrue(self.path.read_bytes().endswith(b"\n\n" + self.foreign))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.update(expected=self.path.read_bytes())["status"], "unchanged")

    def test_foreign_no_row_wrong_directory_and_identity_refuse(self):
        for change in ({"session_id": "ses_fixtureother01"}, {"session_id": "ses_missing0001"},
                       {"directory": "/wrong"}, {"session_id": None}):
            with self.subTest(change=change):
                with self.assertRaises((ValueError, KeyError)):
                    self.update(**change)
                self.assertEqual(self.path.read_bytes(), self.raw)

    def test_expected_bytes_time_reference_and_private_failures(self):
        for change in ({"expected": b"stale"}, {"milestone_at": "2027-01-01T00:00:00+00:00"},
                       {"milestone_at": "2026-10-22T12:00:00"}, {"milestone_ref": ""}):
            with self.assertRaises(ValueError):
                self.update(**change)
        for path, mode in ((self.path, 0o644), (self.root, 0o755)):
            path.chmod(mode)
            with self.assertRaises(ValueError):
                self.update()
            path.chmod(0o600 if path == self.path else 0o700)
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_changed_bytes_before_replace_preserved(self):
        import docket.milestone as module
        original = module.subprocess.run
        concurrent = self.raw.replace(b'"hold"', b'"done"')
        def race(*args, **kwargs):
            result = original(*args, **kwargs)
            self.path.write_bytes(concurrent)
            return result
        with patch.object(module.subprocess, "run", side_effect=race):
            with self.assertRaisesRegex(ValueError, "conflict"):
                self.update()
        self.assertEqual(self.path.read_bytes(), concurrent)
        self.assertEqual(list(self.root.glob(".milestone-*")), [])

    def test_roster_link_and_duplicate_row_refused(self):
        target = self.root / "target"
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            self.update()
        self.path.unlink()
        target.rename(self.path)
        self.path.write_bytes(self.raw + json.dumps(self.own).encode() + b"\n")
        with self.assertRaises(ValueError):
            self.update(expected=None)

    def test_lock_contention_refuses_without_edit(self):
        import fcntl
        lock = self.root / ".lead-checkins.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.update()
            self.assertEqual(self.path.read_bytes(), self.raw)
        finally:
            os.close(fd)

    def test_cli_only_typed_fields_and_native_identity_environment(self):
        home = self.root / "home"
        private = home / ".docket"
        private.mkdir(parents=True, mode=0o700)
        target = private / "lead-checkins.jsonl"
        target.write_bytes(self.raw)
        target.chmod(0o600)
        env = {**os.environ, "HOME": str(home), "DOCKET_CALLER_SESSION": SID,
               "DOCKET_CALLER_DIRECTORY": "/fixture/own", "PYTHONDONTWRITEBYTECODE": "1"}
        payload = {"milestone_at": "2020-01-01T00:00:00Z", "milestone_ref": "fixture accepted outcome"}
        def invoke(value, environment=env):
            return subprocess.run([sys.executable, "-m", "docket.milestone"],
                                  input=json.dumps(value), text=True, capture_output=True,
                                  env=environment, cwd=Path(__file__).resolve().parents[1])
        for extra in ("session_id", "directory", "path", "state", "command"):
            self.assertEqual(invoke({**payload, extra: "forged"}).returncode, 1)
            self.assertEqual(target.read_bytes(), self.raw)
        self.assertEqual(invoke(payload, {**env, "DOCKET_CALLER_DIRECTORY": "/wrong"}).returncode, 1)
        result = invoke(payload)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(roster(target)[SID]["milestone_at"], "2020-01-01T00:00:00+00:00")
        self.assertEqual(roster(target)[SID]["state"], "hold")

    def test_duplicate_json_field_refused(self):
        self.path.write_bytes(self.raw.replace(b'"state": "hold"', b'"state":"done","state":"hold"'))
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            self.update(expected=None)


if __name__ == "__main__":
    unittest.main()
