"""Persistence, cancel, scheduling, lateness, and target checks."""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from docket.clock import FakeClock
from docket.delivery import DeliveryError
from docket.parse import parse_alarm, parse_delay, wake_text
from docket.scheduler import Scheduler
from docket.store import Item, Store
from tests.support import FakeAdapter, utc

from zoneinfo import ZoneInfo


class ParseTests(unittest.TestCase):
    def test_delay_phrases(self):
        now = utc(2026, 10, 16, 12, 0, 0)
        self.assertEqual(parse_delay("20m", now=now), now + timedelta(minutes=20))
        self.assertEqual(parse_delay("in 20 minutes", now=now), now + timedelta(minutes=20))
        self.assertEqual(parse_delay(90, now=now), now + timedelta(seconds=90))
        self.assertEqual(parse_delay("1h30m", now=now), now + timedelta(hours=1, minutes=30))

    def test_clock_time_rolls_to_tomorrow(self):
        zone = ZoneInfo("America/New_York")
        now = utc(2026, 10, 16, 14, 30, 0)  # 10:30 EDT
        fire = parse_alarm("09:00", now=now, zone=zone)
        local = fire.astimezone(zone)
        self.assertEqual((local.hour, local.minute, local.day), (9, 0, 17))

    def test_clock_time_today_if_still_ahead(self):
        zone = ZoneInfo("America/New_York")
        now = utc(2026, 10, 16, 12, 0, 0)  # 08:00 EDT
        fire = parse_alarm("09:00", now=now, zone=zone)
        local = fire.astimezone(zone)
        self.assertEqual((local.hour, local.minute, local.day), (9, 0, 16))

    def test_past_iso_is_kept(self):
        now = utc(2026, 10, 16, 14, 0, 0)
        fire = parse_alarm("2026-10-16T13:00:00+00:00", now=now)
        self.assertEqual(fire, utc(2026, 10, 16, 13, 0, 0))


class StoreTests(unittest.TestCase):
    def test_persists_across_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "docket.db"
            store = Store(path)
            now = utc(2026, 10, 16, 12, 0, 0)
            store.insert(_item("dkt_one", now))
            store.close()
            again = Store(path)
            loaded = again.get("dkt_one")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.reason, "check the file")
            self.assertEqual(loaded.status, "pending")
            again.close()

    def test_cancel_pending_and_missing(self):
        store, now = _store()
        store.insert(_item("dkt_one", now))
        self.assertEqual(store.cancel("dkt_one", now), "cancelled")
        self.assertEqual(store.get("dkt_one").status, "cancelled")
        self.assertEqual(store.cancel("dkt_missing", now), "missing")
        self.assertEqual(store.cancel("dkt_one", now), "terminal")
        store.close()


