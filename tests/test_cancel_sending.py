"""Cancellation of a persisted sending admission, and dishonest host outcomes."""

from __future__ import annotations

import json
import os
import threading
import time
import unittest

from docket.client import ClientError, request
from docket.delivery import OpenCodeAdapter
from docket.parse import new_occurrence_id
from docket.store import Occurrence
from tests.support import RecordingOpenCode
from tests.test_harden import _http, _item, _recurring, _sched
from tests.test_schedule import _watch


AID = "msg_sendingwindow01"


def _occ(item, now, *, status, receipt):
    occ = Occurrence(
        id=new_occurrence_id(), item_id=item.id, seq=1, status=status,
        scheduled_at=now, created_at=now, updated_at=now, late=False,
        late_by_seconds=None, delivered_at=None, error=None, receipt=receipt,
        attempts=1, next_attempt_at=None, firing_at=now, coalesced=0,
        detail={"kind": item.kind},
    )
    item.status = "pending" if item.kind in ("recurring", "watch") else status
    return occ


def _plant(store, item, occ):
    store.insert(item)
    if occ is not None:
        store.attach_occurrence(item, occ)


class SendingCancelTests(unittest.TestCase):
    def setUp(self):
        self.store, self.adapter, self.clock, self.server = _http()
        self.adapter.consume_immediately = False
        self.old = os.environ.get("DOCKET_URL"), os.environ.get("DOCKET_TOKEN")
        os.environ["DOCKET_URL"] = self.server.url
        os.environ["DOCKET_TOKEN"] = "test-token"

    def tearDown(self):
        self.server.stop()
        self.store.close()
        self.store._tmp.cleanup()
        for key, value in zip(("DOCKET_URL", "DOCKET_TOKEN"), self.old):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _post(self, item_id):
        try:
            status, body = request("POST", f"/api/v1/items/{item_id}/cancel", {})
            return status, body, None
        except ClientError as exc:
            return exc.status, None, exc

    def _legacy_stopped(self, body, unit_id):
        self.assertTrue(body["schedule_stopped"])
        self.assertEqual(body["retraction"]["outcome"], "uncertain")
        self.assertNotIn(AID, json.dumps(body))
        req = self.store.get_request(unit_id)
        self.assertEqual(req["request_id"], AID)
        self.assertIsNone(req["frozen"])
        self.assertEqual(req["provenance"], "legacy")
        self.assertEqual(self.adapter.cancelled, [])

    def test_legacy_sending_receipt_stops_without_inventing_an_envelope(self):
        """Saved opaque identity is private; it cannot prove safe retraction."""
        now = self.clock.now()
        item = _recurring("dkt_sending", now, every=60, count=4)
        occ = _occ(item, now, status="firing", receipt={"admission_id": AID, "state": "sending"})
        self.adapter.inbox.add(AID)
        _plant(self.store, item, occ)
        status, body, exc = self._post("dkt_sending")
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self._legacy_stopped(body, occ.id)
        self.assertIn(AID, self.adapter.inbox)
        self.assertEqual(self.store.get("dkt_sending").status, "cancelled")
        self.assertEqual(self.store.list_occurrences("dkt_sending")[0].status, "cancelled")
        before = len(self.store.list_occurrences("dkt_sending"))
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(len(self.store.list_occurrences("dkt_sending")), before)
        self.assertEqual(self.store.get("dkt_sending").status, "cancelled")

    def test_legacy_one_shot_stops_and_preserves_private_admission_id(self):
        now = self.clock.now()
        item = _item("dkt_once", now)
        item.status = "firing"
        item.firing_at = now
        item.receipt = {"admission_id": AID, "state": "sending"}
        self.adapter.inbox.add(AID)
        self.store.insert(item)
        status, body, exc = self._post("dkt_once")
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self._legacy_stopped(body, item.id)
        self.assertEqual(self.store.get(item.id).receipt["admission_id"], AID)
        self.assertIn(AID, self.adapter.inbox)
        self.assertEqual(self.store.get("dkt_once").status, "cancelled")
        self.assertEqual(self.store.list_occurrences("dkt_once"), [])
        looked_up = []
        reconcile = self.adapter.reconcile
        def record(target, admission_id):
            looked_up.append(admission_id)
            return reconcile(target, admission_id)
        self.adapter.reconcile = record
        self.assertEqual(self.server.scheduler.recover(self.store.get(item.id), None, "reconcile"), (200, None))
        self.assertEqual(looked_up, [AID])
        self.assertEqual(self.store.get_request(item.id)["request_id"], AID)
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(self.adapter.sent, [])

    def test_legacy_watch_stops_and_does_not_finish(self):
        now = self.clock.now()
        item = _watch("dkt_watch", now, check="file_appears", path="/tmp/docket-watch-absent")
        occ = _occ(item, now, status="firing", receipt={"admission_id": AID, "state": "sending"})
        self.adapter.inbox.add(AID)
        _plant(self.store, item, occ)
        status, body, exc = self._post("dkt_watch")
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self._legacy_stopped(body, occ.id)
        self.assertIn(AID, self.adapter.inbox)
        self.assertEqual(self.store.get("dkt_watch").status, "cancelled")
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(self.adapter.sent, [])
        self.assertNotEqual(self.store.get("dkt_watch").status, "delivered")

    def test_pending_without_an_admission_does_not_call_the_host(self):
        self.store.insert(_item("dkt_plain", self.clock.now()))
        status, body, exc = self._post("dkt_plain")
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self.assertTrue(body["schedule_stopped"])
        self.assertEqual(body["retraction"]["outcome"], "not_sent")
        self.assertEqual(self.adapter.cancelled, [])
        self.assertEqual(self.store.get("dkt_plain").status, "cancelled")

    def test_firing_sending_absent_from_host_cancels_without_a_later_send(self):
        now = self.clock.now()
        item = _item("dkt_absent", now)
        item.status = "firing"
        item.firing_at = now
        item.receipt = {"admission_id": AID, "state": "sending"}
        self.store.insert(item)
        status, body, exc = self._post("dkt_absent")
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self._legacy_stopped(body, item.id)
        self.assertEqual(self.store.get("dkt_absent").status, "cancelled")
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(self.adapter.sent, [])

    def test_legacy_admitted_fact_survives_local_stop(self):
        now = self.clock.now()
        item = _item("dkt_held", now)
        item.status = "admitted"
        item.receipt = {
            "admission_id": AID, "inbox_id": AID, "state": "admitted", "delivery": "queue",
        }
        self.adapter.inbox.add(AID)
        self.store.insert(item)
        status, body, exc = self._post("dkt_held")
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self._legacy_stopped(body, item.id)
        self.assertTrue(self.store.get_request(item.id)["facts"]["admitted"])
        self.assertIn(AID, self.adapter.inbox)

    def test_legacy_ambiguous_host_held_id_stops_without_a_new_admission(self):
        now = self.clock.now()
        item = _recurring("dkt_ambig", now, every=60, count=4)
        occ = _occ(item, now, status="ambiguous", receipt={"admission_id": AID, "state": "ambiguous"})
        self.adapter.inbox.add(AID)
        _plant(self.store, item, occ)
        status, body, exc = self._post("dkt_ambig")
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self._legacy_stopped(body, occ.id)
        self.assertIn(AID, self.adapter.inbox)
        self.assertEqual(len(self.store.list_occurrences("dkt_ambig")), 1)
        self.assertEqual(
            self.store.list_occurrences("dkt_ambig")[0].receipt["admission_id"], AID,
        )
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(len(self.store.list_occurrences("dkt_ambig")), 1)

    def test_ambiguous_missing_is_not_a_clean_removal(self):
        now = self.clock.now()
        item = _recurring("dkt_ambigmiss", now, every=60, count=4)
        occ = _occ(item, now, status="ambiguous", receipt={"admission_id": AID, "state": "ambiguous"})
        _plant(self.store, item, occ)
        status, body, exc = self._post("dkt_ambigmiss")
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self._legacy_stopped(body, occ.id)
        self.assertEqual(self.store.get("dkt_ambigmiss").status, "cancelled")
        self.assertEqual(self.store.get_occurrence(occ.id).receipt["state"], "ambiguous")
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(len(self.store.list_occurrences("dkt_ambigmiss")), 1)

    def test_legacy_host_consumption_without_envelope_is_not_inferred(self):
        now = self.clock.now()
        item = _recurring("dkt_ate", now, every=60, count=4)
        occ = _occ(item, now, status="firing", receipt={"admission_id": AID, "state": "sending"})
        self.adapter.consumed.add(AID)
        _plant(self.store, item, occ)
        status, body, exc = self._post("dkt_ate")
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self._legacy_stopped(body, occ.id)
        self.assertIn(AID, self.adapter.consumed)
        self.assertFalse(self.store.get_request(occ.id)["facts"].get("consumed", False))
        self.assertEqual(self.adapter.cancelled, [])
        fresh = self.store.get("dkt_ate")
        self.assertEqual(fresh.status, "cancelled")
        self.assertEqual(self.store.get_occurrence(occ.id).receipt["admission_id"], AID)
        self.assertEqual(len(self.store.list_occurrences("dkt_ate")), 1)
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(self.store.get("dkt_ate").status, "cancelled")
        self.assertEqual(len(self.store.list_occurrences("dkt_ate")), 1)

    def test_lost_confirmed_admission_is_cancelled_without_a_new_send(self):
        now = self.clock.now()
        item = _recurring("dkt_lost", now, every=60, count=4)
        occ = _occ(
            item, now, status="admitted",
            receipt={"admission_id": AID, "inbox_id": AID, "state": "admitted"},
        )
        _plant(self.store, item, occ)
        status, body, exc = self._post("dkt_lost")
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self._legacy_stopped(body, occ.id)
        self.assertTrue(self.store.get_request(occ.id)["facts"]["admitted"])
        self.assertEqual(self.store.get("dkt_lost").status, "cancelled")
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(len(self.store.list_occurrences("dkt_lost")), 1)
        self.assertEqual(
            self.store.list_occurrences("dkt_lost")[0].receipt["admission_id"], AID,
        )

    def test_legacy_cancel_stops_even_when_remote_delete_would_fail(self):
        now = self.clock.now()
        item = _recurring("dkt_stuck", now, every=60, count=4)
        occ = _occ(item, now, status="firing", receipt={"admission_id": AID, "state": "sending"})
        self.adapter.inbox.add(AID)
        self.adapter.cancel_fails = True
        _plant(self.store, item, occ)
        status, body, exc = self._post("dkt_stuck")
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self._legacy_stopped(body, occ.id)
        self.assertIn(AID, self.adapter.inbox)
        fresh = self.store.get("dkt_stuck")
        self.assertEqual(fresh.status, "cancelled")
        self.assertEqual(self.store.list_occurrences("dkt_stuck")[0].status, "cancelled")
        self.assertEqual(self.store.list_occurrences("dkt_stuck")[0].receipt["admission_id"], AID)
        self.clock.advance(120)
        self.server.scheduler.tick()
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(len(self.store.list_occurrences("dkt_stuck")), 1)
        self.assertEqual(
            self.store.list_occurrences("dkt_stuck")[0].receipt["admission_id"], AID,
        )
        self.adapter.cancel_fails = False
        status, body, exc = self._post("dkt_stuck")
        # Local stop already won: a second cancel cannot restart the finished schedule.
        self.assertEqual(status, 409)
        self.assertEqual(exc.code, "schedule_finished")
        self._legacy_stopped(exc.payload, occ.id)
        self.assertIn(AID, self.adapter.inbox)
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(self.adapter.sent, [])

    def test_cancel_during_send_deletes_but_cannot_claim_a_retained_barrier(self):
        hold = threading.Event()
        self.adapter.hold_deliver = hold
        self.store.insert(_recurring("dkt_fly", self.clock.now(), every=60, count=4))
        done = threading.Event()

        def run():
            self.server.scheduler.tick()
            done.set()

        threading.Thread(target=run, daemon=True).start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.adapter.entered:
            time.sleep(0.01)
        self.assertTrue(self.adapter.entered)
        result = {}

        def cancel():
            result["posted"] = self._post("dkt_fly")

        worker = threading.Thread(target=cancel)
        worker.start()
        time.sleep(0.05)
        hold.set()
        self.assertTrue(done.wait(3))
        worker.join(3)
        status, body, exc = result["posted"]
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self.assertTrue(body["schedule_stopped"])
        self.assertEqual(body["retraction"]["outcome"], "uncertain")
        aid = self.adapter.sent[0]["admission_id"]
        self.assertRegex(aid, r"^msg_[0-9a-f]{32}$")
        occ = self.store.list_occurrences("dkt_fly")[0]
        req = self.store.get_request(occ.id)
        self.assertEqual(req["request_id"], aid)
        self.assertEqual(body["item"]["delivery"]["request_id"], aid)
        self.assertEqual(json.loads(req["frozen"]["body"])["text"], self.adapter.sent[0]["message"])
        self.assertNotIn(aid, self.adapter.inbox)
        self.assertIn(aid, [row[1] for row in self.adapter.cancelled])
        self.assertEqual(self.store.get("dkt_fly").status, "cancelled")
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(len(self.store.list_occurrences("dkt_fly")), 1)
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(self.store.get("dkt_fly").status, "cancelled")

    def test_cancel_during_send_does_not_claim_success_when_the_host_keeps_it(self):
        hold = threading.Event()
        self.adapter.hold_deliver = hold
        self.adapter.cancel_fails = True
        self.store.insert(_item("dkt_keep", self.clock.now()))
        done = threading.Event()

        def run():
            self.server.scheduler.tick()
            done.set()

        threading.Thread(target=run, daemon=True).start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.adapter.entered:
            time.sleep(0.01)
        self.assertTrue(self.adapter.entered)
        result = {}

        def cancel():
            result["posted"] = self._post("dkt_keep")

        worker = threading.Thread(target=cancel)
        worker.start()
        time.sleep(0.05)
        hold.set()
        self.assertTrue(done.wait(3))
        worker.join(3)
        status, body, exc = result["posted"]
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self.assertTrue(body["schedule_stopped"])
        self.assertEqual(body["retraction"]["outcome"], "uncertain")
        aid = self.adapter.sent[0]["admission_id"]
        self.assertIn(aid, self.adapter.inbox)
        self.assertEqual(self.store.get("dkt_keep").status, "cancelled")
        self.assertEqual(self.store.get_request("dkt_keep")["request_id"], aid)
        self.assertEqual(len(self.adapter.sent), 1)
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(len(self.adapter.sent), 1)

    def test_modern_delete_failure_still_durably_stops_locally(self):
        self.store.insert(_item("dkt_delete_failure", self.clock.now()))
        self.server.scheduler.tick()
        before = self.store.get_request("dkt_delete_failure")
        aid = before["request_id"]
        self.adapter.cancel_fails = True
        status, body, exc = self._post("dkt_delete_failure")
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self.assertTrue(body["schedule_stopped"])
        self.assertEqual(body["retraction"]["outcome"], "uncertain")
        self.assertIn(aid, self.adapter.inbox)
        self.assertIn(aid, [row[1] for row in self.adapter.cancelled])
        self.assertEqual(self.store.get("dkt_delete_failure").status, "cancelled")
        self.assertTrue(self.store.get_request("dkt_delete_failure")["facts"]["admitted"])
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(self.store.get_request("dkt_delete_failure")["frozen"], before["frozen"])

    def test_modern_delivered_history_survives_series_stop_without_advancement(self):
        self.adapter.consume_immediately = True
        self.store.insert(_recurring("dkt_consumed", self.clock.now(), every=60, count=4))
        self.server.scheduler.tick()
        occ = self.store.list_occurrences("dkt_consumed")[0]
        before = self.store.get_request(occ.id)
        self.assertTrue(before["facts"]["consumed"])
        # The completed occurrence remains positive history, even as the series stops.
        status, body, exc = self._post("dkt_consumed")
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self.assertTrue(body["schedule_stopped"])
        self.assertTrue(self.store.get_request(occ.id)["facts"]["consumed"])
        self.assertEqual(self.store.get_occurrence(occ.id).status, "delivered")
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(len(self.store.list_occurrences("dkt_consumed")), 1)

    def test_modern_open_consumption_reports_too_late(self):
        self.store.insert(_item("dkt_too_late", self.clock.now()))
        self.server.scheduler.tick()
        aid = self.adapter.sent[0]["admission_id"]
        self.adapter.inbox.discard(aid)
        self.adapter.consumed.add(aid)
        status, body, exc = self._post("dkt_too_late")
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self.assertTrue(body["schedule_stopped"])
        self.assertEqual(body["retraction"]["outcome"], "too_late")
        self.assertTrue(self.store.get_request("dkt_too_late")["facts"]["consumed"])
        self.assertEqual(self.adapter.cancelled, [])
        self.assertEqual(self.store.get("dkt_too_late").status, "cancelled")


