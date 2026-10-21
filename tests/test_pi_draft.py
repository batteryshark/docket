"""Pure companion-boundary fixtures; never execute Node/Pi or inspect its store."""
import copy
import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from docket.api import Handler
from docket.cli import main
from docket.mcp_server import TOOLS, _call
from docket.clock import FakeClock
from docket.checks import Outcome
from docket.delivery import DeliveryError, OpenCodeAdapter
from docket.delivery_state import project
from docket.host import configured_adapter
from docket.pi import MachineRunner, PiAdapter
from docket.scheduler import Scheduler
from docket.store import Store
from tests.support import FakeAdapter, utc
from tests.test_core import _item
from tests.test_schedule import _recurring


CAP = {"protocol": 1, "contract": "pi-a2a-external-v1",
       "operations": ["capabilities", "lookup", "send", "status", "cancel"],
       "delivery": ["queue", "steer"], "expectReply": False, "maxCaller": 48,
       "maxRequestId": 96, "maxTextBytes": 32768, "maxInputBytes": 262144,
       "maxOutputBytes": 8192, "maxRetainedIdentities": 50000, "maxActivePayloads": 128,
       "capacityScope": "shared_ledger", "maxStateBytes": 134217728,
       "inspection": "read_only", "targetSnapshot": True, "reservation": False}


def receipt(state="admitted", *, evidence="not_checked", **facts):
    values = {key: False for key in ("admitted", "claimed", "injected", "observed", "consumed", "historicalUncertainty")}
    values.update(facts)
    if state in ("admitted", "injected", "consumed"):
        values["admitted"] = True
    if state in ("injected", "consumed"):
        values.update(claimed=True, injected=True)
    if state == "consumed":
        values.update(observed=True, consumed=True)
    return {"v": 1, "ok": True, "receipt": {"state": state, "facts": values,
            "evidence": evidence, "cancelAvailable": not values["claimed"], "retry": "same_key_only"}}


class PiDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "pi-companion.json"
        self.settings = {"v": 1, "node": str(self.root / "reviewed-node"),
                         "cli": str(self.root / "installation" / "cli.mjs"),
                         "state": str(self.root / "shared-state")}
        self.write_config()
        self.calls = []
        self.responses = {}
        self.adapter = PiAdapter(config=self.config, runner=self.run_boundary)
        self.clock = FakeClock(utc(2026, 10, 20, 12))
        self.path = self.root / "docket.db"
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.default = FakeAdapter()
        self.scheduler = Scheduler(self.store, self.default, self.clock, pi_adapter=self.adapter)
        self.item = _item("dkt_pi_fixture", self.clock.now())
        self.item.target = {"adapter": "pi", "session_id": "pi/fake_receiver",
                            "provider_binding": self.adapter.binding_snapshot()}
        self.store.insert(self.item)

    def write_config(self):
        self.config.write_text(json.dumps(self.settings))
        self.config.chmod(0o600)

    def run_boundary(self, argv, raw, env):
        frame = json.loads(raw)
        self.calls.append((argv, raw, env, frame))
        self.assertEqual(argv, [self.settings["node"], self.settings["cli"], "machine"])
        self.assertEqual(env["PI_A2A_STATE"], self.settings["state"])
        self.assertNotIn("NODE_OPTIONS", env)
        op = frame["op"]
        if op == "send":
            req = self.store.get_request(self.current_unit)
            self.assertEqual(req["dispatch_state"], "possible")
            self.assertEqual(json.loads(req["frozen"]["body"]), frame)
        response = self.responses.get(op)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            return response
        if response is None:
            if op == "capabilities":
                response = {"v": 1, "ok": True, "capabilities": CAP}
            elif op == "lookup":
                response = {"v": 1, "ok": True, "target": {"to": frame["to"], "exists": True,
                            "live": True, "capable": True, "snapshot": True, "reservation": False}}
            else:
                response = receipt()
        return 0, (json.dumps(response) + "\n").encode()

    @property
    def current_unit(self):
        occ = self.store.open_occurrence(self.store.get(self.item.id))
        return occ.id if occ else self.item.id

    def tick(self):
        self.scheduler.tick()
        return self.store.get(self.item.id)

    def sends(self):
        return [call[3] for call in self.calls if call[3]["op"] == "send"]

    def create(self, kind="timer", *, selected="pi", **fields):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(store=self.store, scheduler=self.scheduler, _mutate=threading.Lock())
        handler._send = Mock()
        request = {"session_id": "pi/fake_other", "reason": "synthetic", "in": "1m", **fields}
        if selected is not None:
            request["adapter"] = selected
        handler._create(kind, request)
        return handler._send.call_args.args

    def test_uppercase_windows_launcher_environment_is_retained(self):
        inherited = {"SYSTEMROOT": r"Q:\Inherited System Root", "WINDIR": r"Q:\Inherited Windows",
                     "PATH": r"Q:\trusted-bin", "PATHEXT": ".EXE;.CMD",
                     "NODE_OPTIONS": "--require=untrusted-sentinel", "NODE_PATH": "untrusted-sentinel"}
        with patch.dict(os.environ, inherited, clear=True):
            result = self.adapter._call({"v": 1, "op": "capabilities"})
        self.assertEqual(result, {"v": 1, "ok": True, "capabilities": CAP})
        self.assertEqual(len(self.calls), 1)
        argv, raw, env, frame = self.calls[0]
        self.assertEqual(env.get("SYSTEMROOT"), inherited["SYSTEMROOT"])
        self.assertNotIn("NODE_OPTIONS", env)
        self.assertNotIn("NODE_PATH", env)
        self.assertEqual(env, {key: inherited[key] for key in ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT")}
                         | {"PI_A2A_STATE": self.settings["state"]})
        self.assertEqual(argv, [self.settings["node"], self.settings["cli"], "machine"])
        self.assertEqual(raw, b'{"v":1,"op":"capabilities"}\n')
        self.assertEqual(frame, {"v": 1, "op": "capabilities"})

    def test_pure_validation_then_fire_revalidation(self):
        code, body = self.create()
        self.assertEqual(code, 201)
        self.assertEqual([c[3]["op"] for c in self.calls], ["capabilities", "lookup"])
        self.assertEqual(body["item"]["target"]["adapter"], "pi")
        self.assertNotIn("provider_binding", body["item"]["target"])
        self.tick()
        self.assertEqual([c[3]["op"] for c in self.calls][-3:], ["capabilities", "lookup", "send"])

    def test_queue_frozen_exact_fields_and_no_reply(self):
        self.tick()
        body = self.sends()[0]
        self.assertEqual(set(body), {"v", "op", "caller", "id", "to", "text", "delivery", "expectReply"})
        self.assertRegex(body["caller"], r"^docket-[0-9a-f]{32}$")
        self.assertRegex(body["id"], r"^msg_[0-9a-f]{32}$")
        self.assertEqual((body["delivery"], body["expectReply"]), ("queue", False))
        self.assertEqual(self.store.get(self.item.id).status, "admitted")

    def test_mixed_api_items_send_status_and_cancel_use_local_adapter(self):
        self.default.consume_immediately = False
        code, body = self.create(selected=None, session_id="ses_knownsession0001", **{"in": 0})
        self.assertEqual(code, 201)
        oc_id = body["item"]["id"]
        self.tick()
        pi_req = self.store.get_request(self.item.id)
        oc_req = self.store.get_request(oc_id)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(len(self.default.sent), 1)
        self.assertEqual(pi_req["frozen"]["binding"]["adapter"], "pi")
        self.assertEqual(oc_req["frozen"]["binding"]["adapter"], "fake")
        self.assertEqual(pi_req["caller"], oc_req["caller"])
        self.assertNotEqual(pi_req["request_id"], oc_req["request_id"])
        self.assertIs(self.scheduler.adapter, self.default)
        self.responses["status"] = receipt("admitted")
        self.clock.advance(31)
        self.tick()
        self.assertEqual(self.store.get(self.item.id).status, "admitted")
        self.assertEqual(self.store.get(oc_id).status, "admitted")
        self.responses["cancel"] = receipt("cancelled", admitted=True)
        self.assertEqual(self.scheduler.stop(self.store.get(self.item.id)), "retracted")
        self.scheduler.stop(self.store.get(oc_id))
        self.assertEqual(self.default.cancelled, [("ses_knownsession0001", oc_req["request_id"])])
        self.assertEqual([c[3]["id"] for c in self.calls if c[3]["op"] == "cancel"], [pi_req["request_id"]])
        self.assertIs(self.scheduler.adapter, self.default)

    def test_default_create_and_fire_never_read_pi_configuration(self):
        self.store.cancel(self.item.id, self.clock.now())
        self.scheduler = Scheduler(self.store, self.default, self.clock)
        with patch("docket.pi.read_private_bytes", side_effect=AssertionError("default touched Pi config")):
            code, body = self.create(selected=None, session_id="ses_knownsession0001", **{"in": 0})
            self.assertEqual(code, 201)
            self.tick()
        self.assertEqual(self.store.get(body["item"]["id"]).status, "delivered")
        self.assertIsNone(self.scheduler._pi_adapter)

    def test_invalid_pi_config_does_not_block_default_items(self):
        self.config.unlink()
        code, body = self.create(selected=None, session_id="ses_knownsession0001", **{"in": 0})
        self.assertEqual(code, 201)
        self.tick()
        self.assertEqual(self.store.get(body["item"]["id"]).status, "delivered")
        self.assertEqual(self.sends(), [])

    def test_explicit_selection_and_prefixes_fail_closed(self):
        for selected, target in ((None, "pi/fake_other"), ("pi", "ses_knownsession0001"),
                                 ("pi", "oc/ses_fake"), ("pi", "chief"), ("other", "pi/fake")):
            code, _ = self.create(selected=selected, session_id=target)
            self.assertEqual(code, 400)
        self.assertEqual(self.calls, [])

    def test_pi_adapter_is_cached_without_changing_default(self):
        scheduler = Scheduler(self.store, self.default, self.clock)
        with patch("docket.pi.PiAdapter", return_value=self.adapter) as factory:
            self.assertIs(scheduler.adapter_for_target({"adapter": "pi"}), self.adapter)
            self.assertIs(scheduler.adapter_for_target({"adapter": "pi"}), self.adapter)
            factory.assert_called_once_with()
        self.assertIs(scheduler.adapter_for_target({"adapter": "opencode"}), self.default)

    def test_per_target_retry_uses_original_pi_binding_and_id(self):
        self.responses["lookup"] = {"v": 1, "ok": True, "target": {"to": self.item.target["session_id"],
                                  "exists": True, "capable": True, "live": False,
                                  "snapshot": True, "reservation": False}}
        self.tick()
        before = self.store.get_request(self.item.id)
        self.assertEqual(before["dispatch_state"], "never")
        self.assertEqual(self.scheduler.recover(self.store.get(self.item.id), None, "retry"), (202, None))
        del self.responses["lookup"]
        self.clock.advance(3)
        self.tick()
        self.assertEqual(self.sends()[0]["id"], before["request_id"])
        self.assertEqual(self.store.get_request(self.item.id)["frozen"], before["frozen"])
        self.assertEqual(self.default.sent, [])

    def test_definite_preclaim_failure_is_failed_not_consumed_or_retryable(self):
        self.responses["send"] = receipt("failed", evidence="deadline", admitted=True)
        item = self.tick()
        self.assertEqual(item.status, "undeliverable")
        view = project(self.store, item)
        self.assertEqual(view["outcome"], "failed")
        self.assertFalse(view["facts"].get("consumed", False))
        self.assertFalse(view["recovery"]["retry"])
        self.assertEqual(len(self.sends()), 1)

    def test_status_preclaim_failure_after_admission_preserves_admitted_fact(self):
        self.tick()
        self.responses["status"] = receipt("failed", evidence="owner_unavailable", admitted=True)
        self.clock.advance(31)
        item = self.tick()
        self.assertEqual(project(self.store, item)["outcome"], "failed")
        self.assertTrue(self.store.get_request(self.item.id)["facts"]["admitted"])
        self.assertEqual(len(self.sends()), 1)

    def test_failed_receipt_without_admitted_fact_does_not_invent_admission(self):
        self.responses["send"] = receipt("failed", evidence="deadline")
        item = self.tick()
        req = self.store.get_request(item.id)
        self.assertEqual(project(self.store, item)["outcome"], "failed")
        self.assertFalse(req["facts"].get("admitted", False))
        self.assertEqual(req["issue"]["admission_certainty"], "unknown")
        self.assertEqual(req["dispatch_state"], "possible")

    def test_failed_after_prior_claim_cannot_erase_positive_facts(self):
        self.responses["send"] = receipt("injected")
        self.tick()
        self.responses["status"] = receipt("failed", admitted=True, evidence="deadline")
        self.clock.advance(31)
        item = self.tick()
        self.assertEqual(project(self.store, item)["outcome"], "uncertain")
        self.assertTrue(self.store.get_request(self.item.id)["facts"]["injected"])
        self.assertFalse(self.store.get_request(self.item.id)["facts"].get("failed_before_claim", False))

    def test_cli_optional_selection_and_unchanged_omission(self):
        for selected in (None, "pi", "opencode"):
            argv = ["timer", "--session", "pi/fake_receiver" if selected == "pi" else "ses_knownsession0001",
                    "--in", "1m", "--reason", "synthetic", "--json"]
            if selected:
                argv += ["--adapter", selected]
            with patch("docket.cli.request", return_value=(201, {"item": {"id": "fixture"}})) as request, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(argv), 0)
            body = request.call_args.args[2]
            if selected:
                self.assertEqual(body["adapter"], selected)
            else:
                self.assertNotIn("adapter", body)

    def test_mcp_all_create_tools_offer_and_forward_optional_selection(self):
        cases = {"docket_timer": {"in": "1m"}, "docket_alarm": {"at": "12:01"},
                 "docket_recurring": {"every": 60, "count": 2},
                 "docket_watch": {"check": "file_appears", "until": "1h", "path": "/fake"}}
        for name, fields in cases.items():
            schema = next(tool["inputSchema"] for tool in TOOLS if tool["name"] == name)
            self.assertEqual(schema["properties"]["adapter"]["enum"], ["opencode", "pi"])
            self.assertNotIn("adapter", schema["required"])
            for selected in (None, "pi"):
                args = {"session_id": "pi/fake_receiver", "reason": "synthetic", **fields}
                if selected:
                    args["adapter"] = selected
                with patch("docket.mcp_server.request", return_value=(201, {})) as request:
                    _call(name, args)
                if selected:
                    self.assertEqual(request.call_args.args[2]["adapter"], "pi")
                else:
                    self.assertNotIn("adapter", request.call_args.args[2])

    def test_urgent_maps_steer(self):
        self.item.urgent = True
        self.store.save(self.item)
        self.tick()
        self.assertEqual(self.sends()[0]["delivery"], "steer")

    def test_injected_observed_is_not_delivered(self):
        self.responses["send"] = receipt("injected", observed=True, historicalUncertainty=True)
        self.assertEqual(self.tick().status, "admitted")
        facts = self.store.get_request(self.item.id)["facts"]
        self.assertTrue(facts["injected"] and facts["observed"] and facts["historicalUncertainty"])
        self.assertFalse(facts.get("consumed", False))

    def test_only_saved_exact_consumed_is_delivered(self):
        self.responses["send"] = receipt("consumed", evidence="saved_exact", historicalUncertainty=True)
        self.assertEqual(self.tick().status, "delivered")
        self.assertTrue(self.store.get_request(self.item.id)["facts"]["historicalUncertainty"])

    def test_weak_consumed_receipt_is_uncertain(self):
        self.responses["send"] = receipt("consumed", evidence="unreadable")
        self.assertEqual(self.tick().status, "ambiguous")
        self.assertFalse(self.store.get_request(self.item.id)["facts"].get("consumed"))

    def test_claimed_uncertainty_keeps_positive_facts_but_not_delivery(self):
        self.responses["send"] = receipt("uncertain", admitted=True, claimed=True, historicalUncertainty=True)
        item = self.tick()
        self.assertEqual(item.status, "ambiguous")
        view = project(self.store, item)
        self.assertEqual(view["outcome"], "uncertain")
        self.assertTrue(view["facts"]["admitted"] and view["facts"]["claimed"])
        self.assertFalse(view["recovery"]["retry"])

    def test_status_can_advance_to_saved_intake_without_resend(self):
        self.responses["send"] = (6, b'{"v":1,"ok":false,"error":"STORAGE_UNCERTAIN","retry":"same_key_only"}\n')
        self.assertEqual(self.tick().status, "ambiguous")
        before = self.store.get_request(self.item.id)
        self.responses["status"] = receipt("consumed", evidence="saved_exact")
        self.clock.advance(31)
        self.assertEqual(self.tick().status, "delivered")
        self.assertEqual(len(self.sends()), 1)
        status = self.calls[-1][3]
        self.assertEqual(status, {"v": 1, "op": "status", "caller": before["caller"], "id": before["request_id"]})

    def test_unknown_after_send_never_replaces_identity(self):
        self.responses["send"] = TimeoutError()
        self.tick()
        before = self.store.get_request(self.item.id)
        self.responses["status"] = (3, b'{"v":1,"ok":false,"error":"UNKNOWN","retry":"same_key_only"}\n')
        self.clock.advance(31)
        self.tick()
        self.assertEqual(self.scheduler.recover(self.store.get(self.item.id), None, "retry"), (409, "acceptance_unknown"))
        after = self.store.get_request(self.item.id)
        self.assertEqual((before["caller"], before["request_id"], before["frozen"]),
                         (after["caller"], after["request_id"], after["frozen"]))
        self.assertEqual(len(self.sends()), 1)

    def test_restart_retains_caller_envelope_and_status_only(self):
        self.tick()
        before = self.store.get_request(self.item.id)
        self.store.close()
        self.store = Store(self.path)
        self.scheduler = Scheduler(self.store, self.default, self.clock,
                                   pi_adapter=PiAdapter(config=self.config, runner=self.run_boundary))
        self.clock.advance(31)
        self.tick()
        after = self.store.get_request(self.item.id)
        self.assertEqual((before["caller"], before["frozen"]), (after["caller"], after["frozen"]))
        self.assertFalse(after["binding_owned"])
        self.assertEqual(len(self.sends()), 1)

    def test_target_binding_change_before_first_fire_refuses_send(self):
        self.settings["state"] += "-changed"
        self.write_config()
        self.tick()
        self.assertEqual(self.sends(), [])

    def test_frozen_envelope_cannot_be_replaced(self):
        self.tick()
        frozen = self.store.get_request(self.item.id)["frozen"]
        changed = copy.deepcopy(frozen)
        changed["body"] += " "
        with self.assertRaises(ValueError):
            self.store.freeze_request(self.item.id, changed)
        self.assertEqual(len(self.sends()), 1)

    def test_wire_envelope_mismatch_is_refused_before_runner(self):
        self.tick()
        frozen = self.store.get_request(self.item.id)["frozen"]
        count = len(self.calls)
        for field, value in (("expectReply", True), ("to", "pi/other"), ("caller", "docket-other")):
            changed = copy.deepcopy(frozen)
            body = json.loads(changed["body"])
            body[field] = value
            changed["body"] = json.dumps(body)
            with self.assertRaises(DeliveryError):
                self.adapter.status_frozen(changed)
        self.assertEqual(len(self.calls), count)

    def test_changed_binding_blocks_status_not_reroutes(self):
        self.tick()
        count = len(self.calls)
        self.settings["cli"] = str(self.root / "other" / "cli.mjs")
        self.write_config()
        self.clock.advance(31)
        self.tick()
        self.assertEqual(len(self.calls), count)
        self.assertTrue(self.store.get_request(self.item.id)["facts"]["admitted"])

    def test_positive_claim_facts_survive_later_unavailable(self):
        self.responses["send"] = receipt("injected", historicalUncertainty=True)
        self.tick()
        self.responses["status"] = OSError()
        self.clock.advance(31)
        self.tick()
        facts = self.store.get_request(self.item.id)["facts"]
        self.assertTrue(facts["admitted"] and facts["claimed"] and facts["injected"])
        self.assertEqual(len(self.sends()), 1)

    def test_cancel_tombstone_is_not_admission_proof(self):
        self.responses["send"] = OSError()
        self.tick()
        self.responses["status"] = (3, b'{"v":1,"ok":false,"error":"UNKNOWN","retry":"same_key_only"}\n')
        self.responses["cancel"] = receipt("cancelled")
        self.assertEqual(self.scheduler.stop(self.store.get(self.item.id)), "retracted")
        facts = self.store.get_request(self.item.id)["facts"]
        self.assertTrue(facts["cancelled"])
        self.assertFalse(facts.get("admitted", False))
        self.assertEqual(set(self.calls[-1][3]), {"v", "op", "caller", "id"})

    def test_cancelled_status_cannot_erase_injected_history(self):
        self.responses["send"] = receipt("injected", historicalUncertainty=True)
        self.tick()
        before = self.store.get_request(self.item.id)
        self.responses["status"] = receipt("cancelled")
        self.assertEqual(self.scheduler.recover(self.store.get(self.item.id), None, "reconcile"), (200, None))
        req = self.store.get_request(self.item.id)
        self.assertFalse(req["facts"].get("cancelled", False))
        self.assertTrue(all(req["facts"][k] for k in ("admitted", "claimed", "injected", "historicalUncertainty")))
        self.assertEqual((req["issue"]["code"], req["issue"]["phase"]), ("acceptance_unknown", "status"))
        self.assertEqual(project(self.store, self.store.get(self.item.id))["outcome"], "uncertain")
        self.assertEqual(self.scheduler.recover(self.store.get(self.item.id), None, "retry"), (409, "already_admitted"))
        self.responses["cancel"] = receipt("cancelled")
        self.assertEqual(self.scheduler.stop(self.store.get(self.item.id)), "uncertain")
        self.assertEqual(self.store.get(self.item.id).status, "cancelled")
        self.assertFalse(self.store.get_request(self.item.id)["facts"].get("cancelled", False))
        # Contradiction must not become a stopping barrier to later saved intake.
        self.responses["status"] = receipt("consumed", evidence="saved_exact")
        self.scheduler.recover(self.store.get(self.item.id), None, "reconcile")
        self.assertEqual(project(self.store, self.store.get(self.item.id))["outcome"], "delivered")
        after = self.store.get_request(self.item.id)
        self.assertEqual((before["caller"], before["request_id"], before["frozen"]),
                         (after["caller"], after["request_id"], after["frozen"]))
        self.assertEqual(len(self.sends()), 1)

    def test_cancel_response_cannot_erase_claimed_history(self):
        self.responses["send"] = receipt("uncertain", admitted=True, claimed=True)
        self.tick()
        self.responses["status"] = receipt("admitted")
        self.responses["cancel"] = receipt("cancelled")
        self.assertEqual(self.scheduler.stop(self.store.get(self.item.id)), "uncertain")
        item = self.store.get(self.item.id)
        req = self.store.get_request(item.id)
        self.assertEqual(item.status, "cancelled")
        self.assertTrue(req["stop_requested"] and req["facts"]["claimed"])
        self.assertFalse(req["facts"].get("cancelled", False) or req["facts"].get("consumed", False))
        self.assertEqual((req["issue"]["code"], req["issue"]["phase"]), ("acceptance_unknown", "cancel"))
        self.assertEqual(project(self.store, item)["outcome"], "uncertain")
        self.store.close()
        self.store = Store(self.path)
        self.scheduler = Scheduler(self.store, self.default, self.clock, pi_adapter=self.adapter)
        self.responses["status"] = receipt("cancelled")
        self.scheduler.recover(self.store.get(item.id), None, "reconcile")
        self.assertFalse(self.store.get_request(item.id)["facts"].get("cancelled", False))
        self.assertEqual(self.scheduler.recover(self.store.get(item.id), None, "retry"), (409, "cancelled_identity"))
        self.assertEqual(len(self.sends()), 1)

    def test_cancelled_snapshot_preserves_saved_consumption(self):
        self.responses["send"] = receipt("consumed", evidence="saved_exact")
        self.tick()
        self.responses["status"] = receipt("cancelled")
        self.responses["cancel"] = receipt("cancelled")
        self.scheduler.recover(self.store.get(self.item.id), None, "reconcile")
        self.assertEqual(self.store.get(self.item.id).status, "delivered")
        self.assertEqual(self.scheduler.stop(self.store.get(self.item.id)), "too_late")
        req = self.store.get_request(self.item.id)
        self.assertTrue(req["facts"]["consumed"])
        self.assertFalse(req["facts"].get("cancelled", False))
        self.assertEqual(req["issue"]["code"], "acceptance_unknown")
        self.assertEqual(project(self.store, self.store.get(self.item.id))["outcome"], "delivered")
        self.assertEqual(len(self.sends()), 1)

    def test_too_late_is_not_consumed_or_removed(self):
        self.tick()
        self.responses["cancel"] = (4, b'{"v":1,"ok":false,"error":"TOO_LATE","retry":"same_key_only"}\n')
        self.assertEqual(self.scheduler.stop(self.store.get(self.item.id)), "uncertain")
        facts = self.store.get_request(self.item.id)["facts"]
        self.assertFalse(facts.get("consumed", False) or facts.get("cancelled", False))

    def test_unsupported_directory_and_idle_watch(self):
        self.assertEqual(self.create(directory=str(self.root))[0], 400)
        self.assertEqual(self.create("watch", check="session_idle")[0], 400)
        self.assertEqual(self.calls, [])

    def test_other_watches_use_ordinary_target_route(self):
        code, _ = self.create("watch", check="file_appears", path=str(self.root / "not-there"), until="1h")
        self.assertEqual(code, 201)
        self.assertEqual([c[3]["op"] for c in self.calls], ["capabilities", "lookup"])

    def test_file_process_url_watches_latch_into_same_no_reply_route(self):
        for fields in ({"check": "file_appears", "path": str(self.root / "fake-file")},
                       {"check": "process_ended", "pid": 12345},
                       {"check": "url_answers", "url": "https://example.invalid/fixture"}):
            with self.subTest(check=fields["check"]), patch.object(self.scheduler.checks, "capture_process", return_value="fake-birth"):
                code, body = self.create("watch", until="1h", **fields)
                self.assertEqual(code, 201)
                self.item = self.store.get(body["item"]["id"])
                self.scheduler._apply_watch_outcome(self.item, Outcome("met"), self.clock.now())
                frame = self.sends()[-1]
                self.assertEqual(frame["to"], "pi/fake_other")
                self.assertFalse(frame["expectReply"])

    def test_lookup_offline_missing_unsupported_fail_closed(self):
        for field, code in (("exists", "bad_target"), ("live", "unavailable"), ("capable", "unsupported_capability")):
            target = {"to": self.item.target["session_id"], "exists": True, "live": True,
                      "capable": True, "snapshot": True, "reservation": False, field: False}
            self.responses["lookup"] = {"v": 1, "ok": True, "target": target}
            self.assertEqual(self.adapter.validate(self.item.target).code, code)

    def test_old_or_malformed_capabilities_rejected(self):
        for change in ({"protocol": 0}, {"expectReply": True}, {"inspection": "mutating"}, {"reservation": True}):
            self.responses["capabilities"] = {"v": 1, "ok": True, "capabilities": {**CAP, **change}}
            self.assertFalse(self.adapter.validate(self.item.target).ok)

    def test_wire_limits_and_malformed_results_remain_uncertain(self):
        self.tick()
        frozen = self.store.get_request(self.item.id)["frozen"]
        cases = [b"", b"{}", b"{}\n{}\n", b"x" * 8192 + b"\n", b"\xff\n",
                 b'{"v":1,"v":1,"ok":true}\n', b'{"v":1,"ok":true}\n', b'\xef\xbb\xbf{}\n',
                 b'{"v":1,"ok":false,"error":"UNKNOWN"}\n']
        for raw in cases:
            with self.subTest(raw=raw[:40]):
                self.responses["status"] = (0, raw)
                with self.assertRaises(DeliveryError):
                    self.adapter.status_frozen(frozen)

    def test_timeout_partial_commit_capacity_and_conflict_do_not_retry(self):
        self.tick()
        frozen = self.store.get_request(self.item.id)["frozen"]
        for response in (subprocess.TimeoutExpired("fixture", 10), OSError(),
                         (6, b'{"v":1,"ok":false,"error":"STORAGE_UNCERTAIN"}\n'),
                         (3, b'{"v":1,"ok":false,"error":"CAPACITY"}\n'),
                         (4, b'{"v":1,"ok":false,"error":"CONFLICT"}\n')):
            self.responses["status"] = response
            with self.assertRaises(DeliveryError):
                self.adapter.status_frozen(frozen)
        self.assertEqual(len(self.sends()), 1)

    def test_text_limit_checked_before_dispatch(self):
        with self.assertRaises(DeliveryError):
            self.adapter.prepare_body("docket-" + "a" * 32, "msg_" + "b" * 32,
                                      "é" * 16385, False, self.item.target)
        self.assertEqual(self.calls, [])

    def test_missing_bad_config_only_affects_explicit_pi(self):
        self.config.unlink()
        with self.assertRaises(DeliveryError):
            self.adapter.binding_snapshot()
        with patch.dict(os.environ, {"DOCKET_DELIVERY_HOST": "opencode"}), patch("docket.host._configured", return_value=False):
            self.assertIsInstance(configured_adapter(), OpenCodeAdapter)
        self.assertIs(self.scheduler.adapter_for_target({"adapter": "pi"}), self.adapter)
        self.assertIs(self.scheduler.adapter_for_target({"adapter": "opencode"}), self.default)
        self.settings["node"] = "node"
        self.write_config()
        with self.assertRaises(DeliveryError):
            self.adapter.binding_snapshot()

    def test_config_private_file_guard(self):
        self.config.chmod(0o644)
        with self.assertRaises(DeliveryError):
            self.adapter.binding_snapshot()

    def test_recurring_occurrences_have_distinct_original_ids(self):
        self.store.cancel(self.item.id, self.clock.now())
        self.item = _recurring("dkt_pi_series", self.clock.now(), every=60, count=2)
        self.item.target = {"adapter": "pi", "session_id": "pi/fake_receiver"}
        self.store.insert(self.item)
        self.responses["send"] = receipt("consumed", evidence="saved_exact")
        self.tick()
        self.clock.advance(61)
        self.tick()
        self.assertEqual(len(self.sends()), 2)
        self.assertNotEqual(self.sends()[0]["id"], self.sends()[1]["id"])
        self.assertEqual(self.sends()[0]["caller"], self.sends()[1]["caller"])

    def test_runner_fixed_argv_eof_and_timeout_with_fake_process_only(self):
        child = Mock()
        child.stdout = io.BytesIO(b'{"v":1,"ok":true}\n')
        child.stdin = Mock()
        child.stdin.write.return_value = 3
        child.returncode = 0
        child.poll.return_value = 0
        runner = MachineRunner()
        with patch("docket.pi.subprocess.Popen", return_value=child) as spawn:
            rc, raw = runner(["/trusted/node", "/trusted/cli.mjs", "machine"], b"{}\n", {})
            self.assertEqual(rc, 0)
            self.assertLessEqual(len(raw), 8192)
            self.assertFalse(spawn.call_args.kwargs["shell"])
            self.assertEqual(spawn.call_args.kwargs["bufsize"], 0)
            child.stdin.write.assert_called_once_with(b"{}\n")
            child.stdin.close.assert_called_once()
            self.assertTrue(all(0 <= call.kwargs["timeout"] <= 30 for call in child.wait.call_args_list))
        child.wait.side_effect = [subprocess.TimeoutExpired("fixture", 10), None]
        child.poll.side_effect = [None, 0]
        child.stdout = io.BytesIO(b"")
        with patch("docket.pi.subprocess.Popen", return_value=child):
            with self.assertRaises(subprocess.TimeoutExpired):
                runner(["/trusted/node", "/trusted/cli.mjs", "machine"], b"{}\n", {})
            child.kill.assert_called_once()

    def test_runner_bounds_stdout_with_fake_process_only(self):
        child = Mock()
        child.stdout = io.BytesIO(b"x" * 9000)
        child.stdin = io.BytesIO()
        child.returncode = 0
        child.poll.return_value = 0
        with patch("docket.pi.subprocess.Popen", return_value=child):
            with self.assertRaises(OSError):
                MachineRunner()(["/trusted/node", "/trusted/cli.mjs", "machine"], b"{}\n", {})

    def test_inherited_handle_or_blocked_close_cannot_block_caller_or_grow_io(self):
        for blocked_close in (False, True):
            with self.subTest(blocked_close=blocked_close):
                release = threading.Event()
                entered = threading.Event()
                class Pipe:
                    def read(self, limit):
                        if not blocked_close:
                            entered.set()
                            release.wait()
                        return b""
                    def close(self):
                        if blocked_close:
                            entered.set()
                            release.wait()
                child = Mock()
                child.stdout = Pipe()
                child.stdin = io.BytesIO()
                child.returncode = 0
                child.poll.return_value = 0  # Direct child exited; inherited pipe is still open.
                runner = MachineRunner(timeout=0.03, cleanup=0.01)
                threads = []
                try:
                    with patch("docket.pi.subprocess.Popen", return_value=child) as spawn:
                        start = time.monotonic()
                        with self.assertRaises(subprocess.TimeoutExpired):
                            runner(["/trusted/node", "/trusted/cli.mjs", "machine"], b"{}\n", {})
                        self.assertLess(time.monotonic() - start, 0.5)
                        self.assertTrue(entered.is_set())
                        threads = runner._active[1]
                        with self.assertRaises(OSError):
                            runner(["/trusted/node", "/trusted/cli.mjs", "machine"], b"{}\n", {})
                        self.assertEqual(spawn.call_count, 1)
                        self.assertEqual(len(threads), 2)
                        child.kill.assert_not_called()
                finally:
                    release.set()
                    for thread in threads:
                        thread.join(timeout=0.5)
                        self.assertFalse(thread.is_alive())
