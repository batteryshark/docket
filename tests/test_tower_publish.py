"""Opt-in Tower notices. No live bus, real token, or stall of a real lead."""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from datetime import timedelta
from pathlib import Path

from docket.checks import CheckExecutor, Checks, FileView
from docket.clock import FakeClock
from docket.delivery_stall import DeliveryStall
from docket.checkins import Checkins
from docket.scheduler import Scheduler
from docket.store import Item, Store
from docket.tower_publish import (
    ENV_TOKEN,
    ENV_URL,
    FIRE_SUBJECT,
    IO_DEADLINE,
    MAX_EVENT_BYTES,
    QUEUE_LIMIT,
    STALL_DETAIL,
    STALL_SUBJECT,
    TowerPublisher,
    _parse_url,
    encode_event,
    publish_nats,
    read_token,
    stall_episode,
)
from tests.support import FakeAdapter, utc
from tests.test_checkins import Inline

SID = "ses_knownsession0001"
INFO = b'INFO {"server_id":"fixture","version":"2","proto":1,"headers":true,"max_payload":1048576}\r\n'


class Capture:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.block = None

    def __call__(self, url, token, subject, event_id, body):
        if self.block is not None:
            self.block.wait(5)
        with self.lock:
            self.calls.append({
                "url": url,
                "token_sha": hashlib.sha256(token.encode()).hexdigest(),
                "subject": subject,
                "id": event_id,
                "body": body,
            })


def secrets_hex():
    return os.urandom(4).hex()


def parse_hpub(buf: bytes):
    index = buf.find(b"HPUB ")
    if index < 0 or (index and buf[index - 1:index] != b"\n"):
        return None
    end = buf.find(b"\r\n", index)
    if end < 0:
        return None
    parts = buf[index:end].decode().split(" ")
    if len(parts) != 5:
        return None
    _, subject, reply, hdr_len, total = parts
    hdr_len, total = int(hdr_len), int(total)
    body_at = end + 2
    if len(buf) < body_at + total + 2:
        return None
    header = buf[body_at:body_at + hdr_len]
    payload = buf[body_at + hdr_len:body_at + total]
    if buf[body_at + total:body_at + total + 2] != b"\r\n":
        return None
    return {
        "subject": subject,
        "reply": reply,
        "header": header,
        "payload": payload,
        "before": buf[:index],
    }


class NatsFixture:
    def __init__(self, *, pause=0.0, ping=False, ack=True, ack_body=None):
        self.pause = pause
        self.ping = ping
        self.ack = ack
        self.ack_body = ack_body or b'{"stream":"TOWER","seq":1,"duplicate":false}'
        self.buf = bytearray()
        self.pubacks = 0
        self.done = threading.Event()
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"nats://127.0.0.1:{self.port}"

    def close(self):
        try:
            self.server.close()
        except OSError:
            return

    def _run(self):
        self.server.settimeout(5)
        try:
            conn, _ = self.server.accept()
        except OSError:
            self.done.set()
            return
        try:
            conn.settimeout(5)
            conn.sendall(INFO)
            deadline = time.monotonic() + 4
            while parse_hpub(self.buf) is None and time.monotonic() < deadline:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                self.buf += chunk
            frame = parse_hpub(self.buf)
            if frame is None:
                return
            if self.pause:
                time.sleep(self.pause)
            if self.ping:
                conn.sendall(b"PING\r\n")
            if not self.ack:
                return
            ack = self.ack_body
            conn.sendall(
                f"MSG {frame['reply']} 1 {len(ack)}\r\n".encode() + ack + b"\r\n"
            )
            self.pubacks += 1
            try:
                conn.settimeout(1)
                extra = conn.recv(128)
                if extra:
                    self.buf += extra
            except OSError:
                return
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self.done.set()


