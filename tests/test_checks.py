"""Bounded built-in checks. No caller-supplied commands."""

from __future__ import annotations

import http.server
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from docket.checks import Checks, FileView, probe_url, read_process_start, stat_file
from tests.support import utc


class FileCheckTests(unittest.TestCase):
    def test_stat_reports_file_directory_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = stat_file(str(root / "nope"))
            self.assertEqual(missing.state, "missing")
            directory = stat_file(str(root))
            self.assertEqual(directory.state, "other")
            path = root / "a;echo no"
            path.write_text("hello")
            found = stat_file(str(path))
            self.assertEqual(found.state, "file")
            self.assertEqual(found.size, 5)
            self.assertIsNotNone(found.ino)

    def test_stable_window_uses_mtime_size_and_identity(self):
        now = utc(2026, 10, 16, 12, 0, 0)
        view = FileView("file", dev=7, ino=8, size=10, mtime_ns=100)

        def file_stat(_path):
            return view

        checks = Checks(file_stat=file_stat)
        spec = {"check": "file_stable", "path": "/tmp/a", "stable_for": 30, "observations": {}}
        first = checks.evaluate(spec, now)
        self.assertEqual(first.kind, "waiting")
        spec["observations"] = first.observations
        second = checks.evaluate(spec, now + timedelta(seconds=10))
        self.assertEqual(second.kind, "waiting")
        spec["observations"] = second.observations
        third = checks.evaluate(spec, now + timedelta(seconds=40))
        self.assertEqual(third.kind, "met")
        self.assertEqual(third.summary["identity"], [7, 8])

    def test_unavailable_stat_is_not_a_match(self):
        checks = Checks(file_stat=lambda _path: FileView("unavailable", detail="timed out"))
        outcome = checks.evaluate(
            {"check": "file_appears", "path": "/tmp/a"}, utc(2026, 10, 16, 12),
        )
        self.assertEqual(outcome.kind, "unavailable")


class ProcessCheckTests(unittest.TestCase):
    def test_running_process_and_exit(self):
        proc = subprocess.Popen(["sleep", "30"])
        try:
            start = read_process_start(proc.pid)
            self.assertTrue(start)
            self.assertEqual(read_process_start(proc.pid), start)
            waiting = Checks().evaluate({
                "check": "process_ended", "pid": proc.pid, "process_start": start,
            }, utc(2026, 10, 16, 12))
            self.assertEqual(waiting.kind, "waiting")
        finally:
            proc.kill()
            proc.wait(timeout=5)
        self.assertIsNone(read_process_start(proc.pid))
        ended = Checks().evaluate({
            "check": "process_ended", "pid": proc.pid, "process_start": start,
        }, utc(2026, 10, 16, 12))
        self.assertEqual(ended.kind, "met")
        self.assertEqual(ended.summary["ended"], "exited")

    def test_pid_reuse_is_ended_and_ps_failure_is_not(self):
        def reader(_pid):
            return "other start"

        reused = Checks(process_reader=reader).evaluate({
            "check": "process_ended", "pid": 50, "process_start": "original start",
        }, utc(2026, 10, 16, 12))
        self.assertEqual(reused.summary["ended"], "pid_reused")

        def down(_pid):
            raise TimeoutError("ps timed out")

        outage = Checks(process_reader=down).evaluate({
            "check": "process_ended", "pid": 50, "process_start": "original start",
        }, utc(2026, 10, 16, 12))
        self.assertEqual(outage.kind, "unavailable")
        with self.assertRaises(ValueError):
            read_process_start(1)

    def test_capture_rejects_a_dead_pid(self):
        checks = Checks(process_reader=lambda _pid: None)
        with self.assertRaises(ValueError):
            checks.capture_process(4321)


class UrlCheckTests(unittest.TestCase):
    def test_answers_follows_one_redirect_and_stops(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                if self.path == "/loop":
                    self.send_response(302)
                    self.send_header("Location", "/loop")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/go":
                    self.send_response(302)
                    self.send_header("Location", "/ok")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/file":
                    self.send_response(302)
                    self.send_header("Location", "file:///etc/passwd")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b"x" * 200_000
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            status, final = probe_url(base + "/ok", timeout=2, deadline=4)
            self.assertEqual(status, 200)
            self.assertTrue(final.endswith("/ok"))
            status, final = probe_url(base + "/go", timeout=2, deadline=4)
            self.assertEqual(status, 200)
            self.assertTrue(final.endswith("/ok"))
            with self.assertRaises(ValueError):
                probe_url(base + "/loop", timeout=2, deadline=4, max_redirects=2)
            with self.assertRaises(ValueError):
                probe_url(base + "/file", timeout=2, deadline=4)
            self.assertLessEqual(len(seen), 8)
        finally:
            server.shutdown()
            server.server_close()

    def test_timeout_and_refusal_do_not_match(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            with self.assertRaises(TimeoutError):
                probe_url(f"http://127.0.0.1:{port}/hang", timeout=0.3, deadline=0.6)
        finally:
            sock.close()
        refused = Checks().evaluate({
            "check": "url_answers", "url": "http://127.0.0.1:9/nope", "expect": "2xx",
        }, utc(2026, 10, 16, 12))
        self.assertEqual(refused.kind, "waiting")
        self.assertNotIn("body", refused.summary or {})

    def test_expect_2xx_does_not_treat_500_as_an_answer(self):
        checks = Checks(url_probe=lambda _url: (500, "http://127.0.0.1/x"))
        outcome = checks.evaluate({
            "check": "url_answers", "url": "http://127.0.0.1/x", "expect": "2xx",
        }, utc(2026, 10, 16, 12))
        self.assertEqual(outcome.kind, "waiting")
        any_answer = Checks(url_probe=lambda _url: (500, "http://127.0.0.1/x")).evaluate({
            "check": "url_answers", "url": "http://127.0.0.1/x", "expect": "any",
        }, utc(2026, 10, 16, 12))
        self.assertEqual(any_answer.kind, "met")


class SessionCheckTests(unittest.TestCase):
    def test_outage_is_not_idle_and_missing_is_terminal(self):
        class Adapter:
            def session_state(self, _session_id):
                from docket.delivery import DeliveryError
                raise DeliveryError("unavailable", "down", retryable=True)

        outage = Checks(Adapter()).evaluate({
            "check": "session_idle", "watch_session": "ses_knownsession0001",
        }, utc(2026, 10, 16, 12))
        self.assertEqual(outage.kind, "unavailable")

        class Missing:
            def session_state(self, _session_id):
                return "missing"

        terminal = Checks(Missing()).evaluate({
            "check": "session_idle", "watch_session": "ses_knownsession0001",
        }, utc(2026, 10, 16, 12))
        self.assertEqual(terminal.kind, "terminal")


if __name__ == "__main__":
    unittest.main()
