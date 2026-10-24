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
from docket.scheduler import log_scheduler_error
from docket.store import OUTSTANDING, Item
from docket.delivery_state import issue, project, receipt_projection

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8768
MAX_BODY_BYTES = 65536
BODY_READ_SECONDS = 5.0
# Former event intake, inspection, and journal paths. They 404 before auth so a
# leftover producer or channel credential cannot be checked or replay an admission.
_RETIRED_EXACT = frozenset({"/api/v1/events", "/v1/actions/record"})


def _retired_event_path(raw: str) -> bool:
    path = urllib.parse.urlsplit(raw).path.rstrip("/") or "/"
    return path in _RETIRED_EXACT or path.startswith("/api/v1/events/")


def _raw_request_path(handler):
    """Real HTTP requests have path. Direct handler fixtures supply _split instead."""
    raw = getattr(handler, "path", None)
    return raw if isinstance(raw, str) else None


class DocketServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        from docket.platform import is_windows
        if is_windows():
            import socket
            # SO_REUSEADDR on Windows can allow another listener to steal the
            # address. Set exclusive use before bind; never enable reuse there.
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        return super().server_bind()

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
        raw = _raw_request_path(self)
        if raw is not None and _retired_event_path(raw):
            return self._error(404, "not_found", "no such path")
        if not self._boundary_ok():
            return self._error(403, "forbidden", "loopback only")
        path, query = self._split()
        if raw is None and _retired_event_path(path):
            return self._error(404, "not_found", "no such path")
        if path == "/api/v1/tower/project-index":
            if not self._authed():
                return self._error(401, "unauthorized", "admin bearer required; use the normal Docket token")
            if urllib.parse.urlsplit(self.path).query:
                return self._error(400, "invalid", "no query selectors allowed; use the fixed endpoint")
            from docket.project_index import projection
            try:
                payload = projection(self.server.scheduler.checkins, self.server.scheduler.clock.now())
            except Exception:
                return self._error(503, "unavailable", "index source unavailable; restore the valid private source before retrying")
            return self._send(200, payload)
        if path in ("/api/v1/tower/project-markers", "/api/v1/tower/snapshot-refresh-schedule"):
            if not self._authed():
                return self._error(401, "unauthorized", "admin bearer required; use the normal Docket token")
            from docket.tower_clock import projection, view
            if query:
                return self._error(400, "invalid", "no query selectors allowed; use the fixed endpoint")
            if path.endswith("project-markers"):
                try:
                    payload = projection(self.server.scheduler.checkins.roster_path, self.server.scheduler.clock.now())
                except Exception:
                    return self._error(503, "unavailable", "marker source unavailable; restore the valid private source before retrying")
                return self._send(200, payload)
            return self._send(200, view(self.server.store, self.server.scheduler.clock.now()))
        if "view" in query:
            return self._panel_read(path, query)
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
                "delivery_contract": {"version": 1, "writer_format": 1,
                    "operations": ["reconcile", "retry"],
                    "retry_policy": "one explicit same-unit authorization; no budget reset",
                    "opencode_replay_after_possible_dispatch": False,
                    "same_daemon_pre_dispatch_recovery": True},
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
            bodies = [_public_item(self.server.store, item) for item in items]
            if query.get("outcome"):
                bodies = [body for body in bodies if body["delivery"]["outcome"] == query["outcome"]]
            return self._send(200, {"items": bodies})
        if path == "/api/v1/occurrences":
            status = query.get("status")
            if status == "all":
                status = None
            found = self.server.store.list_occurrences(status=status)
            bodies = [_public_occurrence(self.server.store, occ) for occ in found]
            if query.get("outcome"):
                bodies = [body for body in bodies if body["delivery"]["outcome"] == query["outcome"]]
            return self._send(200, {"occurrences": bodies})
        if path.startswith("/api/v1/items/"):
            rest = path[len("/api/v1/items/"):]
            if "/" in rest:
                item_id, action = rest.split("/", 1)
                if action == "occurrences":
                    if self.server.store.get(item_id) is None:
                        return self._error(404, "not_found", "no such item")
                    found = self.server.store.list_occurrences(item_id)
                    return self._send(200, {"occurrences": [_public_occurrence(self.server.store, occ) for occ in found]})
                return self._error(404, "not_found", "no such path")
            item = self.server.store.get(rest)
            if item is None:
                return self._error(404, "not_found", "no such item")
            body = _public_item(self.server.store, item)
            recent = self.server.store.list_occurrences(item.id, limit=10)
            body["recent_occurrences"] = [_public_occurrence(self.server.store, occ) for occ in recent]
            return self._send(200, {"item": body})
        return self._error(404, "not_found", "no such path")

    def do_POST(self):
        raw = _raw_request_path(self)
        if raw is not None and _retired_event_path(raw):
            # Do not read a body or compare a bearer. Closing avoids turning the
            # unread bytes into a following keep-alive request.
            self.close_connection = True
            return self._error(404, "not_found", "no such path")
        if not self._boundary_ok():
            return self._error(403, "forbidden", "loopback only")
        if not self._authed():
            return self._error(401, "unauthorized", "bearer token required")
        path, _query = self._split()
        if raw is None and _retired_event_path(path):
            self.close_connection = True
            return self._error(404, "not_found", "no such path")
        body = self._body()
        if body is None:
            return
        if path in ("/api/v1/tower/snapshot-refresh-schedule", "/api/v1/tower/snapshot-refresh-schedule/cancel"):
            from docket.tower_clock import mutate, ClockError
            try:
                with self.server._mutate, self.server.store._lock:
                    payload = mutate(self.server.scheduler, body, cancel=path.endswith("/cancel"))
            except ClockError as exc:
                return self._error(exc.status, exc.code, exc.detail)
            except LimitError:
                return self._error(409, "limit", "schedule capacity exhausted; release capacity before ensure")
            return self._send(200, payload)
        for prefix, is_occurrence in (("/api/v1/items/", False), ("/api/v1/occurrences/", True)):
            if path.startswith(prefix):
                rest = path[len(prefix):].split("/")
                if len(rest) == 2 and rest[1] in ("reconcile", "retry"):
                    if body:
                        return self._error(400, "invalid", issue("invalid")["detail"])
                    return self._recover(rest[0], rest[1], is_occurrence)
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

    def _panel_read(self, path, query):
        # Opt-in only. No caller-selected fields, order, limits or outcome filters.
        pairs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
        if (query.get("view") != "panel" or set(pairs) - {"view", "status"}
                or any(len(values) != 1 for values in pairs.values())):
            return self._error(400, "invalid", "unsupported panel query")
        status = query.get("status")
        if path == "/api/v1/items" and status in ("outstanding", "terminal"):
            items, truncated = self.server.store.panel_items(status)
            limit = 200 if status == "outstanding" else 40
            order = "fire_at,id" if status == "outstanding" else "updated_at_desc,id_desc"
            return self._send(200, {"items": [_panel_item(self.server.store, item) for item in items],
                                    "window": _panel_window(limit, order, truncated)})
        if path == "/api/v1/occurrences" and status == "all":
            rows, truncated = self.server.store.panel_occurrences()
            return self._send(200, {"occurrences": [_panel_occurrence(self.server.store, row) for row in rows],
                                    "window": _panel_window(40, "updated_at_desc,id_desc", truncated)})
        if path.startswith("/api/v1/items/") and "/" not in path[len("/api/v1/items/"):] and set(pairs) == {"view"}:
            item = self.server.store.get(path[len("/api/v1/items/"):])
            if item is None:
                return self._error(404, "not_found", "no such item")
            return self._send(200, {"item": _panel_item(self.server.store, item)})
        return self._error(400, "invalid", "unsupported panel query")

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
        selection = body.get("adapter", "opencode")
        if selection not in ("opencode", "pi"):
            return self._error(400, "unsupported_capability", issue("unsupported_capability")["detail"])
        adapter = self.server.scheduler.adapter_for_target({"adapter": selection})
        pi_target = selection == "pi"
        bad = adapter.validate_target_id(session_id) if pi_target else validate_session_id(session_id)
        if bad:
            return self._error(400, "bad_target", bad)
        directory = body.get("directory")
        if directory is not None and (not isinstance(directory, str) or not directory.strip()):
            return self._error(400, "invalid", "directory must be a path")
        target = {
            "adapter": "pi" if pi_target else "opencode",
            "session_id": session_id,
        }
        if directory:
            target["directory"] = directory
        if pi_target:
            if directory or (kind == "watch" and body.get("check") == "session_idle"):
                return self._error(400, "unsupported_capability", issue("unsupported_capability")["detail"])
            try:
                target["provider_binding"] = adapter.binding_snapshot()
            except Exception:
                return self._error(503, "backend_unverifiable", issue("backend_unverifiable")["detail"])
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
        check = adapter.validate(target)
        if not check.ok:
            status = 503 if check.code == "unavailable" else 400
            return self._error(status, check.code, check.detail)
        target["host"] = _host_status(adapter)
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
        return self._send(201, {"item": _public_item(self.server.store, item)})

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
        except Exception:
            return self._error(503, "unavailable", issue("unavailable")["detail"])
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
                code = "already_consumed" if item.status == "delivered" else "schedule_finished"
                return self._send(409, {"error": issue(code), "item": _public_item(self.server.store, item),
                                        "schedule_stopped": item.status == "cancelled",
                                        "retraction": {"outcome": "uncertain"}})
            outcome = self.server.scheduler.stop(item)
            fresh = self.server.store.get(item_id)
            return self._send(200, {"item": _public_item(self.server.store, fresh), "schedule_stopped": True,
                                    "retraction": {"outcome": outcome}})

    def _recover(self, ident, action, is_occurrence):
        with self.server._mutate:
            occ = self.server.store.get_occurrence(ident) if is_occurrence else None
            item = self.server.store.get(occ.item_id if occ else ident) if (occ or not is_occurrence) else None
            if not item:
                return self._error(404, "not_found", "no such delivery unit")
            if not is_occurrence and item.kind in ("watch", "recurring"):
                return self._send(409, {"error": issue("not_ready"), "item": _public_item(self.server.store, item)})
            status, code = self.server.scheduler.recover(item, occ, action)
            fresh = self.server.store.get(item.id)
            body = {"item": _public_item(self.server.store, fresh), "operation": action,
                    "authorized": status == 202}
            if occ:
                body["occurrence"] = _public_occurrence(self.server.store, self.server.store.get_occurrence(occ.id))
            if code:
                body["error"] = issue(code, phase=action, certainty="unknown")
            return self._send(status, body)


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
        # Preserve an explicitly empty new opt-in parameter so it is rejected;
        # retain historical blank handling for every existing parameter.
        views = urllib.parse.parse_qs(parsed.query, keep_blank_values=True).get("view")
        if views is not None:
            query["view"] = views[-1]
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


