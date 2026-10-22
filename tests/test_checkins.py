"""Isolated source tests: fake host/clock, no service, sockets or live files."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from docket.checkins import Checkins, roster
from docket.checks import Checks
from docket.delivery import DeliveryError, OpenCodeAdapter
from docket.scheduler import Scheduler
from docket.store import Store
from tests.support import FakeAdapter

SID = "ses_knownsession0001"


class Clock:
    value = datetime(2026, 10, 22, tzinfo=timezone.utc)

    def now(self):
        return self.value


class Inline:
    def __init__(self):
        self.results = {}

    def submit(self, key, fn):
        self.results[key] = fn()
        return True

    def take(self, key):
        return self.results.pop(key, None)

    def wait(self, keys, timeout):
        pass


class Host(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.goal = {"id": "goal-1", "createdAt": 1792641904418, "status": "active"}

    def session_goal(self, sid):
        if self.unavailable:
            raise DeliveryError("unavailable", "offline")
        return self.goal


class CheckinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "test.sqlite")
        self.clock = Clock()
        self.host = Host()
        self.scheduler = Scheduler(self.store, self.host, self.clock, check_executor=Inline())
        self.manager = self.scheduler.checkins
        self.row = {"session": SID, "directory": "/fixture", "state": "active"}
        self.write()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def write(self):
        self.manager.roster_path.write_text(json.dumps(self.row) + "\n")
        self.manager.roster_path.chmod(0o600)

    def tick(self, minutes=0):
        if not minutes:
            self.scheduler.tick()
        for _ in range(minutes):
            self.clock.value += timedelta(minutes=1)
            self.scheduler.tick()

    def item(self, kind="idle"):
        ident = self.manager.state["leads"][SID]["slots"][kind]["id"]
        return self.store.get(ident)

    def test_idle_29_30_and_consumed_cooldown(self):
        self.tick()
        self.tick(29)
        self.assertEqual(len(self.host.sent), 0)
        self.tick(1)
        self.assertEqual(len(self.host.sent), 1)
        self.assertIn("You've been idle", self.host.sent[0]["message"])
        self.tick(29)
        self.assertEqual(len(self.host.sent), 1)
        self.tick(2)
        self.assertEqual(len(self.host.sent), 2)
        self.assertEqual(len({x["admission_id"] for x in self.host.sent}), 2)

    def test_busy_resets_idle_but_not_milestone(self):
        self.tick()
        self.tick(29)
        self.host.active.add(SID)
        self.tick(1)
        self.assertEqual(len(self.host.sent), 0)
        self.host.active.clear()
        self.tick(30)
        self.assertEqual(len(self.host.sent), 0)
        self.tick(1)
        self.assertEqual(len(self.host.sent), 1)

    def test_milestone_179_180_active_and_once_per_epoch(self):
        self.host.active.add(SID)
        self.tick()
        self.tick(179)
        self.assertEqual(len(self.host.sent), 0)
        self.host.goal["updatedAt"] = 9999999
        self.host.goal["tokensUsed"] = 123
        self.tick(1)
        self.assertEqual(len(self.host.sent), 1)
        self.assertIn("no recorded visible milestone", self.host.sent[0]["message"])
        self.tick(179)
        self.assertEqual(len(self.host.sent), 1)
        self.tick(2)
        self.assertEqual(len(self.host.sent), 2)

    def test_explicit_marker_resets_baseline(self):
        self.host.active.add(SID)
        self.tick()
        self.tick(179)
        self.row.update(milestone_at=self.clock.now().isoformat(), milestone_ref="accepted screenshot")
        self.write()
        self.tick(1)
        self.assertEqual(len(self.host.sent), 0)
        self.tick(180)
        self.assertEqual(len(self.host.sent), 1)

    def test_hold_remove_done_complete_missing_before_match_and_send(self):
        for change in ("hold", "done", "remove", "complete", "missing", "identity"):
            with self.subTest(change=change):
                self.row["state"] = "active"
                self.host.goal = {"id": "g" + change, "createdAt": 123, "status": "blocked"}
                self.write()
                self.manager.reconcile(self.clock.now())
                item = self.item()
                self.manager.state["leads"][SID]["idle_since"] = (self.clock.now() - timedelta(minutes=31)).isoformat()
                self.assertTrue(self.manager.allowed(item, self.clock.now()))
                if change in ("hold", "done"):
                    self.row["state"] = change
                    self.write()
                elif change == "remove":
                    self.manager.roster_path.unlink()
                elif change == "complete":
                    self.host.goal["status"] = "complete"
                elif change == "missing":
                    self.host.goal = {}
                else:
                    self.host.goal["createdAt"] += 1
                self.assertEqual(self.manager.evaluate(item, self.clock.now()).kind, "waiting")
                # Force a previously latched outcome, then exercise the actual
                # occurrence delivery path, not merely a predicate unit test.
                from docket.checks import Outcome
                self.scheduler._latch_watch(item, item.spec, Outcome("met"), self.clock.now())
                self.assertEqual(self.store.get(item.id).status, "cancelled")
                self.assertEqual(len(self.host.sent), 0)

    def test_queued_hold_retracts_only_owned_id(self):
        self.host.consume_immediately = False
        self.tick()
        self.tick(30)
        aid = self.host.sent[0]["admission_id"]
        self.host.inbox.add("msg_unrelated")
        self.row["state"] = "hold"
        self.write()
        self.tick(1)
        self.assertNotIn(aid, self.host.inbox)
        self.assertIn("msg_unrelated", self.host.inbox)
        self.assertEqual(self.item().status, "cancelled")

    def test_goal_tuple_reset_and_missing_identity_suppresses(self):
        self.tick()
        self.tick(29)
        old = self.item()
        self.host.goal["createdAt"] += 1
        self.tick(1)
        self.assertEqual(self.store.get(old.id).status, "cancelled")
        self.tick(29)
        self.assertEqual(len(self.host.sent), 0)
        self.tick(1)
        self.assertEqual(len(self.host.sent), 1)
        for goal in ({"id": "g", "status": "active"}, {"id": "", "createdAt": 123, "status": "active"},
                     {"id": "g", "createdAt": True, "status": "active"}):
            self.host.goal = goal
            self.assertIsNone(self.manager.snapshot(SID))

    def test_failed_queued_retraction_blocks_new_epoch(self):
        self.host.consume_immediately = False
        self.tick()
        self.tick(30)
        old = self.item()
        self.host.cancel_fails = True
        self.row["state"] = "hold"
        self.write()
        self.tick(1)
        req = self.store.get_request(self.store.open_occurrence(old).id)
        self.assertFalse(req["facts"].get("cancelled"))
        self.assertEqual(req["issue"]["code"], "acceptance_unknown")
        self.row["state"] = "active"
        self.host.goal["id"] = "different-goal"
        self.write()
        self.tick(35)
        self.assertEqual(len(self.host.sent), 1)
        self.assertEqual(self.item().id, old.id)

    def test_retry_hold_suppressed_and_old_intent_immutable(self):
        self.tick()
        old = self.item()
        original = dict(old.spec["lead_checkin"])
        # Fail before dispatch; transport exceptions after begin_send are uncertain
        # in the frozen core and must never be treated as automatic retry permission.
        def unavailable_binding():
            raise DeliveryError("unavailable", "fixture", retryable=True)
        self.host.binding_snapshot = unavailable_binding
        self.tick(30)
        self.assertIsNotNone(self.store.open_occurrence(self.item()))
        self.row["state"] = "hold"
        self.write()
        del self.host.binding_snapshot
        # Retry runs before the next cadence reconciliation. Fresh send gate
        # must still suppress, not rely only on the minute reconciler.
        self.clock.value += timedelta(seconds=3)
        self.scheduler.tick()
        self.assertEqual(len(self.host.sent), 0)
        self.assertEqual(self.store.get(old.id).status, "cancelled")
        self.assertEqual(self.store.get(old.id).spec["lead_checkin"], original)

    def test_explicit_marker_rechecked_after_latch(self):
        self.tick()
        item = self.item("milestone")
        self.clock.value += timedelta(hours=3)
        self.row.update(milestone_at=self.clock.now().isoformat(), milestone_ref="visible acceptance")
        self.write()
        from docket.checks import Outcome
        self.scheduler._latch_watch(item, item.spec, Outcome("met"), self.clock.now())
        self.assertEqual(len(self.host.sent), 0)
        self.assertEqual(self.store.get(item.id).status, "cancelled")

    def test_ambiguous_never_mints_replacement(self):
        self.host.conflict = True
        self.tick()
        self.tick(30)
        ident = self.item().id
        self.assertEqual(self.store.open_occurrence(self.item()).status, "ambiguous")
        self.tick(40)
        self.assertEqual(self.item().id, ident)
        self.assertEqual(len(self.store.list()), 2)

    def test_no_duplicate_and_expiry_renews_beyond_today(self):
        self.host.active.add(SID)
        self.tick()
        old = self.item("milestone")
        self.manager.reconcile(self.clock.now())
        self.assertEqual(len(self.store.list()), 1)
        self.scheduler._expire_watch(old, self.clock.now(), "fixture expiry")
        self.manager.reconcile(self.clock.now() + timedelta(days=2))
        self.assertNotEqual(self.item("milestone").id, old.id)
        self.assertEqual(len(self.store.list_outstanding()), 1)
        self.assertEqual((self.item("milestone").spec["lead_checkin"]["baseline"]), old.spec["lead_checkin"]["baseline"])

    def test_caps_use_existing_setter_limit(self):
        rows = []
        for i in range(5):
            sid = "ses_fixture00000" + str(i)
            self.host.known.add(sid)
            rows.append({"session": sid, "directory": "/fixture", "state": "active"})
        self.manager.roster_path.write_text("\n".join(map(json.dumps, rows)))
        self.tick()
        self.assertEqual(len(self.store.list_outstanding()), 8)
        self.assertIn("capacity", self.manager.error)

    def test_missing_unreadable_roster_and_outage_suppress(self):
        self.tick()
        self.tick(29)
        item = self.item()
        self.manager.roster_path.chmod(0o644)
        self.assertFalse(self.manager.allowed(item, self.clock.now() + timedelta(minutes=2)))
        self.manager.roster_path.unlink()
        self.tick(1)
        self.assertEqual(len(self.host.sent), 0)
        self.write()
        self.host.unavailable = True
        self.tick(31)
        self.assertEqual(len(self.host.sent), 0)

    def test_restart_and_observation_gap_reset_idle(self):
        self.tick()
        self.tick(29)
        self.scheduler.checkins = self.manager = Checkins(self.scheduler)
        self.tick(1)
        self.assertEqual(len(self.host.sent), 0)
        self.clock.value += timedelta(minutes=35)
        self.tick()
        self.assertEqual(len(self.host.sent), 0)

    def test_caller_label_is_not_ownership_and_ordinary_idle_unchanged(self):
        self.tick()
        item = self.item()
        item.id = "caller_forgery"
        self.assertFalse(self.manager.allowed(item, self.clock.now() + timedelta(hours=1)))
        self.assertEqual(Checks(self.host).evaluate({"check": "session_idle", "watch_session": SID}, self.clock.now()).kind, "met")
        item.spec = {"check": "session_idle", "watch_session": SID}
        self.assertTrue(self.manager.allowed(item, self.clock.now()))

    def test_missing_or_corrupt_ledger_fails_closed(self):
        self.tick()
        self.manager.ledger_path.unlink()
        self.assertTrue(Checkins(self.scheduler).broken)
        self.manager.ledger_path.write_text("not json")
        self.manager.ledger_path.chmod(0o600)
        self.assertTrue(Checkins(self.scheduler).broken)

    def test_roster_validation_no_spec_file_access(self):
        for mutation in ({"session": "../bad"}, {"state": "parked"}, {"milestone_at": "yesterday"},
                         {"milestone_ref": "without timestamp"}, {"directory": 42}):
            good = dict(self.row)
            self.row.update(mutation)
            self.write()
            with self.assertRaises((ValueError, TypeError)):
                roster(self.manager.roster_path)
            self.row = good

    def test_adapter_goal_uses_existing_paired_request_hook(self):
        adapter = OpenCodeAdapter()
        calls = []
        def request(method, path):
            calls.append((method, path))
            return 200, {"data": {"metadata": {"openchamber": {"goal": self.host.goal}}}}
        adapter._request = request
        self.assertEqual(adapter.session_goal(SID), self.host.goal)
        self.assertEqual(calls, [("GET", f"/api/session/{SID}")])

    def test_hold_uses_frozen_status_not_id_only_consumption(self):
        self.host.consume_immediately = False
        self.tick()
        self.tick(30)
        old = self.item()
        unit = self.store.open_occurrence(old)
        before = self.store.get_request(unit.id)
        # Legacy reconcile claims consumption, but exact-envelope status is
        # unavailable. Hold must stop durably without upgrading that weak claim.
        self.host.status_frozen = lambda frozen: "uncertain"
        self.host.consumed.add(before["request_id"])
        self.row["state"] = "hold"
        self.write()
        self.tick(1)
        after = self.store.get_request(unit.id)
        self.assertEqual(after["frozen"], before["frozen"])
        self.assertEqual(after["request_id"], before["request_id"])
        self.assertTrue(after["stop_requested"])
        self.assertFalse(after["facts"].get("consumed"))
        self.assertFalse(after["facts"].get("cancelled"))
        self.row["state"] = "active"
        self.host.goal["id"] = "fixture-new-goal"
        self.write()
        self.tick(35)
        self.assertEqual(self.item().id, old.id)
        self.assertEqual(len(self.host.sent), 1)
