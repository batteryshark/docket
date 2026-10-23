"""Real Docket HTTP/CLI + unmodified Tool Stack wire, isolated DB/ports only."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from docket.clock import FakeClock
from docket.event_journal import EventJournal, PATH
from docket.scheduler import Scheduler
from docket.subscription_api import SubscriptionServer
from docket.subscription_worker import EventStore
from docket.subscriptions import SubscriptionRefusal
from tests.support import FakeAdapter, utc
from tests.test_subscription_api import ADMIN, manifest, write_config
from tests.test_subscriptions import TOKEN


class JournalHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.store = EventStore(self.root / "db")
        self.store.install_manifest(manifest())
        self.channel = "isolated-journal-channel-" + "j" * 32
        self.config = self.root / "channel.json"
        write_config(self.config, {"version": 1, "namespace": "isolated-broker-v1", "source": "isolated-summary",
            "channel_token_sha256": hashlib.sha256(self.channel.encode()).hexdigest()})
        self.journal = EventJournal(self.store.path.resolve(), self.config)
        self.adapter = FakeAdapter()
        self.server = SubscriptionServer(self.store, Scheduler(self.store, self.adapter, FakeClock(utc(2026, 10, 22, 12, 0, 0))),
            ADMIN, port=0, enabled=True, journal=self.journal)
        self.server.serve_in_thread()

    def tearDown(self):
        self.server.stop(); self.server._thread.join(timeout=5)
        self.assertFalse(self.server._thread.is_alive())
        self.assertEqual(self.adapter.sent, [])
        self.store.close(); self.tmp.cleanup()

    def envelope(self):
        return {"arguments": {"source": "isolated-summary", "text": "bounded outcome <untrusted>"},
                "broker_request_id": 1, "caller": {"name": "docket-" + "a" * 32}}

    def request(self, method, path, *, token=None, channel=None, data=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
        values = {"Content-Type": "application/json", **(headers or {})}
        if token is not None:
            values["Authorization"] = "Bearer " + token
        if channel is not None:
            values["X-Toolstack-Secret"] = channel
        try:
            connection.request(method, path, json.dumps(data) if data is not None else None, values)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_separate_write_auth_protected_listing_and_ordinary_timer(self):
        for token in (None, ADMIN, TOKEN):
            self.assertEqual(self.request("POST", PATH, token=token, data=self.envelope())[0], 401)
        self.assertEqual(self.request("POST", PATH, channel=TOKEN, data=self.envelope())[0], 401)
        status, receipt = self.request("POST", PATH, channel=self.channel, data=self.envelope())
        self.assertEqual((status, receipt["status"]), (200, "recorded"))
        self.assertEqual(self.request("POST", PATH, channel=self.channel, data=self.envelope()), (status, receipt))
        self.assertEqual(self.request("GET", PATH, channel=self.channel)[0], 405)
        for token in (None, TOKEN, self.channel):
            self.assertEqual(self.request("GET", "/api/v1/events/outcomes", token=token, channel=self.channel)[0], 401)
        status, rows = self.request("GET", "/api/v1/events/outcomes", token=ADMIN)
        self.assertEqual(status, 200)
        self.assertEqual(len(rows["outcomes"]), 1)
        self.assertEqual(rows["outcomes"][0]["text"], "bounded outcome <untrusted>")
        self.assertNotIn(self.channel, json.dumps(rows))
        self.assertEqual(self.request("GET", "/api/v1/events/outcomes?limit=101", token=ADMIN)[0], 400)
        self.assertEqual(self.request("GET", "/api/v1/events/outcomes?before_id=0", token=ADMIN)[0], 400)
        status, item = self.request("POST", "/api/v1/timers", token=ADMIN,
            data={"session_id": "ses_knownsession0001", "in": "20m", "reason": "ordinary unchanged"})
        self.assertEqual(status, 201)
        self.assertEqual(self.store.get(item["item"]["id"]).status, "pending")

    def test_preflight_rejects_before_body_read_and_duplicate_headers(self):
        # No body is sent for an oversized declaration: this must immediately
        # reject rather than waiting on the socket or reaching handle().
        with patch.object(self.journal, "handle", side_effect=AssertionError("body must not be parsed")):
            self.assertEqual(self.request("POST", PATH, token=ADMIN, headers={"Content-Length": "8192"})[0], 401)
            self.assertEqual(self.request("POST", PATH, channel=self.channel, headers={"Content-Length": "999999"})[0], 413)
            for name in ("Content-Length", "Content-Type", "X-Toolstack-Secret"):
                connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
                try:
                    connection.putrequest("POST", PATH)
                    values = {"Content-Length": "3", "Content-Type": "application/json", "X-Toolstack-Secret": self.channel}
                    for key, value in values.items():
                        connection.putheader(key, value)
                        if key == name:
                            connection.putheader(key, value)
                    connection.endheaders()
                    response = connection.getresponse()
                    self.assertEqual(response.status, 400); response.read()
                finally:
                    connection.close()
        self.assertEqual(self.journal.list_outcomes(), [])

    def test_actual_cli_outcomes_uses_admin_file_and_json_display(self):
        self.request("POST", PATH, channel=self.channel, data=self.envelope())
        token = self.root / "admin.token"; token.write_text(ADMIN); token.chmod(0o600)
        env = {**os.environ, "DOCKET_URL": self.server.url, "DOCKET_TOKEN_FILE": str(token), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("DOCKET_TOKEN", None)
        result = subprocess.run([sys.executable, "-B", "-m", "docket", "events", "outcomes", "--json"],
            cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["outcomes"]), 1)

    @unittest.skipUnless(os.environ.get("TOOLSTACK_SOURCE"), "explicit unmodified Tool Stack required")
    def test_real_http_runtime_registry_toolstate_to_docket_server(self):
        sys.path.insert(0, os.environ["TOOLSTACK_SOURCE"])
        from broker.registry import Registry
        from broker.runtime import HttpRuntime
        from broker.tool_state import ToolState
        tool = self.root / "registry/docket"; tool.mkdir(parents=True)
        descriptor = Path(__file__).resolve().parents[1] / "integrations/toolstack/event-journal/toolyard.toml"
        (tool / "toolyard.toml").write_text(descriptor.read_text().replace("port = 8768", "port = " + str(self.server.server_address[1])))
        state = self.root / "toolstate.json"; write_config(state, {"docket": {"e_secret": self.channel}})
        runtime = HttpRuntime(timeout=2, tool_secret=ToolState(state).e_secret_for)
        operation = Registry.from_sources(str(self.root / "registry"), ()).lookup("docket", "record")
        args = self.envelope()["arguments"]
        first = runtime.execute(operation, args, 99, "docket-" + "b" * 32)
        self.assertEqual(first["status"], "recorded")
        self.assertEqual(runtime.execute(operation, args, 99, "docket-" + "b" * 32), first)
        self.assertEqual(len(self.journal.list_outcomes()), 1)

    def test_journal_cannot_be_bound_to_different_database(self):
        other = EventJournal(self.root / "other.db", self.config)
        with self.assertRaises(SubscriptionRefusal):
            SubscriptionServer(self.store, self.server.scheduler, ADMIN, port=0, enabled=True, journal=other)


class JournalCliTests(unittest.TestCase):
    def test_journal_flag_is_explicit_and_requires_events(self):
        import contextlib
        import io
        from docket.cli import main
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"DOCKET_HOME": temporary}), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["serve", "--event-journal-config", str(Path(temporary) / "channel.json")]), 1)
            self.assertFalse((Path(temporary) / "docket.db").exists())
        with patch("docket.cli._serve", return_value=0) as serve:
            self.assertEqual(main(["serve", "--events-config", "/private/manifest", "--event-host-config", "/private/host", "--event-journal-config", "/private/journal"]), 0)
            self.assertEqual(serve.call_args.kwargs["event_journal_config"], "/private/journal")


if __name__ == "__main__":
    unittest.main()
