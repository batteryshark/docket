"""Real worker/host/fixed factory/Sandbox/ScopedBroker, fake PROCESS seam only.

No native/model call or current prepared case is used; all grants are temp fixtures.
"""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from docket.event_host import configured_event_adapter
from docket.delivery import Receipt
from docket.sandbox_native import NativeRuntime, save, INTEGRATION
from docket.subscription_worker import EventStore, EventWorker
from docket.wake_profiles import bind_handler_instance
from tests.test_wake_profiles import fixture


@unittest.skipUnless(os.environ.get("TOOLSTACK_SOURCE"), "explicit unmodified Tool Stack required")
class FastTerminalTests(unittest.TestCase):
    @contextmanager
    def case(self, *, uncertain=False, invalid_terminal=None, partial_receipt=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            lifecycle = root / "lifecycle"; lifecycle.mkdir(mode=0o700)
            bindings = root / "bindings"; bindings.mkdir(mode=0o700)
            baseline, profile, budget = fixture()
            baseline["target"] = {"handler_class": "fast-terminal-fixture"}
            baseline["role"] = profile["role"] = "docket-test-handler"
            del baseline["goal_id"], baseline["goal_generation"]
            baseline["models"] = ["xai/grok-4.7"]; profile["model"] = baseline["models"][0]
            baseline["policy"] = profile["policy"] = {"docket_fixture": "allow"}
            profile["required_tools"] = ["docket_fixture"]
            installed = {"baseline": baseline, "profile": profile, "available_tools": ["docket_fixture"], "remaining_budget": budget}
            save(bindings / "installation.json", installed)
            save(bindings / "owner.json", {"target": baseline["target"], "owner_generation": baseline["owner_generation"], "stopped": False, "revoked": False})
            save(root / "sandbox.json", {"version": 1, "backend": "seatbelt-opencode", "state_root": str(lifecycle),
                "toolstack_root": os.environ["TOOLSTACK_SOURCE"], "broker_db": str(lifecycle / "broker.db"),
                "handler_port": 47971, "gateway_port": 47972, "events_plugin": str(INTEGRATION),
                "installation": installed, "timeout": 30, "approval": {"mode": "refuse"}})
            host = configured_event_adapter({"version": 1, "url": "http://127.0.0.1:47971", "binding_root": str(bindings), "sandbox_config": str(root / "sandbox.json")})
            store = EventStore(root / "queue.db")
            token = "temp-fixture-token-" + "x" * 32
            store.install_publisher("fixture", token)
            store.install_subscription("fixture", "fixture", "fixture.result", {"action": "handler", "baseline": baseline, "profile": profile})
            delivery = store.accept(token, {"id": "first", "type": "fixture.result", "payload": {"result": "fixture"}})
            worker = EventWorker(store, host)
            calls, preparation = [], {}
            def allocate(runtime, workspace, generation, *, allocation_id, prepared, deadline):
                calls.append("allocate")
                instance = bind_handler_instance(prepared, baseline, session_id="ses_" + allocation_id, directory=str(workspace),
                    environment_id=allocation_id, generation=generation, goal_id="fixture-goal", goal_generation=1792699749334)
                return {"session_id": instance["target"]["session_id"], "runtime_ref": {"allocation_id": allocation_id}}
            def launch(runtime, binding, prepared, bearer):
                calls.append("launch"); preparation.update(prepared)
                if uncertain:
                    raise ValueError("fixture lost ack after launch intent")
            def status(runtime, binding):
                calls.append("status")
                native = {"session_id": binding["session_id"], "admission_id": preparation["admission_id"],
                    "body_sha256": hashlib.sha256(preparation["briefing"].encode()).hexdigest(),
                    "terminal_id": "msg_fixtureterminal", "outcome": "succeeded", "not_active": True, "empty_inbox": True}
                native.update(invalid_terminal or {})
                return {"state": "admitted", "consumed": True, "finished": True, "trusted_completion": True,
                        "native_terminal": native, "output_manifest": []}
            def stop(runtime, binding):
                calls.append("stop")
                self.assertEqual(host.sandbox.lookup(binding["delivery_id"])["phase"], "revoked")
                return True
            receipt = host.sandbox.receipt
            def incomplete(binding):
                result = receipt(binding); result["phase"] = "revoked"
                return result
            try:
                with patch.object(NativeRuntime, "allocate", allocate), patch.object(NativeRuntime, "launch", launch), \
                        patch.object(NativeRuntime, "status", status), patch.object(NativeRuntime, "stop", stop):
                    with patch.object(host.sandbox, "receipt", incomplete if partial_receipt else receipt):
                        yield store, worker, host, delivery, calls
            finally:
                worker.close(); store.close(); host.sandbox.broker.ctx.store.close()

    def test_first_finished_status_from_admitted_or_uncertain_records_exact_terminal(self):
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain), self.case(uncertain=uncertain) as (store, worker, host, delivery, calls):
                now = time.time() + 0.1
                worker.tick(now)
                state = store.state(delivery["id"])
                self.assertEqual(state["phase"], "uncertain" if uncertain else "admitted")
                frozen = store.event_work(delivery["id"])["prepared_json"]
                worker.tick(now + 6)
                row = store.inspect_events(delivery["id"])[0]
                self.assertEqual((row["phase"], row["terminal"]), ("consumed", "handler_finished"))
                self.assertEqual(row["generation"], state["generation"])
                proof = store.handler_terminal(delivery["id"])
                self.assertEqual(proof["native_terminal"]["terminal_id"], "msg_fixtureterminal")
                self.assertEqual(proof["phase"], "pruned")
                self.assertNotIn("grant", proof)
                self.assertEqual(store.event_work(delivery["id"])["prepared_json"], frozen)
                self.assertEqual(calls, ["allocate", "launch", "status", "stop"])
                # Prior-finished host dispatch is status-only, and returns the
                # structured terminal receipt, not a bare state string.
                prepared = json.loads(frozen)
                terminal_receipt = host.dispatch(prepared, delivery["admission_id"], lifecycle={
                    "delivery_id": delivery["id"], "generation": state["generation"], "deadline": time.time() + 30, "lease_until": time.time() + 20})
                self.assertEqual(terminal_receipt.state, "handler_finished")
                store.record_receipt(delivery["id"], state["generation"], terminal_receipt)
                self.assertEqual(store.handler_terminal(delivery["id"]), proof)
                worker.tick(now + 12)
                self.assertEqual(calls, ["allocate", "launch", "status", "stop"])
                self.assertEqual(store._db.execute("SELECT COUNT(*) FROM subscription_admission_history WHERE phase='sending'").fetchone()[0], 1)

    def test_invalid_partial_or_mismatched_terminal_never_finishes(self):
        for invalid, partial in (({"body_sha256": "wrong"}, False), ({"admission_id": "msg_wrong"}, False),
                                 ({"not_active": False}, False), ({"empty_inbox": False}, False), ({}, True)):
            with self.subTest(invalid=invalid, partial=partial), self.case(invalid_terminal=invalid, partial_receipt=partial) as (store, worker, host, delivery, calls):
                now = time.time() + 0.1
                worker.tick(now); worker.tick(now + 6)
                self.assertIsNone(store.handler_terminal(delivery["id"]))
                self.assertNotEqual(store.inspect_events(delivery["id"])[0]["terminal"], "handler_finished")
                self.assertEqual(calls.count("launch"), 1)

    def test_terminal_receipt_mapping_rechecks_identity_and_preserves_proof(self):
        with self.case() as (store, worker, host, delivery, calls):
            now = time.time() + 0.1
            worker.tick(now)
            state = store.state(delivery["id"])
            aid = delivery["admission_id"]
            bare = Receipt(aid, "queue", aid, state="handler_finished", consumed=True)
            store.record_receipt(delivery["id"], state["generation"], bare)
            self.assertEqual(store.state(delivery["id"])["phase"], "admitted")
            self.assertIsNone(store.handler_terminal(delivery["id"]))
            prepared = json.loads(store.event_work(delivery["id"])["prepared_json"])
            self.assertIsNone(host.terminal_receipt(prepared, aid))
            self.assertNotIn("status", calls)  # Export itself cannot poll/prune.
            self.assertEqual(host.reconcile(prepared, aid), "handler_finished")
            valid = host.terminal_receipt(prepared, aid)
            self.assertIsInstance(valid, Receipt)
            for key, value in (("generation", "wrong"), ("delivery_id", "evd_wrong"), ("phase", "revoked")):
                invalid = copy.deepcopy(valid)
                invalid.raw["handler_terminal"][key] = value
                store.record_receipt(delivery["id"], state["generation"], invalid)
                self.assertIsNone(store.handler_terminal(delivery["id"]))
            store.record_receipt(delivery["id"], state["generation"], valid)
            self.assertEqual(store.state(delivery["id"])["phase"], "consumed")
            self.assertEqual(store.handler_terminal(delivery["id"])["native_terminal"]["admission_id"], aid)
            worker.tick(now + 6)
            self.assertEqual(store.inspect_events(delivery["id"])[0]["terminal"], "handler_finished")
            self.assertEqual(calls.count("launch"), 1)


if __name__ == "__main__":
    unittest.main()
