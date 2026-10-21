"""Regression proofs for the four independent core findings; no I/O to hosts."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docket.api import _public_item, _public_occurrence
from docket.clock import FakeClock
from docket.delivery import DeliveryError
from docket.delivery_state import issue
from docket.host import OpenChamberAdapter
from docket.scheduler import Scheduler
from docket.store import Occurrence, Store
from tests.support import utc
from tests.test_core import _item
from tests.test_recovery_core import Host
from tests.test_schedule import _recurring


class Crash(BaseException):
    pass


class RecoveryReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "review.db"
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.clock = FakeClock(utc(2026, 10, 19, 12, 0, 0))
        self.host = Host()
        self.scheduler = Scheduler(self.store, self.host, self.clock)
        self.item = _item("dkt_review", self.clock.now())
        self.store.insert(self.item)

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.scheduler = Scheduler(self.store, self.host, self.clock)

    @staticmethod
    def binding(origin, client=None):
        return {"adapter": "openchamber", "mode": "paired" if client else "unpaired-loopback",
                "origin": origin, "client_id": client, "namespace_verified": False}

    def test_origin_b_then_frozen_a_is_refused_before_any_host_io(self):
        adapter = OpenChamberAdapter(paired=False, root=Path(self.tmp.name))
        a, b = "http://127.0.0.1:1111", "http://127.0.0.1:2222"
        with patch("docket.host._configured", return_value=False), \
             patch("docket.host.desktop_origin", side_effect=[b, a]) as lookup, \
             patch("docket.host.health") as health, patch("docket.host.json_request") as transport:
            with self.assertRaises(DeliveryError) as raised:
                adapter._request("POST", "/api/session/test/prompt", {}, expected_binding=self.binding(a))
        self.assertEqual(raised.exception.code, "backend_unverifiable")
        self.assertEqual(lookup.call_count, 1)
        health.assert_not_called()
        transport.assert_not_called()

    def test_checked_origin_is_used_without_a_second_settings_read(self):
        adapter = OpenChamberAdapter(paired=False, root=Path(self.tmp.name))
        a, b = "http://127.0.0.1:1111", "http://127.0.0.1:2222"
        with patch("docket.host._configured", return_value=False), \
             patch("docket.host.desktop_origin", side_effect=[a, b]) as lookup, \
             patch("docket.host.health") as health, \
             patch("docket.host.json_request", return_value=(200, {})) as transport:
            adapter._request("POST", "/api/session/test/prompt", {}, expected_binding=self.binding(a))
        self.assertEqual(lookup.call_count, 1)
        health.assert_called_once_with(a)
        self.assertEqual(transport.call_args.args[0], a)

    def test_paired_client_b_then_frozen_a_is_refused_before_host_io(self):
        adapter = OpenChamberAdapter(root=Path(self.tmp.name))
        origin = "http://127.0.0.1:1111"
        a = {"version": 1, "clientId": "client-a", "clientToken": "token-a"}
        b = {"version": 1, "clientId": "client-b", "clientToken": "token-b"}
        with patch("docket.host._configured", return_value=True), \
             patch("docket.host.desktop_origin", return_value=origin), \
             patch("docket.host.read_private", side_effect=[b, a]) as lookup, \
             patch("docket.host.valid_client_id", return_value=True), \
             patch("docket.host.valid_client_token", return_value=True), \
             patch("docket.host.health") as health, patch("docket.host.json_request") as transport:
            with self.assertRaises(DeliveryError) as raised:
                adapter._request("POST", "/api/session/test/prompt", {},
                                 expected_binding=self.binding(origin, "client-a"))
        self.assertEqual(raised.exception.code, "backend_unverifiable")
        self.assertEqual(lookup.call_count, 1)
        health.assert_not_called()
        transport.assert_not_called()

    def test_own_client_validation_and_post_use_the_same_captured_record(self):
        adapter = OpenChamberAdapter(root=Path(self.tmp.name))
        origin, other = "http://127.0.0.1:1111", "http://127.0.0.1:2222"
        a = {"version": 1, "clientId": "client-a", "clientToken": "token-a"}
        b = {"version": 1, "clientId": "client-b", "clientToken": "token-b"}
        with patch("docket.host._configured", return_value=True), \
             patch("docket.host.desktop_origin", side_effect=[origin, other]) as origins, \
             patch("docket.host.read_private", side_effect=[a, b]) as records, \
             patch("docket.host.valid_client_id", return_value=True), \
             patch("docket.host.valid_client_token", return_value=True), \
             patch("docket.host.health") as health, \
             patch("docket.host.json_request", side_effect=[(200, {"clients": ["owned"]}), (200, {})]) as transport, \
             patch("docket.host.require_own_record") as own:
            adapter._request("POST", "/api/session/test/prompt", {},
                             expected_binding=self.binding(origin, "client-a"))
        self.assertEqual((origins.call_count, records.call_count), (1, 1))
        health.assert_called_once_with(origin)
        own.assert_called_once_with(["owned"], a)
        self.assertEqual(transport.call_args_list[0].args[:3], (origin, "GET", "/api/client-auth/clients"))
        self.assertEqual(transport.call_args_list[0].kwargs["token"], a["clientToken"])
        self.assertEqual(transport.call_args_list[1].args[:2], (origin, "POST"))
        self.assertEqual(transport.call_args_list[1].args[4], a["clientToken"])
        self.assertNotIn("token-a", json.dumps(self.binding(origin, "client-a")))

    def authorize_after_exhausted_pre_binding_failures(self):
        for _ in range(8):
            with patch.object(self.host, "binding_snapshot", side_effect=DeliveryError("unavailable")):
                self.scheduler.tick()
            self.clock.advance(120)
        self.assertEqual(self.store.get(self.item.id).attempts, 8)
        self.assertEqual(self.scheduler.recover(self.store.get(self.item.id), None, "retry"), (202, None))
        return self.store.get_request(self.item.id)

    def test_crash_before_claim_preserves_authorization_and_one_attempt_after_reopen(self):
        before = self.authorize_after_exhausted_pre_binding_failures()
        with patch.object(self.store, "claim", side_effect=Crash):
            with self.assertRaises(Crash):
                self.scheduler.tick()
        self.assertTrue(self.store.get_request(self.item.id)["manual_pending"])
        self.assertEqual(self.store.get(self.item.id).attempts, 8)
        self.reopen()
        self.scheduler.tick()
        self.clock.advance(120)
        self.scheduler.tick()
        after = self.store.get_request(self.item.id)
        self.assertEqual((after["caller"], after["request_id"]), (before["caller"], before["request_id"]))
        self.assertEqual(self.store.get(self.item.id).attempts, 9)
        self.assertFalse(after["manual_pending"])
        self.assertEqual(len(self.host.bodies), 1)

    def test_crash_after_claim_spends_one_attempt_and_closes_failed_not_stranded(self):
        before = self.authorize_after_exhausted_pre_binding_failures()
        claim = self.store.claim
        def crash_after(*args, **kwargs):
            self.assertTrue(claim(*args, **kwargs))
            raise Crash()
        with patch.object(self.store, "claim", side_effect=crash_after):
            with self.assertRaises(Crash):
                self.scheduler.tick()
        self.assertEqual(self.store.get(self.item.id).status, "firing")
        self.assertEqual(self.store.get(self.item.id).attempts, 9)
        self.assertFalse(self.store.get_request(self.item.id)["manual_pending"])
        self.reopen()
        for _ in range(3):
            self.clock.advance(120)
            self.scheduler.tick()
        row, req = self.store.get(self.item.id), self.store.get_request(self.item.id)
        self.assertEqual((row.status, row.attempts), ("undeliverable", 9))
        self.assertEqual(req["request_id"], before["request_id"])
        self.assertEqual(req["dispatch_state"], "never")
        self.assertEqual(_public_item(self.store, row)["delivery"]["outcome"], "failed")
        self.assertEqual(self.scheduler.recover(row, None, "retry"), (409, "retry_budget"))
        self.assertEqual(self.host.bodies, [])

    def test_failed_claim_commit_rolls_back_counter_and_authorization_together(self):
        self.authorize_after_exhausted_pre_binding_failures()
        with patch("docket.store._seal_db", side_effect=OSError("controlled failure")):
            with self.assertRaises(OSError):
                self.store.claim(self.item.id, self.clock.now(), max_attempts=8)
        self.assertEqual((self.store.get(self.item.id).status, self.store.get(self.item.id).attempts), ("pending", 8))
        self.assertTrue(self.store.get_request(self.item.id)["manual_pending"])
        self.reopen()
        self.scheduler.tick()
        self.assertEqual(self.store.get(self.item.id).attempts, 9)
        self.assertEqual(len(self.host.bodies), 1)

    def test_prepared_manual_claim_crash_closes_even_after_binding_ownership_loss(self):
        self.host.unavailable = True
        self.scheduler.tick()
        before = self.store.get_request(self.item.id)
        self.assertEqual(self.scheduler.recover(self.store.get(self.item.id), None, "retry"), (202, None))
        self.assertTrue(self.store.claim(self.item.id, self.clock.now(), max_attempts=8))
        self.reopen()
        self.clock.advance(120)
        self.scheduler.tick()
        self.assertEqual((self.store.get(self.item.id).status, self.store.get(self.item.id).attempts), ("undeliverable", 2))
        req = self.store.get_request(self.item.id)
        self.assertEqual(req["issue"]["code"], "retry_budget")
        self.assertEqual(req["frozen"], before["frozen"])
        self.assertFalse(req["binding_owned"])
        self.assertEqual(self.host.bodies, [])

    def test_last_automatic_claim_crash_closes_budget_but_does_not_spend_manual_authorization(self):
        for _ in range(7):
            with patch.object(self.host, "binding_snapshot", side_effect=DeliveryError("unavailable")):
                self.scheduler.tick()
            self.clock.advance(120)
        before = self.store.get_request(self.item.id)
        self.assertTrue(self.store.claim(self.item.id, self.clock.now(), max_attempts=8))
        self.reopen()
        self.clock.advance(120)
        self.scheduler.tick()
        self.assertEqual((self.store.get(self.item.id).status, self.store.get(self.item.id).attempts), ("undeliverable", 8))
        self.assertEqual(self.store.get_request(self.item.id)["issue"]["code"], "attempt_budget")
        self.assertFalse(self.store.get_request(self.item.id)["manual_used"])
        self.assertEqual(self.scheduler.recover(self.store.get(self.item.id), None, "retry"), (202, None))
        self.scheduler.tick()
        self.assertEqual(self.store.get(self.item.id).attempts, 9)
        self.assertEqual(self.store.get_request(self.item.id)["request_id"], before["request_id"])
        self.assertEqual(len(self.host.bodies), 1)

    def test_occurrence_claim_commit_and_authorization_are_atomic_too(self):
        self.store.cancel(self.item.id, self.clock.now())
        series = _recurring("dkt_series", self.clock.now(), every=60, count=3)
        self.store.insert(series)
        with patch.object(self.host, "binding_snapshot", side_effect=DeliveryError("unavailable")):
            self.scheduler.tick()
        occ = self.store.list_occurrences(series.id)[0]
        self.assertEqual(self.scheduler.recover(self.store.get(series.id), occ, "retry"), (202, None))
        claim = self.store.claim_occurrence
        def crash_after(*args, **kwargs):
            self.assertTrue(claim(*args, **kwargs))
            raise Crash()
        with patch.object(self.store, "claim_occurrence", side_effect=crash_after):
            with self.assertRaises(Crash):
                self.scheduler.tick()
        req = self.store.get_request(occ.id)
        self.assertFalse(req["manual_pending"])
        self.assertEqual(self.store.get_occurrence(occ.id).attempts, 2)
        self.reopen()
        self.clock.advance(120)
        self.scheduler.tick()
        self.assertEqual(self.store.get_occurrence(occ.id).status, "undeliverable")
        self.assertEqual(self.store.get_occurrence(occ.id).attempts, 2)
        self.assertEqual(self.store.get_request(occ.id)["dispatch_state"], "never")
        self.assertEqual(self.host.bodies, [])

    def test_cancel_between_authorization_and_claim_never_spends_or_sends(self):
        self.authorize_after_exhausted_pre_binding_failures()
        claim = self.store.claim
        def cancel_then_claim(*args, **kwargs):
            self.store.cancel(self.item.id, self.clock.now())
            return claim(*args, **kwargs)
        with patch.object(self.store, "claim", side_effect=cancel_then_claim):
            self.scheduler.tick()
        self.assertEqual((self.store.get(self.item.id).status, self.store.get(self.item.id).attempts), ("cancelled", 8))
        self.assertFalse(self.store.get_request(self.item.id)["manual_pending"])
        self.assertEqual(self.host.bodies, [])

    def test_missing_or_valid_open_pointer_never_exposes_raw_occurrence_error(self):
        series = _recurring("dkt_series", self.clock.now(), every=60, count=3)
        self.store.insert(series)
        occ = Occurrence(id="occ_review", item_id=series.id, seq=1, status="pending",
            scheduled_at=self.clock.now(), created_at=self.clock.now(), updated_at=self.clock.now(),
            late=False, late_by_seconds=None, delivered_at=None, error="PROVIDER_SECRET_SENTINEL\x1b[31m",
            receipt={"state": "admitted", "admission_id": "msg_" + "a" * 32}, attempts=1,
            next_attempt_at=None, firing_at=None, coalesced=0, detail={})
        self.store.attach_occurrence(series, occ)
        for pointer in (None, "occ_missing", occ.id):
            with self.subTest(pointer=pointer):
                series = self.store.get(series.id)
                series.spec["open_occurrence_id"] = pointer
                self.store.save(series)
                body = _public_item(self.store, self.store.get(series.id))
                self.assertNotIn("PROVIDER_SECRET_SENTINEL", json.dumps(body))
                self.assertEqual(body["open_occurrence"]["error"], issue("unavailable")["detail"])
                if pointer != occ.id:
                    self.assertEqual(body["delivery"]["outcome"], "uncertain")
                direct = _public_occurrence(self.store, self.store.get_occurrence(occ.id))
                self.assertTrue(direct["delivery"]["facts"]["admitted"])
                self.assertNotIn("PROVIDER_SECRET_SENTINEL", json.dumps(direct))
        occ.receipt = ["PROVIDER_SECRET_SENTINEL"]
        self.store.save_occurrence(occ)
        self.assertNotIn("PROVIDER_SECRET_SENTINEL", json.dumps(_public_item(self.store, self.store.get(series.id))))
        self.assertNotIn("PROVIDER_SECRET_SENTINEL", json.dumps(_public_occurrence(self.store, occ)))

    def test_malformed_or_missing_meta_fails_startup_without_rotation_with_or_without_requests(self):
        for has_requests in (False, True):
            for value in (None, "invalid-caller", "docket-" + "A" * 32, "docket-" + "a" * 32 + "\n"):
                with self.subTest(requests=has_requests, value=value):
                    path = Path(self.tmp.name) / f"meta-{has_requests}-{len(list(Path(self.tmp.name).glob('meta-*')))}.db"
                    store = Store(path)
                    if has_requests:
                        store.insert(self.item)
                        store.reserve_request(self.item)
                    if value is None:
                        store._db.execute("DELETE FROM delivery_meta")
                    else:
                        store._db.execute("UPDATE delivery_meta SET caller=?", (value,))
                    store._db.commit()
                    store.close()
                    with self.assertRaises(ValueError):
                        Store(path)
                    with sqlite3.connect(path) as raw:
                        actual = raw.execute("SELECT caller FROM delivery_meta").fetchone()
                    self.assertEqual(actual, None if value is None else (value,))

    def test_valid_identity_survives_restart_and_mismatched_request_caller_refuses_startup(self):
        req = self.store.reserve_request(self.item)
        self.reopen()
        self.assertEqual(self.store.reserve_request(self.item)["caller"], req["caller"])
        self.store._db.execute("UPDATE delivery_meta SET caller=?", ("docket-" + "b" * 32,))
        self.store._db.commit()
        self.store.close()
        with self.assertRaisesRegex(ValueError, "does not match"):
            Store(self.path)

    def test_runtime_caller_corruption_refuses_reserve_freeze_and_begin_send(self):
        self.host.unavailable = True
        self.scheduler.tick()
        req = self.store.get_request(self.item.id)
        for bad in (None, "invalid-caller", "docket-" + "b" * 32):
            with self.subTest(caller=bad):
                self.store._db.execute("DELETE FROM delivery_meta")
                if bad is not None:
                    self.store._db.execute("INSERT INTO delivery_meta VALUES (1,1,?)", (bad,))
                self.store._db.commit()
                with self.assertRaises(ValueError):
                    self.store.reserve_request(self.item)
                with self.assertRaises(ValueError):
                    self.store.freeze_request(self.item.id, req["frozen"])
                with self.assertRaises(ValueError):
                    self.store.begin_send(self.item.id, self.clock.now())
                self.assertEqual(self.store.get_request(self.item.id)["caller"], req["caller"])
                self.assertEqual(self.store.get_request(self.item.id)["dispatch_state"], "never")
        self.assertEqual(self.host.bodies, [])

    def test_runtime_valid_caller_replacement_is_refused_even_without_requests(self):
        original = self.store._db.execute("SELECT caller FROM delivery_meta").fetchone()[0]
        replacement = "docket-" + ("a" * 32 if original != "docket-" + "a" * 32 else "b" * 32)
        self.store._db.execute("UPDATE delivery_meta SET caller=?", (replacement,))
        self.store._db.commit()
        with self.assertRaisesRegex(ValueError, "refusing caller rotation"):
            self.store.reserve_request(self.item)
        self.assertIsNone(self.store.get_request(self.item.id))
        self.assertEqual(self.store._db.execute("SELECT caller FROM delivery_meta").fetchone()[0], replacement)
