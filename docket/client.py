"""HTTP client used by the CLI and the MCP server."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from docket.store import server_path, token_path


class ClientError(Exception):
    def __init__(
        self,
        detail: str,
        *,
        status: int | None = None,
        code: str | None = None,
        payload: dict | None = None,
    ):
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.code = code
        self.payload = payload if isinstance(payload, dict) else None


def resolve_url() -> str:
    env = os.environ.get("DOCKET_URL")
    if env:
        return env.rstrip("/")
    path = server_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    url = data.get("url") if isinstance(data, dict) else None
    if not url:
        raise ClientError("docket is not running (no server URL). Start it with: docket service start")
    return str(url).rstrip("/")


def resolve_token() -> str:
    env = os.environ.get("DOCKET_TOKEN")
    if env:
        return env
    path = Path(os.environ.get("DOCKET_TOKEN_FILE", token_path()))
    try:
        return path.read_text().strip()
    except OSError as exc:
        raise ClientError("docket token is not available") from exc


def request(method: str, path: str, body: dict | None = None, *, timeout: float = 15):
    url = resolve_url() + path
    data = None if body is None else json.dumps(body).encode()
    headers = {"Accept": "application/json"}
    if method != "GET":
        headers["Authorization"] = "Bearer " + resolve_token()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        parsed = None
        try:
            raw = exc.read()
            if raw:
                data = json.loads(raw)
                if isinstance(data, dict):
                    parsed = data
        except Exception:
            parsed = None
        error = parsed.get("error") if parsed is not None else None
        detail = None
        code = None
        if isinstance(error, dict):
            raw_detail = error.get("detail")
            if isinstance(raw_detail, str) and raw_detail.strip():
                detail = raw_detail.strip()
            raw_code = error.get("code")
            if isinstance(raw_code, str) and raw_code.strip():
                code = raw_code.strip()
        if not detail:
            detail = f"HTTP {exc.code}"
        raise ClientError(detail, status=exc.code, code=code, payload=parsed) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ClientError("docket is not reachable") from exc
