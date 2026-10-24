"""Isolated supplemental index contract; no native observations or scheduler ticks."""
import copy
import hashlib
import io
import json
import tempfile
import unittest
import urllib.request
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from docket.api import DocketServer
from docket.cli import main
from docket.project_index import projection, validate_response


class ProjectIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.now = datetime(2026, 10, 24, tzinfo=timezone.utc)
        self.row = dict(session="ses_fixture", directory="/fixture/é", state="active")
        self.manager = SimpleNamespace(roster_path=self.root / "roster", broken=False, error=None,
            state={"leads": {"ses_fixture": {"goal": ["native", 123], "directory": self.row["directory"]}}},
            delivery_stall=SimpleNamespace(samples={}, error=None))
        self.native = Mock(side_effect=AssertionError("forbidden native call"))
        self.manager.snapshot = self.native
        self.manager.scheduler = SimpleNamespace(adapter=self.native)
        self.write([self.row])

    def tearDown(self):
        self.assertEqual(self.native.mock_calls, [])
        self.tmp.cleanup()

    def write(self, rows):
        self.manager.roster_path.write_text("".join(json.dumps(r)+"\n" for r in rows))
        self.manager.roster_path.chmod(0o600)

    def sample(self, age=0):
        key = hashlib.sha256(json.dumps(["ses_fixture", self.row["directory"], ["native", 123]], sort_keys=True).encode()).hexdigest()
        at = self.now - timedelta(seconds=age)
        self.manager.delivery_stall.samples = {"ses_fixture": (key, at, at-timedelta(hours=1))}

    def activity(self):
        before = copy.deepcopy((self.manager.state, self.manager.delivery_stall.samples))
        raw = self.manager.roster_path.read_bytes()
        result = projection(self.manager, self.now)["leads"][0]["activity"]
        self.assertEqual(before, (self.manager.state, self.manager.delivery_stall.samples))
        self.assertEqual(raw, self.manager.roster_path.read_bytes())
        return result

    def test_markerless_states(self):
        self.write([dict(self.row, session="ses_"+state, state=state) for state in ("active", "hold", "done")])
        result = projection(self.manager, self.now)
        self.assertEqual(len(result["leads"]), 3)
        self.assertTrue(all(r["activity"] == {"state": "unknown"} for r in result["leads"]))

    def test_boundaries_original_time(self):
        for age in (0, 42.9, 120, 120.9, 121, -0.1):
            with self.subTest(age=age):
                self.sample(age)
                activity = self.activity()
                if 0 <= age <= 120:
                    self.assertEqual(activity, dict(state="busy", sample_at=(self.now-timedelta(seconds=age)).isoformat(timespec="microseconds").replace("+00:00", "Z"), age_seconds=int(age), basis="docket.running_sample"))
                else:
                    self.assertEqual(activity, {"state": "unknown"})

    def test_ineligible_and_failures_unknown(self):
        for case in ("restart", "hold", "done", "goal", "goal_bool", "directory", "broken", "error", "stall_error", "hash", "session"):
            with self.subTest(case=case):
                self.sample()
                old = copy.deepcopy(self.manager.state)
                row = dict(self.row)
                if case == "restart": self.manager.delivery_stall.samples.clear()
                if case in ("hold", "done"): row["state"] = case
                if case == "goal": self.manager.state["leads"]["ses_fixture"]["goal"] = ["changed", 123]
                if case == "goal_bool": self.manager.state["leads"]["ses_fixture"]["goal"] = ["native", True]
                if case == "directory": row["directory"] = "/other"
                if case == "broken": self.manager.broken = True
                if case == "error": self.manager.error = "failed"
                if case == "stall_error": self.manager.delivery_stall.error = "failed"
                if case == "hash": self.manager.delivery_stall.samples["ses_fixture"] = ("x"*64, self.now, self.now)
                if case == "session": self.manager.state["leads"] = {}
                self.write([row])
                self.assertEqual(self.activity(), {"state": "unknown"})
                self.manager.state = old
                self.manager.broken = False
                self.manager.error = self.manager.delivery_stall.error = None
        self.write([self.row])

    def test_strict_schema(self):
        self.sample(42)
        good = projection(self.manager, self.now)
        mutations = [lambda p: p.update(version=True), lambda p: p.update(extra=1),
            lambda p: p.update(complete=False), lambda p: p.update(observed_at="2026-10-24"),
            lambda p: p["leads"].append(p["leads"][0]),
            lambda p: p["leads"][0].update(private="secret"),
            lambda p: p["leads"][0]["activity"].update(age_seconds=True),
            lambda p: p["leads"][0]["activity"].update(sample_at="bad"),
            lambda p: p["leads"][0]["activity"].update(hash="a"*64)]
        for mutate in mutations:
            bad = copy.deepcopy(good)
            mutate(bad)
            with self.assertRaises(ValueError): validate_response(bad)

    def test_source_and_output_bounds(self):
        for count in (0, 20):
            self.write([dict(self.row, session="ses_f"+str(i)) for i in range(count)])
            self.assertEqual(len(projection(self.manager, self.now)["leads"]), count)
        for rows in ([dict(self.row, session="ses_f"+str(i)) for i in range(21)],
                     [dict(self.row, session="ses_f"+str(i), directory="/"+"é"*4000) for i in range(3)]):
            self.write(rows)
            with self.assertRaises(ValueError): projection(self.manager, self.now)
        for text in ("not json", "x"*256001):
            self.manager.roster_path.write_text(text)
            with self.assertRaises(ValueError): projection(self.manager, self.now)
        self.write([self.row])
        self.manager.roster_path.chmod(0o644)
        with self.assertRaises(ValueError): projection(self.manager, self.now)
        self.manager.roster_path.unlink()
        with self.assertRaises(FileNotFoundError): projection(self.manager, self.now)

    def test_cli(self):
        good = projection(self.manager, self.now)
        for payload, exitcode in ((good, 0), (dict(good, version=True), 1)):
            out = io.StringIO()
            with patch("docket.cli.request", return_value=(200, payload)) as request, redirect_stdout(out):
                self.assertEqual(main(["project-index", "--json"]), exitcode)
            request.assert_called_once_with("GET", "/api/v1/tower/project-index", None, authenticated=True)
            self.assertEqual(json.loads(out.getvalue())["version"], 1)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["project-index", "--json", "--session", "ses_no"]), 1)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], "invalid")
        with redirect_stdout(io.StringIO()) as helpout, self.assertRaises(SystemExit): main(["project-index", "--help"])
        self.assertIn("docket project-index --json", helpout.getvalue())

    def test_http_auth_query_and_unavailable(self):
        scheduler = SimpleNamespace(checkins=self.manager, clock=SimpleNamespace(now=lambda: self.now))
        server = DocketServer(None, scheduler, "fixture-token", port=0)
        server.serve_in_thread()
        try:
            for suffix, token, expected in (("", None, 401), ("?x", "fixture-token", 400), ("?x=", "fixture-token", 400), ("", "fixture-token", 200)):
                req = urllib.request.Request(server.url+"/api/v1/tower/project-index"+suffix,
                    headers={"Authorization": "Bearer "+token} if token else {})
                try:
                    response = urllib.request.urlopen(req)
                except urllib.error.HTTPError as exc: response = exc
                with response:
                    self.assertEqual(response.status, expected)
                    body = json.loads(response.read())
                    if expected == 200: validate_response(body)
            self.manager.roster_path.unlink()
            req = urllib.request.Request(server.url+"/api/v1/tower/project-index", headers={"Authorization": "Bearer fixture-token"})
            with self.assertRaises(urllib.error.HTTPError) as caught: urllib.request.urlopen(req)
            self.assertEqual(caught.exception.code, 503)
            caught.exception.close()
        finally:
            server.stop()