class SchedulerTests(unittest.TestCase):
    def test_fires_due_item_and_leaves_future(self):
        store, adapter, clock, sched = _world()
        now = clock.now()
        store.insert(_item("dkt_due", now - timedelta(seconds=1)))
        store.insert(_item("dkt_later", now + timedelta(hours=1)))
        sched.tick()
        self.assertEqual(store.get("dkt_due").status, "delivered")
        self.assertEqual(store.get("dkt_later").status, "pending")
        self.assertEqual(adapter.sent[0]["delivery"], "queue")
        self.assertIn("Item: dkt_due", adapter.sent[0]["message"])
        self.assertNotIn("echo ", adapter.sent[0]["message"])
        store.close()

    def test_cancel_before_tick_does_not_deliver(self):
        store, adapter, clock, sched = _world()
        now = clock.now()
        store.insert(_item("dkt_due", now - timedelta(seconds=1)))
        self.assertEqual(store.cancel("dkt_due", now), "cancelled")
        sched.tick()
        self.assertEqual(adapter.sent, [])
        self.assertEqual(store.get("dkt_due").status, "cancelled")
        store.close()

    def test_bad_archived_and_wrong_at_fire(self):
        store, adapter, clock, sched = _world()
        now = clock.now()
        store.insert(_item("dkt_bad", now, session="ses_missing0001"))
        store.insert(_item("dkt_arch", now, session="ses_archived0001"))
        adapter.archived.add("ses_archived0001")
        adapter.known.add("ses_archived0001")
        adapter.known.add("ses_moved0001")
        adapter.directories["ses_moved0001"] = "/actual"
        store.insert(_item(
            "dkt_wrong", now, session="ses_moved0001", directory="/expected",
        ))
        sched.tick()
        self.assertIn("bad_target", store.get("dkt_bad").error)
        self.assertEqual(store.get("dkt_bad").status, "undeliverable")
        self.assertIn("archived", store.get("dkt_arch").error)
        self.assertIn("wrong_target", store.get("dkt_wrong").error)
        self.assertEqual(adapter.sent, [])
        store.close()

    def test_urgent_uses_steer_and_late_is_reported(self):
        store, adapter, clock, sched = _world()
        past = clock.now() - timedelta(minutes=12)
        store.insert(_item("dkt_late", past, urgent=True))
        sched.tick()
        sent = adapter.sent[0]
        self.assertEqual(sent["delivery"], "steer")
        self.assertTrue(sent["urgent"])
        self.assertIn("Late: yes", sent["message"])
        self.assertIn("12 minutes", sent["message"])
        self.assertNotIn("asleep", sent["message"])
        self.assertNotIn("down", sent["message"])
        self.assertTrue(store.get("dkt_late").late)
        store.close()

    def test_on_time_is_not_late(self):
        store, adapter, clock, sched = _world()
        store.insert(_item("dkt_ok", clock.now() - timedelta(seconds=1)))
        sched.tick()
        self.assertIn("Late: no", adapter.sent[0]["message"])
        self.assertFalse(store.get("dkt_ok").late)
        store.close()

    def test_context_is_data_not_a_command(self):
        store, adapter, clock, sched = _world()
        item = _item("dkt_cmd", clock.now())
        item.context = {"command": "echo should-not-run"}
        store.insert(item)
        sched.tick()
        self.assertIn("echo should-not-run", adapter.sent[0]["message"])
        self.assertEqual(len(adapter.sent), 1)
        store.close()

    def test_retry_then_undeliverable(self):
        store, adapter, clock, sched = _world()
        # Validation outage is positive proof of pre-dispatch failure, unlike an
        # exception raised inside deliver (which must remain status-only).
        adapter.unavailable = True
        store.insert(_item("dkt_retry", clock.now()))
        sched.tick()
        self.assertEqual(store.get("dkt_retry").status, "pending")
        self.assertEqual(store.get("dkt_retry").attempts, 1)
        self.assertEqual(store.get("dkt_retry").next_attempt_at, clock.now() + timedelta(seconds=2))
        sched.tick()
        self.assertEqual(store.get("dkt_retry").attempts, 1)
        for _ in range(8):
            clock.advance(120)
            sched.tick()
        self.assertEqual(store.get("dkt_retry").status, "undeliverable")
        request = store.get_request("dkt_retry")
        self.assertEqual(request["dispatch_state"], "never")
        self.assertEqual(store.get("dkt_retry").attempts, 8)
        self.assertEqual(adapter.sent, [])
        store.close()

    def test_reopen_does_not_lose_a_pending_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "docket.db"
            clock = FakeClock(utc(2026, 10, 16, 12, 0, 0))
            store = Store(path)
            store.insert(_item("dkt_keep", clock.now() + timedelta(minutes=5)))
            store.close()
            store = Store(path)
            adapter = FakeAdapter()
            sched = Scheduler(store, adapter, clock)
            sched.tick()
            self.assertEqual(adapter.sent, [])
            self.assertEqual(store.get("dkt_keep").status, "pending")
            store.close()


def _store():
    tmp = tempfile.TemporaryDirectory()
    store = Store(Path(tmp.name) / "docket.db")
    store._tmp = tmp
    return store, utc(2026, 10, 16, 12, 0, 0)


def _world():
    tmp = tempfile.TemporaryDirectory()
    store = Store(Path(tmp.name) / "docket.db")
    store._tmp = tmp
    adapter = FakeAdapter()
    clock = FakeClock(utc(2026, 10, 16, 12, 0, 0))
    return store, adapter, clock, Scheduler(store, adapter, clock)


def _item(item_id, fire_at, *, session="ses_knownsession0001", urgent=False,
          directory=None, item_id_override=None):
    now = utc(2026, 10, 16, 11, 0, 0)
    target = {"adapter": "opencode", "session_id": session}
    if directory:
        target["directory"] = directory
    return Item(
        id=item_id_override or item_id,
        kind="timer",
        status="pending",
        fire_at=fire_at,
        created_at=now,
        updated_at=now,
        reason="check the file",
        context={},
        target=target,
        urgent=urgent,
        created_by="ses_setter",
        late=False,
        late_by_seconds=None,
        delivered_at=None,
        cancelled_at=None,
        error=None,
        receipt=None,
        attempts=0,
        next_attempt_at=None,
        firing_at=None,
        spec=None,
    )


class WakeTextTests(unittest.TestCase):
    def test_notice_names_the_item(self):
        text = wake_text(
            kind="alarm", item_id="dkt_x", reason="morning check",
            fire_at=utc(2026, 10, 16, 13, 0, 0), now=utc(2026, 10, 16, 13, 8, 0),
            late=True, urgent=False, created_by="ses_chief", context={"file": "a"},
            zone=ZoneInfo("UTC"),
        )
        self.assertIn("Item: dkt_x", text)
        self.assertIn("morning check", text)
        self.assertIn("Docket did not run anything.", text)


if __name__ == "__main__":
    unittest.main()
