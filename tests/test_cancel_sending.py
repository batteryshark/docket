"""Cancellation of a persisted sending admission, and dishonest host outcomes."""

from __future__ import annotations

import os
import threading
import time
import unittest

from docket.client import ClientError, request
from docket.delivery import OpenCodeAdapter, retract_admission
from docket.parse import new_occurrence_id
from docket.store import Occurrence
from tests.support import RecordingOpenCode
from tests.test_harden import _http, _item, _recurring
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

    def test_persisted_sending_receipt_retracts_host_held_admission_id(self):
        """Crash window: host accepted, Docket stored admission_id, not inbox_id."""
        now = self.clock.now()
        item = _recurring("dkt_sending", now, every=60, count=4)
        occ = _occ(item, now, status="firing", receipt={"admission_id": AID, "state": "sending"})
        self.adapter.inbox.add(AID)
        _plant(self.store, item, occ)
        status, body, exc = self._post("dkt_sending")
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self.assertEqual(body["retraction"]["outcome"], "retracted")
        self.assertEqual(body["retraction"]["admission_id"], AID)
        self.assertIn(AID, [row[1] for row in self.adapter.cancelled])
        self.assertNotIn(AID, self.adapter.inbox)
        self.assertEqual(self.store.get("dkt_sending").status, "cancelled")
        self.assertEqual(self.store.list_occurrences("dkt_sending")[0].status, "cancelled")
        before = len(self.store.list_occurrences("dkt_sending"))
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(len(self.store.list_occurrences("dkt_sending")), before)
        self.assertEqual(self.store.get("dkt_sending").status, "cancelled")

    def test_one_shot_sending_window_retracts_admission_id(self):
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
        self.assertEqual(body["retraction"]["admission_id"], AID)
        self.assertNotIn(AID, self.adapter.inbox)
        self.assertEqual(self.store.get("dkt_once").status, "cancelled")
        self.assertEqual(self.store.list_occurrences("dkt_once"), [])
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(self.adapter.sent, [])

    def test_watch_sending_window_retracts_and_does_not_finish(self):
        now = self.clock.now()
        item = _watch("dkt_watch", now, check="file_appears", path="/tmp/docket-watch-absent")
        occ = _occ(item, now, status="firing", receipt={"admission_id": AID, "state": "sending"})
        self.adapter.inbox.add(AID)
        _plant(self.store, item, occ)
        status, _body, exc = self._post("dkt_watch")
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self.assertNotIn(AID, self.adapter.inbox)
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
        self.assertEqual(body["retraction"]["outcome"], "none")
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
        self.assertEqual(body["retraction"]["outcome"], "missing")
        self.assertEqual(self.store.get("dkt_absent").status, "cancelled")
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(self.adapter.sent, [])

    def test_admitted_inbox_id_is_retracted(self):
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
        self.assertEqual(body["retraction"]["outcome"], "retracted")
        self.assertNotIn(AID, self.adapter.inbox)

    def test_ambiguous_host_held_id_is_retracted_without_a_new_admission(self):
        now = self.clock.now()
        item = _recurring("dkt_ambig", now, every=60, count=4)
        occ = _occ(item, now, status="ambiguous", receipt={"admission_id": AID, "state": "ambiguous"})
        self.adapter.inbox.add(AID)
        _plant(self.store, item, occ)
        status, _body, exc = self._post("dkt_ambig")
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self.assertNotIn(AID, self.adapter.inbox)
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
        status, _body, exc = self._post("dkt_ambigmiss")
        self.assertEqual(status, 409)
        self.assertEqual(exc.code, "ambiguous")
        self.assertEqual(self.store.get("dkt_ambigmiss").status, "cancelled")
        self.assertEqual(self.store.get("dkt_ambigmiss").receipt["state"], "ambiguous")
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(len(self.store.list_occurrences("dkt_ambigmiss")), 1)

    def test_consumed_sending_window_is_not_success_and_does_not_advance(self):
        now = self.clock.now()
        item = _recurring("dkt_ate", now, every=60, count=4)
        occ = _occ(item, now, status="firing", receipt={"admission_id": AID, "state": "sending"})
        self.adapter.consumed.add(AID)
        _plant(self.store, item, occ)
        status, _body, exc = self._post("dkt_ate")
        self.assertEqual(status, 409)
        self.assertEqual(exc.code, "consumed")
        self.assertEqual(self.adapter.cancelled, [])
        fresh = self.store.get("dkt_ate")
        self.assertEqual(fresh.status, "cancelled")
        self.assertEqual(fresh.receipt["state"], "consumed")
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
        self.assertEqual(body["retraction"]["outcome"], "lost")
        self.assertEqual(self.store.get("dkt_lost").status, "cancelled")
        self.assertEqual(self.store.get("dkt_lost").receipt["state"], "lost")
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(len(self.store.list_occurrences("dkt_lost")), 1)
        self.assertEqual(
            self.store.list_occurrences("dkt_lost")[0].receipt["admission_id"], AID,
        )

    def test_host_retraction_failure_is_not_success_and_does_not_duplicate(self):
        now = self.clock.now()
        item = _recurring("dkt_stuck", now, every=60, count=4)
        occ = _occ(item, now, status="firing", receipt={"admission_id": AID, "state": "sending"})
        self.adapter.inbox.add(AID)
        self.adapter.cancel_fails = True
        _plant(self.store, item, occ)
        status, _body, exc = self._post("dkt_stuck")
        self.assertEqual(status, 409)
        self.assertEqual(exc.code, "retraction_failed")
        self.assertIn(AID, self.adapter.inbox)
        fresh = self.store.get("dkt_stuck")
        self.assertIn(fresh.status, ("pending", "firing"))
        self.assertEqual(self.store.list_occurrences("dkt_stuck")[0].status, "firing")
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
        self.assertIsNone(exc)
        self.assertEqual(status, 200)
        self.assertEqual(body["retraction"]["outcome"], "retracted")
        self.assertNotIn(AID, self.adapter.inbox)
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(self.adapter.sent, [])

    def test_cancel_during_send_retracts_the_accepted_id(self):
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
        self.assertEqual(body["retraction"]["outcome"], "retracted")
        aid = self.adapter.sent[0]["admission_id"]
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
        status, _body, exc = result["posted"]
        self.assertEqual(status, 409)
        self.assertEqual(exc.code, "retraction_failed")
        aid = self.adapter.sent[0]["admission_id"]
        self.assertIn(aid, self.adapter.inbox)
        self.assertEqual(self.store.get("dkt_keep").status, "admitted")
        self.assertEqual(len(self.adapter.sent), 1)
        self.clock.advance(3600)
        self.server.scheduler.tick()
        self.assertEqual(len(self.adapter.sent), 1)


