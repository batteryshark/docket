"""Loopback HTTP API. Mutations need the local bearer token."""

from __future__ import annotations

import json
import secrets
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from docket import __version__
from docket.bounds import (
    CHECKS,
    LimitError,
    check_limits,
    normalize_created_by,
    public_limits,
    recurring_spec,
    reject_forbidden,
    watch_spec,
)
from docket.parse import (
    IMPLEMENTED_KINDS,
    RESERVED_KINDS,
    check_context,
    check_reason,
    new_id,
    parse_alarm,
    parse_delay,
    require_bool,
    validate_session_id,
)
from docket.delivery import (
    AMBIGUOUS,
    CONSUMED,
    FAILED,
    LOST,
    MISSING,
    RETRACTED,
    retract_admission,
)
from docket.scheduler import log_scheduler_error
from docket.store import OUTSTANDING, Item

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8768
MAX_BODY_BYTES = 65536
BODY_READ_SECONDS = 5.0
SEND_WAIT_SECONDS = 3.0


class DocketServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, store, scheduler, token: str, host: str = DEFAULT_HOST,
                 port: int = DEFAULT_PORT):
        if host not in ("127.0.0.1", "localhost"):
            raise SystemExit("docket binds to loopback only")
        self.store = store
        self.scheduler = scheduler
        self.token = token
        self._mutate = threading.Lock()
        self._thread: threading.Thread | None = None
        self._sched_stop = threading.Event()
        self._sched_thread: threading.Thread | None = None
        try:
            super().__init__((host, port), Handler)
        except OSError as exc:
            raise SystemExit(
                f"docket: cannot bind {host}:{port} ({exc.strerror})"
            ) from None

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def serve_in_thread(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def start_scheduler(self, interval: float = 0.5) -> None:
        def run():
            while not self._sched_stop.is_set():
                try:
                    self.scheduler.tick()
                except Exception as exc:
                    # A tick must not kill the process. The next tick retries.
                    log_scheduler_error(exc)
                if self._sched_stop.wait(interval):
                    break
        self._sched_thread = threading.Thread(target=run, name="docket-scheduler", daemon=True)
        self._sched_thread.start()

    def stop(self) -> None:
        self._sched_stop.set()
        self.shutdown()
        self.server_close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: DocketServer

    def log_message(self, *_args):
        return

    def do_GET(self):
        if not self._boundary_ok():
            return self._error(403, "forbidden", "loopback only")
        path, query = self._split()
        if path == "/health":
            outstanding = self.server.store.list_outstanding()
            return self._send(200, {
                "ok": True,
                "version": __version__,
                "pending": len(outstanding),
                "outstanding": len(outstanding),
                "host": _host_status(self.server.scheduler.adapter),
            })
        if path == "/api/v1/meta":
            return self._send(200, {
                "version": __version__,
                "kinds": list(IMPLEMENTED_KINDS),
                "checks": list(CHECKS),
                "reserved": list(RESERVED_KINDS),
                "delivery": ["queue", "steer"],
                "host": _host_status(self.server.scheduler.adapter),
                "statuses": [
                    "pending", "firing", "admitted", "ambiguous",
                    "delivered", "completed", "cancelled", "undeliverable", "expired",
                ],
                "outstanding": list(OUTSTANDING),
                "limits": public_limits(),
            })
        if path == "/api/v1/items":
            status = query.get("status")
            if status in (None, "pending", "outstanding"):
                # The default pending view is every non-terminal state, including
                # a firing claim and an admission that has not been consumed.
                items = self.server.store.list_outstanding() if status != None else self.server.store.list()
            elif status == "all":
                items = self.server.store.list()
            else:
                items = self.server.store.list(status)
            counts = self.server.store.occurrence_counts()
            opens = self.server.store.open_summaries()
            return self._send(200, {"items": [_with_counts(item, counts, opens) for item in items]})
        if path == "/api/v1/occurrences":
            status = query.get("status")
            if status == "all":
                status = None
            found = self.server.store.list_occurrences(status=status)
            return self._send(200, {"occurrences": [occ.public() for occ in found]})
        if path.startswith("/api/v1/items/"):
            rest = path[len("/api/v1/items/"):]
            if "/" in rest:
                item_id, action = rest.split("/", 1)
                if action == "occurrences":
                    if self.server.store.get(item_id) is None:
                        return self._error(404, "not_found", "no such item")
                    found = self.server.store.list_occurrences(item_id)
                    return self._send(200, {"occurrences": [occ.public() for occ in found]})
                return self._error(404, "not_found", "no such path")
            item = self.server.store.get(rest)
            if item is None:
                return self._error(404, "not_found", "no such item")
            body = _with_counts(item, self.server.store.occurrence_counts())
            recent = self.server.store.list_occurrences(item.id, limit=10)
            body["recent_occurrences"] = [occ.public() for occ in recent]
            return self._send(200, {"item": body})
        return self._error(404, "not_found", "no such path")

    def do_POST(self):
        if not self._boundary_ok():
            return self._error(403, "forbidden", "loopback only")
        if not self._authed():
            return self._error(401, "unauthorized", "bearer token required")
        path, _query = self._split()
        body = self._body()
        if body is None:
            return
        if path == "/api/v1/timers":
            return self._create("timer", body)
        if path == "/api/v1/alarms":
            return self._create("alarm", body)
        if path == "/api/v1/recurring":
            return self._create("recurring", body)
        if path == "/api/v1/watches":
            return self._create("watch", body)
        if path == "/api/v1/items":
            kind = body.get("kind")
            if kind in RESERVED_KINDS:
                return self._error(501, "not_implemented", f"{kind} is reserved")
            if kind not in IMPLEMENTED_KINDS:
                return self._error(400, "invalid", "kind must be timer, alarm, recurring, or watch")
            return self._create(kind, body)
        if path.startswith("/api/v1/items/") and path.endswith("/cancel"):
            item_id = path[len("/api/v1/items/"):-len("/cancel")].strip("/")
            return self._cancel(item_id)
        return self._error(404, "not_found", "no such path")

    def _create(self, kind: str, body: dict):
        with self.server._mutate:
            return self._create_locked(kind, body)

    def _create_locked(self, kind: str, body: dict):
        try:
            reject_forbidden(body)
            reason = check_reason(body.get("reason"))
            context = check_context(body.get("context"))
        except ValueError as exc:
            return self._error(400, "invalid", str(exc))
        session_id = body.get("session_id") or ""
        bad = validate_session_id(session_id)
        if bad:
            return self._error(400, "bad_target", bad)
        directory = body.get("directory")
        if directory is not None and (not isinstance(directory, str) or not directory.strip()):
            return self._error(400, "invalid", "directory must be a path")
        target = {
            "adapter": "opencode",
            "session_id": session_id,
        }
        if directory:
            target["directory"] = directory
        now = self.server.scheduler.clock.now()
        try:
            if kind == "timer":
                fire_at = parse_delay(body.get("in", body.get("delay")), now=now)
                spec = None
            elif kind == "alarm":
                fire_at = parse_alarm(body.get("at") or "", now=now)
                spec = None
            elif kind == "recurring":
                fire_at, spec = recurring_spec(body, now=now)
            elif kind == "watch":
                fire_at, spec = watch_spec(
                    body, now=now,
                    capture_process=self.server.scheduler.checks.capture_process,
                )
            else:
                return self._error(400, "invalid", "kind must be timer, alarm, recurring, or watch")
        except ValueError as exc:
            return self._error(400, "invalid", str(exc))
        except RuntimeError as exc:
            return self._error(503, "unavailable", str(exc))
        check = self.server.scheduler.adapter.validate(target)
        if not check.ok:
            status = 503 if check.code == "unavailable" else 400
            return self._error(status, check.code, check.detail)
        target["host"] = _host_status(self.server.scheduler.adapter)
        if kind == "watch" and spec and spec.get("check") == "session_idle":
            watched = spec.get("watch_session")
            if watched != session_id:
                state = self._watched_state(watched)
                if state is not None:
                    return state
        try:
            created_by = normalize_created_by(body.get("created_by"))
        except ValueError as exc:
            return self._error(400, "invalid", str(exc))
        try:
            urgent = require_bool(body.get("urgent") if "urgent" in body else None, "urgent")
        except ValueError as exc:
            return self._error(400, "invalid", str(exc))
        try:
            check_limits(
                self.server.store.active_items(),
                kind=kind,
                session_id=session_id,
                created_by=created_by or None,
            )
        except LimitError as exc:
            return self._error(409, "limit", exc.detail)
        item = Item(
            id=new_id(),
            kind=kind,
            status="pending",
            fire_at=fire_at,
            created_at=now,
            updated_at=now,
            reason=reason,
            context=context,
            target=target,
            urgent=urgent,
            created_by=created_by or None,
            late=False,
            late_by_seconds=None,
            delivered_at=None,
            cancelled_at=None,
            error=None,
            receipt=None,
            attempts=0,
            next_attempt_at=None,
            firing_at=None,
            spec=spec,
        )
        self.server.store.insert(item)
        return self._send(201, {"item": item.public()})

    def _watched_state(self, session_id: str):
        adapter = self.server.scheduler.adapter
        if not hasattr(adapter, "session_state"):
            check = adapter.validate({"adapter": "opencode", "session_id": session_id})
            if not check.ok:
                status = 503 if check.code == "unavailable" else 400
                return self._error(status, check.code, check.detail)
            return None
        try:
            state = adapter.session_state(session_id)
        except Exception as exc:
            return self._error(503, "unavailable", str(exc) or "session check unavailable")
        if state == "missing":
            return self._error(400, "bad_target", "watched session not found")
        if state == "archived":
            return self._error(400, "archived", "watched session is archived")
        if state not in ("idle", "active"):
            return self._error(503, "unavailable", "watched session could not be checked")
        return None

    def _cancel(self, item_id: str):
        with self.server._mutate:
            item = self.server.store.get(item_id)
            if item is None:
                return self._error(404, "not_found", "no such item")
            if item.status not in OUTSTANDING:
                return self._terminal_cancel(item)
            self._wait_for_send(item.id)
            item = self.server.store.get(item_id) or item
            if item.status not in OUTSTANDING:
                return self._terminal_cancel(item)
            aid = _cancel_admission_id(item, self.server.store.open_occurrence(item))
            if aid and _admission_consumed(self.server.store, item.id, aid):
                return self._stop_for_host(
                    item, aid, CONSUMED, 409, "consumed",
                    "not removed: the host already consumed this admission",
                )
            return self._cancel_outstanding(item)

    def _wait_for_send(self, item_id: str) -> None:
        """Let an in-flight deliver finish so cancel sees the host's answer.

        The scheduler does not take ``_mutate``. Waiting here cannot deadlock
        a deliver. A crash recovery has no in-flight send and does not wait.
        """
        scheduler = self.server.scheduler
        sending = getattr(scheduler, "sending", None)
        wait = getattr(scheduler, "wait_sending", None)
        if not callable(sending) or not callable(wait) or not sending(item_id):
            return
        wait(item_id, SEND_WAIT_SECONDS)

    def _cancel_outstanding(self, item):
        occ = self.server.store.open_occurrence(item)
        receipt = _cancel_receipt(item, occ)
        aid = _admission_of(receipt)
        target = item.target if isinstance(item.target, dict) else None
        now = self.server.scheduler.clock.now()
        if not aid:
            result = self.server.store.cancel(item.id, now)
            return self._cancelled_response(item.id, result, "none", None)
        if not target or not target.get("session_id"):
            self.server.store.note_outstanding_error(
                item.id, now, "not removed: the admission has no session to retract",
            )
            return self._refuse(
                503, AMBIGUOUS,
                "not removed: the admission has no session to retract",
                item.id, AMBIGUOUS, aid,
            )
        outcome = retract_admission(
            self.server.scheduler.adapter, target, aid, confirmed=_confirmed(receipt),
        )
        if outcome == RETRACTED:
            result = self.server.store.cancel(
                item.id, now, receipt_state=RETRACTED, admission_id=aid, clear_error=True,
            )
            return self._cancelled_response(item.id, result, RETRACTED, aid)
        if outcome == MISSING and (receipt or {}).get("state") != AMBIGUOUS:
            result = self.server.store.cancel(
                item.id, now, receipt_state=MISSING, admission_id=aid, clear_error=True,
            )
            return self._cancelled_response(item.id, result, MISSING, aid)
        if outcome == CONSUMED:
            return self._stop_for_host(
                item, aid, CONSUMED, 409, "consumed",
                "not removed: the host already consumed this admission",
            )
        if outcome == LOST:
            # The host does not hold the prompt. Stopping the series is a
            # completed cancel, not a live drop. The receipt records lost so
            # a reader does not treat it as a witnessed retraction.
            result = self.server.store.cancel(
                item.id, now, receipt_state=LOST, admission_id=aid,
                error="admitted prompt was not on the host",
            )
            return self._cancelled_response(item.id, result, LOST, aid)
        if outcome == MISSING and (receipt or {}).get("state") == AMBIGUOUS:
            return self._stop_for_host(
                item, aid, AMBIGUOUS, 409, AMBIGUOUS,
                "not removed: the admission was ambiguous and was not confirmed on the host",
            )
        if outcome == FAILED:
            self.server.store.note_outstanding_error(
                item.id, now, "retraction failed: the host still holds this admission",
            )
            return self._refuse(
                409, "retraction_failed",
                "not removed: the host still holds this admission",
                item.id, FAILED, aid,
            )
        self.server.store.note_outstanding_error(
            item.id, now, "not removed: the host could not be checked, so the admission was not dropped",
        )
        return self._refuse(
            503, AMBIGUOUS,
            "not removed: the host could not be checked, so the admission was not dropped",
            item.id, AMBIGUOUS, aid,
        )

    def _stop_for_host(self, item, aid: str, state: str, status: int, code: str, detail: str):
        """Stop later wakes without claiming the prompt was dropped."""
        now = self.server.scheduler.clock.now()
        if item.status in OUTSTANDING:
            self.server.store.cancel(
                item.id, now, receipt_state=state, error=detail, admission_id=aid,
            )
        return self._refuse(status, code, detail, item.id, state, aid)

    def _terminal_cancel(self, item):
        state = (item.receipt or {}).get("state")
        if item.status in ("delivered", "completed") or state == CONSUMED:
            return self._refuse(
                409, "consumed",
                "not removed: the host already consumed this admission",
                item.id, CONSUMED, _admission_of(item.receipt),
            )
        if item.status == "undeliverable" or state == LOST:
            return self._refuse(
                409, "lost",
                "not removed: the admitted prompt is neither in the inbox nor the transcript",
                item.id, LOST, _admission_of(item.receipt),
            )
        return self._error(409, "conflict", "item is not pending")

    def _cancelled_response(self, item_id: str, result: str, outcome: str, aid: str | None):
        if result == "missing":
            return self._error(404, "not_found", "no such item")
        if result != "cancelled":
            fresh = self.server.store.get(item_id)
            if fresh is not None:
                return self._terminal_cancel(fresh)
            return self._error(409, "conflict", "item is not pending")
        fresh = self.server.store.get(item_id)
        return self._send(200, {
            "item": fresh.public() if fresh else {"id": item_id, "status": "cancelled"},
            "retraction": {"outcome": outcome, "admission_id": aid},
        })

    def _refuse(self, status: int, code: str, detail: str, item_id: str, outcome: str, aid: str | None):
        fresh = self.server.store.get(item_id)
        self._send(status, {
            "error": {"code": code, "detail": detail},
            "item": fresh.public() if fresh else {"id": item_id},
            "retraction": {"outcome": outcome, "admission_id": aid},
        })

    def _boundary_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urllib.parse.urlsplit(origin)
            if parsed.hostname not in ("127.0.0.1", "localhost"):
                return False
        return True

    def _authed(self) -> bool:
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer "):
            return False
        given = header[7:]
        if len(given) != len(self.server.token):
            return False
        return secrets.compare_digest(given, self.server.token)

    def _split(self):
        parsed = urllib.parse.urlsplit(self.path)
        query = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        return parsed.path.rstrip("/") or "/", query

    def _body(self) -> dict | None:
        header = self.headers.get("Content-Length")
        if header is None or str(header).strip() == "":
            return {}
        try:
            length = int(str(header).strip())
        except ValueError:
            self._error(400, "invalid", "Content-Length must be an integer")
            return None
        if length < 0:
            self._error(400, "invalid", "Content-Length must not be negative")
            return None
        if length == 0:
            return {}
        if length > MAX_BODY_BYTES:
            self._error(413, "invalid", "request body is too large")
            return None
        try:
            self.connection.settimeout(BODY_READ_SECONDS)
            raw = self.rfile.read(length)
        except (TimeoutError, socket.timeout, OSError):
            self.close_connection = True
            self._error(408, "invalid", "request body read timed out")
            return None
        if len(raw) != length:
            self._error(400, "invalid", "request body ended early")
            return None
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error(400, "invalid", "request body must be a JSON object")
            return None
        if not isinstance(value, dict):
            self._error(400, "invalid", "request body must be a JSON object")
            return None
        return value

    def _send(self, status: int, payload: dict):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status: int, code: str, detail: str):
        self._send(status, {"error": {"code": code, "detail": detail}})


