"""Ephemeral loopback integration; fake dispatch only, no installed host access."""
import contextlib
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import time
import threading
from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import patch

from docket.cli import main
from docket.clock import FakeClock
from docket.scheduler import Scheduler
from docket.subscription_api import SubscriptionServer
from docket.subscription_worker import EventStore, EventWorker
from tests.support import FakeAdapter, utc
from tests.test_subscription_api import ADMIN, manifest, write_config
from tests.test_subscription_worker import Adapter
from tests.test_subscriptions import TOKEN, event


class RuntimeHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = EventStore(self.root / "db")
        self.store.install_manifest(manifest())
        self.adapter = Adapter()
        self.worker = EventWorker(self.store, self.adapter)
        self.scheduler = Scheduler(self.store, FakeAdapter(), FakeClock(utc(2026, 10, 22, 12, 0, 0)), event_worker=self.worker)
        self.server = SubscriptionServer(self.store, self.scheduler, ADMIN, port=0, enabled=True)
        self.server.serve_in_thread()

    def tearDown(self):
        self.server.stop()
        self.server._thread.join(timeout=5)
        self.store.close()
        self.tmp.cleanup()

    def request(self, method, path, token=ADMIN, data=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            connection.request(method, path, json.dumps(data) if data is not None else None,
                               {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_http_to_worker_redacted_operator_inspect(self):
        status, result = self.request("POST", "/api/v1/events", TOKEN, event(text="private-payload-marker"))
        self.assertEqual(status, 202)
        delivery = result["delivery"]
        self.assertEqual(delivery["status"], "accepted")
        self.worker.tick(time.time() + 1)
        path = "/api/v1/events/" + delivery["id"]
        self.assertEqual(self.request("GET", path, TOKEN)[0], 401)
        status, body = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(body["events"][0]["phase"], "admitted")
        self.assertNotIn("private-payload-marker", json.dumps(body))
        self.assertEqual(self.request("GET", "/api/v1/events/deadletters")[1]["events"], [])
        self.assertEqual(self.request("GET", "/api/v1/events/missing")[0], 404)

    def test_http_rate_refusal_before_json_and_dedup_preserved(self):
        original = self.store.accept(TOKEN, event())
        for _ in range(120):
            self.store.rate_request("fixture")
        self.assertEqual(self.request("POST", "/api/v1/events", TOKEN, event())[0], 429)
        # Time-window backpressure does not delete or replace the original intent.
        self.assertEqual(self.store.accept(TOKEN, event())["admission_id"], original["admission_id"])

    def test_scheduler_runs_event_tick_after_ordinary_work(self):
        calls = []
        with patch.object(self.scheduler.checkins, "tick", side_effect=lambda now: calls.append("ordinary")), patch.object(self.worker, "scheduler_tick", side_effect=lambda now: calls.append("events") or []):
            self.scheduler.tick()
        self.assertEqual(calls, ["ordinary", "events"])

    def test_public_pi_adapter_and_event_worker_coexist_without_fallback(self):
        pi = object()
        scheduler = Scheduler(self.store, self.scheduler.adapter, self.scheduler.clock,
                              pi_adapter=pi, event_worker=self.worker)
        self.assertIs(scheduler.adapter_for_target({"adapter": "pi"}), pi)
        self.assertIs(scheduler.adapter_for_target({"adapter": "opencode"}), self.scheduler.adapter)
        calls = []
        with patch.object(scheduler.checkins, "tick", side_effect=lambda now: calls.append("ordinary")), patch.object(self.worker, "scheduler_tick", side_effect=lambda now: calls.append("events") or []):
            scheduler.tick()
        self.assertEqual(calls, ["ordinary", "events"])
        self.assertIs(scheduler._pi_adapter, pi)

    def test_real_client_authenticates_event_get_without_changing_health(self):
        from docket.client import request, ClientError
        with patch.dict("os.environ", {"DOCKET_URL": self.server.url, "DOCKET_TOKEN": ADMIN}):
            self.assertEqual(request("GET", "/api/v1/events")[0], 200)
            self.assertEqual(request("GET", "/api/v1/events/deadletters")[0], 200)
        with patch("docket.client.resolve_token", side_effect=AssertionError("health must stay public")), patch.dict("os.environ", {"DOCKET_URL": self.server.url}):
            self.assertEqual(request("GET", "/health")[0], 200)
        with patch.dict("os.environ", {"DOCKET_URL": self.server.url, "DOCKET_TOKEN": TOKEN}):
            with self.assertRaises(ClientError) as refusal:
                request("GET", "/api/v1/events")
            self.assertEqual(refusal.exception.status, 401)

    def test_slow_event_adapter_does_not_delay_ordinary_timer(self):
        entered, release = threading.Event(), threading.Event()
        prepare = self.adapter.prepare
        def slow(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return prepare(*args)
        self.adapter.prepare = slow
        self.store.accept(TOKEN, event())
        self.scheduler.clock = FakeClock(datetime.now(timezone.utc) + timedelta(seconds=1))
        try:
            self.scheduler.tick()
            self.assertTrue(entered.wait(5))
            status, created = self.request("POST", "/api/v1/timers", data={
                "in": "1s", "reason": "ordinary fixture", "session_id": "ses_knownsession0001"})
            self.assertEqual(status, 201)
            self.scheduler.clock.advance(2)
            started = time.monotonic()
            self.scheduler.tick()
            self.assertLess(time.monotonic() - started, 1)
            self.assertTrue(self.scheduler.adapter.sent)
        finally:
            release.set()
            self.assertTrue(self.worker.close())


class EventCliTests(unittest.TestCase):
    def test_list_inspect_deadletter_routes(self):
        for args, path in ((["list"], "/api/v1/events"), (["deadletters"], "/api/v1/events/deadletters"), (["inspect", "evd_test"], "/api/v1/events/evd_test")):
            with patch("docket.cli.request", return_value=(200, {"events": []})) as request, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["events", *args, "--json"]), 0)
                request.assert_called_once_with("GET", path)

    def test_trusted_native_install_is_offline_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest()
            value["version"] = 2
            value["subscription"]["profile"] = {"action": "owner", "baseline": {"version": "fixture"}, "profile": {"fields": ["summary"]}}
            path = root / "manifest.json"
            write_config(path, value)
            args = ["events", "install", "--config", str(path), "--database", str(root / "db")]
            with patch("docket.cli.request", side_effect=AssertionError("no HTTP install")), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
                self.assertEqual(main(args), 0)
            store = EventStore(root / "db")
            try:
                self.assertEqual(store.accept(TOKEN, event())["status"], "accepted")
                self.assertTrue(store.configured())
            finally:
                store.close()

    def test_daemon_events_fail_closed_without_both_configs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"DOCKET_HOME": tmp}), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["serve", "--events-config", str(Path(tmp) / "missing")]), 1)
            self.assertFalse((Path(tmp) / "docket.db").exists())


if __name__ == "__main__":
    unittest.main()
