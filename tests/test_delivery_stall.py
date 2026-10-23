"""Isolated policy and filesystem fixtures; never write the real chief inbox."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from docket.checkins import Checkins
from docket.delivery_stall import DeliveryStall, artifacts, publish, resolve
from tests.test_checkins import Inline

SID = "ses_knownsession0001"


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.row = dict(session=SID, directory="/fixture", state="active")
        self.goal = dict(id="g", createdAt=1234, status="active")
        self.activity, self.valid, self.fp = "active", True, "a"
        adapter = SimpleNamespace(session_goal=lambda _: self.goal,
                                  session_state=lambda _: self.activity,
                                  validate=lambda _: SimpleNamespace(ok=self.valid))
        self.m = SimpleNamespace(roster_path=self.root / "roster", state={},
                                 scheduler=SimpleNamespace(adapter=adapter, check_executor=Inline()),
                                 goal_identity=Checkins.goal_identity, save=lambda: None)
        self.sent = []
        self.lane = self.new_lane()
        self.now = datetime(2026, 10, 23, tzinfo=timezone.utc)

    def new_lane(self):
        return DeliveryStall(self.m, lambda *a, **kw: dict(fingerprint=self.fp, reference="source", limitations="bounded"),
                             lambda key, text, **kw: self.sent.append((key, text)),
                             resolver=lambda: {"directory": "/fake-chief"})

    def tick(self, count=1):
        for _ in range(count):
            self.m.roster_path.write_text(json.dumps(self.row))
            self.m.roster_path.chmod(0o600)
            self.lane.tick(self.now)
            self.now += timedelta(minutes=1)

    def test_threshold_dedupe_and_progress(self):
        self.tick(30)
        self.assertEqual(self.sent, [])
        self.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("actual project scope/completion not inferred", self.sent[0][1])
        self.lane = self.new_lane()
        self.tick(32)
        self.activity = "idle"
        self.tick()
        self.activity = "active"
        self.tick(32)
        self.assertEqual(len(self.sent), 1)
        self.fp = "b"
        self.tick(32)  # consume prior frozen sample, then observe fresh progress
        self.assertEqual(len(self.sent), 2)
        self.assertNotEqual(self.sent[0][0], self.sent[1][0])

    def test_restart_gap_and_milestone(self):
        self.tick(29)
        self.lane = self.new_lane()
        self.tick(29)
        self.now += timedelta(minutes=3)
        self.tick(31)  # discard stale pipelined result before a new baseline
        self.assertEqual(self.sent, [])
        self.row.update(milestone_at=self.now.isoformat(), milestone_ref="activity")
        self.tick()
        self.assertEqual(len(self.sent), 1)

    def test_suppression(self):
        for status in ("blocked", "done", "unknown"):
            self.goal["status"] = status
            self.tick(31)
        self.goal["status"] = "active"
        for activity in ("idle", "archived", "unknown"):
            self.activity = activity
            self.tick(31)
        self.activity = "active"
        self.valid = False
        self.tick(31)
        self.valid = True
        for state in ("hold", "done"):
            self.row["state"] = state
            self.tick(31)
        self.assertEqual(self.sent, [])

    def test_invalid_evidence_and_config(self):
        self.tick(29)
        self.fp = ""
        self.tick(31)
        self.fp = "a"
        self.tick(30)
        self.assertEqual(self.sent, [])
        for value in (["relative"], ["/x"] * 17, "abc", ["/a\x00"]):
            with self.assertRaises(ValueError):
                artifacts(dict(delivery_artifacts=value))

    def test_atomic_sink_replacement_and_links(self):
        pointer = self.root / "chief.json"
        first, second = self.root / "first", self.root / "second"
        for directory in (first, second):
            directory.mkdir(mode=0o755)
            pointer.write_text(json.dumps(dict(session=SID, directory=str(directory))))
            publish("abc", "first", pointer)
            publish("abc", "first", pointer)
            with self.assertRaises(ValueError):
                publish("abc", "second", pointer)
            alert = directory / "inbox/delivery-stall-abc.md"
            self.assertEqual(alert.read_text(), "first")
            self.assertEqual(alert.stat().st_mode & 0o777, 0o600)
        pointer.unlink()
        pointer.symlink_to(first / "inbox/delivery-stall-abc.md")
        with self.assertRaises(OSError):
            publish("def", "no", pointer)

    def test_failed_publication_retries_frozen_destination(self):
        destinations = []
        def fail(*args, **kwargs):
            destinations.append(kwargs["destination"])
            raise OSError("uncertain")
        self.lane.writer = fail
        self.tick(31)
        self.assertIsNotNone(self.lane.error)
        self.lane = self.new_lane()
        self.lane.resolver = lambda: self.fail("must not resolve replacement")
        self.lane.writer = lambda *a, **kw: destinations.append(kw["destination"])
        self.tick(31)
        self.assertEqual(self.sent, [])
        self.assertEqual(destinations, [{"directory": "/fake-chief"}] * 2)

    def test_prewrite_refusal_does_not_reserve(self):
        def refuse():
            raise ValueError("bad pointer")
        self.lane.resolver = refuse
        self.tick(31)
        self.assertIsNotNone(self.lane.error)
        state = next(iter(self.m.state["delivery_stalls"].values()))
        self.assertNotIn("publication", state)
        self.assertFalse(state["alerted"])
        self.lane = self.new_lane()
        self.tick(31)
        self.assertEqual(len(self.sent), 1)

    def test_hold_never_probes_native(self):
        self.row["state"] = "hold"
        self.m.scheduler.adapter.session_goal = lambda _: self.fail("native probe")
        self.tick()
        self.assertIsNone(self.lane.error)

    def test_frozen_sink_after_pointer_replacement(self):
        pointer = self.root / "chief.json"
        first, second = self.root / "first", self.root / "second"
        first.mkdir()
        second.mkdir()
        pointer.write_text(json.dumps(dict(session=SID, directory=str(first))))
        destination = resolve(pointer)
        publish("abc", "frozen", destination=destination)
        pointer.write_text(json.dumps(dict(session=SID, directory=str(second))))
        publish("abc", "frozen", destination=destination)
        self.assertFalse((second / "inbox").exists())
        (first / "inbox").rename(first / "old-inbox")
        (first / "inbox").mkdir()
        with self.assertRaises(ValueError):
            publish("abc", "frozen", destination=destination)

    def test_artifact_config_does_not_rearm(self):
        self.tick(31)
        self.row["delivery_artifacts"] = ["/fixture/output"]
        self.fp = "config-change"
        self.tick(31)
        self.assertEqual(len(self.sent), 1)

    def test_evidence_topology_does_not_rearm(self):
        self.tick(31)
        self.lane.reader = lambda *a, **kw: dict(fingerprint="new-root", reference="root", limitations="bounded", scope="a" * 64)
        self.tick(31)
        self.assertEqual(len(self.sent), 1)
        self.lane.reader = lambda *a, **kw: dict(fingerprint="new-content", reference="root", limitations="bounded", scope="a" * 64)
        self.tick(32)  # one frozen result still belongs to the preceding reader
        self.assertEqual(len(self.sent), 2)

    def test_unsafe_pointer_permissions(self):
        pointer = self.root / "chief.json"
        pointer.write_text(json.dumps(dict(session=SID, directory=str(self.root))))
        pointer.chmod(0o666)
        with self.assertRaises(ValueError):
            publish("abc", "no", pointer)
        self.assertFalse((self.root / "inbox").exists())

    def test_pending_does_not_block_or_certify_and_hold_discards(self):
        class Deferred:
            def __init__(self):
                self.jobs, self.results = {}, {}
            def submit(self, key, fn):
                self.jobs[key] = fn
                return True
            def take(self, key):
                return self.results.pop(key, None)
            def finish(self):
                for key, fn in self.jobs.items():
                    self.results[key] = fn()
                self.jobs.clear()
        executor = Deferred()
        self.m.scheduler.check_executor = executor
        self.tick(31)
        self.assertEqual(len(executor.jobs), 1)
        self.assertEqual(self.sent, [])
        executor.finish()
        self.tick()
        self.assertIsNotNone(self.lane.error)  # old completion rejected
        self.tick()
        self.row["state"] = "hold"
        self.tick()
        executor.finish()
        self.tick()
        self.assertEqual(self.lane.pending, {})
        self.assertEqual(self.lane.retired, set())
        self.assertEqual(self.sent, [])

    def test_deferred_continuous_flow_with_cadence_drift(self):
        class Deferred:
            def __init__(self):
                self.jobs, self.results = {}, {}
                self.executions = 0
            def submit(self, key, fn):
                self.jobs[key] = fn
                return True
            def take(self, key):
                return self.results.pop(key, None)
            def finish(self):
                for key, fn in self.jobs.items():
                    self.results[key] = fn()
                    self.executions += 1
                self.jobs.clear()
            def wait(self, *args):
                raise AssertionError("must never wait")
        executor = Deferred()
        self.m.scheduler.check_executor = executor
        self.tick()  # submission only; no work executes inside the policy tick
        self.assertEqual(executor.executions, 0)
        keys = set(executor.jobs)
        for sample in range(1, 42):
            executor.finish()
            self.now += timedelta(milliseconds=250)  # 60.25-second cadence
            executed = executor.executions
            self.tick()
            self.assertEqual(executor.executions, executed)
            self.assertEqual(set(executor.jobs), keys)  # same key, queued immediately
            self.assertEqual(len(self.sent), 0 if sample <= 30 else 1)