def _host_status(adapter) -> dict:
    status = getattr(adapter, "host_status", None)
    if callable(status):
        return status()
    return {"adapter": getattr(adapter, "name", "unknown"), "mode": "generic-opencode"}


def _with_counts(item: Item, counts: dict, opens: dict | None = None) -> dict:
    body = item.public()
    body["occurrences"] = counts.get(item.id, {})
    body["delivery_state"] = receipt_projection(item.receipt, malformed=item.receipt_malformed)["state"]
    if opens and item.id in opens:
        body["open_occurrence"] = opens[item.id]
    return body


def _safe_legacy(body, delivery, *, receipt_malformed=False):
    if isinstance(body.get("target"), dict):
        body["target"] = {key: value for key, value in body["target"].items() if key != "provider_binding"}
    diagnostic = delivery.get("issue") or delivery.get("check_issue")
    if body.get("error"):
        body["error"] = diagnostic["detail"] if diagnostic else issue("unavailable")["detail"]
    receipt = body.get("receipt")
    typed = receipt_projection(receipt, malformed=receipt_malformed)
    body["receipt_issue"] = typed["issue"]
    if isinstance(receipt, dict):
        body["receipt"] = {key: typed[key] for key in ("admission_id", "inbox_id", "delivery", "state")
                           if key in receipt}
    elif receipt is not None:
        body["receipt"] = None
    body["delivery"] = delivery
    return body