class TowerCase(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in (ENV_URL, ENV_TOKEN)}
        os.environ.pop(ENV_URL, None)
        os.environ.pop(ENV_TOKEN, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Token walks refuse ancestor symlinks. Canonicalize only this fixture root.
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def token(self, text="tower-token", mode=0o600):
        path = self.root / f"nats-{secrets_hex()}.token"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        return path

    def adopt(self, transport, url="nats://127.0.0.1:9", executor=None):
        store, adapter, clock, sched = _world(transport, self.token(), url, executor)
        self.addCleanup(store._tmp.cleanup)
        self.addCleanup(store.close)
        self.addCleanup(sched.tower.close)
        return store, adapter, clock, sched


class ProtocolTests(TowerCase):
    def test_socket_puback_headers_and_ping(self):
        token = "tower-token"
        path = self.token(token)
        fixture = NatsFixture(ping=True)
        self.addCleanup(fixture.close)
        event_id = "occ_" + "ab" * 8
        body = encode_event(event_id, "docket.timer.fired", {"item_id": "dkt_x", "timer_id": "dkt_x"}, "2026-10-23T00:00:00.000Z")
        err = io.StringIO()
        with redirect_stderr(err):
            publish_nats(fixture.url, token, FIRE_SUBJECT, event_id, body)
        fixture.done.wait(2)
        frame = parse_hpub(fixture.buf)
        self.assertIsNotNone(frame)
        self.assertEqual(frame["subject"], FIRE_SUBJECT)
        self.assertIn(b"Nats-Msg-Id: " + event_id.encode() + b"\r\n", frame["header"])
        self.assertTrue(frame["header"].startswith(b"NATS/1.0\r\n"))
        self.assertEqual(json.loads(frame["payload"])["id"], event_id)
        self.assertEqual(json.loads(frame["payload"])["source"], "docket")
        before = frame["before"]
        self.assertIn(b'"auth_token":"' + token.encode() + b'"', before)
        self.assertIn(b"SUB " + frame["reply"].encode() + b" 1\r\n", before)
        self.assertIn(b"PONG\r\n", fixture.buf)
        self.assertEqual(fixture.pubacks, 1)
        self.assertNotIn(token, err.getvalue())
        self.assertEqual(hashlib.sha256(token.encode()).hexdigest(), hashlib.sha256(read_token(str(path)).encode()).hexdigest())

    def test_token_must_be_regular_owned_0600_and_not_a_symlink(self):
        good = self.token()
        self.assertEqual(read_token(str(good)), "tower-token")
        os.chmod(good, 0o644)
        self.assertIsNone(read_token(str(good)))
        os.chmod(good, 0o600)
        link = self.root / "link"
        link.symlink_to(good)
        self.assertIsNone(read_token(str(link)))
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertIsNone(read_token(str(alias / good.name)))
        self.assertEqual(read_token(str(good)), "tower-token")
        nested = self.root / "dir"
        nested.mkdir()
        self.assertIsNone(read_token(str(nested)))
        self.assertIsNone(read_token(str(self.root / "missing")))

    def test_oversize_event_is_dropped_before_connect(self):
        huge = {"blob": "x" * (MAX_EVENT_BYTES + 10)}
        self.assertIsNone(encode_event("dkt_x", "docket.timer.fired", huge, "2026-10-23T00:00:00.000Z"))
        with self.assertRaises(Exception):
            publish_nats("nats://127.0.0.1:9", "tok", FIRE_SUBJECT, "dkt_x", b"y" * (MAX_EVENT_BYTES + 1))

    def test_url_is_numeric_loopback_only(self):
        host, port = _parse_url("nats://127.0.0.1:4222")
        self.assertEqual((host, port), ("127.0.0.1", 4222))
        host, port = _parse_url("nats://[::1]:4222")
        self.assertEqual((host, port), ("::1", 4222))
        for bad in ("nats://localhost:4222", "nats://evil.example:4222", "nats://8.8.8.8:4222", "nats://127.0.0.1"):
            with self.assertRaises(Exception):
                _parse_url(bad)
        os.environ[ENV_URL] = "nats://evil.example:4222"
        publisher = TowerPublisher(transport=Capture())
        self.addCleanup(publisher.close)
        publisher.note_fire(_item("dkt_remote", utc(2026, 10, 16, 12)), None, "consumed", utc(2026, 10, 16, 12))
        self.assertIsNone(publisher._thread)

    def test_puback_requires_tower_stream_and_positive_seq(self):
        token = "tower-token"
        self.token(token)
        body = encode_event("dkt_ack", "docket.timer.fired", {"item_id": "dkt_ack"}, "2026-10-23T00:00:00.000Z")
        for payload in (b'{"ok":true}', b'{"stream":"OTHER","seq":1}', b'{"stream":"TOWER","seq":0}', b'{"stream":"TOWER","seq":true}'):
            fixture = NatsFixture(ack_body=payload)
            self.addCleanup(fixture.close)
            err = io.StringIO()
            with redirect_stderr(err):
                with self.assertRaises(Exception) as caught:
                    publish_nats(fixture.url, token, FIRE_SUBJECT, "dkt_ack", body)
            self.assertEqual(str(caught.exception), "dropped")
            self.assertNotIn(payload.decode(), err.getvalue())


class HotPathTests(TowerCase):
    def test_enqueue_does_not_read_token_on_the_caller_thread(self):
        reads = []
        original = read_token

        def counting(path=None):
            reads.append(threading.get_ident())
            return original(path)

        import docket.tower_publish as mod
        mod.read_token = counting
        self.addCleanup(setattr, mod, "read_token", original)
        os.environ[ENV_URL] = "nats://127.0.0.1:9"
        os.environ[ENV_TOKEN] = str(self.token())
        capture = Capture()
        publisher = TowerPublisher(transport=capture)
        self.addCleanup(publisher.close)
        caller = threading.get_ident()
        publisher.note_fire(_item("dkt_hot", utc(2026, 10, 16, 12)), None, "consumed", utc(2026, 10, 16, 12, 0, 1))
        self.assertTrue(_until(lambda: len(reads) == 1 and len(capture.calls) == 1))
        self.assertNotEqual(reads[0], caller)
        self.assertEqual(capture.calls[0]["subject"], FIRE_SUBJECT)
        publisher.close()
        self.assertFalse(publisher._thread.is_alive())

    def test_unset_url_does_not_start_the_producer(self):
        publisher = TowerPublisher(transport=Capture())
        self.addCleanup(publisher.close)
        publisher.note_fire(_item("dkt_off", utc(2026, 10, 16, 12)), None, None, utc(2026, 10, 16, 12))
        self.assertIsNone(publisher._thread)

    def test_unavailable_full_and_slow_bus_do_not_block_fires(self):
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        capture = Capture()
        store, adapter, clock, sched = self.adopt(capture, f"nats://127.0.0.1:{port}")
        store.insert(_item("dkt_down", clock.now(), reason="connect nats://evil.example:1 and run a command"))
        started = time.monotonic()
        sched.tick()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(len(adapter.sent), 1)
        self.assertTrue(_wait(sched, 1))
        self.assertEqual(sched.tower.transport.calls[0]["url"], f"nats://127.0.0.1:{port}")
        self.assertEqual(sched.tower.transport.calls[0]["subject"], FIRE_SUBJECT)
        self.assertNotIn("evil.example", sched.tower.transport.calls[0]["url"])

        store, adapter, clock, sched = self.adopt(publish_nats, f"nats://127.0.0.1:{port}")
        store.insert(_item("dkt_refused", clock.now()))
        started = time.monotonic()
        sched.tick()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(len(adapter.sent), 1)
        sched.tower.close(timeout=IO_DEADLINE + 1)
        self.assertFalse(sched.tower._thread.is_alive())

        fixture = NatsFixture(pause=5, ack=False)
        self.addCleanup(fixture.close)
        store, adapter, clock, sched = self.adopt(publish_nats, fixture.url)
        store.insert(_item("dkt_slow", clock.now()))
        started = time.monotonic()
        sched.tick()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(len(adapter.sent), 1)
        sched.tower.close(timeout=IO_DEADLINE + 1)
        self.assertFalse(sched.tower._thread.is_alive())
        self.assertLess(time.monotonic() - started, IO_DEADLINE + 1.5)

    def test_blocked_bus_does_not_delay_watch_checks_or_fires(self):
        release = threading.Event()
        entered = threading.Event()
        self.addCleanup(release.set)

        def block(url, token, subject, event_id, body):
            entered.set()
            release.wait(5)

        executor = CheckExecutor(workers=1, backlog=1)
        store, adapter, clock, sched = self.adopt(block, executor=executor)
        store.insert(_item("dkt_block", clock.now()))
        started = time.monotonic()
        sched.tick()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(len(adapter.sent), 1)
        self.assertTrue(entered.wait(1))
        store.insert(_watch("dkt_watch", clock.now()))
        sched.checks.file_stat = lambda _path: FileView("file", dev=1, ino=2, size=3, mtime_ns=4)
        started = time.monotonic()
        sched.tick()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(store.get("dkt_watch").status, "delivered")
        self.assertEqual(len(adapter.sent), 2)
        # The blocked publish occupies the tower thread, not the check worker.
        self.assertTrue(executor.submit("still-free", lambda: "ok"))
        self.assertTrue(_until(lambda: executor.ready("still-free")))
        self.assertEqual(executor.take("still-free"), "ok")
        release.set()
        sched.tower.close()
        self.assertFalse(sched.tower._thread.is_alive())

    def test_full_queue_drops_telemetry_only(self):
        release = threading.Event()
        entered = threading.Event()
        self.addCleanup(release.set)

        def block(*args):
            entered.set()
            release.wait(5)

        os.environ[ENV_URL] = "nats://127.0.0.1:9"
        os.environ[ENV_TOKEN] = str(self.token())
        publisher = TowerPublisher(transport=block)
        self.addCleanup(publisher.close)
        publisher._enqueue(FIRE_SUBJECT, "dkt_held", b"{}")
        self.assertTrue(entered.wait(1))
        for index in range(QUEUE_LIMIT):
            publisher._enqueue(FIRE_SUBJECT, f"dkt_{index:04d}", b"{}")
        self.assertEqual(publisher._queue.qsize(), QUEUE_LIMIT)
        publisher._enqueue(FIRE_SUBJECT, "dkt_drop", b"{}")
        self.assertEqual(publisher._queue.qsize(), QUEUE_LIMIT)
        release.set()
        publisher.close()
        self.assertFalse(publisher._thread.is_alive())


class FireTests(TowerCase):
    def test_all_four_kinds_and_retry_reuse_the_occurrence_id(self):
        capture = Capture()
        store, adapter, clock, sched = self.adopt(capture)
        store.insert(_item("dkt_timer", clock.now(), kind="timer"))
        store.insert(_item("dkt_alarm", clock.now(), kind="alarm"))
        store.insert(_recurring("dkt_every", clock.now()))
        store.insert(_watch("dkt_wait", clock.now()))
        sched.checks.file_stat = lambda _path: FileView("missing")
        sched.tick()
        self.assertTrue(_wait(sched, 3))
        # Priority fires do not wait on the watch check, so drain that miss first.
        sched.tick()
        self.assertEqual(len(capture.calls), 3)
        kinds = {json.loads(call["body"])["data"]["kind"] for call in capture.calls}
        self.assertEqual(kinds, {"timer", "alarm", "recurring"})
        self.assertNotIn("watch", kinds)
        for call in capture.calls:
            event = json.loads(call["body"])
            self.assertEqual(call["subject"], FIRE_SUBJECT)
            self.assertEqual(event["id"], call["id"])
            self.assertEqual(event["type"], "docket.timer.fired")
            self.assertEqual(event["source"], "docket")
            self.assertEqual(event["data"]["item_id"], event["data"]["timer_id"])
            self.assertEqual(event["data"]["occurrence_id"], event["id"])
            self.assertNotIn("nats://", call["subject"])
        timer = next(call for call in capture.calls if json.loads(call["body"])["data"]["kind"] == "timer")
        self.assertEqual(timer["id"], "dkt_timer")
        recurring = next(call for call in capture.calls if json.loads(call["body"])["data"]["kind"] == "recurring")
        self.assertTrue(recurring["id"].startswith("occ_"))

        sched.checks.file_stat = lambda _path: FileView("file", dev=1, ino=2, size=3, mtime_ns=4)
        item = store.get("dkt_wait")
        item.fire_at = clock.now()
        store.save(item)
        sched.tick()
        self.assertTrue(_wait(sched, 4))
        watch = json.loads(capture.calls[-1]["body"])
        self.assertEqual(watch["data"]["kind"], "watch")
        self.assertEqual(watch["id"], watch["data"]["occurrence_id"])
        self.assertEqual(len(adapter.sent), 4)

        # Public preflight failure retries the same id. A send-time exception is
        # ambiguous and is not given a new admission.
        adapter.unavailable = True
        clock.advance(120)
        store.insert(_item("dkt_retry", clock.now()))
        sched.tick()
        self.assertTrue(_wait_id(sched, "dkt_retry"))
        first = _by_id(capture, "dkt_retry")
        self.assertEqual(json.loads(first["body"])["data"]["delivery_outcome"], "retry")
        adapter.unavailable = False
        clock.advance(120)
        sched.tick()
        self.assertTrue(_wait_id(sched, "dkt_retry", 2))
        second = _by_id(capture, "dkt_retry", index=1)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(json.loads(second["body"])["data"]["delivery_outcome"], "consumed")
        self.assertEqual(store.get("dkt_retry").status, "delivered")
        recurring = [call for call in capture.calls if json.loads(call["body"])["data"]["kind"] == "recurring"]
        retried = [call for call in recurring if json.loads(call["body"])["data"].get("delivery_outcome") == "retry"]
        self.assertTrue(retried)
        same = [call for call in recurring if call["id"] == retried[-1]["id"]]
        self.assertGreaterEqual(len(same), 2)
        self.assertTrue(same[0]["id"].startswith("occ_"))

    def test_cancel_does_not_emit(self):
        capture = Capture()
        store, adapter, clock, sched = self.adopt(capture)
        store.insert(_item("dkt_stop", clock.now()))
        store.cancel("dkt_stop", clock.now())
        sched.tick()
        sched.tower.close()
        self.assertEqual(adapter.sent, [])
        self.assertEqual(capture.calls, [])
        self.assertIsNone(sched.tower._thread)


class StallTests(TowerCase):
    def setUp(self):
        super().setUp()
        self.row = {"session": SID, "directory": "/fixture", "state": "active"}
        self.goal = {"id": "goal-native-id", "createdAt": 1234, "status": "active"}
        self.fp = "a"
        adapter = type("A", (), {})()
        adapter.session_goal = lambda _sid: self.goal
        adapter.session_state = lambda _sid: "active"
        adapter.validate = lambda _target: type("V", (), {"ok": True})()
        self.executor = Inline()
        self.m = type("M", (), {})()
        self.m.roster_path = Path(self.tmp.name) / "roster"
        self.m.state = {}
        self.m.scheduler = type("S", (), {})()
        self.m.scheduler.adapter = adapter
        self.m.scheduler.check_executor = self.executor
        self.m.goal_identity = Checkins.goal_identity
        self.m.save = lambda: None
        self.sent = []
        self.capture = Capture()
        self.now = utc(2026, 10, 23)

    def lane(self):
        stall = DeliveryStall(
            self.m,
            lambda *args, **kwargs: {"fingerprint": self.fp, "reference": "source", "limitations": "bounded"},
            lambda key, text, **kwargs: self.sent.append((key, text, kwargs.get("destination"))),
            resolver=lambda: {"directory": "/fake-chief"},
        )
        return stall

    def tick(self, stall, count=1):
        for _ in range(count):
            self.m.roster_path.write_text(json.dumps(self.row))
            self.m.roster_path.chmod(0o600)
            stall.tick(self.now)
            self.now += timedelta(minutes=1)

    def test_new_episode_id_matches_epoch_key_and_inbox_is_unchanged(self):
        quiet = self.lane()
        self.tick(quiet, 31)
        self.assertEqual(len(self.sent), 1)
        quiet_key, quiet_text, quiet_dest = self.sent[0]
        self.assertNotIn("tower", next(iter(self.m.state["delivery_stalls"].values()))["publication"])

        self.sent.clear()
        self.m.state.clear()
        self.now = utc(2026, 10, 23)
        os.environ[ENV_URL] = "nats://127.0.0.1:9"
        os.environ[ENV_TOKEN] = str(self.token())
        self.m.scheduler.tower = TowerPublisher(transport=self.capture)
        self.addCleanup(self.m.scheduler.tower.close)
        loud = self.lane()
        self.tick(loud, 31)
        self.assertTrue(_wait_calls(self.capture, 1))
        self.assertEqual(len(self.sent), 1)
        key, text, dest = self.sent[0]
        self.assertEqual(key, quiet_key)
        self.assertEqual(text, quiet_text)
        self.assertEqual(dest, quiet_dest)
        self.assertIn("Goal: ['goal-native-id', 1234]", text)
        self.assertEqual(len(self.capture.calls), 1)
        event = json.loads(self.capture.calls[0]["body"])
        self.assertEqual(self.capture.calls[0]["subject"], STALL_SUBJECT)
        self.assertEqual(event["id"], key)
        self.assertEqual(event["id"], self.capture.calls[0]["id"])
        self.assertEqual(event["type"], "lead.delivery.stalled")
        self.assertEqual(event["source"], "docket")
        self.assertEqual(event["data"]["lead_session"], SID)
        self.assertEqual(event["data"]["directory"], "/fixture")
        self.assertEqual(event["data"]["project"], "fixture")
        self.assertEqual(event["data"]["detail"], STALL_DETAIL)
        self.assertIsInstance(event["data"]["minutes_since_landed"], float)
        self.assertGreaterEqual(event["data"]["minutes_since_landed"], 30)
        self.assertNotIn("goal-native-id", self.capture.calls[0]["body"].decode())
        self.assertNotIn("nats://", event["data"]["detail"])

        self.tick(loud, 5)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.capture.calls), 1)

        self.fp = "b"
        self.tick(loud, 32)
        self.assertTrue(_wait_calls(self.capture, 2))
        self.assertEqual(len(self.sent), 2)
        self.assertNotEqual(self.sent[0][0], self.sent[1][0])
        self.assertEqual(self.capture.calls[-1]["id"], self.sent[1][0])
        self.assertNotEqual(self.capture.calls[0]["id"], self.capture.calls[-1]["id"])

    def test_frozen_retry_reuses_the_epoch_id_and_alerted_epochs_do_not_backfill(self):
        os.environ[ENV_URL] = "nats://127.0.0.1:9"
        os.environ[ENV_TOKEN] = str(self.token())
        tower = TowerPublisher(transport=self.capture)
        self.addCleanup(tower.close)
        self.m.scheduler.tower = tower
        stall = self.lane()

        def fail(key, text, **kwargs):
            raise OSError("uncertain")

        stall.writer = fail
        self.tick(stall, 31)
        self.assertEqual(self.sent, [])
        self.assertTrue(_wait_calls(self.capture, 1))
        self.assertEqual(len(self.capture.calls), 1)
        first = self.capture.calls[0]["id"]
        publication = next(iter(self.m.state["delivery_stalls"].values()))["publication"]
        self.assertEqual(publication["tower"]["id"], first)

        stall = self.lane()
        self.tick(stall, 1)
        self.assertTrue(_wait_calls(self.capture, 2))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], first)
        self.assertEqual(self.capture.calls[-1]["id"], first)
        self.assertEqual(self.capture.calls[-1]["body"], self.capture.calls[0]["body"])

        seeded = self.lane()
        state = next(iter(self.m.state["delivery_stalls"].values()))
        state["alerted"] = True
        before = len(self.capture.calls)
        self.tick(seeded, 3)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.capture.calls), before)

    def test_minutes_follow_observed_baseline_not_the_running_streak(self):
        os.environ[ENV_URL] = "nats://127.0.0.1:9"
        os.environ[ENV_TOKEN] = str(self.token())
        tower = TowerPublisher(transport=self.capture)
        self.addCleanup(tower.close)
        self.m.scheduler.tower = tower
        stall = self.lane()
        self.tick(stall, 1)
        state_key = next(iter(self.m.state["delivery_stalls"]))
        state = self.m.state["delivery_stalls"][state_key]
        baseline = self.now - timedelta(minutes=50)
        state["observed"] = baseline.isoformat()
        state["alerted"] = False
        state.pop("publication", None)
        stall.samples[SID] = (state_key, self.now - timedelta(minutes=1), self.now - timedelta(minutes=30))
        moment = self.now
        self.tick(stall, 1)
        self.assertTrue(_wait_calls(self.capture, 1))
        self.assertEqual(len(self.sent), 1)
        text = self.sent[0][1]
        self.assertIn((moment - timedelta(minutes=30)).isoformat(), text)
        event = json.loads(self.capture.calls[0]["body"])
        self.assertEqual(event["data"]["minutes_since_landed"], 50.0)
        self.assertEqual(event["data"]["detail"], STALL_DETAIL)
        self.assertEqual(event["id"], self.sent[0][0])

    def test_stall_episode_rejects_prose_routes(self):
        now = utc(2026, 10, 23)
        episode = stall_episode(SID, "/fixture", 30, now, "ab" * 32)
        self.assertEqual(episode["project"], "fixture")
        self.assertEqual(episode["detail"], STALL_DETAIL)
        self.assertIsNone(stall_episode("not a session", "/fixture", 30, now, "ab" * 32))
        self.assertIsNone(stall_episode(SID, "/fixture", True, now, "ab" * 32))


