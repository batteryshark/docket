"""Host-neutral MCP server over stdio.

Speaks newline-delimited JSON-RPC and the older Content-Length framing.
Tools call the local Docket HTTP API. Nothing is written to stdout except
MCP messages.
"""

from __future__ import annotations

import json
import sys

from docket import __version__
from docket.client import ClientError, request

PROTOCOL = "2025-06-18"
KNOWN = {"2024-11-05", "2025-03-26", "2025-06-18"}

TOOLS = [
    {
        "name": "docket_timer",
        "description": "Set a relative timer that wakes an agent session with a reason. Does not run a command.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Target agent session id (ses_…)"},
                "in": {"description": "Delay in seconds, or a duration like 20m or 'in 20 minutes'"},
                "reason": {"type": "string"},
                "urgent": {"type": "boolean", "description": "If true, may arrive mid-turn. Default waits for the turn to end."},
                "context": {"type": "object"},
                "created_by": {"type": "string"},
                "directory": {"type": "string", "description": "Optional directory pin. A later move is a wrong target."},
            },
            "required": ["session_id", "in", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "docket_alarm",
        "description": "Set an absolute alarm (HH:MM local, or an ISO timestamp) that wakes an agent session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "at": {"type": "string"},
                "reason": {"type": "string"},
                "urgent": {"type": "boolean"},
                "context": {"type": "object"},
                "created_by": {"type": "string"},
                "directory": {"type": "string"},
            },
            "required": ["session_id", "at", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "docket_recurring",
        "description": "Set a bounded recurring wake. every is at least 60 seconds. A count or an until is required. Missed intervals while asleep coalesce into one late wake, not a storm. Does not run a command.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "every": {"description": "Interval in seconds, or a duration like 15m. Minimum 60 seconds."},
                "reason": {"type": "string"},
                "count": {"type": "integer", "description": "Maximum wakes. Required if until is omitted. Max 100."},
                "until": {"description": "Duration or ISO-8601 timestamp. Required if count is omitted."},
                "start": {"description": "First fire. Duration or ISO timestamp. Default is one interval from now."},
                "urgent": {"type": "boolean"},
                "context": {"type": "object"},
                "created_by": {"type": "string"},
                "directory": {"type": "string"},
            },
            "required": ["session_id", "every", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "docket_watch",
        "description": "Wait with a built-in read-only check, then wake the session once. Checks: file_appears, file_stable (file stops growing), process_ended, url_answers, session_idle. until is required. Does not run a command or notify the principal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session to wake"},
                "check": {"type": "string", "enum": ["file_appears", "file_stable", "process_ended", "url_answers", "session_idle"]},
                "until": {"description": "Duration or ISO-8601 timestamp. Required."},
                "reason": {"type": "string"},
                "every": {"description": "Poll interval. Defaults are conservative; URL polls are slower."},
                "path": {"type": "string", "description": "Absolute path for a file check"},
                "stable_for": {"description": "How long a file must stop changing, for file_stable"},
                "pid": {"type": "integer", "description": "Process id for process_ended. Identity is captured at set time."},
                "url": {"type": "string", "description": "http(s) URL for url_answers. No credentials."},
                "expect": {"type": "string", "enum": ["2xx", "any"], "description": "Default 2xx"},
                "watch_session": {"type": "string", "description": "Session to wait on for session_idle. Defaults to session_id."},
                "urgent": {"type": "boolean"},
                "context": {"type": "object"},
                "created_by": {"type": "string"},
                "directory": {"type": "string"},
            },
            "required": ["session_id", "check", "until", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "docket_list",
        "description": "List docket items. status defaults to pending, which includes firing claims and admissions not yet consumed. Use all, admitted, ambiguous, delivered, completed, cancelled, undeliverable, or expired for one state or every outcome.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "docket_occurrences",
        "description": "List occurrence outcomes for one item, or across items. Use status undeliverable to see wakes that could not be delivered. Each repeat has its own occurrence id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Item id. Omit to list across items."},
                "status": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "docket_cancel",
        "description": "Cancel a pending docket item. Cancelling a recurring item or a watch stops later wakes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
]


