"""Clock-only cut: event intake is absent, and preexisting event rows stay put.

No live bus, token file, native host, or service. Ordinary Store opens a
database that already has dummy event tables; it must not drop them.
"""

from __future__ import annotations

import http.client
import importlib
import inspect
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from docket.api import DocketServer
from docket.client import ClientError, request
from docket.clock import FakeClock
from docket.mcp_server import TOOLS, _handle
from docket.scheduler import Scheduler
from docket.store import Store
from tests.support import FakeAdapter, utc

ROOT = Path(__file__).resolve().parents[1]
REMOVED_MODULES = (
    "docket.event_host",
    "docket.event_journal",
    "docket.subscription_admission",
    "docket.subscription_api",
    "docket.subscription_worker",
    "docket.subscriptions",
    "docket.wake_profiles",
    "docket.sandbox",
    "docket.sandbox_native",
    "docket.toolstack",
    "docket.toolstack_installed",
)
REMOVED_FILES = (
    "docket/event_host.py",
    "docket/event_journal.py",
    "docket/subscription_admission.py",
    "docket/subscription_api.py",
    "docket/subscription_worker.py",
    "docket/subscriptions.py",
    "docket/wake_profiles.py",
    "docket/sandbox.py",
    "docket/sandbox_native.py",
    "docket/toolstack.py",
    "docket/toolstack_installed.py",
    "integrations/opencode/index.js",
    "integrations/opencode/index.mjs",
    "integrations/opencode/package.json",
    "integrations/opencode/docket-events.mjs",
    "integrations/opencode/sandbox-gateway.py",
    "integrations/opencode/sandbox-native-runner.py",
    "integrations/opencode/sandbox-test-server.py",
    "integrations/opencode/sandbox-owned-http-tool.py",
    "integrations/opencode/sandbox-fixture/index.js",
    "integrations/opencode/sandbox-toolstack/index.js",
    "integrations/toolstack/event-journal/toolyard.toml",
)
EVENT_TABLES = (
    "subscription_publishers",
    "event_subscriptions",
    "subscription_events",
    "subscription_deliveries",
    "subscription_admissions",
    "subscription_runtime",
    "subscription_handler_terminals",
    "docket_event_outcomes",
)
CLOCK_REQUIRED = (
    "docket_timer",
    "docket_alarm",
    "docket_recurring",
    "docket_watch",
    "docket_list",
    "docket_occurrences",
    "docket_cancel",
)
BUS_ENV = ("DOCKET_TOWER_NATS_URL", "DOCKET_TOWER_TOKEN_FILE")


def _seed(path: Path) -> None:
    db = sqlite3.connect(path)
    try:
        for name in EVENT_TABLES:
            db.execute(f"CREATE TABLE {name} (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute(
                f"INSERT INTO {name} (id, payload) VALUES (?, ?)",
                (name, "preexisting-row"),
            )
        db.commit()
    finally:
        db.close()


def _event_rows(path: Path) -> dict:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        present = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        rows = {}
        for name in EVENT_TABLES:
            if name not in present:
                rows[name] = None
                continue
            rows[name] = db.execute(f"SELECT id, payload FROM {name} ORDER BY id").fetchall()
        clock = {
            "items": db.execute("SELECT COUNT(*) FROM items").fetchone()[0] if "items" in present else None,
            "occurrences": db.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0] if "occurrences" in present else None,
        }
        return {"tables": present, "rows": rows, "clock": clock}
    finally:
        db.close()


