"""Regression tests for the milestone 1 review and M2 delivery reliability."""

from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path

from docket.api import DocketServer
from docket.clock import FakeClock
from docket.delivery import DeliveryError, OpenCodeAdapter
from docket.overlay import OpenChamberArchive
from docket.parse import require_bool, wake_text
from docket.scheduler import Scheduler, log_scheduler_error
from docket.store import Item, Store
from tests.support import FakeAdapter, RecordingOpenCode, utc

from zoneinfo import ZoneInfo


class StrictBoolTests(unittest.TestCase):
    def test_only_json_booleans(self):
        self.assertFalse(require_bool(None, "urgent"))
        self.assertFalse(require_bool(False, "urgent"))
        self.assertTrue(require_bool(True, "urgent"))
        for value in ("false", "true", "False", 0, 1, "0"):
            with self.assertRaises(ValueError):
                require_bool(value, "urgent")


class LatenessWordingTests(unittest.TestCase):
    def test_delay_is_not_blamed_on_sleep(self):
        text = wake_text(
            kind="alarm", item_id="dkt_x", reason="check",
            fire_at=utc(2026, 10, 16, 12, 0, 0), now=utc(2026, 10, 16, 12, 12, 0),
            late=True, urgent=False, created_by=None, context={},
            zone=ZoneInfo("UTC"),
        )
        self.assertIn("Late: yes, 12 minutes after the scheduled time.", text)
        self.assertNotIn("asleep", text)
        self.assertNotIn("service was down", text)


class StateModeTests(unittest.TestCase):
    def test_database_and_directory_are_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            path = root / "docket.db"
            store = Store(path)
            store.insert(_item("dkt_one", utc(2026, 10, 16, 12)))
            store.close()
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            for suffix in ("-wal", "-shm"):
                side = Path(str(path) + suffix)
                if side.exists():
                    self.assertEqual(stat.S_IMODE(side.stat().st_mode), 0o600)


class OutstandingListTests(unittest.TestCase):
    def test_pending_view_includes_firing_admitted_and_ambiguous(self):
        store, _adapter, clock, server = _http()
        now = clock.now()
        for status in ("pending", "firing", "admitted", "ambiguous", "delivered"):
            item = _item(f"dkt_{status}", now)
            item.status = status
            store.insert(item)
        import os as _os
        from docket.client import request
        _os.environ["DOCKET_URL"] = server.url
        _os.environ["DOCKET_TOKEN"] = "test-token"
        _status, listed = request("GET", "/api/v1/items?status=pending")
        ids = {item["id"] for item in listed["items"]}
        self.assertEqual(ids, {"dkt_pending", "dkt_firing", "dkt_admitted", "dkt_ambiguous"})
        self.assertNotIn("dkt_delivered", ids)
        server.stop()
        store.close()


class HttpBoundTests(unittest.TestCase):
    def setUp(self):
        self.store, self.adapter, self.clock, self.server = _http()
        os.environ["DOCKET_URL"] = self.server.url
        os.environ["DOCKET_TOKEN"] = "test-token"

    def tearDown(self):
        self.server.stop()
        self.store.close()

    def test_string_false_is_not_urgent(self):
        from docket.client import ClientError, request
        with self.assertRaises(ClientError) as caught:
            request("POST", "/api/v1/timers", {
                "in": 30, "reason": "no", "session_id": "ses_knownsession0001",
                "urgent": "false",
            })
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.store.list(), [])
        status, created = request("POST", "/api/v1/timers", {
            "in": 30, "reason": "quiet", "session_id": "ses_knownsession0001",
            "urgent": False,
        })
        self.assertEqual(status, 201)
        self.assertFalse(created["item"]["urgent"])

    def test_negative_and_huge_lengths_are_rejected(self):
        self.assertEqual(_raw(self.server, b"-1", b"")[0], 400)
        self.assertEqual(_raw(self.server, b"999999", b"")[0], 413)
        self.assertEqual(self.store.list(), [])

    def test_slow_body_is_cut_off(self):
        import docket.api as api
        previous = api.BODY_READ_SECONDS
        api.BODY_READ_SECONDS = 0.2
        try:
            started = time.monotonic()
            status, _body = _raw(self.server, b"32", b"", wait=False)
            elapsed = time.monotonic() - started
        finally:
            api.BODY_READ_SECONDS = previous
        self.assertEqual(status, 408)
        self.assertLess(elapsed, 3)


class ArchiveGuardTests(unittest.TestCase):
    def test_overlay_archives_at_set_and_fire_without_opencode_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions-archive.json"
            path.write_text(json.dumps({"ses_overlay0001": 1790000000000, "ses_null0001": None}))
            guard = OpenChamberArchive(path)
            self.assertEqual(guard.check("ses_overlay0001"), "archived")
            self.assertEqual(guard.check("ses_null0001"), "clear")
            self.assertEqual(guard.check("ses_live0001"), "clear")
            missing = OpenChamberArchive(Path(tmp) / "absent.json")
            self.assertEqual(missing.check("ses_overlay0001"), "clear")
            broken = Path(tmp) / "broken.json"
            broken.write_text("{")
            self.assertEqual(OpenChamberArchive(broken).check("ses_live0001"), "unreadable")

            fake = RecordingOpenCode()
            try:
                fake.sessions["ses_overlay0001"] = {
                    "id": "ses_overlay0001",
                    "time": {"created": 1},
                    "location": {"directory": "/work/docket"},
                }
                fake.sessions["ses_live0001"] = {
                    "id": "ses_live0001",
                    "time": {"created": 1},
                    "location": {"directory": "/work/docket"},
                }
                adapter = OpenCodeAdapter(
                    discovery=lambda: (fake.url, "Basic dGVzdA=="),
                    archive=OpenChamberArchive(path),
                )
                self.assertEqual(adapter.validate({"session_id": "ses_overlay0001"}).code, "archived")
                self.assertTrue(adapter.validate({"session_id": "ses_live0001"}).ok)
                self.assertEqual(adapter.session_state("ses_overlay0001"), "archived")
                store, clock, sched = _sched(adapter)
                now = clock.now()
                store.insert(_item("dkt_arch", now, session="ses_overlay0001"))
                sched.tick()
                self.assertEqual(store.get("dkt_arch").status, "undeliverable")
                self.assertIn("archived", store.get("dkt_arch").error)
                self.assertFalse(any(call[0] == "POST" for call in fake.calls))
                store.close()
            finally:
                fake.stop()

    def test_core_scheduler_does_not_import_the_overlay(self):
        source = Path(__file__).resolve().parents[1] / "docket" / "scheduler.py"
        text = source.read_text()
        self.assertNotIn("openchamber", text.lower())
        self.assertNotIn("overlay", text)


