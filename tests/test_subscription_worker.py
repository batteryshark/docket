"""Isolated queue/restart tests. No native discovery or model requests."""
import json
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

from docket.delivery import Receipt
from docket.subscription_admission import BINDING_KEYS
from docket.subscription_worker import EventStore, EventWorker
from docket.subscriptions import SubscriptionRefusal
from tests.test_subscriptions import TOKEN, config, event


class Adapter:
    def __init__(self):
        self.sends = []
        self.lookups = []
        self.outcome = "missing"
        self.prepare_error = None
        self.dispatch_error = None
        self.revoked = False
        self.lifecycles = []

    def prepare(self, installed, data, admission_id):
        if self.prepare_error:
            raise self.prepare_error
        return {**{key: installed[key] for key in BINDING_KEYS},
                "action": installed.get("action", "owner"), "admission_id": admission_id,
                "briefing": "Trusted task.\nUNTRUSTED EVENT DATA\n" + json.dumps(data),
                "event_data": data}

    def validate(self, prepared):
        return {**{key: prepared[key] for key in BINDING_KEYS},
                "stopped": False, "revoked": self.revoked}

    def dispatch(self, prepared, stable_id, *, lifecycle):
        assert set(lifecycle) == {"delivery_id", "deadline", "lease_until", "generation"}
        self.lifecycles.append(lifecycle)
        self.sends.append((prepared, stable_id))
        if self.dispatch_error:
            raise self.dispatch_error
        return Receipt(stable_id, "queue", admission_id=stable_id)

    def reconcile(self, prepared, stable_id):
        self.lookups.append(stable_id)
        return self.outcome


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "db"
        self.store = EventStore(self.path)
        self.store.install_publisher("fixture", TOKEN)
        self.store.install_subscription("sub", "fixture", "fixture.completed", config())
        self.delivery = self.store.accept(TOKEN, event(text="private-event-marker"))
        self.id = self.delivery["id"]
        self.now = time.time() + 1
        self.adapter = Adapter()
        self.worker = EventWorker(self.store, self.adapter)

    def tearDown(self):
        self.worker.close()
        self.store.close()
        self.tmp.cleanup()

    def reopen(self):
        self.worker.close()
        self.store.close()
        self.store = EventStore(self.path)
        self.worker = EventWorker(self.store, self.adapter)

    def test_survives_restart_and_consumption_is_not_completion(self):
        self.reopen()
        self.worker.tick(self.now)
        self.assertEqual(len(self.adapter.sends), 1)
        self.assertEqual(self.adapter.sends[0][1], self.delivery["admission_id"])
        self.assertEqual(self.store.state(self.id)["phase"], "admitted")
        self.reopen()
        self.adapter.outcome = "consumed"
        self.worker.tick(self.now + 6)
        row = self.store.inspect_events(self.id)[0]
        self.assertEqual(row["terminal"], "consumed")
        self.assertNotEqual(row["terminal"], "handler_finished")
        self.assertEqual(self.adapter.lookups, [self.delivery["admission_id"]])

    def test_lost_ack_and_missing_never_redispatch(self):
        self.adapter.dispatch_error = OSError("raw-secret")
        self.worker.tick(self.now)
        self.reopen()
        self.worker.tick(self.now + 6)
        self.assertEqual(self.store.state(self.id)["phase"], "uncertain")
        self.worker.tick(self.now + 12)
        self.assertEqual(len(self.adapter.sends), 1)
        self.assertNotIn("raw-secret", json.dumps(self.store.inspect_events(self.id)))
        self.adapter.outcome = "consumed"
        self.worker.tick(self.now + 18)
        self.assertEqual(self.store.state(self.id)["phase"], "consumed")

    def test_preparation_backoff_deadline_and_redaction(self):
        self.adapter.prepare_error = OSError("raw-secret")
        self.worker.tick(self.now)
        row = self.store.inspect_events(self.id)[0]
        self.assertEqual(row["attempts"], 1)
        self.assertAlmostEqual(row["next_at"], self.now + 2, delta=0.1)
        self.assertEqual(self.worker.tick(self.now + 1), [])
        self.reopen()
        self.adapter.prepare_error = None
        self.worker.tick(self.now + 3601)
        row = self.store.inspect_events(self.id, deadletters=True)[0]
        self.assertEqual(row["reason"], "deadline_before_dispatch")
        text = json.dumps(row)
        self.assertNotIn("private-event-marker", text)
        self.assertNotIn(TOKEN, text)
        self.assertEqual(self.adapter.sends, [])

    def test_revoked_profile_and_publisher_refuse_before_dispatch(self):
        self.adapter.revoked = True
        self.worker.tick(self.now)
        self.assertEqual(self.adapter.sends, [])
        self.assertEqual(self.store.inspect_events(deadletters=True)[0]["reason"], "profile_or_owner_refused")

    def test_installed_revocation_refuses(self):
        self.store.revoke(publisher_id="fixture")
        self.worker.tick(self.now)
        self.assertEqual(self.adapter.sends, [])

    def test_frozen_identity_cannot_change(self):
        prepare = self.adapter.prepare
        def changed(*args):
            prepared = prepare(*args)
            prepared["owner_generation"] = "new-owner"
            return prepared
        self.adapter.prepare = changed
        self.worker.tick(self.now)
        self.assertEqual(self.adapter.sends, [])
        self.assertEqual(self.store.inspect_events(deadletters=True)[0]["reason"], "profile_or_owner_refused")

    def test_retry_exhaustion(self):
        self.adapter.prepare_error = OSError("offline")
        for index in range(8):
            self.worker.tick(self.now + index * 301)
        self.assertEqual(self.store.inspect_events(deadletters=True)[0]["reason"], "retry_exhausted")
        self.assertEqual(self.adapter.sends, [])

    def test_stale_preparer_cannot_commit_after_lease_reclaim(self):
        self.store.due_events(self.now)
        owner = self.store.claim_event(self.id, self.now)
        self.assertIsNone(self.store.claim_event(self.id, self.now))
        other = EventStore(self.path)
        try:
            self.assertIsNotNone(other.claim_event(self.id, self.now + 61))
            prepared = self.adapter.prepare(config(), {}, self.delivery["admission_id"])
            with self.assertRaises(SubscriptionRefusal):
                self.store.save_prepared(self.id, owner, prepared, self.now + 62)
        finally:
            other.close()

    def test_deadline_retains_uncertain_effect_evidence(self):
        self.adapter.dispatch_error = OSError("lost ack")
        self.worker.tick(self.now)
        self.worker.tick(self.now + 3601)
        row = self.store.inspect_events(deadletters=True)[0]
        self.assertEqual(row["reason"], "deadline_effect_unresolved")
        self.assertEqual(row["phase"], "uncertain")
        self.reopen()
        self.worker.tick(self.now + 7200)
        self.assertEqual(len(self.adapter.sends), 1)

    def test_publisher_rate_persists(self):
        for _ in range(120):
            self.store.rate_request("fixture", now=self.now)
        self.reopen()
        with self.assertRaises(SubscriptionRefusal) as caught:
            self.store.rate_request("fixture", now=self.now)
        self.assertEqual(caught.exception.code, "capacity")
        self.store.rate_request("fixture", now=self.now + 61)

    def test_same_owner_serialized_until_consumed(self):
        second = self.store.accept(TOKEN, event("second"))["id"]
        self.worker.tick(self.now)
        self.assertEqual(len(self.adapter.sends), 1)
        self.adapter.outcome = "consumed"
        self.worker.tick(self.now + 6)
        # Ordering is deterministic but may process the waiting row before the
        # consumed row; a later scheduler tick observes released capacity.
        self.worker.tick(self.now + 12)
        self.assertEqual(len(self.adapter.sends), 2)
        self.assertNotEqual(self.adapter.sends[0][1], self.adapter.sends[1][1])

    def test_native_refusal_after_intent_is_status_only(self):
        self.adapter.dispatch_error = ValueError("native profile refusal after possible effect")
        self.worker.tick(self.now)
        self.assertEqual(self.store.state(self.id)["phase"], "uncertain")
        self.worker.tick(self.now + 6)
        self.assertEqual(len(self.adapter.sends), 1)

    def test_native_refusal_before_intent_deadletters(self):
        self.adapter.prepare_error = ValueError("native widening refused")
        self.worker.tick(self.now)
        self.assertEqual(self.store.inspect_events(deadletters=True)[0]["reason"], "profile_or_owner_refused")
        self.assertEqual(self.adapter.sends, [])

    def test_known_profile_widening_has_safe_inspect_reason(self):
        from docket.wake_profiles import ProfileRefusal
        self.adapter.prepare_error = ProfileRefusal("profile widens installed tool policy")
        self.worker.tick(self.now)
        self.assertEqual(self.store.inspect_events(deadletters=True)[0]["reason"], "profile_widens_installed_tool_policy")
        self.assertEqual(self.adapter.sends, [])

    def test_plain_handler_finished_is_not_terminal_proof_and_lifecycle_is_trusted(self):
        handler_config = config()
        handler_config["action"] = "handler"
        self.store.install_subscription("handler", "fixture", "handler.event", handler_config)
        handler = self.store.accept(TOKEN, {**event("handler"), "type": "handler.event"})["id"]
        # Remove the fixture owner's otherwise competing delivery from this test.
        self.adapter.revoked = True
        self.worker.tick(self.now)
        self.adapter.revoked = False
        # Both were correctly refused; create a fresh handler with a new source
        # identity after revocation clears (no automatic reactivation).
        handler = self.store.accept(TOKEN, {**event("handler2"), "type": "handler.event"})["id"]
        self.worker.tick(self.now + 6)
        lifecycle = self.adapter.lifecycles[-1]
        self.assertEqual(lifecycle["delivery_id"], handler)
        self.assertEqual(lifecycle["generation"], self.store.state(handler)["generation"])
        self.assertLess(lifecycle["lease_until"], lifecycle["deadline"])
        self.adapter.outcome = "consumed"
        self.worker.tick(self.now + 12)
        self.assertEqual(self.store.state(handler)["phase"], "consumed")
        self.assertIsNone(self.store.inspect_events(handler)[0]["terminal"])
        self.adapter.outcome = "handler_finished"
        self.worker.tick(self.now + 18)
        self.assertIsNone(self.store.inspect_events(handler)[0]["terminal"])
        self.assertIsNone(self.store.handler_terminal(handler))

    def test_deadletter_unresolved_effect_still_reconciles_without_replay(self):
        self.adapter.dispatch_error = OSError("lost ack")
        self.worker.tick(self.now)
        self.worker.tick(self.now + 3601)
        self.assertEqual(len(self.store.inspect_events(deadletters=True)), 1)
        self.reopen()
        self.adapter.outcome = "consumed"
        self.worker.tick(self.now + 3662)
        self.assertEqual(self.store.inspect_events(self.id)[0]["terminal"], "consumed")
        self.assertEqual(len(self.adapter.sends), 1)
        self.assertTrue(any(row["reason"] == "deadline_effect_unresolved" for row in self.store.inspect_events(self.id)[0]["evidence"]))

    def test_scheduler_tick_nonblocking_with_no_backlog(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def slow(now):
            calls.append(now)
            entered.set()
            self.assertTrue(release.wait(5))
            return [self.id]
        self.worker.tick = slow
        try:
            self.assertEqual(self.worker.scheduler_tick(self.now), [])
            self.assertTrue(entered.wait(5))
            thread = self.worker._thread
            self.assertEqual(self.worker.scheduler_tick(self.now + 1), [])
            self.assertIs(self.worker._thread, thread)
            self.assertEqual(len(calls), 1)
            self.assertFalse(self.worker.close(timeout=0))
            self.assertEqual(self.worker.scheduler_tick(self.now + 2), [])
        finally:
            release.set()
            self.worker.close()

    def test_global_active_capacity(self):
        for index in range(5):
            installed = config()
            installed["target"]["session_id"] = "ses_fixture_" + str(index)
            event_type = "fixture." + str(index)
            self.store.install_subscription("sub-" + str(index), "fixture", event_type, installed)
            self.store.accept(TOKEN, {**event(str(index)), "type": event_type})
        self.adapter.outcome = "admitted"
        self.worker.tick(self.now)
        self.worker.tick(self.now + 6)
        self.assertEqual(len(self.adapter.sends), 4)

    def test_batch_clock_refreshes_second_claim_after_slow_first_preparation(self):
        second = self.store.accept(TOKEN, event("second"))["id"]
        self.store.due_events(self.now)
        elapsed = [0.0]
        prepare = self.adapter.prepare
        def slow_first(*args):
            prepared = prepare(*args)
            if args[2] == self.delivery["admission_id"]:
                elapsed[0] += 61
            return prepared
        self.adapter.prepare = slow_first
        with patch("docket.subscription_worker.time.monotonic", side_effect=lambda: elapsed[0]), \
                patch.object(self.store, "due_events", return_value=[self.id, second]), \
                patch.object(self.store, "claim_event", wraps=self.store.claim_event) as claims, \
                patch.object(self.store, "save_prepared", wraps=self.store.save_prepared) as saves:
            self.worker.tick(self.now)
        self.assertEqual([call.args[1] for call in claims.call_args_list], [self.now, self.now + 61])
        self.assertEqual([call.args[3] for call in saves.call_args_list], [self.now + 61, self.now + 61])
        # The first lease expired during preparation; only the second delivery
        # dispatches, with a newly claimed lease ahead of actual batch time.
        self.assertEqual(len(self.adapter.sends), 1)
        self.assertEqual(self.adapter.lifecycles[0]["delivery_id"], second)
        self.assertEqual(self.adapter.lifecycles[0]["lease_until"], self.now + 121)
        self.assertGreater(self.adapter.lifecycles[0]["lease_until"], self.now + elapsed[0])


if __name__ == "__main__":
    unittest.main()