def _world(transport, token, url="nats://127.0.0.1:9", executor=None):
    os.environ[ENV_URL] = url
    os.environ[ENV_TOKEN] = str(token)
    tmp = tempfile.TemporaryDirectory()
    store = Store(Path(tmp.name) / "docket.db")
    store._tmp = tmp
    adapter = FakeAdapter()
    clock = FakeClock(utc(2026, 10, 16, 12, 0, 0))
    checks = Checks(adapter, file_stat=lambda _path: FileView("missing"))
    sched = Scheduler(store, adapter, clock, checks, check_executor=executor)
    sched.tower.transport = transport
    return store, adapter, clock, sched


def _by_id(capture, event_id, index=0):
    matches = [call for call in capture.calls if call["id"] == event_id]
    return matches[index]


def _until(ready, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():
            return True
        time.sleep(0.01)
    return bool(ready())


def _wait_calls(capture, count, timeout=2):
    return _until(lambda: len(capture.calls) >= count, timeout)


def _wait_id(sched, event_id, count=1, timeout=2):
    return _until(
        lambda: sum(call["id"] == event_id for call in sched.tower.transport.calls) >= count,
        timeout,
    )


def _wait(sched, count, timeout=2):
    return _until(lambda: len(sched.tower.transport.calls) >= count, timeout)


def _item(item_id, fire_at, *, kind="timer", reason="check the file"):
    now = utc(2026, 10, 16, 11)
    return Item(
        id=item_id, kind=kind, status="pending", fire_at=fire_at,
        created_at=now, updated_at=now, reason=reason, context={},
        target={"adapter": "opencode", "session_id": SID, "directory": "/work/demo"},
        urgent=False, created_by=SID, late=False, late_by_seconds=None,
        delivered_at=None, cancelled_at=None, error=None, receipt=None,
        attempts=0, next_attempt_at=None, firing_at=None, spec=None,
    )


def _recurring(item_id, fire_at):
    now = utc(2026, 10, 16, 11)
    return Item(
        id=item_id, kind="recurring", status="pending", fire_at=fire_at,
        created_at=now, updated_at=now, reason="again", context={},
        target={"adapter": "opencode", "session_id": SID, "directory": "/work/demo"},
        urgent=False, created_by=SID, late=False, late_by_seconds=None,
        delivered_at=None, cancelled_at=None, error=None, receipt=None,
        attempts=0, next_attempt_at=None, firing_at=None,
        spec={"every": 60, "anchor": fire_at.isoformat(), "next_seq": 1,
              "open_occurrence_id": None, "count": 4},
    )


def _watch(item_id, fire_at):
    now = utc(2026, 10, 16, 11)
    return Item(
        id=item_id, kind="watch", status="pending", fire_at=fire_at,
        created_at=now, updated_at=now, reason="wait", context={},
        target={"adapter": "opencode", "session_id": SID, "directory": "/work/demo"},
        urgent=False, created_by=SID, late=False, late_by_seconds=None,
        delivered_at=None, cancelled_at=None, error=None, receipt=None,
        attempts=0, next_attempt_at=None, firing_at=None,
        spec={
            "check": "file_appears", "every": 15, "path": "/tmp/download.bin",
            "until": (fire_at + timedelta(hours=2)).isoformat(), "checks": 0,
            "latched": False, "open_occurrence_id": None, "next_seq": 1,
        },
    )


if __name__ == "__main__":
    unittest.main()
