"""Source-only fixed clock/projection fixtures: no real host, bus, or roster."""
import hashlib
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from docket.api import DocketServer
from docket.cli import main
from docket.client import request, ClientError
from docket.clock import FakeClock
from docket.scheduler import Scheduler
from docket.store import Store
from docket.tower_clock import (SERIES, ACTOR, MARKER_TYPE, REFRESH_TYPE, ClockError,
                                markers, mutate, observe, projection, utc, view)
from docket.tower_publish import TowerPublisher, encode_event, QUEUE_LIMIT
from tests.support import utc as date


class FixedClockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.env = patch.dict(os.environ, {"DOCKET_HOME": str(self.root), "DOCKET_TOWER_NATS_URL": "nats://127.0.0.1:4222"})
        self.env.start()
        self.store = Store(self.root / "fixture.db")
        self.clock = FakeClock(date(2026, 10, 24, 4))
        self.adapter = Mock()
        self.scheduler = Scheduler(self.store, self.adapter, self.clock)
        self.publisher = TowerPublisher(transport=Mock())
        self.publisher._ensure = Mock()  # no worker and no network even with valid opt-in
        self.scheduler.tower = self.publisher
        self.now = self.clock.now()

    def tearDown(self):
        self.assertEqual(self.adapter.mock_calls, [])
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def ensure(self, days=90):
        return mutate(self.scheduler, {"until": utc(self.clock.now()+timedelta(days=days))})

    def fire(self, seconds=900):
        self.clock.advance(seconds)
        self.scheduler._fire_recurring(self.store.get(SERIES))

    def row(self, state="hold", **extra):
        return dict(session="ses_fixture", directory="/fixture/é", state=state,
                    milestone_at="2026-10-20T05:00:00+01:00", milestone_ref="untrusted <command> é", **extra)

    def write_roster(self, rows):
        path = self.scheduler.checkins.roster_path
        path.write_text("".join(json.dumps(r, ensure_ascii=False)+"\n" for r in rows))
        path.chmod(0o600)
        return path

    def test_absent_ensure_idempotent_anchor_and_show(self):
        self.assertIsNone(view(self.store, self.now)["schedule"])
        result = self.ensure()
        self.assertTrue(result["changed"])
        s = result["schedule"]
        self.assertEqual(s["item_id"], SERIES)
        self.assertEqual(s["anchor"], utc(self.now))
        self.assertEqual(s["next_fire_at"], utc(self.now+timedelta(seconds=900)))
        self.assertIsNone(s["last_occurrence"])
        self.assertFalse(self.ensure()["changed"])
        self.assertFalse(view(self.store, self.now)["changed"])
        self.assertEqual(self.store.get(SERIES).target, {"adapter": "tower"})

    def test_strict_ensure_policy(self):
        invalid = ({}, {"until": None}, {"until": "2026-10-25"}, {"until": utc(self.now)},
                   {"until": utc(self.now+timedelta(days=91))})
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(ClockError):
                mutate(self.scheduler, body)
        for field in ("command", "topic", "recipient", "session", "urgent", "context", "purpose", "every", "resume"):
            with self.subTest(field=field), self.assertRaises(ClockError):
                mutate(self.scheduler, {"until": utc(self.now+timedelta(days=1)), field: "no"})

    def test_unconfigured_creation_refuses(self):
        with patch.dict(os.environ, {"DOCKET_TOWER_NATS_URL": ""}):
            with self.assertRaises(ClockError) as caught:
                self.ensure()
        self.assertEqual(caught.exception.code, "publisher_disabled")
        self.assertIsNone(self.store.get(SERIES))

    def test_capacity_global_and_actor_not_bypassed(self):
        from docket.bounds import LimitError
        for field in ("MAX_PENDING_GLOBAL", "MAX_RECURRING_PER_ACTOR", "MAX_RECURRING_PER_TARGET"):
            with self.subTest(field=field), patch("docket.bounds."+field, 0), self.assertRaises(LimitError):
                self.ensure()

    def test_event_receipt_exact_schema_no_generic_fire(self):
        self.ensure()
        self.fire()
        s = view(self.store, self.clock.now())["schedule"]
        occ = s["last_occurrence"]
        self.assertEqual(occ["status"], "enqueued")
        self.assertEqual(occ["queue_result"], "accepted")
        subject, key, body = self.publisher._queue.get_nowait()
        event = json.loads(body)
        self.assertEqual(subject, "ev.docket.snapshot.refresh_due")
        self.assertEqual(event["id"], occ["id"])
        self.assertEqual(key, occ["id"])
        self.assertEqual(event["source"], "docket")
        self.assertEqual(event["type"], REFRESH_TYPE)
        self.assertEqual(event["data"], dict(version=1, purpose="project_pages", item_id=SERIES,
                         occurrence_id=key, scheduled_at=utc(self.clock.now()), fired_at=utc(self.clock.now()),
                         coalesced=0, schedule_until=utc(self.now+timedelta(days=90)), provenance="docket_recurring_clock"))
        stored = self.store.get_occurrence(key)
        self.assertNotIn("admission_id", stored.receipt)
        self.assertIsNone(stored.delivered_at)
        self.assertTrue(self.publisher._queue.empty())

    def test_sleep_coalesces_and_advances(self):
        self.ensure()
        self.fire(3600)
        occ = self.store.list_occurrences(SERIES)[0]
        self.assertEqual(occ.coalesced, 3)
        self.assertEqual(self.store.get(SERIES).fire_at, self.now+timedelta(seconds=4500))

    def test_disabled_and_full_are_terminal_not_retried(self):
        self.ensure()
        with patch.dict(os.environ, {"DOCKET_TOWER_NATS_URL": ""}):
            self.fire()
        self.assertEqual(view(self.store, self.clock.now())["schedule"]["last_occurrence"]["queue_result"], "disabled")
        for _ in range(QUEUE_LIMIT):
            self.publisher._queue.put_nowait(("fixture", "fixture", b"fixture"))
        self.fire()
        s = view(self.store, self.clock.now())["schedule"]
        self.assertEqual(s["last_occurrence"]["queue_result"], "full")
        self.assertEqual(s["last_occurrence"]["status"], "undeliverable")
        self.assertEqual(len(self.store.list_occurrences(SERIES)), 2)

    def test_reservation_persisted_before_enqueue_and_crash_not_resent(self):
        self.ensure()
        def crash(*args):
            occ = self.store.open_occurrence(self.store.get(SERIES))
            self.assertEqual(occ.receipt, {"state": "reserved", "queue_result": "uncertain"})
            raise RuntimeError("fixture crash")
        self.publisher.note_fixed = crash
        with self.assertRaises(RuntimeError):
            self.fire()
        occ = self.store.open_occurrence(self.store.get(SERIES))
        self.clock.advance(61)
        restarted = Scheduler(self.store, self.adapter, self.clock)
        restarted.tower = self.publisher
        with patch.object(restarted.checkins, "tick"):
            restarted.tick()
        self.assertEqual(view(self.store, self.clock.now())["schedule"]["last_occurrence"]["queue_result"], "uncertain")
        self.assertTrue(self.publisher._queue.empty())
        self.assertIsNone(self.store.open_occurrence(self.store.get(SERIES)))

    def test_crash_claim_without_reservation_is_unknown(self):
        self.ensure()
        with patch.object(self.scheduler, "_deliver_occurrence", return_value=False):
            self.fire()
        occ = self.store.open_occurrence(self.store.get(SERIES))
        self.store.claim_occurrence(occ.id, self.clock.now())
        self.clock.advance(61)
        self.scheduler._reclaim_occurrence(occ.id, self.clock.now())
        self.assertEqual(self.store.get_occurrence(occ.id).receipt["queue_result"], "uncertain")

    def test_renew_expired_preserves_identity_anchor_history_sequence(self):
        self.ensure(days=1)
        self.fire(86400)
        original = self.store.get(SERIES)
        self.assertEqual(original.spec["finished"], "until")
        self.clock.advance(1)
        self.assertTrue(view(self.store, self.clock.now())["schedule"]["lease_expired"])
        self.assertIsNone(view(self.store, self.clock.now())["schedule"]["next_fire_at"])
        self.ensure()
        renewed = self.store.get(SERIES)
        self.assertEqual(renewed.spec["anchor"], original.spec["anchor"])
        self.assertEqual(renewed.spec["next_seq"], original.spec["next_seq"])
        self.fire(900)
        self.assertEqual(len(self.store.list_occurrences(SERIES)), 2)

    def test_cancel_absent_idempotent_permanent_across_restart(self):
        self.assertFalse(mutate(self.scheduler, {}, cancel=True)["changed"])
        self.ensure()
        self.fire()
        self.assertTrue(mutate(self.scheduler, {}, cancel=True)["changed"])
        self.assertFalse(mutate(self.scheduler, {}, cancel=True)["changed"])
        restarted = Scheduler(self.store, self.adapter, self.clock)
        with self.assertRaises(ClockError) as caught:
            mutate(restarted, {"until": utc(self.clock.now()+timedelta(days=90))})
        self.assertEqual(caught.exception.code, "schedule_cancelled")
        self.assertEqual(self.publisher._queue.qsize(), 1)  # queued notice not retracted

    def test_cancel_open_and_invalid_cancel(self):
        self.ensure()
        with self.assertRaises(ClockError):
            mutate(self.scheduler, {"resume": True}, cancel=True)
        with patch.object(self.scheduler, "_deliver_occurrence", return_value=False):
            self.fire()
        mutate(self.scheduler, {}, cancel=True)
        self.assertEqual(self.store.list_occurrences(SERIES)[0].status, "cancelled")

    def test_unrelated_failure_not_silently_revived(self):
        self.ensure(days=1)
        item = self.store.get(SERIES)
        item.status = "undeliverable"
        self.store.cas_item(item, ("pending",))
        with self.assertRaises(ClockError) as caught:
            self.ensure()
        self.assertEqual(caught.exception.code, "schedule_failed")

    def test_marker_id_exact_unicode_and_normalized_time(self):
        row = self.row()
        marker = markers({row["session"]: row})[0]
        canonical = dict(session=row["session"], directory=row["directory"], milestone_at="2026-10-20T04:00:00.000000Z",
                         milestone_ref_sha256=hashlib.sha256(row["milestone_ref"].encode()).hexdigest())
        expected = "mil_" + hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(marker["id"], expected)
        self.assertEqual(marker["milestone_at"], canonical["milestone_at"])

    def test_projection_historical_hold_done_and_empty(self):
        for state in ("active", "hold", "done"):
            result = projection(self.write_roster([self.row(state)]), self.now)
            self.assertTrue(result["complete"])
            self.assertEqual(result["markers"][0]["roster_state"], state)
            self.assertEqual(set(result["markers"][0]), {"id", "session", "directory", "milestone_at", "milestone_ref", "roster_state"})
        self.assertEqual(projection(self.write_roster([]), self.now)["markers"], [])
        row = self.row()
        del row["milestone_at"], row["milestone_ref"]
        self.assertEqual(projection(self.write_roster([row]), self.now)["markers"], [])

    def test_projection_refuses_missing_invalid_private_oversized(self):
        path = self.scheduler.checkins.roster_path
        with self.assertRaises(FileNotFoundError):
            projection(path, self.now)
        row = self.row()
        for field, value in (("milestone_at", "bad"), ("milestone_ref", "x"*1001), ("directory", "/"+"x"*4096)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                projection(self.write_roster([dict(row, **{field: value})]), self.now)
        self.write_roster([row]).chmod(0o644)
        with self.assertRaises(ValueError):
            projection(path, self.now)
        path.chmod(0o600)
        path.write_text("x"*256001)
        with self.assertRaises(ValueError):
            projection(path, self.now)
        rows = [dict(row, session=f"ses_fixture{i}", directory="/"+"é"*4000, milestone_ref="é"*1000) for i in range(20)]
        with self.assertRaises(ValueError):
            projection(self.write_roster(rows), self.now)
        with self.assertRaises(ValueError):
            projection(self.write_roster([dict(row, session=f"ses_fixture{i}") for i in range(21)]), self.now)

    def test_observation_seed_hold_dedupe_restart_and_event(self):
        row = self.row()
        rows = {row["session"]: row}
        observe(self.scheduler.checkins, rows, self.now)
        subject, key, body = self.publisher._queue.get_nowait()
        event = json.loads(body)
        self.assertEqual(subject, "ev.lead.milestone.recorded")
        self.assertEqual(key, markers(rows)[0]["id"])
        self.assertEqual(event["time"], utc(self.now))
        self.assertEqual(event["data"]["milestone_at"], "2026-10-20T04:00:00.000000Z")
        self.assertEqual(event["data"]["provenance"], "recorded_marker_observation")
        self.assertNotIn("project", event["data"])
        observe(self.scheduler.checkins, rows, self.now)
        restarted = Scheduler(self.store, self.adapter, self.clock)
        restarted.tower = self.publisher
        observe(restarted.checkins, rows, self.now)
        self.assertTrue(self.publisher._queue.empty())

    def test_observation_reserves_before_crash_no_resend(self):
        row = self.row("done")
        rows = {row["session"]: row}
        key = markers(rows)[0]["id"]
        def crash(*args):
            disk = json.loads(self.scheduler.checkins.ledger_path.read_text())
            self.assertEqual(disk["marker_observations"][key]["queue_result"], "uncertain")
            raise RuntimeError("fixture")
        with patch.object(self.publisher, "note_fixed", side_effect=crash), self.assertRaises(RuntimeError):
            observe(self.scheduler.checkins, rows, self.now)
        observe(self.scheduler.checkins, rows, self.now)
        self.assertTrue(self.publisher._queue.empty())

    def test_observation_off_no_reservation_or_effect(self):
        with patch.dict(os.environ, {"DOCKET_TOWER_NATS_URL": ""}):
            observe(self.scheduler.checkins, {"ses_fixture": self.row()}, self.now)
        self.assertNotIn("marker_observations", self.scheduler.checkins.state)

    def test_stall_hook_reuses_single_roster_read_without_native_for_hold(self):
        from docket.checkins import roster
        self.write_roster([self.row()])
        with patch("docket.checkins.roster", wraps=roster) as reader:
            self.scheduler.checkins.delivery_stall.tick(self.now)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(self.publisher._queue.qsize(), 1)
        self.assertEqual(self.scheduler.checkins.state["leads"], {})

    def test_broken_marker_ledger_suppresses_observation(self):
        self.scheduler.checkins.broken = True
        observe(self.scheduler.checkins, {"ses_fixture": self.row()}, self.now)
        self.assertTrue(self.publisher._queue.empty())

    def test_new_fixed_encodings_reject_generic_fields(self):
        self.ensure()
        self.fire()
        _, key, body = self.publisher._queue.get_nowait()
        event = json.loads(body)
        self.assertIsNone(encode_event(key, REFRESH_TYPE, dict(event["data"], topic="x"), event["time"]))
        self.assertIsNone(encode_event(key, REFRESH_TYPE, dict(event["data"], purpose="other"), event["time"]))
        self.assertIsNone(encode_event(key, "other", event["data"], event["time"]))

    def test_marker_event_accepts_same_session_validation_as_roster(self):
        row = dict(self.row(), session="ses_"+"a"*121)
        observe(self.scheduler.checkins, {row["session"]: row}, self.now)
        _, key, body = self.publisher._queue.get_nowait()
        self.assertEqual(json.loads(body)["data"]["lead_session"], row["session"])


class ApiTests(unittest.TestCase):
    row = FixedClockTests.row
    write_roster = FixedClockTests.write_roster

    def setUp(self):
        FixedClockTests.setUp(self)
        self.server = DocketServer(self.store, self.scheduler, "fixture-admin", port=0)
        self.server.serve_in_thread()
        self.httpenv = patch.dict(os.environ, {"DOCKET_URL": self.server.url, "DOCKET_TOKEN": "fixture-admin"})
        self.httpenv.start()

    def tearDown(self):
        self.server.stop()
        self.httpenv.stop()
        FixedClockTests.tearDown(self)

    def test_privileged_get_auth_and_missing_source_unknown(self):
        for route in ("project-markers", "snapshot-refresh-schedule"):
            with self.assertRaises(ClientError) as caught:
                request("GET", "/api/v1/tower/"+route)
            self.assertEqual(caught.exception.status, 401)
        with self.assertRaises(ClientError) as caught:
            request("GET", "/api/v1/tower/project-markers", authenticated=True)
        self.assertEqual(caught.exception.status, 503)
        self.write_roster([])
        _, result = request("GET", "/api/v1/tower/project-markers", authenticated=True)
        self.assertEqual(result["markers"], [])
        with self.assertRaises(ClientError):
            request("GET", "/api/v1/tower/project-markers?path=/foreign", authenticated=True)

    def test_api_ensure_show_cancel_and_cli_normal_argv(self):
        deadline = utc(self.now+timedelta(days=90))
        commands = (["project-markers", "--json"], ["tower-refresh", "show", "--json"],
                    ["tower-refresh", "ensure", "--until", deadline, "--json"],
                    ["tower-refresh", "cancel", "--json"])
        self.write_roster([self.row()])
        for argv in commands:
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(argv)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())["version"], 1)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["tower-refresh", "ensure", "--until", deadline, "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], "schedule_cancelled")

    def test_cli_json_parse_and_client_errors_not_false_empty(self):
        for argv in (["tower-refresh", "ensure", "--json"], ["project-markers", "--unknown", "--json"]):
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = main(argv)
            self.assertEqual(code, 1)
            self.assertIn("error", json.loads(out.getvalue()))
        for payload in (None, {}, {"version": 1}, {"version": 1, "source": "docket.project-markers", "complete": False, "markers": []}):
            with patch("docket.cli.request", return_value=(200, payload)), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["project-markers", "--json"]), 1)
                self.assertIn("error", json.loads(out.getvalue()))


