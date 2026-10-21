"""Relay and panel integration against an isolated Docket service.

The iframe path is the OpenChamber SDK serviceRequest channel, forwarded to
the real relay process. Direct calls cover the relay's own refusals. This
does not start the scheduler, contact OpenCode, or touch the live LaunchAgent.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE_PORT = 8768
LIVE_PID = "30992"


class TrapHandler(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):
        TrapHandler.hits.append(self.path)
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        TrapHandler.hits.append(self.path)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_args):
        return


def _line(proc: subprocess.Popen[str], timeout: float) -> str:
    found: list[str] = []

    def read() -> None:
        if proc.stdout is not None:
            found.append(proc.stdout.readline())

    thread = threading.Thread(target=read)
    thread.start()
    thread.join(timeout)
    if thread.is_alive() or not found or not found[0]:
        raise TimeoutError(f"no output from {proc.args}")
    return found[0].strip()


def _kill(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait(timeout=5)


def _request(url: str, method: str = "GET", token: str | None = None, body: bytes | None = None, timeout: float = 5):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return response.status, raw.decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _pid_alive(pid: str) -> bool:
    done = subprocess.run(["ps", "-p", pid, "-o", "pid="], capture_output=True, text=True)
    return done.returncode == 0 and pid in done.stdout


class PanelRelayTest(unittest.TestCase):
    def test_relay_and_panel_against_isolated_service(self) -> None:
        live_before = _pid_alive(LIVE_PID)
        trap = ThreadingHTTPServer(("127.0.0.1", 0), TrapHandler)
        trap_thread = threading.Thread(target=trap.serve_forever, daemon=True)
        trap_thread.start()
        trap_url = f"http://127.0.0.1:{trap.server_address[1]}/steal"
        home = Path(tempfile.mkdtemp(prefix="docket-panel-"))
        chrome_data = Path(tempfile.mkdtemp(prefix="docket-chrome-"))
        bad_home = Path(tempfile.mkdtemp(prefix="docket-panel-bad-"))
        procs: list[subprocess.Popen[str]] = []
        evidence = {
            "live_pid_before": live_before,
            "live_port_contacted": False,
        }
        old_home = os.environ.get("DOCKET_HOME")
        try:
            self.assertNotEqual(trap.server_address[1], LIVE_PORT)
            os.environ["DOCKET_HOME"] = str(home)
            docket_token = "docket-panel-test-token-not-for-iframe"
            service_token = "openchamber-relay-token-not-for-iframe"
            (home / "token").write_text(docket_token + "\n")
            (home / "token").chmod(0o600)
            self._seed(home, trap_url)
            env = os.environ.copy()
            env["DOCKET_HOME"] = str(home)
            env.pop("DOCKET_URL", None)
            env.pop("DOCKET_TOKEN", None)
            service = subprocess.Popen(
                ["python3", str(ROOT / "tests" / "panel_service.py")],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            procs.append(service)
            service_url = _line(service, 10)
            self.assertTrue(service_url.startswith("http://127.0.0.1:"))
            self.assertNotIn(f":{LIVE_PORT}", service_url)
            evidence["service_url"] = service_url
            relay_env = env.copy()
            relay_env["OPENCHAMBER_SERVICE_PORT"] = "0"
            relay_env["OPENCHAMBER_SERVICE_TOKEN"] = service_token
            relay = subprocess.Popen(
                ["node", str(ROOT / "openchamber" / "service" / "main.mjs")],
                cwd=ROOT,
                env=relay_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            procs.append(relay)
            relay_line = _line(relay, 10)
            self.assertTrue(relay_line.startswith("docket-relay listening "))
            relay_port = int(relay_line.rsplit(" ", 1)[1])
            self.assertNotEqual(relay_port, LIVE_PORT)
            relay_url = f"http://127.0.0.1:{relay_port}"
            evidence["relay_url"] = relay_url

            status, body = _request(relay_url + "/health")
            self.assertEqual(status, 401)
            status, body = _request(relay_url + "/health", token=service_token)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"ready": True})
            self.assertNotIn(docket_token, body)

            status, body = _request(relay_url + "/snapshot", token=service_token)
            self.assertEqual(status, 200, body)
            snapshot = json.loads(body)
            self.assertNotIn(docket_token, body)
            self.assertNotIn(service_token, body)
            self.assertNotIn("context-must-not-reach-iframe", body)
            self.assertNotIn("receipt", body)
            self.assertIn(trap_url, body)
            self.assertEqual(TrapHandler.hits, [])
            ids = {item["id"] for item in snapshot["outstanding"]}
            self.assertIn("dkt_a11ce00000000001", ids)
            self.assertIn("dkt_a11ce00000000002", ids)
            self.assertNotIn("dkt_a11ce00000000005", ids)
            self.assertFalse(snapshot["history_unavailable"])
            self.assertTrue(any(row["error"] == "host dropped the admission" for row in snapshot["undeliverable_occurrences"]))

            status, body = _request(
                relay_url + "/no-such-route",
                method="POST",
                token=service_token,
                body=json.dumps({"url": trap_url, "reason": trap_url}).encode(),
            )
            self.assertEqual(status, 404)
            self.assertEqual(body, '{"error":"Unknown Docket route"}')
            status, body = _request(
                relay_url + "/items/not-a-docket-id/remove",
                method="POST",
                token=service_token,
                body=json.dumps({"url": trap_url}).encode(),
            )
            self.assertEqual(status, 404)
            self.assertEqual(body, '{"error":"Unknown Docket route"}')
            status, body = _request(
                relay_url + "/items/dkt_a11ce00000000005/remove",
                method="POST",
                token=service_token,
                body=b"{}",
            )
            self.assertEqual(status, 409)
            parsed = json.loads(body)
            self.assertFalse(parsed["removed"])
            self.assertEqual(parsed["outcome"], "undeliverable")
            self.assertIn("already undeliverable", parsed["detail"])
            self.assertNotIn(docket_token, body)
            self.assertEqual(TrapHandler.hits, [])

            self._assert_refuses_off_loopback(bad_home, service_token)
            evidence["off_loopback_refused"] = True

            shot = ROOT / "evidence" / "panel-feedback-regression.png"
            browser_env = env.copy()
            browser_env.update({
                "DOCKET_RELAY": relay_url,
                "OPENCHAMBER_SERVICE_TOKEN": service_token,
                "DOCKET_TOKEN_EXPECT": docket_token,
                "DOCKET_DB": str(home / "docket.db"),
                "DOCKET_SCREENSHOT": str(shot),
                "DOCKET_CHROME_DATA": str(chrome_data),
                "DOCKET_DEBUG_PORT": "0",
            })
            # Chrome needs a concrete debugging port. Pick one that is not Docket's.
            debug = ThreadingHTTPServer(("127.0.0.1", 0), TrapHandler)
            debug_port = debug.server_address[1]
            debug.server_close()
            self.assertNotEqual(debug_port, LIVE_PORT)
            browser_env["DOCKET_DEBUG_PORT"] = str(debug_port)
            browser = subprocess.run(
                ["node", str(ROOT / "openchamber" / "test" / "browser.mjs")],
                cwd=ROOT,
                env=browser_env,
                capture_output=True,
                text=True,
                timeout=90,
            )
            result_line = next((line for line in reversed(browser.stdout.splitlines()) if line.startswith("PANEL_RESULT ")), "")
            self.assertTrue(result_line, browser.stderr[-2000:] + browser.stdout[-2000:])
            panel = json.loads(result_line.removeprefix("PANEL_RESULT "))
            evidence["panel"] = panel
            evidence["browser_exit"] = browser.returncode
            self.assertTrue(panel.get("ok"), panel)
            self.assertEqual(browser.returncode, 0, browser.stderr[-2000:])
            self.assertTrue(shot.is_file())
            self.assertGreater(shot.stat().st_size, 1000)
            self.assertEqual(TrapHandler.hits, [])

            status, body = _request(service_url + "/api/v1/items/dkt_a11ce00000000001")
            self.assertEqual(json.loads(body)["item"]["status"], "delivered")
            status, body = _request(service_url + "/api/v1/items/dkt_a11ce00000000002")
            self.assertEqual(json.loads(body)["item"]["status"], "cancelled")
            status, body = _request(service_url + "/api/v1/items/dkt_a11ce00000000005")
            self.assertEqual(json.loads(body)["item"]["status"], "undeliverable")
            evidence["states"] = {
                "consumed_stayed_delivered": True,
                "admitted_cancelled": True,
                "undeliverable_unchanged": True,
            }
        finally:
            for proc in procs:
                _kill(proc)
                proc.communicate(timeout=5)
            trap.shutdown()
            trap.server_close()
            trap_thread.join(5)
            live_after = _pid_alive(LIVE_PID)
            evidence["live_pid_after"] = live_after
            evidence["trap_hits"] = list(TrapHandler.hits)
            (ROOT / "evidence").mkdir(exist_ok=True)
            (ROOT / "evidence" / "panel-feedback-regression.json").write_text(json.dumps(evidence, indent=2) + "\n")
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(chrome_data, ignore_errors=True)
            shutil.rmtree(bad_home, ignore_errors=True)
            if old_home is None:
                os.environ.pop("DOCKET_HOME", None)
            else:
                os.environ["DOCKET_HOME"] = old_home
            if live_before and not live_after:
                self.fail("live Docket pid disappeared during the panel test")

    def _assert_refuses_off_loopback(self, home: Path, service_token: str) -> None:
        home.mkdir(exist_ok=True)
        (home / "token").write_text("unused-token\n")
        (home / "server.json").write_text(json.dumps({"url": "http://10.255.255.1:9"}) + "\n")
        env = os.environ.copy()
        env["DOCKET_HOME"] = str(home)
        env["DOCKET_URL"] = "http://10.255.255.1:9"
        env["OPENCHAMBER_SERVICE_PORT"] = "0"
        env["OPENCHAMBER_SERVICE_TOKEN"] = service_token
        proc = subprocess.Popen(
            ["node", str(ROOT / "openchamber" / "service" / "main.mjs")],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            line = _line(proc, 10)
            port = int(line.rsplit(" ", 1)[1])
            started = time.monotonic()
            status, body = _request(
                f"http://127.0.0.1:{port}/snapshot",
                token=service_token,
                timeout=3,
            )
            self.assertLess(time.monotonic() - started, 2.5)
            self.assertEqual(status, 503, body)
            self.assertIn("not a loopback", body)
        finally:
            _kill(proc)
            proc.communicate(timeout=5)

    def _seed(self, home: Path, trap_url: str) -> None:
        from docket.store import Item, Store

        now = datetime.now(timezone.utc)
        future = now + timedelta(hours=3)
        store = Store(home / "docket.db")
        rows = [
            self._item(
                "dkt_a11ce00000000001", "timer", "pending", future, now,
                reason=f"Check <img src=x onerror=alert(1)> {trap_url}",
                context={"note": "context-must-not-reach-iframe", "url": trap_url},
                target=self._target("ses_panelpending0001", "/example/projects/docket"),
                urgent=True, created_by="chief",
            ),
            self._item(
                "dkt_a11ce00000000002", "alarm", "admitted", future, now,
                reason="Alarm for the downloads lead",
                target=self._target("ses_paneladmitted001", "/example/projects/downloads"),
                created_by="lead-downloads", late=True, late_by_seconds=42,
                receipt={"state": "admitted", "inbox_id": "msg_paneladmit0001", "admission_id": "msg_paneladmit0001", "delivery": "queue"},
            ),
            self._item(
                "dkt_a11ce00000000003", "watch", "pending", future, now,
                reason="Wait until the file stops growing",
                target=self._target("ses_panelwatch000001", "/example/projects/docket"),
                created_by="lead-watch",
                spec={"check": "file_stable", "every": 15, "path": "/tmp/docket-panel-not-read"},
            ),
            self._item(
                "dkt_a11ce00000000004", "recurring", "pending", future, now,
                reason="Series still waiting",
                target=self._target("ses_panelseries00001", "/example/projects/docket"),
                created_by="chief",
                spec={"every": 60, "count": 3, "next_seq": 2},
            ),
            self._item(
                "dkt_a11ce00000000005", "timer", "undeliverable", now - timedelta(minutes=5), now,
                reason="Wake the archived session",
                target=self._target("ses_paneldead0000001", "/example/projects/downloads"),
                created_by="lead-downloads",
                error="session archived before the fire",
            ),
        ]
        for row in rows:
            store.insert(row)
        store.close()
        db = sqlite3.connect(home / "docket.db")
        db.execute(
            """INSERT INTO occurrences (
                 id, item_id, seq, status, scheduled_at, created_at, updated_at,
                 late, late_by_seconds, error, attempts, coalesced
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "occ_a11ce0000000001", "dkt_a11ce00000000004", 1, "undeliverable",
                now.isoformat(), now.isoformat(), now.isoformat(),
                1, 8, "host dropped the admission", 1, 0,
            ),
        )
        db.commit()
        db.close()

    def _target(self, session_id: str, directory: str) -> dict:
        return {"adapter": "opencode", "session_id": session_id, "directory": directory}

    def _item(self, item_id, kind, status, fire_at, now, **extra) -> "Item":
        from docket.store import Item

        return Item(
            id=item_id,
            kind=kind,
            status=status,
            fire_at=fire_at,
            created_at=now,
            updated_at=now,
            reason=extra["reason"],
            context=extra.get("context") or {},
            target=extra["target"],
            urgent=bool(extra.get("urgent")),
            created_by=extra.get("created_by"),
            late=bool(extra.get("late")),
            late_by_seconds=extra.get("late_by_seconds"),
            delivered_at=None,
            cancelled_at=None,
            error=extra.get("error"),
            receipt=extra.get("receipt"),
            attempts=0,
            next_attempt_at=None,
            firing_at=None,
            spec=extra.get("spec"),
        )


if __name__ == "__main__":
    unittest.main()
