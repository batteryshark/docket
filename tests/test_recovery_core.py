"""Isolated storage/scheduler/handler probes: no listeners, real hosts or agent calls."""
import json
import tempfile
import threading
import unittest
from io import BytesIO
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from docket.api import Handler, _public_item, _public_occurrence
from docket.clock import FakeClock
from docket.delivery import DeliveryError, OpenCodeAdapter
from docket.delivery_state import project
from docket.scheduler import Scheduler
from docket.store import Store
from docket.bounds import check_limits, LimitError
from tests.support import FakeAdapter, utc
from tests.test_core import _item
from tests.test_schedule import _recurring


class Host(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.origin = "fake-one"
        self.bodies = []
        self.entered_event = threading.Event()
        self.release = None
        self.timeout_after_accept = False

    def binding_snapshot(self):
        return {"adapter": "fake", "origin": self.origin, "namespace_verified": False}

    def deliver_frozen(self, frozen):
        self.bodies.append(frozen["body"])
        self.entered_event.set()
        if self.release:
            if not self.release.wait(2):
                raise RuntimeError("test release timed out")
        body = json.loads(frozen["body"])
        result = super().deliver(frozen["target"], body["text"], urgent=frozen["urgent"], admission_id=body["id"])
        if self.timeout_after_accept:
            raise TimeoutError("secret-host-output")
        return result


class CoreRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "docket.db"
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.host = Host()
        self.clock = FakeClock(utc(2026, 10, 19, 12, 0, 0))
        self.scheduler = Scheduler(self.store, self.host, self.clock)
        self.item = _item("dkt_1234567890abcdef", self.clock.now())
        self.store.insert(self.item)

    def run_tick(self):
        self.scheduler.tick()
        return self.store.get(self.item.id)

    def restart(self):
        self.store.close()
        self.store = Store(self.path)
        self.scheduler = Scheduler(self.store, self.host, self.clock)

    def test_freeze_before_send_and_immutable_ordinary_save(self):
        real = self.host.deliver_frozen
        def check(frozen):
            req = self.store.get_request(self.item.id)
            self.assertEqual(req["dispatch_state"], "possible")
            self.assertEqual(req["frozen"], frozen)
            self.assertIsNotNone(req["send_started_at"])
            return real(frozen)
        self.host.deliver_frozen = check
        self.run_tick()
        req = self.store.get_request(self.item.id)
        self.assertRegex(req["caller"], r"^docket-[0-9a-f]{32}$")
        self.assertRegex(req["request_id"], r"^msg_[0-9a-f]{32}$")
        modified = self.store.get(self.item.id)
        modified.reason = "new data"
        self.store.save(modified)
        self.assertEqual(req["frozen"], self.store.get_request(self.item.id)["frozen"])
        with self.assertRaises(ValueError):
            self.store.freeze_request(self.item.id, {"changed": True})

    def test_validation_retry_keeps_exact_body_across_clock_and_source_changes(self):
        self.host.unavailable = True
        self.run_tick()
        req = self.store.get_request(self.item.id)
        self.assertEqual(req["dispatch_state"], "never")
        body = req["frozen"]["body"]
        self.clock.advance(180)
        changed = self.store.get(self.item.id)
        changed.reason = "must not replace prepared payload"
        self.store.save(changed)
        self.host.unavailable = False
        self.assertEqual(self.run_tick().status, "delivered")
        self.assertEqual(self.host.bodies, [body])

    def test_prepared_request_loses_same_daemon_ownership_on_restart(self):
        self.host.unavailable = True
        self.run_tick()
        before = self.store.get_request(self.item.id)
        self.restart()
        self.host.unavailable = False
        self.clock.advance(120)
        self.run_tick()
        after = self.store.get_request(self.item.id)
        self.assertEqual((before["caller"], before["request_id"], before["frozen"]),
                         (after["caller"], after["request_id"], after["frozen"]))
        self.assertFalse(after["binding_owned"])
        self.assertEqual(after["issue"]["code"], "backend_unverifiable")
        self.assertEqual(self.host.bodies, [])

    def test_reservation_without_binding_can_prepare_after_restart(self):
        with patch.object(self.host, "binding_snapshot", side_effect=DeliveryError("unavailable")):
            self.run_tick()
        req = self.store.get_request(self.item.id)
        self.assertIsNone(req["frozen"])
        self.restart()
        self.clock.advance(120)
        self.run_tick()
        self.assertEqual(self.store.get_request(self.item.id)["request_id"], req["request_id"])
        self.assertEqual(len(self.host.bodies), 1)

    def test_post_accept_timeout_is_status_only_and_can_later_prove_consumed(self):
        self.host.timeout_after_accept = True
        self.assertEqual(self.run_tick().status, "ambiguous")
        before = self.store.get_request(self.item.id)
        self.assertEqual(project(self.store, self.store.get(self.item.id))["outcome"], "uncertain")
        self.clock.advance(120)
        self.assertEqual(self.run_tick().status, "delivered")
        self.assertEqual(len(self.host.bodies), 1)
        self.assertEqual(before["request_id"], self.store.get_request(self.item.id)["request_id"])

    def test_missing_after_possible_send_never_replays_or_advances_series(self):
        self.host.fail = DeliveryError("unavailable", "secret", retryable=True)
        self.run_tick()
        self.clock.advance(120)
        self.run_tick()
        self.assertEqual(len(self.host.bodies), 1)
        result = self.scheduler.recover(self.store.get(self.item.id), None, "retry")
        self.assertEqual(result, (409, "acceptance_unknown"))
        self.assertNotIn("secret", json.dumps(_public_item(self.store, self.store.get(self.item.id))))

    def test_send_marker_crash_before_transport_is_conservative(self):
        self.host.unavailable = True
        self.run_tick()
        row = self.store.get(self.item.id)
        row.status = "firing"
        row.firing_at = self.clock.now() - timedelta(seconds=120)
        self.store.save(row)
        self.assertTrue(self.store.begin_send(self.item.id, self.clock.now()))
        self.restart()
        self.host.unavailable = False
        self.run_tick()
        self.assertEqual(self.host.bodies, [])
        self.assertEqual(project(self.store, self.store.get(self.item.id))["outcome"], "uncertain")

    def test_lookup_failure_never_admitted(self):
        self.host.conflict = True
        self.host.lookup_unavailable = True
        self.run_tick()
        self.clock.advance(120)
        self.run_tick()
        req = self.store.get_request(self.item.id)
        self.assertNotIn("admitted", req["facts"])
        self.assertIsNotNone(req["check_issue"])
        self.assertEqual(self.scheduler._lookup_quiet(self.item.target, req["request_id"]), "unavailable")

    def test_binding_change_prevents_pre_dispatch_retry(self):
        self.host.unavailable = True
        self.run_tick()
        self.host.origin = "fake-two"
        self.host.unavailable = False
        self.assertEqual(self.scheduler.recover(self.store.get(self.item.id), None, "retry"),
                         (409, "backend_unverifiable"))
        self.assertEqual(self.host.bodies, [])

    def test_restoring_original_directory_allows_pre_dispatch_recovery_only(self):
        row = self.store.get(self.item.id)
        row.target["directory"] = "/original-project"
        self.store.save(row)
        self.host.directories[row.target["session_id"]] = "/moved-project"
        self.run_tick()
        req = self.store.get_request(row.id)
        self.assertEqual(req["issue"]["code"], "wrong_target")
        self.assertEqual(req["dispatch_state"], "never")
        self.host.directories[row.target["session_id"]] = "/original-project"
        self.assertEqual(self.scheduler.recover(self.store.get(row.id), None, "retry"), (202, None))
        self.clock.advance(120)
        self.assertEqual(self.run_tick().status, "delivered")
        self.assertEqual(self.host.bodies, [req["frozen"]["body"]])

    def test_reconcile_refuses_in_flight_without_reading_host(self):
        self.host.unavailable = True
        self.run_tick()
        with patch.object(self.host, "reconcile", wraps=self.host.reconcile) as lookup:
            gate = self.scheduler._gate(self.item.id)
            gate.acquire()
            try:
                self.assertEqual(self.scheduler.recover(self.store.get(self.item.id), None, "reconcile"),
                                 (409, "in_flight"))
            finally:
                gate.release()
        lookup.assert_not_called()

    def test_manual_retry_once_without_reset_and_duplicate_clicks_coalesce(self):
        self.host.unavailable = True
        for _ in range(8):
            self.run_tick()
            self.clock.advance(120)
        self.assertEqual(self.store.get(self.item.id).attempts, 8)
        self.assertEqual(self.store.get(self.item.id).status, "undeliverable")
        results = []
        def click():
            results.append(self.scheduler.recover(self.store.get(self.item.id), None, "retry"))
        workers = [threading.Thread(target=click) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(2)
        self.assertTrue(all(result in ((202, None), (409, "in_flight")) for result in results))
        self.run_tick()
        self.assertEqual(self.store.get(self.item.id).attempts, 9)
        self.assertEqual(self.scheduler.recover(self.store.get(self.item.id), None, "retry"), (409, "retry_budget"))
        self.clock.advance(120)
        self.run_tick()
        self.assertEqual(self.store.get(self.item.id).attempts, 9)

    def test_cancel_invisible_send_preserves_positive_consumption(self):
        self.host.release = threading.Event()
        worker = threading.Thread(target=self.scheduler.tick)
        worker.start()
        self.assertTrue(self.host.entered_event.wait(2))
        self.assertEqual(self.scheduler.stop(self.store.get(self.item.id)), "uncertain")
        self.host.release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        item = self.store.get(self.item.id)
        self.assertEqual(item.status, "cancelled")
        self.assertTrue(project(self.store, item)["facts"]["consumed"])
        self.assertEqual(self.scheduler.recover(item, None, "retry"), (409, "cancelled_identity"))

    def test_expired_lease_is_not_proof_in_process_transport_ended(self):
        self.host.release = threading.Event()
        worker = threading.Thread(target=self.scheduler.tick)
        worker.start()
        self.assertTrue(self.host.entered_event.wait(2))
        try:
            self.clock.advance(120)
            self.scheduler.tick()
            self.assertEqual(len(self.host.bodies), 1)
            self.assertEqual(self.store.get(self.item.id).status, "firing")
        finally:
            self.host.release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.store.get(self.item.id).status, "delivered")

    def test_cancel_before_send_is_not_sent(self):
        self.assertEqual(self.scheduler.stop(self.item), "not_sent")
        self.run_tick()
        self.assertTrue(project(self.store, self.store.get(self.item.id))["facts"]["cancelled"])
        self.assertEqual(self.host.bodies, [])

    def test_legacy_sending_absent_envelope_retains_id_and_refuses(self):
        row = self.store.get(self.item.id)
        row.status = "firing"
        row.attempts = 1
        row.firing_at = self.clock.now() - timedelta(seconds=120)
        row.receipt = {"admission_id": "msg_" + "a" * 32, "state": "sending"}
        self.store.save(row)
        self.run_tick()
        req = self.store.get_request(row.id)
        self.assertEqual(req["request_id"], row.receipt["admission_id"])
        self.assertIsNone(req["frozen"])
        self.assertEqual(self.scheduler.recover(self.store.get(row.id), None, "retry"), (409, "legacy_envelope_unknown"))
        self.assertEqual(self.host.bodies, [])

    def test_legacy_no_id_never_mints_replacement(self):
        row = self.store.get(self.item.id)
        row.status = "ambiguous"
        row.attempts = 1
        self.store.save(row)
        self.run_tick()
        self.assertIsNone(self.store.get_request(row.id)["request_id"])
        self.assertEqual(self.host.bodies, [])

    def test_recurrence_mixed_results_completed_not_consumed(self):
        item = _recurring("dkt_series", self.clock.now(), every=60, count=2)
        self.store.insert(item)
        # Keep the unrelated one-shot from becoming due during this probe.
        self.store.cancel(self.item.id, self.clock.now())
        self.host.unavailable = True
        for _ in range(8):
            self.scheduler.tick()
            self.clock.advance(120)
        self.host.unavailable = False
        self.scheduler.tick()
        parent = self.store.get(item.id)
        self.clock.advance(120)
        self.scheduler.tick()
        parent = self.store.get(item.id)
        self.assertEqual(parent.status, "completed")
        self.assertEqual(project(self.store, parent)["outcome"], "not_applicable")
        rows = self.store.list_occurrences(item.id)
        self.assertEqual([row.status for row in rows], ["undeliverable", "delivered"])
        self.assertEqual(_public_item(self.store, parent)["delivery_summary"], {"failed": 1, "delivered": 1})
        self.assertEqual(self.scheduler.recover(parent, rows[0], "retry"), (409, "schedule_advanced"))

    def test_reconcile_endpoint_auth_and_empty_body_and_shared_projection(self):
        self.host.unavailable = True
        self.run_tick()
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(store=self.store, scheduler=self.scheduler, _mutate=threading.Lock())
        handler._boundary_ok = lambda: True
        handler._authed = lambda: False
        handler._send = Mock()
        handler._split = lambda: (f"/api/v1/items/{self.item.id}/retry", {})
        handler.do_POST()
        self.assertEqual(handler._send.call_args.args[0], 401)
        handler._authed = lambda: True
        handler._body = lambda: {"reason": "replacement forbidden"}
        handler.do_POST()
        self.assertEqual(handler._send.call_args.args[0], 400)
        handler._body = lambda: {}
        handler.do_POST()
        status, body = handler._send.call_args.args
        self.assertEqual(status, 202)
        self.assertTrue(body["authorized"])
        self.assertEqual(body["item"]["delivery"], _public_item(self.store, self.store.get(self.item.id))["delivery"])
        public = json.dumps(body)
        self.assertNotIn('"frozen"', public)
        self.assertNotIn('"binding"', public)

    def test_opencode_mismatching_transcript_never_consumed(self):
        self.host.unavailable = True
        self.run_tick()
        req = self.store.get_request(self.item.id)
        adapter = OpenCodeAdapter(discovery=lambda: ("http://127.0.0.1:1234", "SECRET"))
        with patch.object(adapter, "_request", return_value=(200, {"data": {"id": req["request_id"]}})):
            with self.assertRaises(DeliveryError):
                adapter.status_frozen(req["frozen"])

    def test_released_opencode_saved_user_shape_requires_exact_identity_type_and_text(self):
        self.host.unavailable = True
        self.run_tick()
        frozen = self.store.get_request(self.item.id)["frozen"]
        body = json.loads(frozen["body"])
        adapter = OpenCodeAdapter()
        # Structural fixture grounded in the controlled 2.0.32 native proof;
        # no private host IDs, marker strings, paths or assistant evidence.
        saved = {"id": body["id"], "time": {"created": 1000}, "text": body["text"], "type": "user"}
        with patch.object(adapter, "_request", return_value=(200, {"data": saved})) as transport:
            self.assertEqual(adapter.status_frozen(frozen), "consumed")
        self.assertEqual(transport.call_args.args[1],
                         f"/api/session/{frozen['target']['session_id']}/message/{body['id']}")
        for field, wrong in (("id", "msg_" + "f" * 32), ("type", "assistant"), ("text", body["text"] + "changed")):
            with self.subTest(field=field):
                with patch.object(adapter, "_request", return_value=(200, {"data": {**saved, field: wrong}})):
                    with self.assertRaises(DeliveryError):
                        adapter.status_frozen(frozen)

    def test_opencode_actual_request_bytes_match_frozen_body_without_network(self):
        self.host.unavailable = True
        self.run_tick()
        frozen = self.store.get_request(self.item.id)["frozen"]
        adapter = OpenCodeAdapter(discovery=lambda: ("http://127.0.0.1:1234", "SECRET"))
        frozen["binding"] = adapter.binding_snapshot()
        body = json.loads(frozen["body"])
        response = BytesIO(json.dumps({"data": {"id": body["id"]}}).encode())
        response.status = 200
        opener = Mock()
        opener.open.return_value = response
        with patch("urllib.request.build_opener", return_value=opener) as built:
            adapter.deliver_frozen(frozen)
        wire = opener.open.call_args.args[0]
        self.assertEqual(wire.data, frozen["body"].encode())
        self.assertNotIn("SECRET", json.dumps(frozen))
        self.assertEqual(built.call_args.args[0].proxies, {})
        self.assertIsNone(built.call_args.args[1].redirect_request(None, None, None, None, None, None))

    def test_frozen_validation_cannot_rediscover_another_origin(self):
        adapter = OpenCodeAdapter(discovery=lambda: ("http://127.0.0.1:1234", "SECRET"))
        binding = adapter.binding_snapshot()
        adapter._discovery = lambda: ("http://127.0.0.1:5678", "OTHER_SECRET")
        with patch("urllib.request.build_opener") as transport:
            result = adapter.validate_frozen({"target": self.item.target, "binding": binding})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "backend_unverifiable")
        transport.assert_not_called()

    def test_positive_admission_survives_status_outage_without_consumption(self):
        self.host.consume_immediately = False
        self.run_tick()
        self.host.lookup_unavailable = True
        self.scheduler.recover(self.store.get(self.item.id), None, "reconcile")
        delivery = project(self.store, self.store.get(self.item.id))
        self.assertEqual(delivery["outcome"], "admitted")
        self.assertIsNone(delivery["facts"]["consumed"])
        self.assertTrue(delivery["stale"])
        self.assertEqual(len(self.host.bodies), 1)

    def test_consumption_is_monotonic_even_if_subsequent_status_is_missing(self):
        self.run_tick()
        self.host.consumed.clear()
        self.scheduler.recover(self.store.get(self.item.id), None, "reconcile")
        delivery = project(self.store, self.store.get(self.item.id))
        self.assertEqual(delivery["outcome"], "delivered")
        self.assertEqual(delivery["execution"], "unknown")
        self.assertEqual(len(self.host.bodies), 1)

    def test_cancel_reservation_snapshot_race_does_not_claim_not_sent(self):
        self.host.unavailable = True
        self.run_tick()
        original = self.store.cancel
        def race(ident, now):
            row = self.store.get(ident)
            row.status = "firing"
            self.store.save(row)
            self.assertTrue(self.store.begin_send(ident, now))
            return original(ident, now)
        self.store.cancel = race
        outcome = self.scheduler.stop(self.store.get(self.item.id))
        self.assertEqual(outcome, "uncertain")
        self.assertIsNone(project(self.store, self.store.get(self.item.id))["facts"]["cancelled"])

    def test_cancelled_uncertain_delivery_keeps_capacity_reserved(self):
        self.host.fail = DeliveryError("unavailable")
        self.run_tick()
        self.scheduler.stop(self.store.get(self.item.id))
        active = self.store.active_items()
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0].capacity_reserved)
        with patch("docket.bounds.MAX_PENDING_PER_TARGET", 1):
            with self.assertRaises(LimitError):
                check_limits(active, kind="timer", session_id=self.item.target["session_id"], created_by=None)

    def test_uncertain_occurrence_holds_series_without_another_identity(self):
        self.store.cancel(self.item.id, self.clock.now())
        item = _recurring("dkt_series", self.clock.now(), every=60, count=2)
        self.store.insert(item)
        self.host.fail = DeliveryError("unavailable")
        self.scheduler.tick()
        first = self.store.list_occurrences(item.id)[0]
        request = self.store.get_request(first.id)
        self.clock.advance(600)
        self.scheduler.tick()
        self.assertEqual([row.id for row in self.store.list_occurrences(item.id)], [first.id])
        self.assertEqual(self.store.get_request(first.id)["request_id"], request["request_id"])
        self.assertEqual(_public_item(self.store, self.store.get(item.id))["delivery"]["unit_id"], first.id)
        self.assertEqual(len(self.host.bodies), 1)

    def test_missing_open_occurrence_is_not_replaced(self):
        self.store.cancel(self.item.id, self.clock.now())
        item = _recurring("dkt_series", self.clock.now(), every=60, count=2)
        item.spec["open_occurrence_id"] = "occ_missing"
        self.store.insert(item)
        self.scheduler.tick()
        self.assertEqual(self.store.get(item.id).status, "ambiguous")
        self.assertEqual(self.store.list_occurrences(item.id), [])
        self.assertEqual(self.host.bodies, [])

    def test_failed_prepare_and_begin_commit_roll_back_before_transport(self):
        req = self.store.reserve_request(self.item)
        frozen = {"body": "{}", "target": self.item.target, "binding": self.host.binding_snapshot()}
        with patch("docket.store._seal_db", side_effect=OSError("secret")):
            with self.assertRaises(OSError):
                self.store.freeze_request(self.item.id, frozen)
        self.assertIsNone(self.store.get_request(self.item.id)["frozen"])
        self.store.freeze_request(self.item.id, frozen)
        self.store.claim(self.item.id, self.clock.now())
        with patch("docket.store._seal_db", side_effect=OSError("secret")):
            with self.assertRaises(OSError):
                self.store.begin_send(self.item.id, self.clock.now())
        self.assertEqual(self.store.get_request(self.item.id)["dispatch_state"], "never")
        self.assertEqual(self.host.bodies, [])

    def test_missing_caller_meta_refuses_rotation(self):
        req = self.store.reserve_request(self.item)
        self.store._db.execute("DELETE FROM delivery_meta")
        self.store._db.commit()
        self.store.close()
        with self.assertRaisesRegex(ValueError, "caller metadata missing"):
            Store(self.path)

    def test_paired_own_record_refusal_prevents_post_without_fallback(self):
        from docket.host import OpenChamberAdapter
        from docket.pairing import RecordRefusal
        adapter = OpenChamberAdapter(root=Path(self.tmp.name))
        with patch("docket.host._configured", return_value=True), \
             patch("docket.host.desktop_origin", return_value="http://127.0.0.1:1234"), \
             patch("docket.host.health"), \
             patch("docket.host.read_private", return_value={"version": 1, "clientId": "test", "clientToken": "SECRET"}), \
             patch("docket.host.valid_client_id", return_value=True), \
             patch("docket.host.valid_client_token", return_value=True), \
             patch("docket.host.json_request", return_value=(200, {"clients": []})) as transport, \
             patch("docket.host.require_own_record", side_effect=RecordRefusal("not the active own record")):
            with self.assertRaises(DeliveryError) as raised:
                adapter._request("POST", "/api/session/test/prompt", {})
        self.assertEqual(raised.exception.code, "refused")
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.call_args.args[1], "GET")
        self.assertNotIn("SECRET", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
