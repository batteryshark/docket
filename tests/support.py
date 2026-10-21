"""Shared fakes for Docket tests."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from docket.delivery import DeliveryError, Receipt, Validation


def block_source_service_processes():
    """Module-scoped fail-closed guard; local process mocks may override it."""
    import contextlib
    import os
    import subprocess
    from unittest.mock import patch

    stack = contextlib.ExitStack()

    def refused(*_args, **_kwargs):
        raise AssertionError("source service tests forbid native process launch")

    stack.enter_context(patch.object(subprocess, "Popen", side_effect=refused))
    for name in ("system", "posix_spawn", "posix_spawnp", "fork", "forkpty",
                 "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
                 "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe"):
        if hasattr(os, name):
            stack.enter_context(patch.object(os, name, side_effect=refused))
    return stack



class FakeAdapter:
    name = "fake"

    def __init__(self):
        self.known = {"ses_knownsession0001"}
        self.archived = set()
        self.directories = {}
        self.unavailable = False
        self.sent = []
        self.cancelled = []
        self.delivered_markers = set()
        self.fail = None
        self.active = set()
        self.inbox = set()
        self.consumed = set()
        self.consume_immediately = True
        self.conflict = False
        self.lookup_unavailable = False
        self.hold_deliver = None
        self.entered = False
        self.cancel_fails = False

    def validate(self, target: dict) -> Validation:
        if self.unavailable:
            return Validation.bad("unavailable", "opencode down")
        session_id = target.get("session_id") or ""
        if session_id in self.archived:
            return Validation.bad("archived", "session is archived")
        expected = target.get("directory")
        actual = self.directories.get(session_id)
        if expected and actual and expected != actual:
            return Validation.bad("wrong_target", "session is not in the directory this item was set for")
        if session_id not in self.known:
            return Validation.bad("bad_target", "session not found")
        return Validation.good()

    def session_state(self, session_id: str) -> str:
        if self.unavailable:
            raise DeliveryError("unavailable", "opencode down", retryable=True)
        if session_id in self.archived:
            return "archived"
        if session_id not in self.known:
            return "missing"
        if session_id in self.active:
            return "active"
        return "idle"

    def reconcile(self, target: dict, admission_id: str) -> str:
        if self.lookup_unavailable or self.unavailable:
            raise DeliveryError("unavailable", "lookup down", retryable=True)
        if admission_id in self.consumed:
            return "consumed"
        if admission_id in self.inbox:
            return "admitted"
        return "missing"

    def deliver(self, target: dict, message: str, *, urgent: bool, admission_id: str | None = None) -> Receipt:
        self.entered = True
        if self.hold_deliver is not None:
            self.hold_deliver.wait(timeout=5)
        if self.fail:
            raise self.fail
        if self.conflict:
            raise DeliveryError("conflict", "admission id already exists", retryable=False)
        mode = "steer" if urgent else "queue"
        aid = admission_id or "msg_test"
        self.sent.append({
            "target": dict(target),
            "message": message,
            "urgent": urgent,
            "delivery": mode,
            "admission_id": aid,
        })
        if self.consume_immediately:
            self.consumed.add(aid)
            state = "consumed"
        else:
            self.inbox.add(aid)
            state = "admitted"
        return Receipt(
            inbox_id=aid, admission_id=aid, delivery=mode,
            state=state, consumed=state == "consumed",
            raw={"id": aid, "delivery": mode},
        )

    def cancel_admission(self, target: dict, inbox_id: str | None) -> None:
        self.cancelled.append((target.get("session_id"), inbox_id))
        if self.cancel_fails:
            return
        if inbox_id:
            self.inbox.discard(inbox_id)


class RecordingOpenCode(ThreadingHTTPServer):
    """A loopback stand-in for the OpenCode prompt API."""

    def __init__(self):
        self.calls = []
        self.sessions = {}
        self.active = {}
        self.inbox = {}
        self.messages = {}
        super().__init__(("127.0.0.1", 0), _OpenCodeHandler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class _OpenCodeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def do_GET(self):
        self.server.calls.append(("GET", self.path))
        if self.path.rstrip("/") == "/api/session/active":
            return self._send(200, {"data": dict(self.server.active)})
        if self.path.startswith("/api/session/") and self.path.endswith("/inbox"):
            session_id = self.path.split("/")[3]
            return self._send(200, {"data": list(self.server.inbox.get(session_id, {}).values())})
        if "/message/" in self.path:
            session_id = self.path.split("/")[3]
            message_id = self.path.rstrip("/").split("/")[-1]
            found = self.server.messages.get(session_id, {}).get(message_id)
            if found is None:
                return self._send(404, {"_tag": "MessageNotFoundError"})
            return self._send(200, {"data": found})
        if "/message" in self.path:
            return self._send(200, {"data": []})
        if self.path.startswith("/api/session/"):
            session_id = self.path.split("/")[3]
            session = self.server.sessions.get(session_id)
            if session is None:
                return self._send(404, {"_tag": "SessionNotFoundError", "sessionID": session_id})
            return self._send(200, {"data": session})
        return self._send(404, {"error": "no"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw)
        self.server.calls.append(("POST", self.path, body))
        if self.path.endswith("/prompt"):
            session_id = self.path.split("/")[3]
            admitted = body.get("id") or "msg_admitted"
            existing = self.server.inbox.get(session_id, {}).get(admitted)
            if existing:
                return self._send(200, {"data": existing})
            item = {
                "id": admitted,
                "delivery": body.get("delivery"),
                "type": "user",
                "payload": {"text": body.get("text")},
            }
            self.server.inbox.setdefault(session_id, {})[admitted] = item
            return self._send(200, {"data": item})
        return self._send(404, {"error": "no"})

    def do_DELETE(self):
        self.server.calls.append(("DELETE", self.path))
        status = getattr(self.server, "delete_status", 204)
        if status >= 400:
            return self._send(status, {"error": "not dropped"})
        parts = self.path.rstrip("/").split("/")
        if len(parts) >= 6 and parts[4] == "inbox":
            session_id, inbox_id = parts[3], parts[5]
            self.server.inbox.get(session_id, {}).pop(inbox_id, None)
        self._send(status, None)

    def _send(self, status: int, payload):
        raw = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)


def utc(*parts) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)