def serve() -> int:
    stdin = sys.stdin.buffer
    while True:
        message = _read(stdin)
        if message is None:
            return 0
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        if message.get("id") is None:
            continue
        try:
            result = _handle(method, message.get("params") or {})
            _write({"jsonrpc": "2.0", "id": message.get("id"), "result": result})
        except ClientError as exc:
            _write({
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {
                    "content": [{"type": "text", "text": exc.detail}],
                    "isError": True,
                },
            })
        except Exception as exc:  # noqa: BLE001 — one bad call must not kill the server
            _write({
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32603, "message": str(exc)},
            })


def _handle(method: str, params: dict):
    if method == "initialize":
        requested = (params or {}).get("protocolVersion") or PROTOCOL
        version = requested if requested in KNOWN else PROTOCOL
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "docket", "version": __version__},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        text = _call(name, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    raise RuntimeError(f"unknown method {method}")


def _call(name: str, arguments: dict) -> str:
    if name == "docket_timer":
        _status, body = request("POST", "/api/v1/timers", _item_body(arguments, "in"))
        return json.dumps(body)
    if name == "docket_alarm":
        _status, body = request("POST", "/api/v1/alarms", _item_body(arguments, "at"))
        return json.dumps(body)
    if name == "docket_recurring":
        body = _item_body(arguments, "every")
        for key in ("count", "until", "start"):
            if arguments.get(key) not in (None, ""):
                body[key] = arguments[key]
        _status, payload = request("POST", "/api/v1/recurring", body)
        return json.dumps(payload)
    if name == "docket_watch":
        body = _item_body(arguments, "until")
        body["check"] = arguments.get("check")
        for key in ("every", "path", "stable_for", "pid", "url", "expect", "watch_session"):
            if arguments.get(key) not in (None, ""):
                body[key] = arguments[key]
        _status, payload = request("POST", "/api/v1/watches", body)
        return json.dumps(payload)
    if name == "docket_occurrences":
        item_id = arguments.get("id")
        status = arguments.get("status")
        if item_id:
            path = f"/api/v1/items/{item_id}/occurrences"
        else:
            path = "/api/v1/occurrences"
            if status:
                path += f"?status={status}"
        _status, payload = request("GET", path)
        if item_id and status:
            payload = {
                "occurrences": [
                    occ for occ in payload.get("occurrences") or []
                    if occ.get("status") == status
                ]
            }
        return json.dumps(payload)
    if name == "docket_list":
        status = arguments.get("status") or "pending"
        _status, body = request("GET", f"/api/v1/items?status={status}")
        return json.dumps(body)
    if name == "docket_cancel":
        item_id = arguments.get("id")
        if not item_id:
            raise ClientError("id is required")
        _status, body = request("POST", f"/api/v1/items/{item_id}/cancel", {})
        return json.dumps(body)
    raise ClientError(f"unknown tool {name}")


def _item_body(arguments: dict, time_key: str) -> dict:
    body = {
        "session_id": arguments.get("session_id"),
        time_key: arguments.get(time_key),
        "reason": arguments.get("reason"),
    }
    if "urgent" in arguments:
        body["urgent"] = arguments["urgent"]
    if "context" in arguments:
        body["context"] = arguments["context"]
    if arguments.get("created_by"):
        body["created_by"] = arguments["created_by"]
    if arguments.get("directory"):
        body["directory"] = arguments["directory"]
    return body


def _read(stdin):
    line = stdin.readline()
    if not line:
        return None
    if line.lower().startswith(b"content-length:"):
        headers = [line]
        while True:
            extra = stdin.readline()
            if not extra or extra in (b"\r\n", b"\n"):
                break
            headers.append(extra)
        length = 0
        for header in headers:
            if header.lower().startswith(b"content-length:"):
                length = int(header.split(b":", 1)[1].strip())
        raw = stdin.read(length) if length else b""
        return json.loads(raw.decode()) if raw else None
    text = line.decode().strip()
    if not text:
        return _read(stdin)
    return json.loads(text)


def _write(message: dict) -> None:
    raw = json.dumps(message, separators=(",", ":")).encode() + b"\n"
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()
