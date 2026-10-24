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
from docket.question_status import QuestionStatus, validate, argv, read
import copy
import sys

SID = "ses_knownsession0001"


def question_snapshot(binding, now, ids=("heartbeat:q",)):
    return dict(version=1, source="chief-question-status", complete=True,
                observed_at=now.isoformat(), **binding,
                questions=[dict(id=q, status="unresolved", asked_at=now.isoformat(), **binding) for q in ids])


class QuestionContractTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 23, tzinfo=timezone.utc)
        self.binding = dict(session=SID, directory="/fixture", goal=dict(id="g", created_at=1234))
        self.value = question_snapshot(self.binding, self.now)

    def test_validation_matrix(self):
        self.assertEqual(validate(self.value, self.binding, self.now), ["heartbeat:q"])
        for field, value in (("version", True), ("version", 2), ("source", "legacy"),
                             ("complete", False), ("complete", 1), ("session", "ses_other"),
                             ("directory", "/other"), ("goal", {}), ("questions", None),
                             ("observed_at", "bad"), ("observed_at", "2026-10-23")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate(dict(self.value, **{field: value}), self.binding, self.now)
        for field, value in (("id", "unstructured"), ("status", "answered"), ("session", "ses_other"),
                             ("directory", "/other"), ("goal", {}), ("asked_at", "1970-01-01T00:00:00Z"),
                             ("asked_at", (self.now+timedelta(seconds=1)).isoformat())):
            broken = copy.deepcopy(self.value)
            broken["questions"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate(broken, self.binding, self.now)
        for seconds in (-1, 121):
            with self.assertRaises(ValueError):
                validate(self.value, self.binding, self.now+timedelta(seconds=seconds))
        self.assertEqual(validate(self.value, self.binding, self.now+timedelta(seconds=120)), ["heartbeat:q"])
        for ids in ((), ("heartbeat:a", "heartbeat:b")):
            self.assertEqual(len(validate(question_snapshot(self.binding, self.now, ids), self.binding, self.now)), len(ids))
        with self.assertRaises(ValueError):
            validate(question_snapshot(self.binding, self.now, ("heartbeat:a",)*2), self.binding, self.now)
        with self.assertRaises(ValueError):
            validate(question_snapshot(self.binding, self.now, tuple(f"heartbeat:{i}" for i in range(129))), self.binding, self.now)

    def test_argv_and_bounded_executable(self):
        for raw in ('[]', '["relative"]', '"/bin/true"', '["/bin/true", null]', 'bad'):
            with self.assertRaises((ValueError, TypeError)):
                argv(raw)
        command = [sys.executable, "-c", "import sys; print(sys.stdin.read())"]
        self.assertEqual(read(argv(json.dumps(command)), self.binding), dict(version=1, **self.binding))
        for script in ("print('x'*32769)", "raise SystemExit(2)", "print('not json')"):
            with self.assertRaises(ValueError):
                read([sys.executable, "-c", script], self.binding)
        # Literal shell syntax remains an argv value, never an executable command.
        literal = [sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1]))", "$(touch NEVER)"]
        self.assertEqual(read(literal, self.binding), "$(touch NEVER)")
        with self.assertRaises(ValueError):
            read(command, dict(self.binding, directory="/" + "x"*4096))

    def test_executable_timeout(self):
        with self.assertRaises(ValueError):
            read([sys.executable, "-c", "import time; time.sleep(10)"], self.binding)

    def test_async_unknown_failure_and_binding_generation(self):
        class Deferred:
            def __init__(self):
                self.jobs, self.results = {}, {}
            def submit(self, key, fn):
                self.jobs[key] = fn
                return True
            def take(self, key):
                return self.results.pop(key, None)
        executor = Deferred()
        source = QuestionStatus(executor, raw='["/fixture/helper"]', reader=lambda cmd, b: question_snapshot(b, self.now))
        self.assertIsNone(source.get(SID, "/fixture", ["g", 1234], self.now))
        old = next(iter(executor.jobs))
        self.assertIsNone(source.get(SID, "/other", ["g", 1234], self.now))
        executor.results[old] = executor.jobs.pop(old)()
        self.assertIsNone(source.get(SID, "/other", ["g", 1234], self.now+timedelta(minutes=1)))
        self.assertNotIn(old, source.retired)
        executor.submit = lambda *a: False
        source.forget(SID)
        self.assertIsNone(source.get(SID, "/fixture", ["g", 1234], self.now))

    def test_failure_never_reuses_success_and_clock_regression(self):
        executor = Inline()
        value = [self.value]
        source = QuestionStatus(executor, raw='["/fixture/helper"]', reader=lambda *a: value[0])
        self.assertIsNone(source.get(SID, "/fixture", ["g", 1234], self.now))
        value[0] = None
        self.assertEqual(source.get(SID, "/fixture", ["g", 1234], self.now+timedelta(seconds=60)), ["heartbeat:q"])
        self.assertIsNone(source.get(SID, "/fixture", ["g", 1234], self.now+timedelta(seconds=120)))
        self.assertIsNone(source.get(SID, "/fixture", ["g", 1234], self.now+timedelta(seconds=119)))


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


