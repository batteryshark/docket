"""Cancellation API -> real relay -> built panel, with an entirely fake host.

One serial, bounded headless browser job; no scheduler thread or live discovery.
Evidence is JSON only, under this checkout. No screenshot is produced.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from docket.api import DocketServer
from docket.clock import SystemClock
from docket.scheduler import Scheduler
from docket.store import Store
from tests.support import FakeAdapter
from tests import test_panel_relay as relay_helpers
from tests.test_panel_relay import TrapHandler, _kill, _line, _request
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]


class FeedbackHost(FakeAdapter):
    def reconcile(self, target, admission_id):
        if "unreadable" in admission_id:
            raise OSError("fake host unavailable")
        if "consumed" in admission_id:
            return "consumed"
        if "failed" in admission_id:
            return "admitted"
        return super().reconcile(target, admission_id)


class PanelFeedbackTest(unittest.TestCase):
    def test_outcomes_through_relay_and_browser(self):
        evidence_dir = ROOT / "evidence"
        evidence_dir.mkdir(exist_ok=True)
        evidence = {"live_calls": False, "scheduler_started": False, "relay": []}
        with tempfile.TemporaryDirectory(prefix="panel-feedback-", dir=evidence_dir) as temp:
            home = Path(temp)
            store = Store(home / "docket.db")
            host = FeedbackHost()
            api = DocketServer(store, Scheduler(store, host, SystemClock()), "fake-docket-token", "127.0.0.1", 0)
            thread = threading.Thread(target=api.serve_forever, daemon=True)
            thread.start()
            env = {key: value for key, value in os.environ.items() if not key.startswith(("DOCKET_", "OPENCHAMBER_SERVICE_"))}
            env.update(DOCKET_HOME=str(home), DOCKET_URL=api.url, DOCKET_TOKEN="fake-docket-token",
                       OPENCHAMBER_SERVICE_PORT="0", OPENCHAMBER_SERVICE_TOKEN="fake-relay-token")
            relay = None
            try:
                cases = [
                    ("pending", "none", 200, "removed", "Removed", "Removed.", False),
                    ("admitted", "retracted", 200, "removed", "Removed", "Removed.", False),
                    ("missing", "missing", 200, "missing", "Item stopped", "Item stopped, but an in-flight wake may still arrive.", False),
                    ("lost", "lost", 200, "lost", "Item stopped", "Item stopped; its earlier delivery could not be found.", False),
                    ("consumed", "consumed", 409, "consumed", "Already delivered", "Already delivered. This delivery was already consumed by the session.", False),
                    ("failed", "failed", 409, "retraction_failed", "Could not confirm removal", "Could not confirm removal. The host still holds this admission; the item remains outstanding.", True),
                    ("unreadable", "ambiguous", 503, "unreadable", "Could not confirm removal", "Could not confirm removal. Refresh to check outstanding items; retries may remain.", True),
                ]
                browser_cases = []
                now = datetime.now(timezone.utc)

                def seed(name, number):
                    item_id = f"dkt_{number:016x}"
                    aid = f"msg_feedback_{name}_{number}"
                    receipt = None if name == "pending" else {
                        "state": "sending" if name == "missing" else "admitted",
                        "admission_id": aid,
                    }
                    if name == "admitted":
                        host.inbox.add(aid)
                    row = relay_helpers.PanelRelayTest()._item(
                        item_id, "timer", "pending" if name == "pending" else "admitted",
                        now + timedelta(hours=1), now, reason=f"Feedback case {name}",
                        target={"adapter": "opencode", "session_id": "ses_fakefeedback001"},
                        receipt=receipt,
                    )
                    store.insert(row)
                    return item_id

                relay = subprocess.Popen(["node", str(ROOT / "openchamber/service/main.mjs")],
                    cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
                relay_url = "http://127.0.0.1:" + _line(relay, 10).rsplit(" ", 1)[1]
                for number, (name, retraction, status, outcome, title, detail, outstanding) in enumerate(cases, 1):
                    item_id = seed(name, number)
                    code, raw = _request(relay_url + f"/items/{item_id}/remove", "POST", "fake-relay-token", b"{}")
                    result = json.loads(raw)
                    self.assertEqual(code, status, raw)
                    self.assertEqual(result["outcome"], outcome)
                    self.assertEqual(result["retraction"], retraction)
                    self.assertEqual(result["removed"], outcome == "removed")
                    self.assertEqual(result["detail"], detail)
                    self.assertNotIn("admission_id", raw)
                    self.assertNotIn("fake-docket-token", raw)
                    self.assertEqual(store.get(item_id).status == "admitted", outstanding)
                    if outstanding:
                        self.assertIn(item_id, {item.id for item in store.list_outstanding()})
                    evidence["relay"].append({"name": name, "http": code, **result})
                    if name in ("missing", "lost"):
                        repeat_code, repeat_raw = _request(relay_url + f"/items/{item_id}/remove", "POST", "fake-relay-token", b"{}")
                        self.assertEqual(repeat_code, 409)
                        self.assertNotIn("removed.", repeat_raw.lower())
                        self.assertIn("does not confirm", repeat_raw)
                    browser_cases.append(dict(name=name, id=seed(name, number + 100), title=title, detail=detail, outstanding=outstanding))

                # SDK host failure and an inconsistent HTTP/body response must
                # not become green success; neither touches the actual item.
                for number, reply in enumerate([
                    {"ok": False, "code": "REQUEST_FAILED", "error": "Fake host proxy failed"},
                    {"ok": True, "payload": {"status": 503, "body": json.dumps({"removed": True, "outcome": "removed", "retraction": "retracted", "detail": "Contradictory response"})}},
                ], 200):
                    browser_cases.append(dict(name=f"host-{number}", id=seed("pending", number),
                        title="Could not confirm removal", detail="Fake host proxy failed" if number == 200 else "Contradictory response", outstanding=True, reply=reply))
                debug = ThreadingHTTPServer(("127.0.0.1", 0), TrapHandler)
                port = debug.server_address[1]
                debug.server_close()
                env.update(DOCKET_RELAY=relay_url, DOCKET_DB=str(home / "docket.db"),
                    DOCKET_SCREENSHOT=str(home / "unused.png"), DOCKET_CHROME_DATA=str(home / "chrome"),
                    DOCKET_DEBUG_PORT=str(port), DOCKET_FEEDBACK_CASES=json.dumps(browser_cases))
                browser = subprocess.run(["node", str(ROOT / "openchamber/test/browser.mjs")],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=110)
                line = next((line for line in browser.stdout.splitlines() if line.startswith("PANEL_RESULT ")), "")
                self.assertTrue(line, browser.stderr[-2000:])
                evidence["browser"] = json.loads(line.removeprefix("PANEL_RESULT "))
                self.assertEqual(browser.returncode, 0, evidence["browser"])
                self.assertTrue(evidence["browser"]["ok"])
                self.assertEqual(host.sent, [])
            finally:
                _kill(relay)
                if relay:
                    relay.communicate(timeout=5)
                api.shutdown()
                api.server_close()
                thread.join(5)
                store.close()
                (evidence_dir / "panel-feedback.json").write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
