"""Delivery adapters.

OpenCode admits a prompt with ``delivery=queue`` or ``delivery=steer`` in one
call. That admission is the race-free handoff: the server parks a queued
prompt until the current turn ends, and a steer may arrive mid-turn. Docket
does not check whether the session is busy and then send.

A caller-supplied ``msg_`` id is the stable admission. Probed against the
local OpenCode server (2.0.23): a retry of the same id returns the original
admission and does not change its text or add a second inbox item. After the
inbox item is cancelled, that id can be admitted again, so a confirmed
admission that later vanishes is not retried. The recovery core requires a saved
user message with that id and exact frozen text, not an id-only lookup or a scan
of recent messages.

Cancellation drops that same id. A receipt can persist the id with state
``sending`` before ``inbox_id`` is saved. The id is still the inbox key.
A delete that the host does not honor, a consumed transcript row, or a lookup
that cannot be read is not a successful retraction.

An optional archive guard can reject sessions archived only in an OpenChamber
overlay. The guard is not required, and this module does not start OpenChamber.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class DeliveryError(Exception):
    def __init__(self, code: str, detail: str = "", *, retryable: bool = False,
                 not_dispatched: bool = False):
        from docket.delivery_state import issue
        safe = issue(code)["detail"]
        super().__init__(safe)
        self.code = issue(code)["code"]
        self.detail = safe
        self.retryable = retryable
        self.not_dispatched = not_dispatched


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


@dataclass
class Validation:
    ok: bool
    code: str
    detail: str = ""

    @classmethod
    def good(cls) -> "Validation":
        return cls(True, "ok")

    @classmethod
    def bad(cls, code: str, detail: str) -> "Validation":
        from docket.delivery_state import issue
        diagnostic = issue(code)
        return cls(False, diagnostic["code"], diagnostic["detail"])


@dataclass
class Receipt:
    inbox_id: str | None
    delivery: str
    admission_id: str | None = None
    state: str = "admitted"
    consumed: bool = False
    raw: dict = field(default_factory=dict)


RETRACTED = "retracted"
CONSUMED = "consumed"
MISSING = "missing"
LOST = "lost"
AMBIGUOUS = "ambiguous"
FAILED = "failed"
_HOST_STATES = frozenset((CONSUMED, "admitted", MISSING))


def retract_admission(adapter, target: dict | None, admission_id: str | None, *,
                      confirmed: bool) -> str:
    """Drop one unconsumed admission, or say why that did not happen.

    ``confirmed`` means Docket already recorded that the host accepted this
    id. A confirmed id that is now absent is ``lost``, not a removal we
    performed. An unconfirmed id that is absent is ``missing``: nothing was
    on the host at the lookup. A delete is still attempted first, because an
    accept can land between the lookup and the delete.

    Outcomes are ``retracted``, ``consumed``, ``missing``, ``lost``,
    ``ambiguous``, and ``failed``. This never mints a new id.
    """
    if not admission_id or not target or not target.get("session_id"):
        return AMBIGUOUS
    before = _read_admission(adapter, target, admission_id)
    if before == "unavailable":
        return AMBIGUOUS
    if before == CONSUMED:
        return CONSUMED
    _delete_admission(adapter, target, admission_id)
    after = _read_admission(adapter, target, admission_id)
    if after == CONSUMED:
        return CONSUMED
    if after == "admitted":
        return FAILED
    if after == "unavailable":
        return AMBIGUOUS
    if before == "admitted":
        return RETRACTED
    return LOST if confirmed else MISSING


def _read_admission(adapter, target: dict, admission_id: str) -> str:
    try:
        found = adapter.reconcile(target, admission_id)
    except Exception:
        return "unavailable"
    if found in _HOST_STATES:
        return found
    return "unavailable"


def _delete_admission(adapter, target: dict, admission_id: str) -> None:
    try:
        adapter.cancel_admission(target, admission_id)
    except Exception:
        return


class DeliveryAdapter:
    name = "base"

    def validate_frozen(self, frozen: dict) -> Validation:
        return self.validate(frozen["target"], expected_binding=frozen["binding"])

    def validate(self, target: dict, *, expected_binding=None) -> Validation:
        raise NotImplementedError

    def reconcile(self, target: dict, admission_id: str) -> str:
        """Return consumed, admitted, or missing. Raise if the host cannot be read."""
        raise NotImplementedError

    def deliver(self, target: dict, message: str, *, urgent: bool,
                admission_id: str | None = None) -> Receipt:
        raise NotImplementedError

    def cancel_admission(self, target: dict, inbox_id: str | None) -> None:
        return None


class OpenCodeAdapter(DeliveryAdapter):
    """Wakes an OpenCode / OpenChamber session through the local server."""

    name = "opencode"

    def __init__(self, discovery=None, timeout: float = 10, archive=None):
        self._discovery = discovery or discover_opencode
        self.timeout = timeout
        self.archive = archive
        self.calls: list[str] = []

    def binding_snapshot(self) -> dict:
        base, _auth = self._discovery()
        return {"adapter": self.name, "mode": "generic-opencode",
                "origin": _opencode_origin(base), "namespace_verified": False}

    def deliver_frozen(self, frozen: dict) -> Receipt:
        body = json.loads(frozen["body"])
        status, response = self._request(
            "POST", f"/api/session/{frozen['target']['session_id']}/prompt", body,
            expected_binding=frozen["binding"],
        )
        # Any result after dispatch is potentially accepted, including malformed JSON,
        # conflicts and 5xx. The scheduler never replays it on this host contract.
        if status != 200:
            raise DeliveryError("conflict" if status == 409 else "unavailable")
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, dict) or data.get("id") != body["id"]:
            raise DeliveryError("acceptance_unknown")
        return Receipt(body["id"], body["delivery"], admission_id=body["id"])

    def status_frozen(self, frozen: dict) -> str:
        """Only exact saved user text is consumption evidence, never an ID alone."""
        body = json.loads(frozen["body"])
        target = frozen["target"]
        status, response = self._request(
            "GET", f"/api/session/{target['session_id']}/message/{body['id']}",
            expected_binding=frozen["binding"],
        )
        if status == 200:
            data = response.get("data") if isinstance(response, dict) else None
            if (isinstance(data, dict) and data.get("id") == body["id"]
                    and data.get("type") == "user" and data.get("text") == body["text"]):
                # Released 2.0.32 saved user message: {id, time, text, type}.
                # Session identity is the exact bound route, not an invented field.
                return "consumed"
            # Unknown/weak transcript shape must not be upgraded to delivery proof.
            raise DeliveryError("acceptance_unknown")
        if status != 404:
            raise DeliveryError("unavailable")
        status, response = self._request(
            "GET", f"/api/session/{target['session_id']}/inbox", expected_binding=frozen["binding"],
        )
        items = response.get("data") if isinstance(response, dict) else None
        if status != 200 or not isinstance(items, list):
            raise DeliveryError("unavailable")
        if any(isinstance(item, dict) and item.get("id") == body["id"] for item in items):
            return "admitted"
        return "missing"

    def cancel_frozen(self, frozen: dict) -> None:
        body = json.loads(frozen["body"])
        self._request("DELETE", f"/api/session/{frozen['target']['session_id']}/inbox/{body['id']}",
                      expected_binding=frozen["binding"])

    def validate(self, target: dict, *, expected_binding=None) -> Validation:
        session_id = target.get("session_id") or ""
        try:
            status, body = self._request("GET", f"/api/session/{session_id}", expected_binding=expected_binding)
        except DeliveryError as exc:
            if exc.retryable:
                return Validation.bad("unavailable", exc.detail)
            return Validation.bad(exc.code, exc.detail)
        if status == 404:
            return Validation.bad("bad_target", "session not found")
        if status != 200:
            return Validation.bad("unavailable", f"session lookup failed ({status})")
        data = (body or {}).get("data") or {}
        archived = (data.get("time") or {}).get("archived")
        if archived:
            return Validation.bad("archived", "session is archived")
        overlay = self._overlay(session_id)
        if overlay == "archived":
            return Validation.bad("archived", "session is archived in the OpenChamber overlay")
        if overlay == "unreadable":
            return Validation.bad("unavailable", "OpenChamber archive overlay could not be read")
        expected = target.get("directory")
        if expected:
            actual = ((data.get("location") or {}).get("directory")) or ""
            try:
                same = _same_dir(expected, actual)
            except OSError:
                return Validation.bad(
                    "unavailable",
                    "session directory identity could not be verified",
                )
            if not same:
                return Validation.bad(
                    "wrong_target",
                    "session is not in the directory this item was set for",
                )
        return Validation.good()

    def session_goal(self, session_id: str) -> dict:
        """Read native goal metadata through the configured (including paired) adapter."""
        status, body = self._request("GET", f"/api/session/{session_id}")
        if status != 200 or not isinstance(body, dict):
            raise DeliveryError("unavailable", "goal lookup unavailable", retryable=True)
        data = body.get("data") or {}
        if (data.get("time") or {}).get("archived"):
            return {}
        return ((data.get("metadata") or {}).get("openchamber") or {}).get("goal") or {}

    def session_state(self, session_id: str) -> str:
        """Return active, idle, missing, or archived.

        Two small GETs. Message history is not read. A lookup failure raises
        DeliveryError so a watch does not treat an outage as idle.
        """
        status, body = self._request("GET", f"/api/session/{session_id}")
        if status == 404:
            return "missing"
        if status != 200:
            raise DeliveryError("unavailable", f"session lookup failed ({status})", retryable=True)
        data = (body or {}).get("data") or {}
        if (data.get("time") or {}).get("archived"):
            return "archived"
        overlay = self._overlay(session_id)
        if overlay == "archived":
            return "archived"
        if overlay == "unreadable":
            raise DeliveryError(
                "unavailable", "OpenChamber archive overlay could not be read", retryable=True,
            )
        status, active = self._request("GET", "/api/session/active")
        if status != 200:
            raise DeliveryError("unavailable", f"active lookup failed ({status})", retryable=True)
        listed = (active or {}).get("data") or {}
        if isinstance(listed, dict) and session_id in listed:
            return "active"
        if isinstance(listed, list) and session_id in listed:
            return "active"
        return "idle"

    def reconcile(self, target: dict, admission_id: str) -> str:
        """Return consumed, admitted, or missing for one stable prompt id.

        A 404 on the message is "not in the transcript yet", not a missing
        session. The inbox lookup distinguishes those. Recent message text is
        not scanned.
        """
        session_id = target["session_id"]
        status, body = self._request(
            "GET", f"/api/session/{session_id}/message/{admission_id}",
        )
        if status == 200:
            data = (body or {}).get("data") if isinstance(body, dict) else None
            if isinstance(data, dict) and data.get("id") == admission_id:
                return "consumed"
        elif status != 404:
            raise DeliveryError(
                "unavailable", f"message lookup failed ({status})", retryable=True,
            )
        status, inbox = self._request("GET", f"/api/session/{session_id}/inbox")
        if status == 404:
            raise DeliveryError("bad_target", "session not found")
        if status != 200:
            raise DeliveryError(
                "unavailable", f"inbox lookup failed ({status})", retryable=True,
            )
        items = (inbox or {}).get("data") if isinstance(inbox, dict) else None
        if isinstance(items, list) and any(
            isinstance(item, dict) and item.get("id") == admission_id for item in items
        ):
            return "admitted"
        return "missing"

    def deliver(self, target: dict, message: str, *, urgent: bool,
                admission_id: str | None = None) -> Receipt:
        # One admission of a durable id. queue waits out a running turn; steer
        # may enter it. resume schedules the loop when the session is idle.
        # A 200 here is admission, not proof the session has consumed the text.
        from docket.parse import new_admission_id

        mode = "steer" if urgent else "queue"
        admission_id = admission_id or new_admission_id()
        status, body = self._request(
            "POST",
            f"/api/session/{target['session_id']}/prompt",
            {
                "id": admission_id,
                "text": message,
                "delivery": mode,
                "resume": True,
            },
        )
        if status == 404:
            raise DeliveryError("bad_target", "session not found")
        if status == 409:
            raise DeliveryError("conflict", "admission id already exists", retryable=False)
        if status != 200:
            raise DeliveryError(
                "unavailable", f"prompt failed ({status})",
                retryable=status >= 500 or status == 401,
            )
        data = (body or {}).get("data") or {}
        returned = data.get("id") or admission_id
        if returned != admission_id:
            raise DeliveryError("conflict", "host admission id did not match", retryable=False)
        return Receipt(
            inbox_id=returned,
            admission_id=returned,
            delivery=data.get("delivery") or mode,
            state="admitted",
            consumed=False,
            raw={"id": returned, "delivery": data.get("delivery") or mode},
        )

    def _overlay(self, session_id: str) -> str:
        if self.archive is None:
            return "clear"
        try:
            result = self.archive.check(session_id)
        except Exception:
            return "unreadable"
        if result not in ("archived", "clear", "unreadable"):
            return "unreadable"
        return result

    def cancel_admission(self, target: dict, inbox_id: str | None) -> None:
        """Ask the host to drop one inbox id. A return is not proof it is gone.

        Callers reconcile afterwards. A transport error is swallowed here so a
        scheduler tick is not killed by the host; the follow-up lookup is the
        outcome. The id is the stable admission id, including a sending
        receipt that has not yet stored ``inbox_id``.
        """
        if not inbox_id:
            return
        try:
            self._request(
                "DELETE",
                f"/api/session/{target['session_id']}/inbox/{inbox_id}",
            )
        except DeliveryError:
            return

    def _request(self, method: str, path: str, body: dict | None = None, *, expected_binding=None):
        base, auth = self._discovery()
        # Validate even injected discovery; neither runtime requests nor metadata use raw bases.
        self._host_origin = None
        base = _opencode_origin(base)
        if expected_binding is not None and expected_binding != {
            "adapter": self.name, "mode": "generic-opencode", "origin": base,
            "namespace_verified": False,
        }:
            raise DeliveryError("backend_unverifiable", not_dispatched=True)
        self._host_origin = base
        self.calls.append(f"{method} {path}")
        url = base.rstrip("/") + path
        data = None if body is None else json.dumps(body, sort_keys=True).encode()
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": auth,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            # A prepared binding must not be redirected or routed through an ambient proxy.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
            with opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                parsed = json.loads(raw) if raw else None
                return response.status, parsed
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                parsed = None
            if exc.code in (401, 503):
                raise DeliveryError(
                    "unavailable", f"opencode returned {exc.code}", retryable=True
                ) from None
            return exc.code, parsed
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DeliveryError(
                "unavailable", "opencode server is not reachable", retryable=True
            ) from exc

    def host_status(self) -> dict:
        return {"adapter": self.name, "mode": "generic-opencode",
                "origin": getattr(self, "_host_origin", None)}


def discover_opencode() -> tuple[str, str]:
    """Return (base_url, Authorization header). Never log the password."""
    state = _read_json(Path("~/.local/state/opencode/service.json").expanduser())
    config = _read_json(Path("~/.config/opencode/service.json").expanduser())
    url = (state or {}).get("url") or ""
    password = (state or {}).get("password") or (config or {}).get("password") or ""
    if not url:
        url = _status_url()
    if not url or not password:
        raise DeliveryError(
            "unavailable",
            "OpenCode service URL or credential is not available",
            retryable=True,
        )
    _assert_loopback(url)
    token = base64.b64encode(f"opencode:{password}".encode()).decode()
    return url, "Basic " + token


def _status_url() -> str:
    try:
        done = subprocess.run(
            ["opencode", "service", "status"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in (done.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("http://") or line.startswith("https://"):
            return line
    return ""


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _assert_loopback(url: str) -> None:
    _opencode_origin(url)


def _opencode_origin(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme not in ("http", "https") or host not in ("127.0.0.1", "localhost", "::1")
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                or (port is not None and not 0 < port < 65536)):
            raise ValueError
        authority = "[::1]" if host == "::1" else host
        origin = f"{parsed.scheme}://{authority}" + (f":{port}" if port is not None else "")
        if url not in (origin, origin + "/"):
            raise ValueError
        return origin
    except (ValueError, TypeError, AttributeError):
        raise DeliveryError("unavailable", "OpenCode URL must be a canonical loopback origin") from None


def _same_dir(expected: str, actual: str) -> bool:
    from docket.platform import same_directory
    return same_directory(expected, actual)


def _contains(value, marker: str) -> bool:
    if isinstance(value, str):
        return marker in value
    if isinstance(value, dict):
        return any(_contains(item, marker) for item in value.values())
    if isinstance(value, list):
        return any(_contains(item, marker) for item in value)
    return False


def scrub(text: str, secrets_to_hide: tuple[str, ...] = ()) -> str:
    redacted = text
    for secret in secrets_to_hide:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted
