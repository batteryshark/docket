"""Recurring schedules, occurrence identity, limits, and watch firing."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from docket.api import DocketServer
from docket.bounds import (
    MAX_RECURRING_COUNT,
    MAX_RECURRING_PER_TARGET,
    check_limits,
    recurring_spec,
    slot_after,
    skipped_between,
    watch_spec,
)
from docket.checks import Checks, FileView
from docket.client import ClientError, request
from docket.clock import FakeClock
from docket.delivery import DeliveryError
from docket.parse import wake_text
from docket.scheduler import Scheduler
from docket.store import Item, Store
from tests.support import FakeAdapter, utc

from zoneinfo import ZoneInfo


M1_SCHEMA = """
CREATE TABLE items (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  fire_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  reason TEXT NOT NULL,
  context_json TEXT NOT NULL DEFAULT '{}',
  target_json TEXT NOT NULL,
  urgent INTEGER NOT NULL DEFAULT 0,
  created_by TEXT,
  late INTEGER NOT NULL DEFAULT 0,
  late_by_seconds REAL,
  delivered_at TEXT,
  cancelled_at TEXT,
  error TEXT,
  receipt_json TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  firing_at TEXT,
  spec_json TEXT
);
CREATE INDEX idx_items_due ON items(status, fire_at);
"""


class BoundTests(unittest.TestCase):
    def test_recurring_requires_a_bound_and_a_minimum_interval(self):
        now = utc(2026, 10, 16, 12, 0, 0)
        with self.assertRaises(ValueError):
            recurring_spec({"every": "15m"}, now=now)
        with self.assertRaises(ValueError):
            recurring_spec({"every": 30, "count": 2}, now=now)
        with self.assertRaises(ValueError):
            recurring_spec({"every": 60, "count": MAX_RECURRING_COUNT + 1}, now=now)
        fire_at, spec = recurring_spec({"every": "15m", "count": 4}, now=now)
        self.assertEqual(spec["every"], 900)
        self.assertEqual(spec["count"], 4)
        self.assertEqual(fire_at, now + timedelta(seconds=900))
        self.assertNotIn("command", spec)

    def test_watch_requires_until_and_rejects_commands(self):
        now = utc(2026, 10, 16, 12, 0, 0)
        with self.assertRaises(ValueError):
            watch_spec({"check": "file_appears", "path": "/tmp/x"}, now=now, capture_process=None)
        with self.assertRaises(ValueError):
            watch_spec({
                "check": "file_appears", "path": "relative", "until": "1h",
            }, now=now, capture_process=None)
        with self.assertRaises(ValueError):
            watch_spec({
                "check": "url_answers", "url": "http://user:pw@example.com/x", "until": "1h",
            }, now=now, capture_process=None)
        with self.assertRaises(ValueError):
            watch_spec({
                "check": "url_answers", "url": "file:///etc/passwd", "until": "1h",
            }, now=now, capture_process=None)
        with self.assertRaises(ValueError):
            watch_spec({
                "check": "file_stable", "path": "/tmp/x", "until": "30s", "stable_for": 60,
            }, now=now, capture_process=None)
        fire_at, spec = watch_spec({
            "check": "file_appears", "path": "/tmp/docket-watch", "until": "2h",
        }, now=now, capture_process=None)
        self.assertEqual(spec["every"], 15)
        self.assertEqual(spec["check"], "file_appears")
        self.assertGreater(fire_at, now)
        _fire, url_spec = watch_spec({
            "check": "url_answers", "url": "http://127.0.0.1:9/health", "until": "30m",
        }, now=now, capture_process=None)
        self.assertEqual(url_spec["every"], 60)
        self.assertEqual(url_spec["expect"], "2xx")

    def test_coalesce_math_skips_intermediate_slots(self):
        anchor = utc(2026, 10, 16, 12, 0, 0)
        now = anchor + timedelta(seconds=600)
        nxt = slot_after(anchor, 60, now)
        self.assertEqual(nxt, anchor + timedelta(seconds=660))
        self.assertEqual(skipped_between(60, anchor, nxt), 10)
        self.assertEqual(skipped_between(60, anchor, anchor + timedelta(seconds=60)), 0)

    def test_timer_notice_is_unchanged_when_no_occurrence(self):
        text = wake_text(
            kind="timer", item_id="dkt_x", reason="check",
            fire_at=utc(2026, 10, 16, 12, 0, 0), now=utc(2026, 10, 16, 12, 0, 1),
            late=False, urgent=False, created_by=None, context={},
            zone=ZoneInfo("UTC"),
        )
        self.assertNotIn("Occurrence:", text)
        self.assertNotIn("Coalesced:", text)
        self.assertIn("Item: dkt_x", text)
        self.assertIn("Docket did not run anything.", text)


class RecurringTests(unittest.TestCase):
    def test_sleep_coalesces_and_does_not_storm(self):
        store, adapter, clock, sched = _world()
        start = clock.now()
        store.insert(_recurring("dkt_every", start, every=60, count=10))
        clock.advance(600)
        sched.tick()
        for _ in range(20):
            sched.tick()
        self.assertEqual(len(adapter.sent), 1)
        message = adapter.sent[0]["message"]
        self.assertIn("Late: yes", message)
        self.assertIn("10 minutes", message)
        self.assertIn("Coalesced: 10", message)
        self.assertIn("Occurrence: occ_", message)
        self.assertIn("Item: dkt_every", message)
        self.assertEqual(adapter.sent[0]["delivery"], "queue")
        self.assertNotIn("echo ", message)
        parent = store.get("dkt_every")
        self.assertEqual(parent.status, "pending")
        self.assertGreater(parent.fire_at, clock.now())
        occs = store.list_occurrences("dkt_every")
        self.assertEqual(len(occs), 1)
        self.assertTrue(occs[0].late)
        self.assertEqual(occs[0].coalesced, 10)
        store.close()

    def test_repeats_use_distinct_occurrence_markers(self):
        store, adapter, clock, sched = _world()
        start = clock.now()
        store.insert(_recurring("dkt_every", start, every=60, count=4))
        clock.advance(600)
        sched.tick()
        adapter.delivered_markers.add("Item: dkt_every")
        clock.advance(120)
        sched.tick()
        self.assertEqual(len(adapter.sent), 2)
        first = _occurrence_marker(adapter.sent[0]["message"])
        second = _occurrence_marker(adapter.sent[1]["message"])
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("Occurrence: occ_"))
        self.assertNotIn(first, adapter.sent[1]["message"])
        store.close()

    def test_same_occurrence_is_not_delivered_twice_after_restart(self):
        store, adapter, clock, sched = _world()
        start = clock.now()
        store.insert(_recurring("dkt_every", start, every=60, count=4))
        sched.tick()
        occ = store.list_occurrences("dkt_every")[0]
        self.assertEqual(len(adapter.sent), 1)
        aid = occ.receipt["admission_id"]
        occ.status = "firing"
        occ.firing_at = start - timedelta(seconds=120)
        occ.delivered_at = None
        occ.receipt = {"admission_id": aid, "state": "sending"}
        store.save_occurrence(occ)
        parent = store.get("dkt_every")
        spec = dict(parent.spec)
        spec["open_occurrence_id"] = occ.id
        parent.spec = spec
        parent.status = "pending"
        parent.fire_at = occ.scheduled_at
        parent.next_attempt_at = clock.now() + timedelta(hours=2)
        store.save(parent)
        adapter.consumed.add(aid)
        adapter.sent.clear()
        clock.advance(1)
        sched.tick()
        self.assertEqual(adapter.sent, [])
        recovered = store.get_occurrence(occ.id)
        self.assertEqual(recovered.status, "delivered")
        self.assertEqual(recovered.receipt["state"], "consumed")
        self.assertEqual(recovered.receipt["admission_id"], aid)
        self.assertEqual(len(store.list_occurrences("dkt_every")), 1)
        store.close()

    def test_retry_keeps_the_same_occurrence(self):
        import json
        store, adapter, clock, sched = _world()
        store.insert(_recurring("dkt_every", clock.now(), every=60, count=3))
        adapter.unavailable = True  # Proven pre-dispatch validation failure.
        sched.tick()
        occ_id = store.get("dkt_every").spec["open_occurrence_id"]
        self.assertEqual(store.get_occurrence(occ_id).attempts, 1)
        prepared = store.get_request(occ_id)
        self.assertEqual(prepared["dispatch_state"], "never")
        adapter.unavailable = False
        clock.advance(120)
        sched.tick()
        self.assertEqual(len(adapter.sent), 1)
        self.assertIn(occ_id, adapter.sent[0]["message"])
        self.assertEqual(len(store.list_occurrences("dkt_every")), 1)
        self.assertEqual(adapter.sent[0]["admission_id"], prepared["request_id"])
        self.assertEqual(adapter.sent[0]["message"], json.loads(prepared["frozen"]["body"])["text"])
        store.close()

    def test_expired_lease_retries_the_open_occurrence(self):
        store, adapter, clock, sched = _world()
        start = clock.now()
        store.insert(_recurring("dkt_every", start, every=60, count=3))
        adapter.unavailable = True
        sched.tick()
        occ = store.list_occurrences("dkt_every")[0]
        prepared = store.get_request(occ.id)
        self.assertEqual(prepared["dispatch_state"], "never")
        self.assertEqual(prepared["facts"], {})
        self.assertEqual(adapter.sent, [])
        # A genuinely unsent claim in the same daemon expires before preflight.
        # Do not resurrect a consumed occurrence or erase any positive evidence.
        self.assertTrue(store.claim_occurrence(occ.id, clock.now()))
        occ = store.get_occurrence(occ.id)
        occ.firing_at = start - timedelta(seconds=120)
        aid = prepared["request_id"]
        store.save_occurrence(occ)
        parent = store.get("dkt_every")
        spec = dict(parent.spec)
        spec["open_occurrence_id"] = occ.id
        parent.spec = spec
        parent.status = "pending"
        parent.fire_at = occ.scheduled_at
        parent.next_attempt_at = clock.now() + timedelta(hours=2)
        store.save(parent)
        adapter.unavailable = False
        clock.advance(1)
        sched.tick()
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(adapter.sent[0]["admission_id"], aid)
        self.assertIn(occ.id, adapter.sent[0]["message"])
        self.assertEqual(len(store.list_occurrences("dkt_every")), 1)
        self.assertEqual(store.get_request(occ.id)["frozen"], prepared["frozen"])
        self.assertEqual(store.get_occurrence(occ.id).attempts, 3)
        store.close()

    def test_cancel_stops_later_repeats_and_a_retry(self):
        store, adapter, clock, sched = _world()
        store.insert(_recurring("dkt_every", clock.now(), every=60, count=5))
        sched.tick()
        self.assertEqual(store.cancel("dkt_every", clock.now()), "cancelled")
        clock.advance(3600)
        sched.tick()
        self.assertEqual(len(adapter.sent), 1)
        adapter.sent.clear()
        store.insert(_recurring("dkt_retry", clock.now(), every=60, count=5))
        adapter.fail = DeliveryError("unavailable", "down", retryable=True)
        sched.tick()
        store.cancel("dkt_retry", clock.now())
        adapter.fail = None
        clock.advance(120)
        sched.tick()
        self.assertEqual(adapter.sent, [])
        self.assertEqual(store.get("dkt_retry").status, "cancelled")
        store.close()

    def test_count_and_until_finish_the_series(self):
        store, adapter, clock, sched = _world()
        start = clock.now()
        store.insert(_recurring("dkt_count", start, every=60, count=2))
        sched.tick()
        clock.advance(60)
        sched.tick()
        clock.advance(60)
        sched.tick()
        self.assertEqual(len(adapter.sent), 2)
        self.assertEqual(store.get("dkt_count").status, "completed")
        self.assertIn("schedule complete", adapter.sent[-1]["message"])

        now = clock.now()
        until = now + timedelta(seconds=90)
        item = _recurring("dkt_until", now, every=60, count=None)
        item.spec["until"] = until.isoformat()
        item.spec["anchor"] = now.isoformat()
        store.insert(item)
        sched.tick()
        clock.advance(60)
        sched.tick()
        clock.advance(60)
        sched.tick()
        self.assertEqual(store.get("dkt_until").status, "completed")
        self.assertEqual(len([sent for sent in adapter.sent if "dkt_until" in sent["message"]]), 2)
        store.close()

    def test_bad_target_stops_and_exhausted_retries_are_reported(self):
        store, adapter, clock, sched = _world()
        store.insert(_recurring("dkt_bad", clock.now(), every=60, count=3, session="ses_missing0001"))
        sched.tick()
        self.assertEqual(adapter.sent, [])
        self.assertEqual(store.get("dkt_bad").status, "undeliverable")
        self.assertEqual(store.list_occurrences("dkt_bad")[0].status, "undeliverable")

        store.insert(_recurring("dkt_blip", clock.now(), every=60, count=3))
        adapter.unavailable = True  # Exhaust only proven pre-dispatch failures.
        sched.tick()
        for _ in range(8):
            clock.advance(120)
            sched.tick()
        self.assertEqual(store.list_occurrences("dkt_blip")[0].status, "undeliverable")
        self.assertEqual(store.get("dkt_blip").status, "pending")
        first = store.list_occurrences("dkt_blip")[0]
        self.assertEqual(store.get_request(first.id)["dispatch_state"], "never")
        self.assertEqual(adapter.sent, [])
        adapter.unavailable = False
        clock.advance(3600)
        sched.tick()
        self.assertEqual(len(adapter.sent), 1)
        self.assertIn("Previous undeliverable:", adapter.sent[0]["message"])
        self.assertEqual(store.occurrence_counts()["dkt_blip"]["undeliverable"], 1)
        store.close()

    def test_urgent_recurring_steers(self):
        store, adapter, clock, sched = _world()
        store.insert(_recurring("dkt_now", clock.now(), every=60, count=1, urgent=True))
        sched.tick()
        self.assertEqual(adapter.sent[0]["delivery"], "steer")
        self.assertTrue(adapter.sent[0]["urgent"])
        self.assertEqual(store.get("dkt_now").status, "completed")
        store.close()


class WatchFireTests(unittest.TestCase):
    def test_file_appears_once_and_expires(self):
        files = ScriptedStat(FileView("missing"))
        store, adapter, clock, sched = _world(files)
        store.insert(_watch("dkt_file", clock.now(), check="file_appears", path="/tmp/download.bin"))
        sched.tick()
        self.assertEqual(adapter.sent, [])
        self.assertEqual(store.get("dkt_file").status, "pending")
        files.view = FileView("file", dev=1, ino=2, size=10, mtime_ns=3)
        store.get("dkt_file")
        item = store.get("dkt_file")
        item.fire_at = clock.now()
        store.save(item)
        sched.tick()
        self.assertEqual(len(adapter.sent), 1)
        self.assertIn("Check: file_appears", adapter.sent[0]["message"])
        self.assertIn("Occurrence: occ_", adapter.sent[0]["message"])
        self.assertEqual(adapter.sent[0]["delivery"], "queue")
        self.assertEqual(store.get("dkt_file").status, "delivered")
        sched.tick()
        self.assertEqual(len(adapter.sent), 1)

        store.insert(_watch("dkt_dir", clock.now(), check="file_appears", path="/tmp/dir"))
        files.view = FileView("other")
        sched.tick()
        self.assertEqual(store.get("dkt_dir").status, "pending")
        files.view = FileView("missing")
        item = store.get("dkt_dir")
        item.spec = dict(item.spec)
        item.spec["until"] = (clock.now() - timedelta(seconds=1)).isoformat()
        item.fire_at = clock.now()
        store.save(item)
        sched.tick()
        self.assertEqual(store.get("dkt_dir").status, "expired")
        self.assertEqual(len(adapter.sent), 1)
        store.close()

    def test_file_stable_is_monotonic(self):
        files = ScriptedStat(FileView("file", dev=1, ino=9, size=10, mtime_ns=100))
        store, adapter, clock, sched = _world(files)
        store.insert(_watch(
            "dkt_grow", clock.now(), check="file_stable", path="/tmp/download.bin", stable_for=10,
        ))
        sched.tick()
        self.assertEqual(adapter.sent, [])
        clock.advance(5)
        item = store.get("dkt_grow")
        item.fire_at = clock.now()
        store.save(item)
        sched.tick()
        self.assertEqual(adapter.sent, [])
        files.view = FileView("file", dev=1, ino=9, size=40, mtime_ns=200)
        clock.advance(30)
        item = store.get("dkt_grow")
        item.fire_at = clock.now()
        store.save(item)
        sched.tick()
        self.assertEqual(adapter.sent, [])
        files.view = FileView("file", dev=1, ino=9, size=40, mtime_ns=200)
        clock.advance(10)
        item = store.get("dkt_grow")
        item.fire_at = clock.now()
        store.save(item)
        sched.tick()
        self.assertEqual(len(adapter.sent), 1)
        self.assertIn("file_stable", adapter.sent[0]["message"])
        self.assertEqual(store.get("dkt_grow").status, "delivered")
        store.close()

    def test_identity_change_and_shrink_do_not_fire(self):
        files = ScriptedStat(FileView("file", dev=1, ino=1, size=5, mtime_ns=1))
        store, adapter, clock, sched = _world(files)
        store.insert(_watch("dkt_id", clock.now(), check="file_stable", path="/tmp/a", stable_for=10))
        sched.tick()
        files.view = FileView("file", dev=1, ino=2, size=5, mtime_ns=1)
        clock.advance(30)
        item = store.get("dkt_id")
        item.fire_at = clock.now()
        store.save(item)
        sched.tick()
        self.assertEqual(adapter.sent, [])
        files.view = FileView("file", dev=1, ino=2, size=1, mtime_ns=1)
        clock.advance(30)
        item = store.get("dkt_id")
        item.fire_at = clock.now()
        store.save(item)
        sched.tick()
        self.assertEqual(adapter.sent, [])
        self.assertEqual(store.get("dkt_id").status, "pending")
        store.close()

    def test_process_pid_reuse_and_ps_outage(self):
        reader = ScriptedProcess("Thu Oct 16 09:00:00 2026")
        store, adapter, clock, sched = _world(process_reader=reader)
        store.insert(_watch("dkt_pid", clock.now(), check="process_ended", pid=4242, process_start=reader.start))
        sched.tick()
        self.assertEqual(adapter.sent, [])
        reader.start = None
        item = store.get("dkt_pid")
        item.fire_at = clock.now()
        store.save(item)
        sched.tick()
        self.assertEqual(adapter.sent[0]["message"].count("Occurrence:"), 1)
        self.assertIn("exited", adapter.sent[0]["message"])

        reader.start = "Thu Oct 16 09:00:00 2026"
        reader.error = TimeoutError("ps timed out")
        store.insert(_watch(
            "dkt_ps", clock.now(), check="process_ended", pid=4242,
            process_start="Thu Oct 16 09:00:00 2026",
        ))
        sched.tick()
        self.assertEqual(store.get("dkt_ps").status, "pending")
        self.assertEqual(len(adapter.sent), 1)
        reader.error = None
        reader.start = "Thu Oct 16 09:05:00 2026"
        item = store.get("dkt_ps")
        item.fire_at = clock.now()
        store.save(item)
        sched.tick()
        self.assertIn("pid_reused", adapter.sent[-1]["message"])
        store.close()

    def test_url_and_session_outages_do_not_match(self):
        probe = ScriptedProbe((500, "http://127.0.0.1/health"))
        store, adapter, clock, sched = _world(url_probe=probe)
        store.insert(_watch("dkt_url", clock.now(), check="url_answers", url="http://127.0.0.1/health"))
        sched.tick()
        self.assertEqual(adapter.sent, [])
        probe.result = TimeoutError("timed out")
        item = store.get("dkt_url")
        item.fire_at = clock.now()
        store.save(item)
        sched.tick()
        self.assertEqual(store.get("dkt_url").status, "pending")
        probe.result = (200, "http://127.0.0.1/health")
        item = store.get("dkt_url")
        item.fire_at = clock.now()
        store.save(item)
        sched.tick()
        self.assertEqual(len(adapter.sent), 1)
        self.assertIn('"status": 200', adapter.sent[0]["message"])
        self.assertEqual(adapter.sent[0]["delivery"], "queue")

        adapter.active.add("ses_knownsession0001")
        store.insert(_watch("dkt_busy", clock.now(), check="session_idle"))
        sched.tick()
        self.assertEqual(len(adapter.sent), 1)
        adapter.active.discard("ses_knownsession0001")
        item = store.get("dkt_busy")
        item.fire_at = clock.now()
        store.save(item)
        sched.tick()
        self.assertEqual(len(adapter.sent), 2)
        self.assertIn("session_idle", adapter.sent[-1]["message"])

        adapter.unavailable = True
        store.insert(_watch("dkt_down", clock.now(), check="session_idle"))
        sched.tick()
        self.assertEqual(store.get("dkt_down").status, "pending")
        adapter.unavailable = False
        adapter.known.discard("ses_knownsession0001")
        item = store.get("dkt_down")
        item.fire_at = clock.now()
        store.save(item)
        sched.tick()
        self.assertEqual(store.get("dkt_down").status, "undeliverable")
        self.assertEqual(len(adapter.sent), 2)
        store.close()

    def test_latched_watch_retries_without_rechecking(self):
        files = ScriptedStat(FileView("file", dev=1, ino=1, size=3, mtime_ns=1))
        store, adapter, clock, sched = _world(files)
        store.insert(_watch("dkt_latch", clock.now(), check="file_appears", path="/tmp/a", urgent=True))
        adapter.unavailable = True  # proven preflight failure, not a possible send
        sched.tick()
        self.assertEqual(adapter.sent, [])
        occ = store.list_occurrences("dkt_latch")[0]
        before = store.get_request(occ.id)
        self.assertEqual(before["dispatch_state"], "never")
        self.assertEqual(occ.attempts, 1)
        self.assertTrue(store.get("dkt_latch").spec["latched"])
        checks = store.get("dkt_latch").spec["checks"]
        files.view = FileView("missing")
        adapter.unavailable = False
        clock.advance(30)
        sched.tick()
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(adapter.sent[0]["delivery"], "steer")
        self.assertIn("file_appears", adapter.sent[0]["message"])
        self.assertEqual(adapter.sent[0]["admission_id"], before["request_id"])
        self.assertEqual(adapter.sent[0]["message"], json.loads(before["frozen"]["body"])["text"])
        self.assertEqual(store.get_request(occ.id)["frozen"], before["frozen"])
        self.assertEqual(store.get_occurrence(occ.id).attempts, 2)
        self.assertEqual(store.get("dkt_latch").spec["checks"], checks)
        self.assertEqual([o.id for o in store.list_occurrences("dkt_latch")], [occ.id])
        store.close()

    def test_latched_watch_possible_dispatch_never_retries_or_relatches(self):
        files = ScriptedStat(FileView("file", dev=1, ino=1, size=3, mtime_ns=1))
        store, adapter, clock, sched = _world(files)
        self.addCleanup(store._tmp.cleanup)
        self.addCleanup(store.close)
        store.insert(_watch("dkt_latch", clock.now(), check="file_appears", path="/tmp/a", urgent=True))
        adapter.fail = DeliveryError("unavailable", "down", retryable=True)
        sched.tick()
        occ = store.list_occurrences("dkt_latch")[0]
        before = store.get_request(occ.id)
        self.assertEqual(occ.status, "ambiguous")
        self.assertEqual(before["dispatch_state"], "possible")
        checks = store.get("dkt_latch").spec["checks"]
        files.view = FileView("missing")
        adapter.fail = None
        clock.advance(30)
        sched.tick()
        self.assertEqual(adapter.sent, [])
        self.assertEqual([o.id for o in store.list_occurrences("dkt_latch")], [occ.id])
        self.assertEqual(store.get_occurrence(occ.id).status, "ambiguous")
        self.assertEqual(store.get_occurrence(occ.id).attempts, 1)
        self.assertTrue(store.get("dkt_latch").spec["latched"])
        self.assertEqual(store.get("dkt_latch").spec["checks"], checks)
        self.assertEqual(store.get_request(occ.id)["frozen"], before["frozen"])
        self.assertEqual(sched.recover(store.get("dkt_latch"), store.get_occurrence(occ.id), "retry"),
                         (409, "acceptance_unknown"))


class MigrationTests(unittest.TestCase):
    def test_m1_database_opens_and_keeps_its_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "docket.db"
            db = sqlite3.connect(path)
            db.executescript(M1_SCHEMA)
            db.execute(
                """INSERT INTO items (
                     id, kind, status, fire_at, created_at, updated_at, reason,
                     context_json, target_json, urgent, created_by, late,
                     late_by_seconds, delivered_at, cancelled_at, error,
                     receipt_json, attempts, next_attempt_at, firing_at, spec_json
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "dkt_old", "timer", "pending", "2026-10-16T18:00:00+00:00",
                    "2026-10-16T12:00:00+00:00", "2026-10-16T12:00:00+00:00",
                    "keep me", "{}", '{"adapter":"opencode","session_id":"ses_knownsession0001"}',
                    0, "ses_setter", 0, None, None, None, None, None, 0, None, None, None,
                ),
            )
            db.commit()
            db.close()
            store = Store(path)
            loaded = store.get("dkt_old")
            self.assertEqual(loaded.reason, "keep me")
            self.assertEqual(loaded.status, "pending")
            self.assertEqual(loaded.kind, "timer")
            self.assertIsNone(loaded.spec)
            tables = {
                row[0] for row in store._db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("occurrences", tables)
            columns = [row[1] for row in store._db.execute("PRAGMA table_info(items)")]
            self.assertNotIn("occurrence_id", columns)
            store.close()
            again = Store(path)
            self.assertEqual(again.get("dkt_old").reason, "keep me")
            again.close()


class LimitTests(unittest.TestCase):
    def test_actor_and_target_caps(self):
        rows = []
        for index in range(MAX_RECURRING_PER_TARGET):
            rows.append(_recurring(f"dkt_{index}", utc(2026, 10, 16, 12), session="ses_knownsession0001"))
        with self.assertRaises(Exception) as caught:
            check_limits(rows, kind="recurring", session_id="ses_knownsession0001", created_by="ses_other")
        self.assertIn("recurring", str(caught.exception))


def _world(files=None, process_reader=None, url_probe=None):
    tmp = tempfile.TemporaryDirectory()
    store = Store(Path(tmp.name) / "docket.db")
    store._tmp = tmp
    adapter = FakeAdapter()
    clock = FakeClock(utc(2026, 10, 16, 12, 0, 0))
    checks = Checks(
        adapter,
        file_stat=files or (lambda _path: FileView("missing")),
        process_reader=process_reader or (lambda _pid: "start"),
        url_probe=url_probe or (lambda _url: (500, _url)),
    )
    return store, adapter, clock, Scheduler(store, adapter, clock, checks)


def _recurring(item_id, fire_at, *, every=60, count=5, urgent=False, session="ses_knownsession0001"):
    now = utc(2026, 10, 16, 11, 0, 0)
    spec = {
        "every": every,
        "anchor": fire_at.isoformat(),
        "next_seq": 1,
        "open_occurrence_id": None,
    }
    if count is not None:
        spec["count"] = count
    return Item(
        id=item_id, kind="recurring", status="pending", fire_at=fire_at,
        created_at=now, updated_at=now, reason="check again", context={},
        target={"adapter": "opencode", "session_id": session}, urgent=urgent,
        created_by="ses_setter", late=False, late_by_seconds=None,
        delivered_at=None, cancelled_at=None, error=None, receipt=None,
        attempts=0, next_attempt_at=None, firing_at=None, spec=spec,
    )


def _watch(item_id, fire_at, *, check, path=None, stable_for=None, pid=None,
           process_start=None, url=None, urgent=False):
    now = utc(2026, 10, 16, 11, 0, 0)
    spec = {
        "check": check,
        "every": 15,
        "until": (fire_at + timedelta(hours=2)).isoformat(),
        "checks": 0,
        "latched": False,
        "open_occurrence_id": None,
        "next_seq": 1,
    }
    if path:
        spec["path"] = path
    if stable_for:
        spec["stable_for"] = stable_for
    if pid:
        spec["pid"] = pid
        spec["process_start"] = process_start
    if url:
        spec["url"] = url
        spec["expect"] = "2xx"
    if check == "session_idle":
        spec["watch_session"] = "ses_knownsession0001"
    return Item(
        id=item_id, kind="watch", status="pending", fire_at=fire_at,
        created_at=now, updated_at=now, reason="wait for it", context={},
        target={"adapter": "opencode", "session_id": "ses_knownsession0001"},
        urgent=urgent, created_by="ses_setter", late=False, late_by_seconds=None,
        delivered_at=None, cancelled_at=None, error=None, receipt=None,
        attempts=0, next_attempt_at=None, firing_at=None, spec=spec,
    )


def _occurrence_marker(message: str) -> str:
    for line in message.splitlines():
        if line.startswith("Occurrence: "):
            return line
    return ""


class ScriptedStat:
    def __init__(self, view):
        self.view = view

    def __call__(self, _path):
        return self.view


class ScriptedProcess:
    def __init__(self, start):
        self.start = start
        self.error = None

    def __call__(self, _pid):
        if self.error:
            raise self.error
        return self.start


class ScriptedProbe:
    def __init__(self, result):
        self.result = result

    def __call__(self, url):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class WaitingWatchDiagnosticTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import Mock

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "docket.db")
        self.addCleanup(self.store.close)
        self.adapter = FakeAdapter()
        self.adapter.validate = Mock(wraps=self.adapter.validate)
        self.clock = FakeClock(utc(2026, 10, 21, 12, 0, 0))
        self.view = FileView("missing")
        checks = Checks(self.adapter, file_stat=lambda _path: self.view)
        self.scheduler = Scheduler(self.store, self.adapter, self.clock, checks=checks)
        self.item = _watch("dkt_waiting", self.clock.now(), check="file_appears", path="/unused/ready.txt")
        self.store.insert(self.item)

    def poll(self):
        from docket.api import _public_item

        self.scheduler._poll_watch(self.store.get(self.item.id))
        return _public_item(self.store, self.store.get(self.item.id))

    def test_absent_file_is_waiting_not_provider_validation_failure(self):
        body = self.poll()
        # A second normal check must also stay clean, without contacting delivery.
        self.clock.advance(30)
        body = self.poll()
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["attempts"], 0)
        self.assertEqual(body["spec"]["checks"], 2)
        self.assertFalse(body["spec"]["latched"])
        self.assertIsNone(body["spec"]["open_occurrence_id"])
        self.assertEqual(body["spec"]["next_seq"], 1)
        self.assertIsNone(self.store.get_request(self.item.id))
        self.assertEqual(body["delivery"]["recovery"], {
            "reconcile": False, "retry": False, "refusal_code": "not_ready"})
        self.adapter.validate.assert_not_called()
        self.assertEqual(self.adapter.sent, [])
        self.assertIsNone(body["delivery"]["issue"])
        self.assertIsNone(body["error"])

    def test_unavailable_check_remains_visible_then_normal_wait_clears_it(self):
        self.view = FileView("unavailable", detail="file check timed out")
        body = self.poll()
        self.assertEqual(body["delivery"]["issue"]["code"], "unavailable")
        self.assertEqual(body["status"], "pending")
        self.view = FileView("missing")
        self.clock.advance(30)
        body = self.poll()
        self.assertIsNone(body["delivery"]["issue"])
        self.assertIsNone(body["error"])
        self.adapter.validate.assert_not_called()

    def test_met_condition_still_validates_and_refuses_unavailable_provider(self):
        self.view = FileView("file", dev=7, ino=8, size=1, mtime_ns=1)
        self.adapter.unavailable = True
        self.poll()
        self.adapter.validate.assert_called_once()
        item = self.store.get(self.item.id)
        self.assertTrue(item.spec["latched"])
        occ = self.store.open_occurrence(item)
        req = self.store.get_request(occ.id)
        self.assertEqual(req["issue"]["code"], "unavailable")
        self.assertEqual(req["dispatch_state"], "never")
        self.assertEqual(self.adapter.sent, [])


