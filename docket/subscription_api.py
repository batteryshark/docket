"""Opt-in candidate HTTP adapter and offline installed-config loader.

Not imported by Docket's daemon/CLI. No auto-configuration, default path, token
creation, scheduler start or live migration. Existing routes use existing admin
authentication; only /api/v1/events uses a separately installed producer token.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat

from docket.api import BODY_READ_SECONDS, DocketServer, Handler
from docket.subscriptions import SubscriptionRefusal, SubscriptionStore

MAX_EVENT_BYTES = 16384
MAX_CONFIG_BYTES = 65536
ERROR_STATUS = {"invalid": 400, "unauthorized": 401, "forbidden": 403,
                "conflict": 409, "capacity": 429}


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _decode(raw):
    def reject_constant(_):
        raise ValueError("non-finite JSON value")
    return json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=reject_constant)


def load_installed(store: SubscriptionStore, path: Path):
    """Explicit administrative operation, never reachable through HTTP.

    Read only a regular, current-user-owned private file without following a final
    symlink. The deployment owner must also control its parent path. Hashes, not
    raw producer credentials, belong in this manifest. Any change needs an explicit
    upgrade; reopening the same manifest does not clear revocation.
    """
    path = Path(path)
    if not path.is_absolute() or not hasattr(os, "O_NOFOLLOW"):
        raise SubscriptionRefusal("private absolute config path required")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            before = os.fstat(file.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_mode & 0o077 or before.st_size > MAX_CONFIG_BYTES):
                raise SubscriptionRefusal("config must be a bounded private owned regular file")
            raw = file.read(MAX_CONFIG_BYTES + 1)
            after = os.fstat(file.fileno())
            if (len(raw) > MAX_CONFIG_BYTES or
                    (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise SubscriptionRefusal("config changed while reading")
    except OSError:
        raise SubscriptionRefusal("installed config unavailable") from None
    try:
        manifest = _decode(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise SubscriptionRefusal("invalid installed JSON") from None
    store.install_manifest(manifest)


class SubscriptionServer(DocketServer):
    """Explicitly constructed candidate; feature disabled by default.

    With enabled=False, even an ordinary Store is valid: no new tables are created
    by this adapter. Enabling requires an already explicitly configured candidate.
    Normal timer/alarm/watch routes retain Docket's original behavior.
    """

    def __init__(self, store, scheduler, token, host="127.0.0.1", port=8768, *, enabled=False):
        if type(enabled) is not bool:
            raise SubscriptionRefusal("enabled must be an explicit boolean")
        if enabled and (not isinstance(store, SubscriptionStore) or not store.configured()):
            raise SubscriptionRefusal("subscription feature unconfigured")
        self.subscriptions_enabled = enabled
        super().__init__(store, scheduler, token, host=host, port=port)
        self.RequestHandlerClass = SubscriptionHandler


class SubscriptionHandler(Handler):
    def do_POST(self):
        if self.path != "/api/v1/events":
            return super().do_POST()
        # Close this request's connection, including early auth/framing refusals:
        # an unread body must never become another keep-alive HTTP request.
        self.close_connection = True
        if not self._boundary_ok():
            return self._error(403, "forbidden", "loopback only")
        if not self.server.subscriptions_enabled:
            return self._error(503, "disabled", "event intake is disabled")
        headers = self.headers.get_all("Authorization", [])
        if len(headers) != 1 or not headers[0].startswith("Bearer "):
            return self._error(401, "unauthorized", "producer bearer required")
        token = headers[0][7:]
        if not token.isascii() or not 32 <= len(token) <= 512 or any(c.isspace() for c in token):
            return self._error(401, "unauthorized", "invalid producer bearer")
        try:
            self.server.store.authenticate(token)
        except SubscriptionRefusal:
            return self._error(401, "unauthorized", "producer authentication refused")
        except (sqlite3.Error, OSError):
            return self._error(503, "unavailable", "event store unavailable")
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1:
            return self._error(400, "invalid", "one Content-Length and no transfer encoding required")
        try:
            length = int(lengths[0])
        except ValueError:
            return self._error(400, "invalid", "invalid Content-Length")
        if length <= 0:
            return self._error(400, "invalid", "JSON event body required")
        if length > MAX_EVENT_BYTES:
            return self._error(413, "too_large", "event body too large")
        if self.headers.get_content_type() != "application/json":
            return self._error(415, "invalid", "application/json required")
        try:
            self.connection.settimeout(BODY_READ_SECONDS)
            raw = self.rfile.read(length)
        except OSError:
            return self._error(408, "invalid", "event body read timed out")
        if len(raw) != length:
            return self._error(400, "invalid", "event body ended early")
        try:
            event = _decode(raw)
        except (ValueError, UnicodeError, RecursionError):
            return self._error(400, "invalid", "invalid JSON event")
        try:
            receipt = self.server.store.accept(token, event)
        except SubscriptionRefusal as error:
            return self._error(ERROR_STATUS[error.code], error.code, str(error))
        except (sqlite3.Error, OSError):
            # Commit/ack uncertainty: caller must retry SAME event identity, never
            # invent a new ID. No payload/credential/database detail is disclosed.
            return self._error(503, "unavailable", "acceptance unconfirmed; retry the same event identity")
        return self._send(202, {"delivery": receipt})