def _admission_of(receipt) -> str | None:
    if not isinstance(receipt, dict):
        return None
    aid = receipt.get("inbox_id") or receipt.get("admission_id")
    return aid if isinstance(aid, str) and aid else None


def _host_status(adapter) -> dict:
    status = getattr(adapter, "host_status", None)
    if callable(status):
        return status()
    return {"adapter": getattr(adapter, "name", "unknown"), "mode": "generic-opencode"}


def _cancel_receipt(item, occ):
    occ_receipt = occ.receipt if occ is not None else None
    if _admission_of(occ_receipt):
        return occ_receipt
    if _admission_of(item.receipt if item is not None else None):
        return item.receipt
    return occ_receipt or (item.receipt if item is not None else None)


def _cancel_admission_id(item, occ) -> str | None:
    return _admission_of(_cancel_receipt(item, occ))


def _confirmed(receipt) -> bool:
    if not isinstance(receipt, dict):
        return False
    if receipt.get("inbox_id"):
        return True
    return receipt.get("state") in ("admitted", LOST)


def _admission_consumed(store, item_id: str, aid: str) -> bool:
    item = store.get(item_id)
    if item is not None and _receipt_has(item.receipt, aid) and (
        item.status in ("delivered", "completed") or (item.receipt or {}).get("state") == CONSUMED
    ):
        return True
    for occ in store.list_occurrences(item_id):
        if _receipt_has(occ.receipt, aid) and (
            occ.status == "delivered" or (occ.receipt or {}).get("state") == CONSUMED
        ):
            return True
    return False


def _receipt_has(receipt, aid: str) -> bool:
    if not isinstance(receipt, dict):
        return False
    return receipt.get("admission_id") == aid or receipt.get("inbox_id") == aid


def _with_counts(item: Item, counts: dict, opens: dict | None = None) -> dict:
    body = item.public()
    body["occurrences"] = counts.get(item.id, {})
    body["delivery_state"] = (item.receipt or {}).get("state")
    if opens and item.id in opens:
        body["open_occurrence"] = opens[item.id]
    return body