class ApiScheduleTests(unittest.TestCase):
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

    def test_meta_recurring_watch_and_occurrence_report(self):
        _status, meta = request("GET", "/api/v1/meta")
        self.assertIn("recurring", meta["kinds"])
        self.assertIn("watch", meta["kinds"])
        self.assertEqual(meta["reserved"], [])
        self.assertIn("file_stable", meta["checks"])
        self.assertEqual(meta["limits"]["min_recurring_interval_seconds"], 60)
        self.assertIn("queue", meta["delivery"])
        self.assertIn("steer", meta["delivery"])
        status, created = request("POST", "/api/v1/recurring", {
            "every": "60s",
            "count": 2,
            "reason": "again",
            "session_id": "ses_knownsession0001",
            "created_by": "ses_setter",
        })
        self.assertEqual(status, 201)
        item_id = created["item"]["id"]
        self.clock.advance(60)
        self.server.scheduler.tick()
        _status, listed = request("GET", f"/api/v1/items/{item_id}/occurrences")
        self.assertEqual(listed["occurrences"][0]["status"], "delivered")
        self.assertNotEqual(listed["occurrences"][0]["id"], item_id)
        with self.assertRaises(ClientError) as caught:
            request("POST", "/api/v1/recurring", {
                "every": "15m", "reason": "no bound", "session_id": "ses_knownsession0001",
            })
        self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(ClientError) as caught:
            request("POST", "/api/v1/watches", {
                "check": "file_appears", "path": "/tmp/x", "until": "1h",
                "reason": "nope", "session_id": "ses_knownsession0001",
                "command": "echo hi",
            })
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("command", caught.exception.detail)

    def test_per_target_recurring_limit(self):
        for _ in range(MAX_RECURRING_PER_TARGET):
            request("POST", "/api/v1/recurring", {
                "every": "15m", "count": 2, "reason": "cap",
                "session_id": "ses_knownsession0001",
            })
        with self.assertRaises(ClientError) as caught:
            request("POST", "/api/v1/recurring", {
                "every": "15m", "count": 2, "reason": "over",
                "session_id": "ses_knownsession0001",
            })
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "limit")


if __name__ == "__main__":
    unittest.main()
