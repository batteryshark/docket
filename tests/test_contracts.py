"""HTTP, CLI, MCP, and OpenCode delivery contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from docket.api import DocketServer
from docket.client import request
from docket.clock import FakeClock
from docket.delivery import OpenCodeAdapter
from docket.scheduler import Scheduler
from docket.store import Store
from tests.support import FakeAdapter, RecordingOpenCode, utc

ROOT = Path(__file__).resolve().parents[1]
PY = os.environ.get("DOCKET_PYTHON", sys.executable)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "docket.db")
        self.adapter = FakeAdapter()
        self.clock = FakeClock(utc(2026, 10, 16, 12, 0, 0))
        self.server = DocketServer(
            self.store, Scheduler(self.store, self.adapter, self.clock),
            token="test-token", port=0,
        )
        self.server.serve_in_thread()
        self._old = os.environ.get("DOCKET_URL"), os.environ.get("DOCKET_TOKEN")
        os.environ["DOCKET_URL"] = self.server.url
        os.environ["DOCKET_TOKEN"] = "test-token"

    def tearDown(self):
        self.server.stop()
        self.store.close()
        self.tmp.cleanup()
        for key, value in zip(("DOCKET_URL", "DOCKET_TOKEN"), self._old):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_timer_list_cancel_and_reserved_kind(self):
        status, created = request("POST", "/api/v1/timers", {
            "in": "20m",
            "reason": "check the download",
            "session_id": "ses_knownsession0001",
            "created_by": "ses_setter",
        })
        self.assertEqual(status, 201)
        item_id = created["item"]["id"]
        self.assertEqual(created["item"]["kind"], "timer")
        _status, listed = request("GET", "/api/v1/items?status=pending")
        self.assertEqual([item["id"] for item in listed["items"]], [item_id])
        request("POST", f"/api/v1/items/{item_id}/cancel", {})
        _status, after = request("GET", "/api/v1/items?status=pending")
        self.assertEqual(after["items"], [])
        _status, cancelled = request("GET", "/api/v1/items?status=cancelled")
        self.assertEqual(cancelled["items"][0]["id"], item_id)

    def test_rejects_bad_archived_wrong_and_unimplemented(self):
        self.adapter.archived.add("ses_archived0001")
        self.adapter.known.add("ses_archived0001")
        self.adapter.known.add("ses_here0001")
        self.adapter.directories["ses_here0001"] = "/real"
        bad = _post({
            "in": 5, "reason": "x", "session_id": "ses_missing0001",
        })
        self.assertEqual(bad["error"]["code"], "bad_target")
        archived = _post({
            "in": 5, "reason": "x", "session_id": "ses_archived0001",
        })
        self.assertEqual(archived["error"]["code"], "archived")
        wrong = _post({
            "in": 5, "reason": "x", "session_id": "ses_here0001",
            "directory": "/other",
        })
        self.assertEqual(wrong["error"]["code"], "wrong_target")
        malformed = _post({"in": 5, "reason": "x", "session_id": "nope"})
        self.assertEqual(malformed["error"]["code"], "bad_target")
        from docket.client import ClientError
        with self.assertRaises(ClientError) as caught:
            request("POST", "/api/v1/items", {
                "kind": "watch", "reason": "x", "session_id": "ses_knownsession0001",
            })
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.code, "invalid")
        with self.assertRaises(ClientError) as caught:
            request("POST", "/api/v1/items", {
                "kind": "nope", "reason": "x", "session_id": "ses_knownsession0001",
            })
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.store.list(), [])

    def test_alarm_and_health_are_loopback_json(self):
        status, health = request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        status, created = request("POST", "/api/v1/alarms", {
            "at": "2026-10-16T18:00:00+00:00",
            "reason": "afternoon check",
            "session_id": "ses_knownsession0001",
        })
        self.assertEqual(status, 201)
        self.assertEqual(created["item"]["kind"], "alarm")

    def test_mutation_requires_token(self):
        os.environ["DOCKET_TOKEN"] = "wrong"
        with self.assertRaises(Exception) as caught:
            request("POST", "/api/v1/timers", {
                "in": 1, "reason": "x", "session_id": "ses_knownsession0001",
            })
        self.assertIn("bearer", str(caught.exception).lower())


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "docket.db")
        self.adapter = FakeAdapter()
        self.server = DocketServer(
            self.store, Scheduler(self.store, self.adapter, FakeClock(utc(2026, 10, 16, 12))),
            token="cli-token", port=0,
        )
        self.server.serve_in_thread()
        self.env = os.environ.copy()
        self.env["DOCKET_URL"] = self.server.url
        self.env["DOCKET_TOKEN"] = "cli-token"
        self.env["PYTHONPATH"] = str(ROOT)

    def tearDown(self):
        self.server.stop()
        self.store.close()
        self.tmp.cleanup()

    def test_timer_list_cancel(self):
        created = self._run([
            "timer", "--in", "20m", "--session", "ses_knownsession0001",
            "--reason", "check the file", "--json",
        ])
        item_id = json.loads(created.stdout)["item"]["id"]
        listed = self._run(["list", "--json"])
        self.assertIn(item_id, listed.stdout)
        cancelled = self._run(["cancel", item_id])
        self.assertEqual(cancelled.returncode, 0)
        after = self._run(["list", "--json"])
        self.assertEqual(json.loads(after.stdout)["items"], [])

    def test_recurring_and_watch(self):
        created = self._run([
            "recurring", "--every", "15m", "--count", "2",
            "--session", "ses_knownsession0001", "--reason", "check again", "--json",
        ])
        self.assertEqual(created.returncode, 0, created.stderr)
        item = json.loads(created.stdout)["item"]
        self.assertEqual(item["kind"], "recurring")
        watched = self._run([
            "watch", "--check", "file_appears", "--path", "/tmp/docket-m2-watch",
            "--until", "1h", "--session", "ses_knownsession0001",
            "--reason", "file shows up", "--json",
        ])
        self.assertEqual(watched.returncode, 0, watched.stderr)
        self.assertEqual(json.loads(watched.stdout)["item"]["spec"]["check"], "file_appears")
        rejected = self._run([
            "watch", "--check", "file_appears", "--path", "relative",
            "--until", "1h", "--session", "ses_knownsession0001", "--reason", "no",
        ])
        self.assertEqual(rejected.returncode, 1)
        listed = self._run(["occurrences", item["id"], "--json"])
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout)["occurrences"], [])

    def test_help_exits(self):
        done = self._run(["--help"])
        self.assertEqual(done.returncode, 0)
        self.assertIn("timer", done.stdout)

    def _run(self, args):
        return subprocess.run(
            [PY, "-m", "docket", *args],
            cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=15,
        )


class McpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "docket.db")
        self.adapter = FakeAdapter()
        self.server = DocketServer(
            self.store, Scheduler(self.store, self.adapter, FakeClock(utc(2026, 10, 16, 12))),
            token="mcp-token", port=0,
        )
        self.server.serve_in_thread()
        env = os.environ.copy()
        env["DOCKET_URL"] = self.server.url
        env["DOCKET_TOKEN"] = "mcp-token"
        env["PYTHONPATH"] = str(ROOT)
        self.proc = subprocess.Popen(
            [PY, "-m", "docket", "mcp"],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream:
                stream.close()
        self.server.stop()
        self.store.close()
        self.tmp.cleanup()

    def test_initialize_list_and_timer(self):
        init = self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "docket")
        tools = self._rpc("tools/list", {})
        names = [tool["name"] for tool in tools["result"]["tools"]]
        self.assertEqual(names, [
            "docket_timer", "docket_alarm", "docket_recurring", "docket_watch",
            "docket_list", "docket_occurrences", "docket_cancel",
        ])
        created = self._rpc("tools/call", {"name": "docket_timer", "arguments": {
            "session_id": "ses_knownsession0001",
            "in": "5m",
            "reason": "mcp check",
        }})
        text = created["result"]["content"][0]["text"]
        item = json.loads(text)["item"]
        self.assertEqual(item["reason"], "mcp check")
        cancelled = self._rpc("tools/call", {"name": "docket_cancel", "arguments": {"id": item["id"]}})
        self.assertIn("cancelled", cancelled["result"]["content"][0]["text"])
        created = self._rpc("tools/call", {"name": "docket_recurring", "arguments": {
            "session_id": "ses_knownsession0001",
            "every": "15m",
            "count": 2,
            "reason": "mcp recurring",
        }})
        self.assertFalse(created["result"].get("isError"))
        watch = self._rpc("tools/call", {"name": "docket_watch", "arguments": {
            "session_id": "ses_knownsession0001",
            "check": "url_answers",
            "url": "http://127.0.0.1:9/health",
            "until": "30m",
            "reason": "mcp watch",
        }})
        self.assertFalse(watch["result"].get("isError"))
        self.assertIn("url_answers", watch["result"]["content"][0]["text"])

    def _rpc(self, method, params):
        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode() + b"\n"
        self.proc.stdin.write(raw)
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        self.assertTrue(line, self.proc.stderr.read().decode() if self.proc.poll() else "no mcp response")
        return json.loads(line)


class OpenCodeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fake = RecordingOpenCode()
        self.fake.sessions["ses_live0001"] = {
            "id": "ses_live0001",
            "time": {"created": 1, "updated": 1},
            "location": {"directory": "/work/docket"},
        }
        self.fake.sessions["ses_archived0001"] = {
            "id": "ses_archived0001",
            "time": {"created": 1, "updated": 1, "archived": 123},
            "location": {"directory": "/work/docket"},
        }
        self.adapter = OpenCodeAdapter(discovery=lambda: (self.fake.url, "Basic dGVzdA=="))

    def tearDown(self):
        self.fake.stop()

    def test_default_queue_does_not_check_status_first(self):
        check = self.adapter.validate({"session_id": "ses_live0001", "directory": "/work/docket"})
        self.assertTrue(check.ok)
        self.adapter.calls.clear()
        self.fake.calls.clear()
        receipt = self.adapter.deliver(
            {"session_id": "ses_live0001"}, "Docket timer fired.\nItem: dkt_x", urgent=False,
        )
        self.assertEqual(receipt.delivery, "queue")
        methods = [call[0] + " " + call[1] for call in self.fake.calls]
        self.assertTrue(any(path.endswith("/prompt") and method == "POST" for method, path in (m.split(" ", 1) for m in methods)))
        posted = self.fake.calls[-1][2]
        self.assertEqual(posted["delivery"], "queue")
        self.assertTrue(posted["resume"])
        self.assertFalse(any("/active" in path for _method, path, *_rest in self.fake.calls))

    def test_urgent_steers(self):
        receipt = self.adapter.deliver(
            {"session_id": "ses_live0001"}, "urgent", urgent=True,
        )
        self.assertEqual(receipt.delivery, "steer")
        self.assertEqual(self.fake.calls[-1][2]["delivery"], "steer")

    def test_session_state_does_not_read_messages(self):
        self.fake.calls.clear()
        self.assertEqual(self.adapter.session_state("ses_live0001"), "idle")
        self.fake.active["ses_live0001"] = {"status": "busy"}
        self.assertEqual(self.adapter.session_state("ses_live0001"), "active")
        self.assertEqual(self.adapter.session_state("ses_missing0001"), "missing")
        self.assertEqual(self.adapter.session_state("ses_archived0001"), "archived")
        paths = [call[1] for call in self.fake.calls]
        self.assertTrue(paths)
        self.assertFalse(any("message" in path for path in paths))

    def test_archived_and_wrong_and_missing(self):
        archived = self.adapter.validate({"session_id": "ses_archived0001"})
        self.assertEqual(archived.code, "archived")
        wrong = self.adapter.validate({
            "session_id": "ses_live0001", "directory": "/somewhere/else",
        })
        self.assertEqual(wrong.code, "wrong_target")
        missing = self.adapter.validate({"session_id": "ses_missing0001"})
        self.assertEqual(missing.code, "bad_target")


def _post(body):
    try:
        _status, payload = request("POST", "/api/v1/timers", body)
        return payload
    except Exception as exc:
        return {"error": {"code": getattr(exc, "code", None), "detail": str(exc)}}


if __name__ == "__main__":
    unittest.main()