class DeliveryHonestyTests(unittest.TestCase):
    def test_admission_is_not_delivered_until_consumed(self):
        store, adapter, clock, sched = _world()
        adapter.consume_immediately = False
        store.insert(_item("dkt_due", clock.now()))
        sched.tick()
        item = store.get("dkt_due")
        self.assertEqual(item.status, "admitted")
        self.assertEqual(item.receipt["state"], "admitted")
        self.assertIsNone(item.delivered_at)
        aid = adapter.sent[0]["admission_id"]
        sched.tick()
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(store.get("dkt_due").status, "admitted")
        adapter.inbox.discard(aid)
        adapter.consumed.add(aid)
        sched.tick()
        self.assertEqual(store.get("dkt_due").status, "delivered")
        self.assertEqual(store.get("dkt_due").receipt["state"], "consumed")
        self.assertEqual(len(adapter.sent), 1)
        store.close()

    def test_confirmed_admission_that_vanishes_is_lost_not_replayed(self):
        store, adapter, clock, sched = _world()
        adapter.consume_immediately = False
        store.insert(_item("dkt_due", clock.now()))
        sched.tick()
        aid = adapter.sent[0]["admission_id"]
        adapter.inbox.discard(aid)
        sched.tick()
        item = store.get("dkt_due")
        self.assertEqual(item.status, "undeliverable")
        self.assertTrue(item.error.startswith("lost:"))
        clock.advance(3600)
        sched.tick()
        self.assertEqual(len(adapter.sent), 1)
        store.close()

    def test_conflict_does_not_mint_a_new_id(self):
        store, adapter, clock, sched = _world()
        adapter.conflict = True
        store.insert(_item("dkt_due", clock.now()))
        sched.tick()
        item = store.get("dkt_due")
        self.assertEqual(item.status, "ambiguous")
        self.assertIn("conflict", item.error)
        self.assertTrue(item.receipt["admission_id"].startswith("msg_"))
        clock.advance(3600)
        sched.tick()
        self.assertEqual(adapter.sent, [])
        self.assertEqual(store.get("dkt_due").receipt["admission_id"], item.receipt["admission_id"])
        store.close()

    def test_unconfirmed_retry_reuses_the_same_id(self):
        store, adapter, clock, sched = _world()
        adapter.fail = DeliveryError("unavailable", "down", retryable=True)
        store.insert(_item("dkt_due", clock.now()))
        sched.tick()
        aid = store.get("dkt_due").receipt["admission_id"]
        self.assertEqual(store.get("dkt_due").status, "pending")
        adapter.fail = None
        clock.advance(30)
        sched.tick()
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(adapter.sent[0]["admission_id"], aid)
        self.assertEqual(store.get("dkt_due").status, "delivered")
        store.close()

    def test_firing_without_an_id_is_not_resent(self):
        store, adapter, clock, sched = _world()
        now = clock.now()
        item = _item("dkt_old", now - timedelta(seconds=120))
        item.status = "firing"
        item.firing_at = now - timedelta(seconds=120)
        store.insert(item)
        sched.tick()
        self.assertEqual(adapter.sent, [])
        self.assertEqual(store.get("dkt_old").status, "ambiguous")
        store.close()


