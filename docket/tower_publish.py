"""Best-effort Tower notices for fires and new delivery-stall episodes.

Off unless ``DOCKET_TOWER_NATS_URL`` is a numeric loopback URL. The scheduling
path only ``put_nowait``s on one in-process queue: it does not connect, read
the token, or wait. A full queue, a bad token, or a bus failure drops the
notice and nothing else. Watch checks keep the existing check executor.

One daemon thread speaks a bounded NATS JetStream publish (INFO, CONNECT with
the token, SUB reply, HPUB with ``Nats-Msg-Id``) and stops. It does not retry,
open a stream, or wake anyone. Tower owns any DATA wake. Process exit drops
whatever is still queued.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import secrets
import socket
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from docket.tower_clock import REFRESH_TYPE, MARKER_TYPE, canonical_time, validate_marker

FIXED_SUBJECTS = ("ev." + REFRESH_TYPE, "ev." + MARKER_TYPE)

ENV_URL = "DOCKET_TOWER_NATS_URL"
ENV_TOKEN = "DOCKET_TOWER_TOKEN_FILE"
DEFAULT_TOKEN_FILE = "~/.tower/secrets/nats.token"

SOURCE = "docket"
FIRE_TYPE = "docket.timer.fired"
FIRE_SUBJECT = "ev.docket.timer.fired"
STALL_TYPE = "lead.delivery.stalled"
STALL_SUBJECT = "ev.lead.delivery.stalled"
STALL_DETAIL = "observed content baseline unchanged; not certified acceptance"
QUESTION_DETAIL = "observed idle behind one recorded unresolved question; not certified inactivity or fault"

MAX_EVENT_BYTES = 64 * 1024
IO_DEADLINE = 2.0
MAX_IO_BYTES = 96 * 1024
MAX_CONTROL = 8192
MAX_TOKEN_BYTES = 4096
QUEUE_LIMIT = 32

_SESSION_RE = re.compile(r"^ses_[A-Za-z0-9]{1,120}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_KIND_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_OUTCOMES = {"consumed", "admitted", "undeliverable", "ambiguous", "retry", "unavailable", "conflict"}


class _Drop(Exception):
    """A publish was abandoned. The message is fixed so it cannot carry a token."""

    def __init__(self):
        super().__init__("dropped")


class _Budget:
    def __init__(self, deadline: float, max_bytes: int):
        self.deadline = deadline
        self.max_bytes = max_bytes
        self.used = 0

    def remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise _Drop()
        return left

    def account(self, count: int) -> None:
        self.used += count
        if self.used > self.max_bytes or time.monotonic() > self.deadline:
            raise _Drop()


def enabled() -> bool:
    """True when the operator opted in to numeric loopback. No token read or connect."""
    try:
        _parse_url(os.environ.get(ENV_URL))
    except _Drop:
        return False
    return True


def stall_episode(lead_session, directory, minutes_since_landed, detected_at, episode_id, *, reason=None):
    """Frozen JSON-safe stall notice, or None if it must not be published.

    ``project`` is the directory's final component, not goal text or event prose.
    ``detail`` is the fixed baseline sentence. The id is the existing epoch key.
    """
    try:
        if not _safe_id(episode_id) or not _session(lead_session):
            return None
        directory = _directory(directory)
        project = _project(directory)
        minutes = _minutes(minutes_since_landed)
        when = _time(detected_at)
        if directory is None or project is None or minutes is None or when is None:
            return None
        payload = {
            "id": episode_id,
            "time": when,
            "lead_session": lead_session,
            "directory": directory,
            "project": project,
            "minutes_since_landed": minutes,
            "detail": STALL_DETAIL,
        }
        if reason == "idle_question":
            payload.pop("minutes_since_landed")
            payload.update(reason=reason, idle_minutes=minutes, detail=QUESTION_DETAIL)
        elif reason is not None:
            return None
        return payload
    except Exception:
        return None


class TowerPublisher:
    """One bounded queue and one daemon thread. The hot path only put_nowait."""

    def __init__(self, transport=None):
        self.transport = transport or publish_nats
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_LIMIT)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False

    def note_fire(self, item, occ, outcome, fired_at) -> None:
        """Hot path. Never connects, reads the token, waits, or raises."""
        try:
            if not enabled():
                return
            occurrence_id = occ.id if occ is not None else item.id
            body = encode_fire(item, occ, occurrence_id, outcome, fired_at)
            if body is None:
                return
            self._enqueue(FIRE_SUBJECT, occurrence_id, body)
        except Exception:
            return

    def note_stall(self, episode) -> None:
        """Hot path for one already-frozen stall episode. Never raises."""
        try:
            if not enabled() or not isinstance(episode, dict):
                return
            body = encode_stall(episode)
            if body is None:
                return
            self._enqueue(STALL_SUBJECT, episode.get("id"), body)
        except Exception:
            return

    def note_fixed(self, event_type, event_id, data, when):
        """Queue receipt only: never broker acknowledgement or downstream freshness."""
        if event_type not in (REFRESH_TYPE, MARKER_TYPE) or not enabled():
            return "disabled"
        body = encode_event(event_id, event_type, data, when)
        if body is None:
            return "uncertain"
        return "accepted" if self._enqueue("ev." + event_type, event_id, body) else "full"

    def close(self, timeout: float = 3.0) -> None:
        """Join the producer after queued notices. A stuck bus is abandoned.

        Process exit does not need this; the thread is a daemon and leftover
        notices are dropped. Tests call it so the thread does not leak.
        """
        with self._lock:
            self._closed = True
            thread = self._thread
        if thread is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        thread.join(timeout)

    def _enqueue(self, subject: str, event_id, body: bytes) -> bool:
        if self._closed or subject not in (FIRE_SUBJECT, STALL_SUBJECT, *FIXED_SUBJECTS) or not _safe_id(event_id):
            return False
        self._ensure()
        try:
            self._queue.put_nowait((subject, event_id, body))
        except queue.Full:
            return False
        return True

    def _ensure(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="docket-tower", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._closed:
                    return
                continue
            try:
                if item is None:
                    return
                self._publish(item)
            except Exception:
                pass
            finally:
                self._queue.task_done()

    def _publish(self, item) -> None:
        subject, event_id, body = item
        url = os.environ.get(ENV_URL)
        if subject not in (FIRE_SUBJECT, STALL_SUBJECT, *FIXED_SUBJECTS) or not _safe_id(event_id):
            return
        try:
            _parse_url(url)
        except _Drop:
            return
        token = read_token(None)
        if not token:
            return
        self.transport(url, token, subject, event_id, body)


def encode_fire(item, occ, occurrence_id, outcome, fired_at) -> bytes | None:
    kind = item.kind if isinstance(getattr(item, "kind", None), str) else ""
    if not _KIND_RE.match(kind) or not _safe_id(occurrence_id) or not _safe_id(item.id):
        return None
    target = item.target if isinstance(getattr(item, "target", None), dict) else {}
    scheduled = occ.scheduled_at if occ is not None else item.fire_at
    when = _time(fired_at)
    scheduled_at = _time(scheduled)
    if when is None or scheduled_at is None:
        return None
    data = {
        "item_id": item.id,
        "timer_id": item.id,
        "kind": kind,
        "occurrence_id": occurrence_id,
        "target_session_id": _session(target.get("session_id")),
        "directory": _directory(target.get("directory")),
        "reason": _text(getattr(item, "reason", None), 2000),
        "created_by": _text(getattr(item, "created_by", None), 128),
        "scheduled_at": scheduled_at,
        "fired_at": when,
    }
    if isinstance(outcome, str) and outcome in _OUTCOMES:
        data["delivery_outcome"] = outcome
    return encode_event(occurrence_id, FIRE_TYPE, data, when)


def encode_stall(episode: dict) -> bytes | None:
    """Build the stall event from the frozen allowlist. Ignores every other key."""
    episode_id = episode.get("id")
    if not _safe_id(episode_id):
        return None
    lead = _session(episode.get("lead_session"))
    directory = _directory(episode.get("directory"))
    project = _project(directory)
    question = episode.get("reason") == "idle_question"
    if episode.get("reason") not in (None, "idle_question"):
        return None
    minutes = _minutes(episode.get("idle_minutes" if question else "minutes_since_landed"))
    when = episode.get("time")
    if lead is None or directory is None or project is None or minutes is None:
        return None
    if not isinstance(when, str) or not when or len(when) > 40 or "\x00" in when:
        return None
    # Detail is ours. A stored or prose value cannot replace it or choose a route.
    data = {
        "lead_session": lead,
        "directory": directory,
        "project": project,
        "minutes_since_landed": minutes,
        "detail": STALL_DETAIL,
    }
    if question:
        data.pop("minutes_since_landed")
        data.update(reason="idle_question", idle_minutes=minutes, detail=QUESTION_DETAIL)
    return encode_event(episode_id, STALL_TYPE, data, when)


def encode_event(event_id: str, event_type: str, data: dict, when: str) -> bytes | None:
    if event_type not in (FIRE_TYPE, STALL_TYPE, REFRESH_TYPE, MARKER_TYPE) or not _safe_id(event_id):
        return None
    if event_type in (REFRESH_TYPE, MARKER_TYPE):
        common = {"version", "provenance"}
        fields = ({"purpose", "item_id", "occurrence_id", "scheduled_at", "fired_at", "coalesced", "schedule_until"}
                  if event_type == REFRESH_TYPE else
                  {"lead_session", "directory", "milestone_at", "milestone_ref", "observed_at", "roster_state"})
        try:
            if not isinstance(data, dict) or set(data) != common | fields or type(data.get("version")) is not int or data["version"] != 1:
                return None
            canonical_time(when)
            if event_type == REFRESH_TYPE:
                canonical_time(data["scheduled_at"])
                canonical_time(data["schedule_until"])
                if (data["purpose"] != "project_pages" or data["provenance"] != "docket_recurring_clock"
                        or data["item_id"] != "dkt_tower_refresh" or data["occurrence_id"] != event_id
                        or not re.fullmatch(r"occ_[0-9a-f]{16}", event_id)
                        or type(data["coalesced"]) is not int or data["coalesced"] < 0 or data["fired_at"] != when):
                    return None
            else:
                validate_marker(dict(id=event_id, session=data["lead_session"], directory=data["directory"],
                                     milestone_at=data["milestone_at"], milestone_ref=data["milestone_ref"],
                                     roster_state=data["roster_state"]))
                if data["provenance"] != "recorded_marker_observation" or data["observed_at"] != when:
                    return None
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
            return None
    event = {
        "specversion": "1.0",
        "id": event_id,
        "source": SOURCE,
        "type": event_type,
        "time": when,
        "datacontenttype": "application/json",
        "data": data,
    }
    try:
        body = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode()
    except (TypeError, ValueError):
        return None
    if len(body) > MAX_EVENT_BYTES:
        return None
    return body


def read_token(path: str | None = None) -> str | None:
    """Owner-only regular file, mode 0600, no symlink. None on any refusal.

    Failures return None and do not include the token or the OS error text.
    """
    try:
        raw = path if path is not None else os.environ.get(ENV_TOKEN) or DEFAULT_TOKEN_FILE
        if not isinstance(raw, str) or not raw or "\x00" in raw or len(raw) > 4096:
            return None
        expanded = os.path.expanduser(raw)
        file_path = Path(expanded)
        if not file_path.is_absolute() or file_path.is_symlink():
            return None
        # Do not resolve: an ancestor symlink must fail the no-follow walk.
        parent = _open_dir(file_path.parent)
        try:
            fd = os.open(file_path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        try:
            view = os.fstat(fd)
            mode = stat.S_IMODE(view.st_mode)
            if (not stat.S_ISREG(view.st_mode) or view.st_uid != os.getuid()
                    or mode != 0o600 or not 0 < view.st_size <= MAX_TOKEN_BYTES):
                return None
            data = os.read(fd, MAX_TOKEN_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_TOKEN_BYTES:
            return None
        token = data.decode("utf-8").strip()
        if not token or any(char in token for char in "\r\n\x00") or len(token) > MAX_TOKEN_BYTES:
            return None
        return token
    except Exception:
        return None


def publish_nats(url: str, token: str, subject: str, event_id: str, body: bytes) -> None:
    """One JetStream publish. Raises only ``_Drop`` or an OS error with no token."""
    if subject not in (FIRE_SUBJECT, STALL_SUBJECT, *FIXED_SUBJECTS) or not _safe_id(event_id):
        raise _Drop()
    if not isinstance(body, bytes) or not body or len(body) > MAX_EVENT_BYTES:
        raise _Drop()
    if not token or any(char in token for char in "\r\n\x00"):
        raise _Drop()
    host, port = _parse_url(url)
    budget = _Budget(time.monotonic() + IO_DEADLINE, MAX_IO_BYTES)
    sock = socket.create_connection((host, port), timeout=budget.remaining())
    try:
        sock.settimeout(budget.remaining())
        reader = _Reader(sock, budget)
        info = reader.line()
        if not info.startswith(b"INFO "):
            raise _Drop()
        connect = {
            "verbose": False,
            "pedantic": False,
            "lang": "python",
            "version": "docket",
            "protocol": 1,
            "headers": True,
            "name": "docket",
            "auth_token": token,
        }
        _send(sock, b"CONNECT " + json.dumps(connect, separators=(",", ":")).encode() + b"\r\n", budget)
        inbox = "_INBOX.docket." + secrets.token_hex(12)
        _send(sock, f"SUB {inbox} 1\r\n".encode(), budget)
        _send(sock, _hpub(subject, inbox, event_id, body), budget)
        _read_puback(reader, sock, budget, inbox)
    finally:
        try:
            sock.close()
        except Exception:
            return


def _hpub(subject: str, reply: str, event_id: str, body: bytes) -> bytes:
    header = f"NATS/1.0\r\nNats-Msg-Id: {event_id}\r\n\r\n".encode()
    total = len(header) + len(body)
    line = f"HPUB {subject} {reply} {len(header)} {total}\r\n".encode()
    return line + header + body + b"\r\n"


def _read_puback(reader: "_Reader", sock: socket.socket, budget: _Budget, inbox: str) -> None:
    for _ in range(8):
        line = reader.line()
        if line == b"PING":
            _send(sock, b"PONG\r\n", budget)
            continue
        if line == b"+OK" or line == b"":
            continue
        if line.startswith(b"-ERR") or line.startswith(b"INFO "):
            raise _Drop()
        if line.startswith(b"MSG ") or line.startswith(b"HMSG "):
            payload = _message_payload(reader, line, inbox)
            try:
                ack = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise _Drop()
            if not isinstance(ack, dict) or "error" in ack or ack.get("stream") != "TOWER":
                raise _Drop()
            seq = ack.get("seq")
            if type(seq) is not int or seq < 1:
                raise _Drop()
            return
        raise _Drop()
    raise _Drop()


def _message_payload(reader: "_Reader", line: bytes, inbox: str) -> bytes:
    parts = line.split(b" ")
    if len(parts) < 4 or parts[1].decode() != inbox:
        raise _Drop()
    if parts[0] == b"HMSG":
        hdr_len, total = int(parts[-2]), int(parts[-1])
        if hdr_len < 0 or total < hdr_len or total > MAX_CONTROL:
            raise _Drop()
        blob = reader.exact(total)
        trailer = reader.exact(2)
        if trailer != b"\r\n":
            raise _Drop()
        return blob[hdr_len:]
    if parts[0] != b"MSG":
        raise _Drop()
    size = int(parts[-1])
    if size < 0 or size > MAX_CONTROL:
        raise _Drop()
    payload = reader.exact(size)
    trailer = reader.exact(2)
    if trailer != b"\r\n":
        raise _Drop()
    return payload


class _Reader:
    def __init__(self, sock: socket.socket, budget: _Budget):
        self.sock = sock
        self.budget = budget
        self.buf = bytearray()

    def line(self, limit: int = MAX_CONTROL) -> bytes:
        while b"\r\n" not in self.buf:
            if len(self.buf) >= limit:
                raise _Drop()
            self._fill()
        index = self.buf.index(b"\r\n")
        if index > limit:
            raise _Drop()
        line = bytes(self.buf[:index])
        del self.buf[:index + 2]
        return line

    def exact(self, count: int) -> bytes:
        if count < 0 or count > self.budget.max_bytes:
            raise _Drop()
        while len(self.buf) < count:
            self._fill()
        out = bytes(self.buf[:count])
        del self.buf[:count]
        return out

    def _fill(self) -> None:
        self.sock.settimeout(self.budget.remaining())
        chunk = self.sock.recv(4096)
        if not chunk:
            raise _Drop()
        self.budget.account(len(chunk))
        self.buf += chunk


def _send(sock: socket.socket, data: bytes, budget: _Budget) -> None:
    budget.account(len(data))
    sock.settimeout(budget.remaining())
    sock.sendall(data)


def _parse_url(url: str) -> tuple[str, int]:
    """Numeric loopback only. No DNS and no remote token egress."""
    if not isinstance(url, str) or not url.startswith("nats://") or len(url) > 64:
        raise _Drop()
    rest = url[len("nats://"):]
    if not rest or any(char in rest for char in "@/?# \r\n\x00"):
        raise _Drop()
    if rest.startswith("["):
        host, sep, tail = rest[1:].partition("]")
        if not sep or not tail.startswith(":") or host != "::1":
            raise _Drop()
        port_text = tail[1:]
    else:
        host, sep, port_text = rest.rpartition(":")
        if not sep or host != "127.0.0.1" or not port_text:
            raise _Drop()
    if not port_text.isdigit():
        raise _Drop()
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise _Drop()
    return host, port


def _open_dir(path: Path) -> int:
    if not path.is_absolute():
        raise _Drop()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if part in ("", ".", ".."):
                raise _Drop()
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except Exception:
        os.close(fd)
        raise


def _safe_id(value) -> bool:
    return isinstance(value, str) and _ID_RE.match(value) is not None


def _session(value) -> str | None:
    if isinstance(value, str) and _SESSION_RE.match(value):
        return value
    return None


def _directory(value) -> str | None:
    if (isinstance(value, str) and value.startswith("/") and "\x00" not in value
            and len(value) <= 4096 and ".." not in Path(value).parts):
        return value
    return None


def _project(directory: str | None) -> str | None:
    if directory is None:
        return None
    label = directory.rstrip("/").rsplit("/", 1)[-1]
    if not label or len(label) > 256 or "\x00" in label:
        return None
    return label


def _text(value, limit: int) -> str | None:
    if not isinstance(value, str) or "\x00" in value:
        return None
    return value[:limit]


def _minutes(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or value > 1_000_000:
        return None
    return round(float(value), 3)


def _time(value) -> str | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