class OpenCodeRetractTests(unittest.TestCase):
    def test_frozen_delete_missing_and_failure_are_uncertain_but_exact_consumption_is_positive(self):
        fake = RecordingOpenCode()
        fake.sessions["ses_live0001"] = {"id": "ses_live0001", "time": {}, "location": {}}
        adapter = OpenCodeAdapter(discovery=lambda: (fake.url, "Basic dGVzdA=="))
        try:
            for label, delete_status in (("absent", 204), ("failed", 500), ("consumed", 204)):
                with self.subTest(label=label):
                    store, clock, sched = _sched(adapter)
                    self.addCleanup(store._tmp.cleanup)
                    self.addCleanup(store.close)
                    item = _item("dkt_" + label, clock.now(), session="ses_live0001")
                    store.insert(item)
                    sched.tick()
                    before = store.get_request(item.id)
                    aid = before["request_id"]
                    self.assertIn(aid, fake.inbox["ses_live0001"])
                    self.assertTrue(before["facts"]["admitted"])
                    fake.delete_status = delete_status
                    if label == "consumed":
                        fake.inbox["ses_live0001"].pop(aid)
                        fake.messages.setdefault("ses_live0001", {})[aid] = {
                            "id": aid, "type": "user", "time": {},
                            "text": json.loads(before["frozen"]["body"])["text"],
                        }
                    outcome = sched.stop(store.get(item.id))
                    self.assertEqual(outcome, "too_late" if label == "consumed" else "uncertain")
                    self.assertEqual(store.get(item.id).status, "cancelled")
                    deletes = [c for c in fake.calls if c[0] == "DELETE" and aid in c[1]]
                    self.assertEqual(len(deletes), 0 if label == "consumed" else 1)
                    if label == "failed":
                        self.assertIn(aid, fake.inbox["ses_live0001"])
                    else:
                        self.assertNotIn(aid, fake.inbox["ses_live0001"])
                    if label == "consumed":
                        self.assertTrue(store.get_request(item.id)["facts"]["consumed"])
                    self.assertEqual(store.get_request(item.id)["frozen"], before["frozen"])
                    clock.advance(3600)
                    sched.tick()
                    self.assertEqual(len([c for c in fake.calls if c[0] == "POST" and c[2]["id"] == aid]), 1)
        finally:
            fake.stop()


if __name__ == "__main__":
    unittest.main()