class ResponseValidationTests(unittest.TestCase):
    def setUp(self):
        self.when = "2026-10-24T04:00:00.000000Z"
        row = dict(session="ses_fixture", directory="/fixture", state="hold",
                   milestone_at="2026-10-20T04:00:00Z", milestone_ref="technical reference")
        self.marker = markers({row["session"]: row})[0]
        self.projection = dict(version=1, source="docket.project-markers", observed_at=self.when,
                               complete=True, markers=[self.marker])
        self.schedule = dict(version=1, source="docket.tower-refresh-schedule", observed_at=self.when,
                             changed=False, schedule=dict(item_id=SERIES, status="pending", purpose="project_pages",
                             every_seconds=900, anchor=self.when, next_fire_at="2026-10-24T04:15:00.000000Z",
                             until="2027-01-22T04:00:00.000000Z", lease_expired=False, cancelled_at=None,
                             last_occurrence=dict(id="occ_0123456789abcdef", status="enqueued",
                             scheduled_at=self.when, queue_result="accepted")))

    def cli_result(self, payload, marker=True):
        argv = ["project-markers", "--json"] if marker else ["tower-refresh", "show", "--json"]
        with patch("docket.cli.request", return_value=(200, payload)), redirect_stdout(io.StringIO()) as out:
            code = main(argv)
        return code, json.loads(out.getvalue())

    def unknown(self, payload, marker=True):
        code, result = self.cli_result(payload, marker)
        self.assertEqual(code, 1)
        self.assertEqual(set(result), {"version", "error"})
        self.assertEqual(result["error"]["code"], "unavailable")

    def test_valid_snapshots_and_old_observation_have_no_fresh_age_gate(self):
        for payload, marker in ((self.projection, True), (self.schedule, False),
                (dict(self.projection, observed_at="2000-01-01T00:00:00.000000Z"), True),
                (dict(self.projection, markers=[]), True), (dict(self.schedule, schedule=None), False)):
            with self.subTest(payload=payload):
                self.assertEqual(self.cli_result(payload, marker), (0, payload))

    def test_envelope_mutations_and_false_empty_bad_time_fail_unknown(self):
        bad_times = (None, True, "garbage", "2026-10-24T04:00:00Z", "2026-10-24T04:00:00.000000+00:00",
                     "2026-02-30T04:00:00.000000Z", "2026-10-24T04:00:00.000Z")
        for payload, marker in ((self.projection, True), (self.schedule, False)):
            for field, value in [("version", True), ("version", 1.0), ("source", "other"), ("extra", None), *[("observed_at", t) for t in bad_times]]:
                with self.subTest(marker=marker, field=field, value=value):
                    self.unknown(dict(payload, **{field: value}), marker)
            for field in payload:
                bad = copy.deepcopy(payload)
                del bad[field]
                self.unknown(bad, marker)
        self.unknown(dict(self.projection, markers=[], observed_at="garbage"))
        for value in (False, 1, "true", None):
            self.unknown(dict(self.projection, complete=value))

    def test_markers_validate_all_types_hash_binding_and_uniqueness(self):
        variants = (("id", "mil_"+"0"*64), ("id", []), ("session", 1), ("session", "foreign"),
                    ("directory", None), ("directory", "relative"), ("directory", "/"+"a"*4096),
                    ("milestone_ref", 1), ("milestone_ref", " "), ("milestone_ref", "x"*1001),
                    ("milestone_ref", "other valid ref but wrong hash"), ("directory", "/other"),
                    ("milestone_at", "2026-10-20T04:00:00Z"), ("milestone_at", self.when),
                    ("roster_state", "unknown"), ("roster_state", []), ("extra", True))
        for field, value in variants:
            with self.subTest(field=field, value=value):
                self.unknown(dict(self.projection, markers=[dict(self.marker, **{field: value})]))
        for field in self.marker:
            bad = dict(self.marker)
            del bad[field]
            self.unknown(dict(self.projection, markers=[bad]))
        self.unknown(dict(self.projection, markers=[self.marker]*2))
        self.unknown(dict(self.projection, markers=[self.marker]*21))
        self.unknown(dict(self.projection, markers=[None]))
        self.unknown(dict(self.projection, markers={}))
        rows = {f"ses_fixture{i}": dict(session=f"ses_fixture{i}", directory="/"+"é"*4000, state="done",
                 milestone_at=self.when, milestone_ref="é"*1000) for i in range(20)}
        self.unknown(dict(self.projection, markers=markers(rows)))

    def test_schedule_types_nullability_enums_and_expiry_consistency(self):
        variants = (("status", "admitted"), ("status", None), ("status", []), ("lease_expired", 0),
                    ("lease_expired", True), ("every_seconds", True), ("every_seconds", 900.0),
                    ("item_id", "dkt_other"), ("purpose", "other"), ("anchor", None),
                    ("until", "garbage"), ("next_fire_at", None), ("next_fire_at", "garbage"),
                    ("next_fire_at", self.when), ("cancelled_at", self.when), ("extra", None))
        for field, value in variants:
            with self.subTest(field=field, value=value):
                self.unknown(dict(self.schedule, schedule=dict(self.schedule["schedule"], **{field: value})), False)
        for field in self.schedule["schedule"]:
            bad = copy.deepcopy(self.schedule)
            del bad["schedule"][field]
            self.unknown(bad, False)
        self.unknown(dict(self.schedule, changed=1), False)
        self.unknown(dict(self.schedule, schedule=None, changed=True), False)
        for status in ("cancelled", "completed", "expired", "undeliverable"):
            self.unknown(dict(self.schedule, schedule=dict(self.schedule["schedule"], status=status)), False)
        cancelled = dict(self.schedule["schedule"], status="cancelled", next_fire_at=None, cancelled_at=self.when)
        self.assertEqual(self.cli_result(dict(self.schedule, schedule=cancelled), False)[0], 0)

    def test_occurrence_schema_types_and_queue_outcome_binding(self):
        for field, value in (("id", "occ_bad"), ("id", 1), ("status", "delivered"), ("status", []),
                             ("scheduled_at", "garbage"), ("queue_result", None), ("queue_result", "published"), ("extra", None)):
            bad = copy.deepcopy(self.schedule)
            bad["schedule"]["last_occurrence"][field] = value
            self.unknown(bad, False)
        for field in self.schedule["schedule"]["last_occurrence"]:
            bad = copy.deepcopy(self.schedule)
            del bad["schedule"]["last_occurrence"][field]
            self.unknown(bad, False)

    def test_new_groups_reject_abbreviations_without_legacy_change(self):
        for argv in (["project-markers", "--j"], ["tower-refresh", "show", "--j"],
                     ["tower-refresh", "ensure", "--unt", self.when]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(argv)
            self.assertEqual(caught.exception.code, 2)

    def test_fixed_encoders_fail_closed_on_invalid_times_and_types(self):
        data = dict(version=1, lead_session=self.marker["session"], directory=self.marker["directory"],
                    milestone_at=self.marker["milestone_at"], milestone_ref=self.marker["milestone_ref"],
                    observed_at=self.when, roster_state="hold", provenance="recorded_marker_observation")
        key = self.marker["id"]
        self.assertIsNotNone(encode_event(key, MARKER_TYPE, data, self.when))
        refresh = dict(version=1, purpose="project_pages", item_id=SERIES, occurrence_id="occ_0123456789abcdef",
                       scheduled_at=self.when, fired_at=self.when, coalesced=0,
                       schedule_until="2027-01-22T04:00:00.000000Z", provenance="docket_recurring_clock")
        self.assertIsNotNone(encode_event(refresh["occurrence_id"], REFRESH_TYPE, refresh, self.when))
        for body, event_type, event_id, fields in ((data, MARKER_TYPE, key, ("observed_at", "milestone_at")),
                    (refresh, REFRESH_TYPE, refresh["occurrence_id"], ("fired_at", "scheduled_at", "schedule_until"))):
            for field in fields:
                for value in (None, True, [], "garbage", "2026-10-24T04:00:00Z"):
                    self.assertIsNone(encode_event(event_id, event_type, dict(body, **{field: value}), self.when))
            self.assertIsNone(encode_event(event_id, event_type, body, "garbage"))
            self.assertIsNone(encode_event(event_id, event_type, body, "2026-10-24T04:01:00.000000Z"))
            self.assertIsNone(encode_event(event_id, event_type, dict(body, version=True), self.when))
        self.assertIsNone(encode_event(key, MARKER_TYPE, dict(data, observed_at="garbage"), "garbage"))
        publisher = TowerPublisher(transport=Mock())
        with patch("docket.tower_publish.enabled", return_value=True), patch.object(publisher, "_enqueue") as enqueue:
            self.assertEqual(publisher.note_fixed(MARKER_TYPE, key, dict(data, observed_at="garbage"), "garbage"), "uncertain")
            enqueue.assert_not_called()
