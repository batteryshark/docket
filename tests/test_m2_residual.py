"""Residual M2 safety regressions.

Behavioral assertions follow the independent review repro, adapted where
admission is no longer treated as consumption. Isolated state only.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from docket.api import DocketServer
from docket.bounds import (
    MAX_RECURRING_PER_ACTOR,
    actor_bucket,
    normalize_created_by,
    watch_spec,
)
from docket.checks import CheckExecutor, probe_url
from docket.client import ClientError, request
from docket.parse import new_occurrence_id
from docket.scheduler import Scheduler
from docket.store import Item, Occurrence, Store
from tests.support import utc
from tests.test_schedule import _recurring, _watch, _world


class ActorCapTests(unittest.TestCase):
    def test_omitted_and_blank_share_one_bucket(self):
        self.assertEqual(actor_bucket(None), actor_bucket("  "))
        self.assertEqual(actor_bucket(""), "anonymous")
        with self.assertRaises(ValueError):
            normalize_created_by("  ")
        self.assertEqual(normalize_created_by("  ses_named  "), "ses_named")

    def test_omitted_created_by_does_not_bypass_the_actor_cap(self):
        tmp = tempfile.TemporaryDirectory()
        store = Store(Path(tmp.name) / "docket.db")
        from tests.support import FakeAdapter
        from docket.clock import FakeClock

        adapter = FakeAdapter()
        clock = FakeClock(utc(2026, 10, 16, 12, 0, 0))
        server = DocketServer(store, Scheduler(store, adapter, clock), token="test-token", port=0)
        server.serve_in_thread()
        old = os.environ.get("DOCKET_URL"), os.environ.get("DOCKET_TOKEN")
        os.environ["DOCKET_URL"] = server.url
        os.environ["DOCKET_TOKEN"] = "test-token"
        for index in range(MAX_RECURRING_PER_ACTOR + 2):
            adapter.known.add(f"ses_target{index:04d}")
        try:
            codes = []
            for index in range(MAX_RECURRING_PER_ACTOR + 1):
                try:
                    status, payload = request("POST", "/api/v1/recurring", {
                        "every": "15m",
                        "count": 2,
                        "reason": "cap",
                        "session_id": f"ses_target{index:04d}",
                    })
                    codes.append(status)
                    if status == 201:
                        self.assertEqual(payload["item"]["created_by"], None)
                        self.assertEqual(payload["item"]["created_by_source"], "anonymous")
                except ClientError as exc:
                    codes.append(exc.status)
            self.assertEqual(codes[-1], 409)
            self.assertNotIn(201, codes[MAX_RECURRING_PER_ACTOR:])
            status, named = request("POST", "/api/v1/recurring", {
                "every": "15m",
                "count": 2,
                "reason": "named",
                "session_id": f"ses_target{MAX_RECURRING_PER_ACTOR + 1:04d}",
                "created_by": "ses_other",
            })
            self.assertEqual(status, 201)
            self.assertEqual(named["item"]["created_by_source"], "self-reported")
            with self.assertRaises(ClientError) as caught:
                request("POST", "/api/v1/recurring", {
                    "every": "15m",
                    "count": 2,
                    "reason": "blank",
                    "session_id": "ses_target0000",
                    "created_by": "  ",
                })
            self.assertEqual(caught.exception.status, 400)
        finally:
            server.stop()
            store.close()
            tmp.cleanup()
            for key, value in zip(("DOCKET_URL", "DOCKET_TOKEN"), old):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class CoalesceClampTests(unittest.TestCase):
    def test_coalesced_does_not_count_slots_after_until(self):
        store, adapter, clock, sched = _world()
        start = clock.now()
        item = _recurring("dkt_until", start, every=60, count=20)
        item.spec["until"] = (start + timedelta(seconds=150)).isoformat()
        item.spec["anchor"] = start.isoformat()
        store.insert(item)
        clock.advance(600)
        sched.tick()
        self.assertEqual(len(adapter.sent), 1)
        message = adapter.sent[0]["message"]
        coalesced = store.list_occurrences("dkt_until")[0].coalesced
        self.assertLessEqual(coalesced, 2)
        self.assertNotIn("Coalesced: 10", message)
        self.assertIn("Remaining: 0", message)
        self.assertIn("self-reported", message)
        self.assertIn(store.get("dkt_until").status, ("completed", "undeliverable"))
        clock.advance(3600)
        before = len(adapter.sent)
        sched.tick()
        self.assertEqual(len(adapter.sent), before)
        store.close()


class CheckExecutorTests(unittest.TestCase):
    def test_backlog_is_bounded(self):
        executor = CheckExecutor(workers=2, backlog=2)
        hold = threading.Event()

        def block():
            hold.wait(2)
            return "done"

        accepted = []
        refused = False
        for index in range(8):
            if executor.submit(f"k{index}", block):
                accepted.append(index)
            else:
                refused = True
                break
        self.assertTrue(refused)
        self.assertLessEqual(len(accepted), executor.workers + executor.backlog)
        self.assertGreaterEqual(len(accepted), executor.backlog)
        hold.set()
        executor.wait([f"k{index}" for index in accepted], timeout=2)

    def test_slow_watch_does_not_block_an_already_due_timer(self):
        def slow(_url):
            time.sleep(0.4)
            return (200, "http://127.0.0.1/slow")

        store, adapter, clock, sched = _world(url_probe=slow)
        now = clock.now()
        store.insert(_watch(
            "dkt_slow", now - timedelta(seconds=1),
            check="url_answers", url="http://127.0.0.1/slow",
        ))
        store.insert(Item(
            id="dkt_timer", kind="timer", status="pending", fire_at=now,
            created_at=now, updated_at=now, reason="on time", context={},
            target={"adapter": "opencode", "session_id": "ses_knownsession0001"},
            urgent=False, created_by="ses_setter", late=False, late_by_seconds=None,
            delivered_at=None, cancelled_at=None, error=None, receipt=None,
            attempts=0, next_attempt_at=None, firing_at=None, spec=None,
        ))
        started = time.monotonic()
        sched.tick()
        elapsed = time.monotonic() - started
        timer_at = None
        for index, row in enumerate(adapter.sent):
            if "dkt_timer" in row["message"]:
                timer_at = index
        self.assertLess(elapsed, 0.2)
        self.assertIsNotNone(timer_at)
        store.close()


class StableWindowTests(unittest.TestCase):
    def test_stable_watch_must_be_able_to_meet_before_until(self):
        now = utc(2026, 10, 16, 12, 0, 0)
        with self.assertRaises(ValueError):
            watch_spec({
                "check": "file_stable",
                "path": "/tmp/download.bin",
                "until": "90m",
                "every": 3600,
                "stable_for": 3600,
            }, now=now, capture_process=None)


class SameOriginTests(unittest.TestCase):
    def test_redirect_stays_on_the_original_origin(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/cred":
                    self.send_response(302)
                    self.send_header("Location", "http://user:pass@127.0.0.1/secret")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/away":
                    self.send_response(302)
                    self.send_header("Location", other_base + "/landed")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/same":
                    self.send_response(302)
                    self.send_header("Location", "/landed")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b"secret-body"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        first = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        second = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        other_base = f"http://127.0.0.1:{second.server_address[1]}"
        for server in (first, second):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{first.server_address[1]}"
        try:
            with self.assertRaises(ValueError):
                probe_url(base + "/cred", timeout=2, deadline=4)
            with self.assertRaises(ValueError):
                probe_url(base + "/away", timeout=2, deadline=4)
            status, final = probe_url(base + "/same", timeout=2, deadline=4)
            self.assertEqual(status, 200)
            self.assertTrue(final.startswith(base))
        finally:
            first.shutdown()
            second.shutdown()
            first.server_close()
            second.server_close()


class PriorHighFindingTests(unittest.TestCase):
    def test_unexpired_firing_occurrence_is_not_admitted_again(self):
        store, adapter, clock, sched = _world()
        now = clock.now()
        item = _recurring("dkt_lease", now, every=60, count=4)
        store.insert(item)
        occ = Occurrence(
            id=new_occurrence_id(), item_id=item.id, seq=1, status="firing",
            scheduled_at=now, created_at=now, updated_at=now, late=False,
            late_by_seconds=None, delivered_at=None, error=None, receipt=None,
            attempts=1, next_attempt_at=None, firing_at=now, coalesced=0,
            detail={"kind": "recurring"},
        )
        store.attach_occurrence(item, occ)
        sched.tick()
        self.assertEqual(adapter.sent, [])
        self.assertEqual(store.get_occurrence(occ.id).status, "firing")
        store.close()

    def test_expired_lease_without_admission_id_is_not_duplicated(self):
        """A missing id is ambiguous, not a new occurrence. Receipt-state adaptation."""
        store, adapter, clock, sched = _world()
        now = clock.now()
        item = _recurring("dkt_expired", now, every=60, count=4)
        store.insert(item)
        occ = Occurrence(
            id=new_occurrence_id(), item_id=item.id, seq=1, status="firing",
            scheduled_at=now, created_at=now, updated_at=now, late=False,
            late_by_seconds=None, delivered_at=None, error=None, receipt=None,
            attempts=1, next_attempt_at=None,
            firing_at=now - timedelta(seconds=120), coalesced=0,
            detail={"kind": "recurring"},
        )
        store.attach_occurrence(item, occ)
        sched.tick()
        self.assertEqual(adapter.sent, [])
        self.assertEqual(store.get_occurrence(occ.id).status, "ambiguous")
        self.assertEqual(len(store.list_occurrences(item.id)), 1)
        store.close()

    def test_cancel_during_cas_is_not_resurrected(self):
        store, adapter, clock, sched = _world()
        store.insert(_recurring("dkt_race", clock.now(), every=60, count=4))
        entered = threading.Event()
        release = threading.Event()
        real = store.cas_occurrence

        def cas_occurrence(occ, expect):
            ok = real(occ, expect)
            if ok and occ.status == "delivered":
                entered.set()
                release.wait(2)
            return ok

        store.cas_occurrence = cas_occurrence
        result = {}

        def cancel():
            self.assertTrue(entered.wait(2))
            result["cancel"] = store.cancel("dkt_race", clock.now())
            release.set()

        threading.Thread(target=cancel).start()
        sched.tick()
        self.assertTrue(release.is_set())
        self.assertEqual(result.get("cancel"), "cancelled")
        self.assertEqual(store.get("dkt_race").status, "cancelled")
        before = len(adapter.sent)
        clock.advance(120)
        sched.tick()
        self.assertEqual(len(adapter.sent), before)
        store.close()


if __name__ == "__main__":
    unittest.main()