class IdleQuestionTests(unittest.TestCase):
    tick = PolicyTests.tick
    new_lane = PolicyTests.new_lane

    def setUp(self):
        PolicyTests.setUp(self)
        self.activity = "idle"
        self.ids = ("heartbeat:q",)
        self.unknown = False
        self.m.snapshot = lambda sid: ((self.row, Checkins.goal_identity(self.goal), self.activity)
            if self.valid and self.row["state"] == "active" and self.goal["status"] in ("active", "blocked")
            and self.activity in ("idle", "active") else None)
        self.m.questions = QuestionStatus(self.m.scheduler.check_executor, raw='["/fixture/helper"]',
            reader=lambda cmd, binding: None if self.unknown else question_snapshot(binding, self.now, self.ids))

    def test_threshold_restart_dedupe_and_activity(self):
        self.tick(31)  # first sample is conservatively unknown
        self.assertEqual(self.sent, [])
        self.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("not certified inactivity or fault", self.sent[0][1])
        self.activity = "active"
        self.tick()
        self.activity = "idle"
        self.lane = self.new_lane()
        self.tick(33)
        self.assertEqual(len(self.sent), 1)
        self.ids = ("heartbeat:new",)
        self.tick(33)
        self.assertEqual(len(self.sent), 2)
        self.assertNotEqual(self.sent[0][0], self.sent[1][0])

    def test_count_unknown_and_native_suppression(self):
        for ids in ((), ("heartbeat:a", "heartbeat:b")):
            self.ids = ids
            self.tick(34)
        self.ids = ("heartbeat:q",)
        self.unknown = True
        self.tick(34)
        self.unknown = False
        for field, value in (("activity", "archived"), ("valid", False)):
            original = getattr(self, field)
            setattr(self, field, value)
            self.tick(34)
            setattr(self, field, original)
        for state in ("hold", "done"):
            self.row["state"] = state
            self.tick(34)
        self.assertEqual(self.sent, [])

    def test_gap_backwards_restart_and_identity_reset(self):
        self.tick(29)
        self.now += timedelta(minutes=3)
        self.tick(29)
        self.now -= timedelta(minutes=2)
        self.tick(29)
        self.lane = self.new_lane()
        self.tick(29)
        self.goal["createdAt"] += 1
        self.tick(29)
        self.row["directory"] = "/other"
        self.tick(29)
        self.assertEqual(self.sent, [])
        self.tick(4)
        self.assertEqual(len(self.sent), 1)

    def test_frozen_retry_and_unknown_suppresses_retry(self):
        attempted = []
        def fail(key, text, **kw):
            attempted.append((key, text, kw))
            raise OSError("fixture")
        self.lane.writer = fail
        self.tick(32)
        self.assertEqual(len(attempted), 1)
        self.unknown = True
        self.tick()  # prior frozen success still fresh; failure next sample
        before = len(attempted)
        self.tick(3)
        self.assertEqual(len(attempted), before)
        self.unknown = False
        self.lane = self.new_lane()
        self.lane.resolver = lambda: self.fail("reserved destination must not change")
        self.tick(2)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0], attempted[0][:2])

    def test_question_tower_episode_is_frozen_and_blocked_goal_eligible(self):
        from unittest.mock import patch
        self.goal["status"] = "blocked"
        events = []
        self.m.scheduler.tower = SimpleNamespace(note_stall=lambda payload: events.append(copy.deepcopy(payload)))
        def fail(*a, **kw):
            raise OSError("fixture")
        self.lane.writer = fail
        with patch("docket.tower_publish.enabled", return_value=True):
            self.tick(32)
            self.assertEqual(len(events), 1)
            self.lane = self.new_lane()
            self.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(events[0], events[1])
        self.assertEqual(events[0]["id"], self.sent[0][0])
        self.assertEqual(events[0]["reason"], "idle_question")
        self.assertNotIn("minutes_since_landed", events[0])