class OpenCodeRetractTests(unittest.TestCase):
    def test_delete_uses_the_admission_id_and_a_failed_delete_is_not_success(self):
        fake = RecordingOpenCode()
        fake.sessions["ses_live0001"] = {"id": "ses_live0001", "time": {}, "location": {}}
        adapter = OpenCodeAdapter(discovery=lambda: (fake.url, "Basic dGVzdA=="))
        target = {"session_id": "ses_live0001"}
        try:
            fake.inbox["ses_live0001"] = {AID: {"id": AID, "delivery": "queue"}}
            outcome = retract_admission(adapter, target, AID, confirmed=False)
            self.assertEqual(outcome, "retracted")
            self.assertNotIn(AID, fake.inbox["ses_live0001"])
            self.assertTrue(any(AID in call[1] and call[0] == "DELETE" for call in fake.calls))

            fake.inbox["ses_live0001"] = {AID: {"id": AID, "delivery": "queue"}}
            fake.delete_status = 500
            failed = retract_admission(adapter, target, AID, confirmed=True)
            self.assertEqual(failed, "failed")
            self.assertIn(AID, fake.inbox["ses_live0001"])

            fake.delete_status = 204
            fake.inbox["ses_live0001"] = {}
            fake.messages["ses_live0001"] = {AID: {"id": AID, "type": "user"}}
            consumed = retract_admission(adapter, target, AID, confirmed=True)
            self.assertEqual(consumed, "consumed")
        finally:
            fake.stop()


if __name__ == "__main__":
    unittest.main()