def _public_item(store, item):
    occ = store.open_occurrence(item)
    body = _with_counts(item, store.occurrence_counts(), store.open_summaries())
    body = _safe_legacy(body, project(store, item, occ), receipt_malformed=item.receipt_malformed)
    # open_summaries sanitizes every row, even if this parent's pointer is broken.
    return _item_delivery_summary(store, item, body)


def _item_delivery_summary(store, item, body):
    if item.kind == "recurring":
        summary = {}
        for row in store.list_occurrences(item.id):
            key = project(store, item, row)["outcome"]
            summary[key] = summary.get(key, 0) + 1
        body["delivery_summary"] = summary
        body["coalesced_slots"] = sum(row.coalesced for row in store.list_occurrences(item.id))
    return body


def _public_occurrence(store, occ):
    item = store.get(occ.item_id)
    return _safe_legacy(occ.public(), project(store, item, occ), receipt_malformed=occ.receipt_malformed)


def _panel_window(limit, order, truncated):
    return {"view": "panel", "limit": limit, "order": order, "truncated": truncated}


def _panel_compact(body, *, occurrence=False):
    """Bound display text only, using the shared typed delivery projection as truth.

    Fewer than 8,000 Unicode code points and 8,192 ASCII structural/numeric bytes
    per row: at most 12 * 8,000 + 8,192 bytes with json.dumps ensure_ascii=True.
    Context, host metadata, receipts and raw diagnostics are never serialized.
    """
    truncated = False

    def text(value, limit):
        nonlocal truncated
        if not isinstance(value, str):
            return None
        truncated |= len(value) > limit
        return value[:limit]

    def number(value):
        nonlocal truncated
        if value is None:
            return None
        if type(value) in (int, float) and -(2**53 - 1) <= value <= 2**53 - 1:
            return value
        truncated = True
        return None

    result = {key: text(body.get(key), limit) for key, limit in (
        ("id", 20), ("kind", 40), ("status", 40), ("fire_at", 50), ("updated_at", 50),
        ("reason", 2000), ("created_by", 300))}
    for key in ("urgent", "late"):
        result[key] = body.get(key)
    result["late_by_seconds"] = number(body.get("late_by_seconds"))
    if occurrence:
        result.update(item_id=text(body.get("item_id"), 20), seq=number(body.get("seq")),
                      scheduled_at=text(body.get("scheduled_at"), 50))
    else:
        target, spec = body.get("target") or {}, body.get("spec") or {}
        result["target"] = {"session_id": text(target.get("session_id"), 300),
                            "directory": text(target.get("directory"), 1000)}
        result["spec"] = {"check": text(spec.get("check"), 80), "every": number(spec.get("every"))}
        result["open_occurrence"] = {"id": text((body.get("open_occurrence") or {}).get("id"), 20)}
        summary = body.get("delivery_summary")
        result["delivery_summary"] = None if summary is None else {
            key: number(summary[key]) for key in (
                "pending", "admitted", "delivered", "failed", "uncertain", "cancelled", "not_applicable") if key in summary}
        result["coalesced_slots"] = number(body.get("coalesced_slots"))
    delivery = body["delivery"]
    result["delivery"] = {key: delivery[key] for key in (
        "facts", "stale", "namespace_verified", "same_daemon_recovery")}
    for key, limit in (("unit_id", 20), ("request_id", 36), ("outcome", 40), ("phase", 40),
                       ("execution", 40), ("evidence", 40), ("last_checked_at", 50)):
        result["delivery"][key] = text(delivery.get(key), limit)
    recovery = delivery["recovery"]
    result["delivery"]["recovery"] = {"reconcile": recovery["reconcile"], "retry": recovery["retry"],
                                        "refusal_code": text(recovery.get("refusal_code"), 40)}
    for key in ("issue", "check_issue", "receipt_issue"):
        diagnostic = delivery.get(key)
        result["delivery"][key] = None if diagnostic is None else {
            field: text(diagnostic.get(field), limit) for field, limit in (
                ("code", 40), ("detail", 500), ("phase", 40), ("admission_certainty", 40))}
    result["fields_truncated"] = truncated
    return result


def _panel_item(store, item):
    # Do not compute the unused global occurrence_counts grouped over all old
    # parents. Delivery and bounded recurring summary are the same shared reads
    # as the default view, without its compatibility/diagnostic containers.
    body = item.public()
    body["delivery"] = project(store, item, store.open_occurrence(item))
    body["open_occurrence"] = store.open_summaries().get(item.id)
    return _panel_compact(_item_delivery_summary(store, item, body))


def _panel_occurrence(store, occ):
    return _panel_compact(_public_occurrence(store, occ), occurrence=True)
