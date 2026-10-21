"""Typed compatibility receipts, including the independent nested-value repro."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from docket.api import Handler, _public_item, _public_occurrence
from docket.delivery_state import RECEIPT_STATES, receipt_projection
from docket.store import Occurrence, Store
from tests.support import utc
from tests.test_schedule import _recurring


SENTINEL = "PRIVATE_RECEIPT_SENTINEL"
AID = "msg_" + "a" * 32


class ReceiptProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "projection.db")
        self.addCleanup(self.store.close)
        now = utc(2026, 10, 19, 12, 0, 0)
        self.item = _recurring("dkt_series_nested", now, every=60, count=3)
        self.store.insert(self.item)
        self.occ = Occurrence(id="occ_nested", item_id=self.item.id, seq=1, status="pending",
            scheduled_at=now, created_at=now, updated_at=now, late=False, late_by_seconds=None,
            delivered_at=None, error="unavailable: raw exception", receipt=None, attempts=1,
            next_attempt_at=None, firing_at=None, coalesced=0, detail={})
        self.store.attach_occurrence(self.item, self.occ)

    def pointer(self, value):
        item = self.store.get(self.item.id)
        item.spec["open_occurrence_id"] = value
        self.store.save(item)

    def save_receipt(self, value):
        occ = self.store.get_occurrence(self.occ.id)
        occ.receipt = value
        self.store.save_occurrence(occ)

    def get(self, path):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(store=self.store)
        handler._boundary_ok = lambda: True
        handler._split = lambda: (path, {})
        handler._send = lambda status, body: (status, body)
        status, body = handler.do_GET()
        self.assertEqual(status, 200)
        return body

    def assert_private_absent_from_all_views(self):
        views = [self.store.open_summaries(),
                 _public_item(self.store, self.store.get(self.item.id)),
                 _public_occurrence(self.store, self.store.get_occurrence(self.occ.id)),
                 self.get("/api/v1/items"), self.get("/api/v1/items/" + self.item.id),
                 self.get("/api/v1/occurrences"), self.get("/api/v1/items/" + self.item.id + "/occurrences")]
        for body in views:
            self.assertNotIn(SENTINEL, json.dumps(body))
        return views[1]

    def test_exact_nested_replay_payload_is_unknown_and_never_escapes(self):
        payload = {"state": {"provider_stderr": SENTINEL}, "admission_id": [SENTINEL]}
        self.save_receipt(payload)
        self.pointer("occ_missing")
        before = self.store._db.execute("SELECT receipt_json FROM occurrences WHERE id=?", (self.occ.id,)).fetchone()[0]
        body = self.assert_private_absent_from_all_views()
        summary = body["open_occurrence"]
        self.assertIsNone(summary["delivery_state"])
        self.assertIsNone(summary["admission_id"])
        self.assertEqual(summary["receipt_issue"]["code"], "invalid_receipt")
        self.assertEqual(body["delivery"]["outcome"], "uncertain")
        self.assertEqual(self.store.get_occurrence(self.occ.id).receipt, payload)
        self.assertEqual(self.store._db.execute("SELECT receipt_json FROM occurrences WHERE id=?", (self.occ.id,)).fetchone()[0], before)

    def test_scalar_container_nested_and_unrecognized_fields_on_regular_and_broken_pointers(self):
        values = [True, 42, 1.5, SENTINEL, [SENTINEL], {"provider_stderr": SENTINEL},
                  {"state": SENTINEL, "admission_id": SENTINEL},
                  {"state": [SENTINEL], "admission_id": {"nested": SENTINEL}},
                  {"state": False, "admission_id": 42},
                  {"state": "admitted", "admission_id": AID, "delivery": {"stderr": SENTINEL}, "inbox_id": [SENTINEL]},
                  {"state": "admitted", "admission_id": "msg_" + "A" * 32},
                  {"state": "admitted", "admission_id": "msg_" + "a" * 33},
                  {"state": "admitted", "admission_id": "msg_" + "a" * 32 + "\n"}]
        for value in values:
            for pointer in (self.occ.id, "occ_missing", None):
                with self.subTest(value=value, pointer=pointer):
                    self.save_receipt(value)
                    self.pointer(pointer)
                    # Also exercise parent compatibility aliases, not just the child.
                    self.store._db.execute("UPDATE items SET receipt_json=? WHERE id=?", (json.dumps(value), self.item.id))
                    self.store._db.commit()
                    body = self.assert_private_absent_from_all_views()
                    self.assertIsNotNone(body["receipt_issue"])
                    self.assertIsNotNone(body["open_occurrence"]["receipt_issue"])
                    if pointer != self.occ.id:
                        self.assertEqual(body["delivery"]["outcome"], "uncertain")

    def test_malformed_receipt_json_and_nonmapping_json_are_safe_without_rewriting_storage(self):
        raw_values = ['{"private":"' + SENTINEL, '"' + SENTINEL + '"',
                      '["' + SENTINEL + '"]', '42', 'true', 'null', b'\xff' + SENTINEL.encode()]
        for raw in raw_values:
            for pointer in (self.occ.id, "occ_missing"):
                with self.subTest(raw=raw, pointer=pointer):
                    self.pointer(pointer)
                    self.store._db.execute("UPDATE occurrences SET receipt_json=? WHERE id=?", (raw, self.occ.id))
                    self.store._db.execute("UPDATE items SET receipt_json=? WHERE id=?", (raw, self.item.id))
                    self.store._db.commit()
                    body = self.assert_private_absent_from_all_views()
                    self.assertIsNone(body["delivery_state"])
                    self.assertIsNone(body["open_occurrence"]["admission_id"])
                    self.assertEqual(body["receipt_issue"]["code"], "invalid_receipt")
                    self.assertEqual(self.store._db.execute("SELECT receipt_json FROM occurrences WHERE id=?", (self.occ.id,)).fetchone()[0], raw)

    def test_valid_existing_receipt_enums_modes_and_canonical_ids_are_preserved(self):
        for state in RECEIPT_STATES:
            for mode in ("queue", "steer"):
                with self.subTest(state=state, mode=mode):
                    payload = {"state": state, "admission_id": AID, "inbox_id": AID, "delivery": mode}
                    self.save_receipt(payload)
                    self.pointer(self.occ.id)
                    summary = self.store.open_summaries()[self.item.id]
                    self.assertEqual((summary["delivery_state"], summary["admission_id"]), (state, AID))
                    self.assertIsNone(summary["receipt_issue"])
                    body = _public_occurrence(self.store, self.store.get_occurrence(self.occ.id))
                    self.assertEqual(body["receipt"], payload)
                    self.assertEqual(body["delivery"]["request_id"], AID)
                    self.assertIsNone(body["delivery"]["receipt_issue"])

    def test_unverified_legacy_strings_remain_private_and_reconcilable_without_identity_replacement(self):
        for legacy in ("msg_sendingwindow01", "msg_fake_queue"):
            with self.subTest(legacy=legacy):
                self.save_receipt({"state": "admitted", "admission_id": legacy})
                body = _public_occurrence(self.store, self.store.get_occurrence(self.occ.id))
                self.assertIsNone(body["receipt"]["admission_id"])
                self.assertIsNone(body["delivery"]["request_id"])
                self.assertEqual(body["receipt_issue"]["code"], "legacy_identity_unverified")
                self.assertTrue(body["delivery"]["recovery"]["reconcile"])
                self.assertEqual(self.store.get_occurrence(self.occ.id).receipt["admission_id"], legacy)
        req = self.store.reserve_request(self.store.get(self.item.id), self.store.get_occurrence(self.occ.id))
        self.assertEqual(req["request_id"], "msg_fake_queue")
        body = _public_occurrence(self.store, self.store.get_occurrence(self.occ.id))
        self.assertIsNone(body["delivery"]["request_id"])
        self.assertEqual(body["delivery"]["receipt_issue"]["code"], "legacy_identity_unverified")
        self.assertTrue(body["delivery"]["recovery"]["reconcile"])
        self.assertFalse(body["delivery"]["recovery"]["retry"])
        self.assertEqual(self.store.get_request(self.occ.id)["request_id"], "msg_fake_queue")

    def test_positive_stored_consumption_is_not_erased_by_malformed_compatibility_receipt(self):
        self.save_receipt({"state": "consumed", "admission_id": AID})
        req = self.store.reserve_request(self.store.get(self.item.id), self.store.get_occurrence(self.occ.id))
        self.assertTrue(req["facts"]["consumed"])
        self.save_receipt({"state": {"stderr": SENTINEL}, "admission_id": [SENTINEL]})
        body = self.assert_private_absent_from_all_views()
        self.assertTrue(body["delivery"]["facts"]["consumed"])
        self.assertEqual(body["delivery"]["outcome"], "delivered")
        self.assertEqual(self.store.get_request(self.occ.id)["facts"], req["facts"])

    def test_legitimate_user_reason_and_context_are_not_redacted_as_receipt_metadata(self):
        item = self.store.get(self.item.id)
        item.reason = "User quoted provider_stderr as legitimate task data"
        item.context = {"state": {"provider_stderr": "USER_DATA_IS_NOT_A_RECEIPT"}}
        self.store.save(item)
        body = _public_item(self.store, self.store.get(item.id))
        self.assertEqual((body["reason"], body["context"]), (item.reason, item.context))
        self.assertIsNone(receipt_projection(None)["issue"])
