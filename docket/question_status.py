"""Read-only, opt-in question status. No shell, native wake, or private state reads."""
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import time
import uuid
from datetime import datetime

ENV = "DOCKET_QUESTION_STATUS_ARGV"


def argv(raw):
    value = json.loads(raw)
    if (not isinstance(value, list) or not 1 <= len(value) <= 32
            or any(not isinstance(v, str) or not v or "\x00" in v for v in value)
            or not os.path.isabs(value[0]) or len(raw.encode()) > 4096):
        raise ValueError("invalid question reader argv")
    return value


def read(command, binding):
    data = json.dumps(dict(version=1, **binding)).encode()
    if len(data) > 4096:
        raise ValueError("question input too large")
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 5
    output = bytearray()
    try:
        process.stdin.write(data)
        process.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                left = deadline - time.monotonic()
                if left <= 0 or not selector.select(left):
                    raise ValueError("question reader timeout")
                chunk = os.read(process.stdout.fileno(), 32769 - len(output))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 32768:
                    raise ValueError("question output too large")
        if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
            raise ValueError("question reader failed")
        return json.loads(output)
    finally:
        # Also terminate inherited-pipe children; the helper has no background role.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        process.stdout.close()


def stamp(value):
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("timezone required")
    return at


def validate(value, binding, now):
    if (not isinstance(value, dict) or type(value.get("version")) is not int
            or value["version"] != 1 or value.get("source") != "chief-question-status"
            or value.get("complete") is not True
            or any(value.get(k) != v for k, v in binding.items())):
        raise ValueError("unknown question snapshot")
    if (not isinstance(value.get("goal"), dict)
            or type(value["goal"].get("created_at")) is not int):
        raise ValueError("invalid goal binding")
    observed = stamp(value.get("observed_at"))
    if not 0 <= (now - observed).total_seconds() <= 120:
        raise ValueError("stale question snapshot")
    questions = value.get("questions")
    if not isinstance(questions, list) or len(questions) > 128:
        raise ValueError("incomplete question set")
    ids = []
    for question in questions:
        if (not isinstance(question, dict)
                or any(question.get(k) != v for k, v in binding.items())
                or question.get("status") != "unresolved"
                or not isinstance(question.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]+:[A-Za-z0-9_.:-]{1,240}", question["id"])):
            raise ValueError("invalid question binding")
        if type(question["goal"].get("created_at")) is not int:
            raise ValueError("invalid goal binding")
        asked = stamp(question.get("asked_at"))
        if not binding["goal"]["created_at"] <= asked.timestamp() * 1000 <= observed.timestamp() * 1000:
            raise ValueError("invalid question time")
        if question["id"] in ids:
            raise ValueError("duplicate question")
        ids.append(question["id"])
    return ids


class QuestionStatus:
    def __init__(self, executor, raw=None, reader=read):
        self.executor, self.reader = executor, reader
        self.raw = os.environ.get(ENV) if raw is None else raw
        self.enabled = self.raw is not None
        self.command = None
        try:
            if self.enabled:
                self.command = argv(self.raw)
        except Exception:
            pass
        self.entries = {}
        self.retired = set()
        self.prefix = "question-status:" + uuid.uuid4().hex

    def forget(self, sid):
        entry = self.entries.pop(sid, None)
        if entry and entry.get("pending"):
            self.retired.add(entry["pending"][0])

    def get(self, sid, directory, goal, now):
        if not self.enabled or self.command is None:
            return None
        for key in tuple(self.retired):
            if self.executor.take(key) is not None:
                self.retired.discard(key)
        binding = dict(session=sid, directory=directory,
                       goal=dict(id=goal[0], created_at=goal[1]))
        scope = json.dumps(binding, sort_keys=True)
        entry = self.entries.get(sid)
        if entry and (entry["scope"] != scope or not 0 <= (now-entry["at"]).total_seconds() <= 120):
            self.forget(sid)
            entry = None
        if entry is None:
            entry = self.entries[sid] = dict(scope=scope, at=now, value=None)
        elif entry.get("sampled") == now:
            try:
                return validate(entry["value"], binding, now)
            except Exception:
                return None
        entry["at"] = now
        entry["sampled"] = now
        pending = entry.get("pending")
        if pending:
            result = self.executor.take(pending[0])
            if result is not None:
                entry.pop("pending")
                value, duration = result
                entry["value"] = value if duration <= 5 and 0 <= (now-pending[1]).total_seconds() <= 120 else None
            elif (now-pending[1]).total_seconds() > 120:
                self.forget(sid)
                return None
            else:
                entry["value"] = None  # an unfinished probe cannot renew success
        if not entry.get("pending"):
            key = self.prefix + ":" + hashlib.sha256(scope.encode()).hexdigest() + ":" + uuid.uuid4().hex
            def probe():
                start = time.monotonic()
                try:
                    return self.reader(self.command, binding), time.monotonic()-start
                except Exception:
                    return None, time.monotonic()-start
            if not self.executor.submit(key, probe):
                entry["value"] = None
                return None
            entry["pending"] = (key, now)
        try:
            ids = validate(entry["value"], binding, now)
            observed = stamp(entry["value"]["observed_at"])
            if entry.get("observed") and observed < entry["observed"]:
                entry["value"] = None
                return None
            entry["observed"] = observed
            return ids
        except Exception:
            return None