class CancelRaceTests(unittest.TestCase):
    def test_cancel_wins_over_an_in_flight_delivery(self):
        store, adapter, clock, sched = _world()
        hold = threading.Event()
        adapter.hold_deliver = hold
        adapter.consume_immediately = False
        store.insert(_item("dkt_due", clock.now()))
        done = threading.Event()

        def run():
            sched.tick()
            done.set()

        threading.Thread(target=run, daemon=True).start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not getattr(adapter, "entered", False):
            time.sleep(0.01)
        self.assertEqual(store.cancel("dkt_due", clock.now()), "cancelled")
        hold.set()
        self.assertTrue(done.wait(3))
        self.assertEqual(store.get("dkt_due").status, "cancelled")
        self.assertTrue(adapter.cancelled)
        store.close()

    def test_compound_cancel_and_finish_do_not_resurrect(self):
        store, now = _store()
        store.insert(_item("dkt_due", now))
        self.assertTrue(store.claim("dkt_due", now))
        barrier = threading.Barrier(2)
        results = []

        def finish():
            barrier.wait(2)
            item = store.get("dkt_due")
            item.status = "delivered"
            item.updated_at = now
            item.delivered_at = now
            results.append(("finish", store.cas_item(item, ("firing",))))

        def cancel():
            barrier.wait(2)
            results.append(("cancel", store.cancel("dkt_due", now)))

        threads = [threading.Thread(target=finish), threading.Thread(target=cancel)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        final = store.get("dkt_due")
        self.assertIn(final.status, ("delivered", "cancelled"))
        if final.status == "cancelled":
            self.assertIsNone(final.delivered_at)
        else:
            self.assertIsNone(final.cancelled_at)
        store.close()


class RecurringReliabilityTests(unittest.TestCase):
    def test_admitted_occurrence_does_not_wake_again(self):
        store, adapter, clock, sched = _world()
        adapter.consume_immediately = False
        store.insert(_recurring("dkt_every", clock.now(), every=60, count=4))
        sched.tick()
        self.assertEqual(len(adapter.sent), 1)
        parent = store.get("dkt_every")
        self.assertEqual(parent.status, "pending")
        occ = store.list_occurrences("dkt_every")[0]
        self.assertEqual(occ.status, "admitted")
        for _ in range(5):
            clock.advance(30)
            sched.tick()
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(len(store.list_occurrences("dkt_every")), 1)
        self.assertEqual(store.get("dkt_every").spec["next_seq"], 2)
        aid = occ.receipt["admission_id"]
        adapter.inbox.discard(aid)
        adapter.consumed.add(aid)
        sched.tick()
        self.assertEqual(store.get_occurrence(occ.id).status, "delivered")
        self.assertEqual(len(adapter.sent), 1)
        self.assertGreater(store.get("dkt_every").fire_at, clock.now())
        store.close()

    def test_cancel_of_admitted_occurrence_does_not_continue(self):
        store, adapter, clock, sched = _world()
        adapter.consume_immediately = False
        store.insert(_recurring("dkt_every", clock.now(), every=60, count=4))
        sched.tick()
        occ = store.list_occurrences("dkt_every")[0]
        self.assertEqual(occ.status, "admitted")
        # The HTTP cancel path drops the inbox admission, then the row.
        adapter.cancel_admission(
            store.get("dkt_every").target, occ.receipt["inbox_id"],
        )
        self.assertEqual(store.cancel("dkt_every", clock.now()), "cancelled")
        self.assertTrue(adapter.cancelled)
        clock.advance(3600)
        sched.tick()
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(store.get("dkt_every").status, "cancelled")
        self.assertEqual(store.list_occurrences("dkt_every")[0].status, "cancelled")
        store.close()

    def test_http_cancel_drops_an_admitted_inbox_item(self):
        store, adapter, clock, server = _http()
        adapter.consume_immediately = False
        os.environ["DOCKET_URL"] = server.url
        os.environ["DOCKET_TOKEN"] = "test-token"
        from docket.client import request
        _status, created = request("POST", "/api/v1/recurring", {
            "every": 60, "count": 2, "reason": "again",
            "session_id": "ses_knownsession0001",
        })
        item = store.get(created["item"]["id"])
        item.fire_at = clock.now()
        store.save(item)
        server.scheduler.tick()
        self.assertEqual(store.list_occurrences(item.id)[0].status, "admitted")
        request("POST", f"/api/v1/items/{item.id}/cancel", {})
        self.assertTrue(adapter.cancelled)
        self.assertEqual(store.get(item.id).status, "cancelled")
        server.stop()
        store.close()


class LeaseOwnershipTests(unittest.TestCase):
    def test_fresh_firing_lease_is_not_admitted_again(self):
        store, adapter, clock, sched = _world()
        now = clock.now()
        item = _recurring("dkt_lease", now, every=60, count=4)
        store.insert(item)
        from docket.parse import new_occurrence_id
        from docket.store import Occurrence
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
        clock.advance(30)
        sched.tick()
        self.assertEqual(adapter.sent, [])
        self.assertEqual(len(store.list_occurrences(item.id)), 1)
        store.close()

    def test_error_after_send_does_not_admit_again_inside_the_lease(self):
        store, adapter, clock, sched = _world()
        store.insert(_recurring("dkt_boom", clock.now(), every=60, count=4))
        calls = {"n": 0}
        real = adapter.deliver

        def deliver(target, message, *, urgent, admission_id=None):
            calls["n"] += 1
            real(target, message, urgent=urgent, admission_id=admission_id)
            raise RuntimeError("admitted, then the response could not be recorded")

        adapter.deliver = deliver
        sched.tick()
        clock.advance(30)
        sched.tick()
        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(store.list_occurrences("dkt_boom")), 1)
        self.assertEqual(store.list_occurrences("dkt_boom")[0].status, "firing")
        store.close()

    def test_cancel_after_occurrence_delivery_is_not_resurrected(self):
        store, adapter, clock, sched = _world()
        store.insert(_recurring("dkt_race", clock.now(), every=60, count=4))
        real = store.cas_occurrence

        def cas_occurrence(occ, expect):
            ok = real(occ, expect)
            if ok and occ.status == "delivered":
                store.cancel("dkt_race", clock.now())
            return ok

        store.cas_occurrence = cas_occurrence
        sched.tick()
        self.assertEqual(store.get("dkt_race").status, "cancelled")
        before = len(adapter.sent)
        clock.advance(3600)
        sched.tick()
        self.assertEqual(len(adapter.sent), before)
        self.assertEqual(store.get("dkt_race").status, "cancelled")
        store.close()