class AbsenceTests(unittest.TestCase):
    def test_event_modules_and_entrypoints_are_gone(self):
        for name in REMOVED_MODULES:
            with self.assertRaises(ModuleNotFoundError):
                importlib.import_module(name)
        for relative in REMOVED_FILES:
            self.assertFalse((ROOT / relative).exists(), relative)
        for name in (
            "docket.clock",
            "docket.scheduler",
            "docket.store",
            "docket.api",
            "docket.cli",
            "docket.checkins",
            "docket.milestone",
            "docket.pairing",
            "docket.tower_publish",
            "docket.delivery_stall",
        ):
            importlib.import_module(name)

    def test_cli_has_no_events_command_or_flags(self):
        help_text = _cli(["--help"])
        self.assertEqual(help_text.returncode, 0, help_text.stderr)
        self.assertIn("timer", help_text.stdout)
        self.assertNotIn("events", help_text.stdout)
        self.assertNotIn("--events-config", help_text.stdout)
        self.assertNotIn("--event-host-config", help_text.stdout)
        self.assertNotIn("--event-journal-config", help_text.stdout)
        rejected = (
            ["events"],
            ["events", "list"],
            ["events", "install", "--config", "/tmp/nope", "--database", "/tmp/nope.db"],
            ["serve", "--events-config", "/tmp/nope"],
            ["daemon", "--event-host-config", "/tmp/nope"],
            ["serve", "--event-journal-config", "/tmp/nope"],
        )
        for args in rejected:
            done = _cli(args)
            self.assertEqual(done.returncode, 2, args)
            self.assertNotIn("listening", done.stdout)
            self.assertNotIn("listening", done.stderr)

    def test_mcp_tools_list_is_clock_only(self):
        listed = _handle("tools/list", {})["tools"]
        names = tuple(tool["name"] for tool in listed)
        self.assertEqual(names, tuple(tool["name"] for tool in TOOLS))
        self.assertTrue(set(CLOCK_REQUIRED) <= set(names))
        blob = json.dumps(listed)
        for word in ("event", "subscription", "journal", "sandbox", "toolstack", "wake_profile"):
            self.assertNotIn(word, blob)

    def test_scheduler_has_no_event_worker(self):
        signature = inspect.signature(Scheduler.__init__)
        self.assertNotIn("event_worker", signature.parameters)
        self.assertIn("pi_adapter", signature.parameters)
        self.assertNotIn("event_worker", inspect.getsource(Scheduler.tick))
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "docket.db")
            try:
                scheduler = Scheduler(store, FakeAdapter(), FakeClock(utc(2026, 10, 23, 12)))
                self.assertFalse(hasattr(scheduler, "event_worker"))
                self.assertTrue(hasattr(scheduler, "_pi_adapter"))
                self.assertIsNotNone(scheduler.tower)
                self.assertIsNotNone(scheduler.checkins)
            finally:
                store.close()


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "docket.db"
        _seed(self.path)
        self.before = _event_rows(self.path)
        self.store = Store(self.path)
        self.server = DocketServer(
            self.store, Scheduler(self.store, FakeAdapter(), FakeClock(utc(2026, 10, 23, 12))),
            token="clock-token", port=0,
        )
        self.server.serve_in_thread()
        self._old = {key: os.environ.get(key) for key in ("DOCKET_URL", "DOCKET_TOKEN", "DOCKET_TOKEN_FILE", *BUS_ENV)}
        os.environ["DOCKET_URL"] = self.server.url
        os.environ["DOCKET_TOKEN"] = "clock-token"
        os.environ["DOCKET_TOKEN_FILE"] = str(Path(self.tmp.name) / "missing-token")
        for key in BUS_ENV:
            os.environ.pop(key, None)

    def tearDown(self):
        self.server.stop()
        self.server.scheduler.tower.close()
        self.store.close()
        self.tmp.cleanup()
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_store_keeps_dummy_event_rows(self):
        opened = _event_rows(self.path)
        self.assertEqual(opened["rows"], self.before["rows"])
        for name in EVENT_TABLES:
            self.assertIn(name, opened["tables"])
            self.assertEqual(opened["rows"][name], [(name, "preexisting-row")])
        self.assertEqual(opened["clock"], {"items": 0, "occurrences": 0})

    def test_removed_paths_404_without_auth_or_replay(self):
        host, port = self.server.server_address[:2]
        bodies = (
            ("GET", "/api/v1/events", None, {}),
            ("GET", "/api/v1/events/deadletters", None, {"Authorization": "Bearer producer-token"}),
            ("GET", "/api/v1/events/outcomes", None, {"Authorization": "Bearer clock-token"}),
            ("POST", "/api/v1/events", {"id": "evt-old", "type": "fixture.completed"}, {"Authorization": "Bearer producer-token"}),
            ("POST", "/api/v1/events?replay=1", {"id": "evt-old"}, {"Authorization": "Bearer clock-token"}),
            ("POST", "/v1/actions/record", {"arguments": {"text": "old"}}, {"X-Toolstack-Secret": "channel-secret"}),
        )
        for method, path, body, headers in bodies:
            status, payload = _raw(host, port, method, path, body, headers)
            self.assertEqual(status, 404, path)
            self.assertEqual(json.loads(payload)["error"]["code"], "not_found")
        status, _payload = _raw(host, port, "POST", "/api/v1/timers", {
            "in": 30, "reason": "still needs a token", "session_id": "ses_knownsession0001",
        }, {})
        self.assertEqual(status, 401)
        os.environ.pop("DOCKET_TOKEN", None)
        with self.assertRaises(ClientError) as caught:
            request("GET", "/api/v1/events/evt-old")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "not_found")
        with self.assertRaises(ClientError) as caught:
            request("POST", "/api/v1/events", {"id": "evt-old"})
        self.assertIsNone(caught.exception.status)
        self.assertIn("token", caught.exception.detail.lower())
        after = _event_rows(self.path)
        self.assertEqual(after["rows"], self.before["rows"])
        self.assertEqual(after["clock"], {"items": 0, "occurrences": 0})


def _cli(args):
    env = os.environ.copy()
    for key in (*BUS_ENV, "DOCKET_URL", "DOCKET_TOKEN", "DOCKET_TOKEN_FILE"):
        env.pop(key, None)
    env["PYTHONPATH"] = str(ROOT)
    with tempfile.TemporaryDirectory() as home:
        env["DOCKET_HOME"] = home
        return subprocess.run(
            [sys.executable, "-B", "-m", "docket", *args],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=15,
        )


def _raw(host, port, method, path, body, headers):
    raw = None if body is None else json.dumps(body).encode()
    hdrs = {"Host": "127.0.0.1", "Accept": "application/json"}
    hdrs.update(headers)
    if raw is not None:
        hdrs["Content-Type"] = "application/json"
        hdrs["Content-Length"] = str(len(raw))
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path, body=raw, headers=hdrs)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


if __name__ == "__main__":
    unittest.main()
