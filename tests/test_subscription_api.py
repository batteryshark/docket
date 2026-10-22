"""Local ephemeral HTTP server, temp DB/config only; scheduler never started."""
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from docket.api import DocketServer
from docket.clock import FakeClock
from docket.scheduler import Scheduler
from docket.store import Store
from docket.subscription_api import SubscriptionServer, load_installed
from docket.subscriptions import SubscriptionRefusal, SubscriptionStore
from tests.support import FakeAdapter, utc
from tests.test_subscriptions import TOKEN, config, event

ADMIN = "fixture-administrative-credential-" + "a" * 32


def manifest():
    return {"version": 1, "publisher": {"id": "fixture", "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest()},
            "subscription": {"id": "sub-1", "event_type": "fixture.completed", "profile": config()}}


def write_config(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


class InstalledConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = SubscriptionStore(self.root / "db")
        self.config_path = self.root / "installed.json"
        write_config(self.config_path, manifest())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_atomic_install_reopen_and_no_reactivation(self):
        self.store._db.execute("CREATE TRIGGER fail_sub BEFORE INSERT ON event_subscriptions BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        self.store._db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            load_installed(self.store, self.config_path)
        self.assertFalse(self.store.configured())
        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM subscription_publishers").fetchone()[0], 0)
        self.store._db.execute("DROP TRIGGER fail_sub")
        self.store._db.commit()
        load_installed(self.store, self.config_path)
        self.store.revoke(publisher_id="fixture")
        self.store.close()
        self.store = SubscriptionStore(self.root / "db")
        load_installed(self.store, self.config_path)
        with self.assertRaises(SubscriptionRefusal):
            self.store.accept(TOKEN, event())

    def test_private_regular_file_and_strict_version(self):
        self.config_path.chmod(0o644)
        with self.assertRaises(SubscriptionRefusal):
            load_installed(self.store, self.config_path)
        self.config_path.chmod(0o600)
        link = self.root / "link"
        link.symlink_to(self.config_path)
        with self.assertRaises(SubscriptionRefusal):
            load_installed(self.store, link)
        for version in (True, 2, "1"):
            value = manifest()
            value["version"] = version
            write_config(self.config_path, value)
            with self.assertRaises(SubscriptionRefusal):
                load_installed(self.store, self.config_path)
        self.assertFalse(self.store.configured())

    def test_replacement_and_raw_secret_fields_refused(self):
        load_installed(self.store, self.config_path)
        value = manifest()
        value["subscription"]["profile"]["profile_version"] = "2"
        write_config(self.config_path, value)
        with self.assertRaisesRegex(SubscriptionRefusal, "explicit upgrade"):
            load_installed(self.store, self.config_path)
        value = manifest()
        value["publisher"]["token"] = TOKEN
        write_config(self.config_path, value)
        with self.assertRaises(SubscriptionRefusal):
            load_installed(self.store, self.config_path)

    def test_duplicate_config_keys_refused(self):
        self.config_path.write_text('{"version":1,"version":1}')
        with self.assertRaises(SubscriptionRefusal):
            load_installed(self.store, self.config_path)


class SubscriptionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = SubscriptionStore(self.root / "db")
        self.config_path = self.root / "installed.json"
        write_config(self.config_path, manifest())
        load_installed(self.store, self.config_path)
        self.adapter = FakeAdapter()
        self.scheduler = Scheduler(self.store, self.adapter, clock=FakeClock(utc(2026, 10, 22, 12, 0, 0)))
        self.server = SubscriptionServer(self.store, self.scheduler, ADMIN, port=0, enabled=True)
        self.server.serve_in_thread()

    def tearDown(self):
        self.server.stop()
        self.server._thread.join(timeout=5)
        self.assertEqual(self.adapter.sent, [])
        self.store.close()
        self.tmp.cleanup()

    def request(self, data=None, *, path="/api/v1/events", token=TOKEN, raw=None, headers=None, method="POST"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        body = raw if raw is not None else json.dumps(data if data is not None else event())
        fields = {"Content-Type": "application/json"}
        if token is not None:
            fields["Authorization"] = "Bearer " + token
        fields.update(headers or {})
        try:
            connection.request(method, path, body=body, headers=fields)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def count(self):
        return self.store._db.execute("SELECT COUNT(*) FROM subscription_events").fetchone()[0]

    def test_durable_acceptance_dedupe_conflict(self):
        status, first = self.request()
        self.assertEqual(status, 202)
        self.assertEqual(first["delivery"]["status"], "accepted")
        with sqlite3.connect(self.root / "db") as observer:
            self.assertEqual(observer.execute("SELECT COUNT(*) FROM subscription_deliveries").fetchone()[0], 1)
        self.assertEqual(self.request(), (202, first))
        status, error = self.request(event(text="changed"))
        self.assertEqual((status, error["error"]["code"]), (409, "conflict"))
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.store.list(), [])

    def test_auth_is_independent_of_malformed_event(self):
        for token in (None, "short", ADMIN, "z" * 40):
            with self.subTest(token_kind="missing" if token is None else "invalid"):
                status, body = self.request(token=token, raw="not JSON")
                self.assertEqual(status, 401)
                self.assertEqual(body["error"]["code"], "unauthorized")
        self.assertEqual(self.request(raw="not JSON")[0], 400)
        self.assertEqual(self.request({**event(), "type": "another.source"})[0], 403)
        self.assertEqual(self.count(), 0)

    def test_forbidden_selectors_and_strict_json(self):
        for key in ("model", "role", "session", "profile", "permissions", "publisher", "budget"):
            for data in ({**event(), key: "untrusted"},
                         {**event(), "payload": {"summary": "result", key: "untrusted"}}):
                self.assertEqual(self.request(data)[0], 400)
        for raw in ('[]', '{"id":"a","id":"b"}', '{"id":NaN}',
                    '{"id":"x","type":"fixture.completed","payload":{"summary":"\\ud800"}}'):
            self.assertEqual(self.request(raw=raw)[0], 400)
        self.assertEqual(self.count(), 0)

    def test_body_framing_boundary_and_sizes(self):
        self.assertEqual(self.request(raw="x" * 16385)[0], 413)
        self.assertEqual(self.request(headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request(headers={"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.request(headers={"Origin": "https://untrusted.invalid"})[0], 403)
        self.assertEqual(self.request(headers={"Content-Length": "-1"})[0], 400)
        self.assertEqual(self.count(), 0)

    def test_capacity_backpressure_without_eviction(self):
        # Seed through candidate store, then verify actual HTTP semantics at the boundary.
        for index in range(100):
            self.store.accept(TOKEN, event(str(index)))
        status, body = self.request(event("overflow"))
        self.assertEqual((status, body["error"]["code"]), (429, "capacity"))
        self.assertEqual(self.request(event("0"))[0], 202)
        self.assertEqual(self.count(), 100)

    def test_failed_commit_is_not_acknowledged(self):
        with patch.object(self.store, "_commit", side_effect=sqlite3.OperationalError("fixture failure")):
            status, body = self.request()
        self.assertEqual((status, body["error"]["code"]), (503, "unavailable"))
        self.assertNotIn("fixture failure", json.dumps(body))
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.request()[0], 202)

    def test_committed_but_unconfirmed_retry_reuses_original_delivery(self):
        commit = self.store._commit
        def committed_then_error():
            commit()
            raise OSError("fixture lost confirmation")
        with patch.object(self.store, "_commit", side_effect=committed_then_error):
            self.assertEqual(self.request()[0], 503)
        self.assertEqual(self.count(), 1)
        original_id = self.store._db.execute("SELECT admission_id FROM subscription_deliveries").fetchone()[0]
        status, reply = self.request()
        self.assertEqual(status, 202)
        self.assertEqual(reply["delivery"]["admission_id"], original_id)
        self.assertEqual(self.count(), 1)

    def test_revocation_is_rechecked_and_no_http_admin_install(self):
        self.store.revoke(subscription_id="sub-1")
        self.assertEqual(self.request()[0], 403)
        self.store.revoke(publisher_id="fixture")
        self.assertEqual(self.request()[0], 401)
        self.assertEqual(self.request(manifest(), path="/api/v1/subscriptions", token=ADMIN)[0], 404)

    def test_producer_cannot_use_admin_routes_and_normal_routes_unchanged(self):
        self.assertEqual(self.request({}, path="/api/v1/timers")[0], 401)
        self.assertEqual(self.request({}, path="/api/v1/timers", token=ADMIN)[0], 400)
        status, meta = self.request(path="/api/v1/meta", method="GET", token=None)
        self.assertEqual(status, 200)
        self.assertIn("timer", meta["kinds"])
        self.assertIn("alarm", meta["kinds"])
        self.assertIn("watch", meta["kinds"])
        for kind, when in (("timers", {"in": "20m"}),
                           ("alarms", {"at": "2026-10-23T12:00:00+00:00"})):
            status, created = self.request({**when, "reason": "fixture compatibility",
                                           "session_id": "ses_knownsession0001"},
                                          path=f"/api/v1/{kind}", token=ADMIN)
            self.assertEqual(status, 201)
            self.assertEqual(created["item"]["status"], "pending")
        self.assertEqual(self.count(), 0)

    def test_disabled_default_and_normal_server_have_no_intake(self):
        with tempfile.TemporaryDirectory() as temp:
            plain = Store(Path(temp) / "db")
            for server_class, expected in ((SubscriptionServer, 503), (DocketServer, 404)):
                server = server_class(plain, self.scheduler, ADMIN, port=0)
                server.serve_in_thread()
                original = self.server
                try:
                    self.server = server
                    self.assertEqual(self.request(token=ADMIN)[0], expected)
                finally:
                    self.server = original
                    server.stop()
                    server._thread.join(timeout=5)
            self.assertIsNone(plain._db.execute("SELECT name FROM sqlite_master WHERE name='subscription_events'").fetchone())
            with self.assertRaises(SubscriptionRefusal):
                SubscriptionServer(plain, self.scheduler, ADMIN, port=0, enabled=True)
            plain.close()


if __name__ == "__main__":
    unittest.main()