class SchedulerLogTests(unittest.TestCase):
    def test_tick_exception_is_logged_and_does_not_kill_the_thread(self):
        class Boom:
            def tick(self):
                raise RuntimeError("scheduler-boom")

        store, _adapter, _clock, server = _http()
        server.scheduler = Boom()
        import io
        from contextlib import redirect_stderr
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            server.start_scheduler(interval=0.05)
            time.sleep(0.2)
            server.stop()
        logged = buffer.getvalue()
        self.assertIn("scheduler error", logged)
        self.assertIn("scheduler-boom", logged)
        self.assertNotIn("password", logged.lower())
        log_scheduler_error(RuntimeError("again"))
        store.close()


def _http():
    tmp = tempfile.TemporaryDirectory()
    store = Store(Path(tmp.name) / "docket.db")
    store._tmp = tmp
    adapter = FakeAdapter()
    clock = FakeClock(utc(2026, 10, 16, 12, 0, 0))
    server = DocketServer(store, Scheduler(store, adapter, clock), token="test-token", port=0)
    server.serve_in_thread()
    return store, adapter, clock, server


def _world():
    tmp = tempfile.TemporaryDirectory()
    store = Store(Path(tmp.name) / "docket.db")
    store._tmp = tmp
    adapter = FakeAdapter()
    clock = FakeClock(utc(2026, 10, 16, 12, 0, 0))
    return store, adapter, clock, Scheduler(store, adapter, clock)


def _sched(adapter):
    tmp = tempfile.TemporaryDirectory()
    store = Store(Path(tmp.name) / "docket.db")
    store._tmp = tmp
    clock = FakeClock(utc(2026, 10, 16, 12, 0, 0))
    return store, clock, Scheduler(store, adapter, clock)


def _store():
    tmp = tempfile.TemporaryDirectory()
    store = Store(Path(tmp.name) / "docket.db")
    store._tmp = tmp
    return store, utc(2026, 10, 16, 12, 0, 0)


def _item(item_id, fire_at, *, session="ses_knownsession0001"):
    now = utc(2026, 10, 16, 11, 0, 0)
    return Item(
        id=item_id, kind="timer", status="pending", fire_at=fire_at,
        created_at=now, updated_at=now, reason="check the file", context={},
        target={"adapter": "opencode", "session_id": session}, urgent=False,
        created_by="ses_setter", late=False, late_by_seconds=None,
        delivered_at=None, cancelled_at=None, error=None, receipt=None,
        attempts=0, next_attempt_at=None, firing_at=None, spec=None,
    )


def _recurring(item_id, fire_at, *, every, count):
    item = _item(item_id, fire_at)
    item.kind = "recurring"
    item.spec = {
        "every": every,
        "anchor": fire_at.isoformat(),
        "count": count,
        "next_seq": 1,
        "open_occurrence_id": None,
    }
    return item


def _raw(server, length: bytes, body: bytes, *, wait: bool = True):
    sock = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=3)
    try:
        sock.sendall(
            b"POST /api/v1/timers HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer test-token\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + length + b"\r\n"
            b"\r\n" + body
        )
        sock.settimeout(3)
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw += chunk
        status = int(raw.split(b" ", 2)[1])
        return status, raw
    finally:
        sock.close()
