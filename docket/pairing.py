"""Own OpenChamber client enrollment. No borrowed credentials or clipboard access.

The own-record check below detects revocation and expiry of Docket's stored
client. It does not prove the host requires authentication. A password-free
loopback can still accept a request that carries no valid bearer. That optional
success is not a reason to reject the host, and it is not a fallback.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import platform
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings
from datetime import datetime
from pathlib import Path

from docket.client import ClientError

MAX_BYTES = 65536


class PairingError(ClientError):
    pass


class RecordRefusal(PairingError):
    """Visible refusal. The detail must not contain a token, link, or host body."""


def fail(detail="invalid Connection link"):
    raise PairingError(detail)


def state_dir() -> Path:
    # Deliberately separate from the daemon database and inbound API token.
    return Path(os.environ.get("DOCKET_HOME", "~/.docket")).expanduser() / "openchamber"


def private_dir(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = root.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        fail("Docket client directory must be owned by this user, mode 0700, and not a symlink")


def read_private(path: Path) -> dict:
    private_dir(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) != 0o600 or st.st_nlink != 1 or st.st_size > MAX_BYTES):
            fail("Docket client file must be a private owner-only regular file (0600)")
        raw = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        fail("invalid Docket client file")
    if not isinstance(value, dict):
        fail("invalid Docket client file")
    return value


def atomic_private(path: Path, value: dict) -> None:
    private_dir(path.parent)
    if path.exists() or path.is_symlink():
        read_private(path)  # Do not replace somebody else's file or follow links.
    fd, temporary = tempfile.mkstemp(prefix=".client-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def identity(root: Path) -> str:
    path = root / "identity.json"
    if not path.exists():
        atomic_private(path, {"dedupeKey": "docket:" + uuid.uuid4().hex})
    value = read_private(path).get("dedupeKey")
    if not isinstance(value, str) or not re.fullmatch(r"docket:[0-9a-f]{32}", value):
        fail("invalid Docket client identity")
    return value


def loopback_origin(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url)
        if (p.scheme != "http" or p.hostname not in ("127.0.0.1", "::1")
                or p.username is not None or p.password is not None or p.query or p.fragment
                or p.path not in ("", "/") or not p.port or not 0 < p.port < 65536):
            fail()
        host = "[::1]" if p.hostname == "::1" else "127.0.0.1"
        result = f"http://{host}:{p.port}"
        if url not in (result, result + "/"):
            fail()
        return result
    except (ValueError, TypeError):
        fail()


def timestamp(value) -> float:
    if not isinstance(value, str):
        fail()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            fail()
        return dt.timestamp()
    except (ValueError, OverflowError):
        fail()


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            fail()
        value[key] = item
    return value


def parse_link(link: str, now: float | None = None) -> tuple[dict, str]:
    now = time.time() if now is None else now
    if not isinstance(link, str) or len(link) > MAX_BYTES or any(ord(c) < 32 for c in link):
        fail()
    try:
        p = urllib.parse.urlsplit(link)
        q = urllib.parse.parse_qs(p.query, strict_parsing=True)
        if (p.scheme != "openchamber" or p.netloc != "connect" or p.path or p.fragment
                or set(q) != {"v", "p"} or q["v"] != ["2"] or len(q["p"]) != 1):
            fail()
        encoded = q["p"][0]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
            fail()
        data = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True),
                          object_pairs_hook=unique_object)
        if not isinstance(data, dict):
            fail()
        for field in ("pairingId", "secret", "fingerprint"):
            value = data.get(field)
            if not isinstance(value, str) or not 1 <= len(value) <= 1024 or any(ord(c) < 33 for c in value):
                fail()
        # Installed 2.3.3 trims both credentials with JavaScript String.trim().
        # Issued credentials are ASCII: reject non-visible-ASCII forms rather
        # than let host-equivalent Unicode whitespace get a new ledger digest.
        # Display labels and other human metadata are deliberately untouched.
        if any(ord(c) > 126 for field in ("pairingId", "secret") for c in data[field]):
            fail()
        expiry = timestamp(data.get("expiresAt"))
        if not now < expiry <= now + 600:
            fail("Connection link expired or outside ten-minute validity window")
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 8:
            fail()
        origins = set()
        for candidate in candidates:
            if not isinstance(candidate, dict) or candidate.get("type") not in ("local", "lan", "loopback", "direct"):
                fail()
            origins.add(loopback_origin(candidate.get("url")))
        if len(origins) != 1:
            fail("Connection link must select exactly one loopback host")
        return data, origins.pop()
    except (ValueError, KeyError, TypeError, UnicodeError):
        fail()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def json_request(origin: str, method: str, path: str, body=None, token=None, timeout=10):
    origin = loopback_origin(origin)
    if not path.startswith("/") or path.startswith("//") or "#" in path:
        fail("invalid host route")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(origin + path, method=method, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                fail("OpenChamber response too large")
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        # Never echo host bodies, headers, URLs, link or token.
        return exc.code, None
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        fail("OpenChamber request failed; not retried")


def health(origin: str) -> None:
    status, body = json_request(origin, "GET", "/health")
    if (status != 200 or not isinstance(body, dict)
            or not isinstance(body.get("openchamberVersion"), str) or not body["openchamberVersion"]):
        fail("loopback host is not an identified OpenChamber desktop server")


def redeem(link: str, root: Path | None = None) -> dict:
    # Reject malformed credentials before even creating private state/the lock.
    data, origin = parse_link(link)
    root = state_dir() if root is None else root
    private_dir(root)
    # Serialize operators and refuse accidental re-pair/replacement of an own token.
    lock = root / ".enrolling"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        fail("enrollment locked; inspect previous attempt before retrying")
    os.close(fd)
    try:
        binding = root / "client.json"
        if binding.exists() or binding.is_symlink():
            fail("Docket already has a client binding; explicit operator recovery required")
        attempt_path = root / "attempt.json"
        # Identify the enrollment, not its query/JSON/base64 spelling. Never store secrets.
        digest = hashlib.sha256(json.dumps([data["pairingId"], data["secret"]],
                                           ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        attempts = []
        if attempt_path.exists() or attempt_path.is_symlink():
            ledger = read_private(attempt_path)
            # Old raw-link hashes cannot identify equivalent links. No safe automatic migration
            # can establish that an enrollment is unused after an unknown outcome.
            attempts = ledger.get("enrollmentDigests")
            if (ledger.get("version") != 2 or "linkDigests" in ledger
                    or not isinstance(attempts, list) or not 1 <= len(attempts) < 128
                    or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)
                           for item in attempts)):
                fail("enrollment attempt ledger needs operator review")
        if digest in attempts:
            fail("this enrollment was already attempted; not retried")
        dedupe = identity(root)
        health(origin)
        # Recheck expiry immediately before the ONLY redeem POST. No retry or candidate fallback.
        if timestamp(data["expiresAt"]) <= time.time():
            fail("Connection link expired")
        atomic_private(attempt_path, {"version": 2, "enrollmentDigests": attempts + [digest]})
        status, result = json_request(origin, "POST", "/api/client-auth/pairing/redeem", {
            "pairingId": data["pairingId"], "secret": data["secret"],
            "clientKind": "desktop", "clientLabel": "Docket", "deviceName": "Docket",
            "devicePlatform": platform.system().lower(), "dedupeKey": dedupe,
        })
        if status != 200 or not isinstance(result, dict):
            fail("redemption failed or outcome unknown; not retried")
        client, token = result.get("client"), result.get("clientToken")
        if (not isinstance(client, dict) or not valid_client_id(client.get("id"))
                or not valid_client_token(token)):
            fail("redemption response binding invalid; outcome unknown, do not retry")
        # The request's validated local origin is authoritative, NOT returned metadata.
        # Save the acquired own credential before even inspecting optional fields.
        # In 2.3.3 the principal's pairing label overrides our fallback clientLabel.
        value = {"version": 1, "clientId": client["id"], "clientToken": token,
                 "enrolledOrigin": origin, "dedupeKey": dedupe}
        atomic_private(binding, value)
        notices = metadata_warnings(result, data, origin)
        return {"paired": True, "origin": origin,
                "warnings": notices,
                "host_enforced_authentication": "not proven"}
    finally:
        lock.unlink()


def valid_client_id(value) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 1024
            and not any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in value))


def valid_client_token(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"oc_client_[A-Za-z0-9_-]+", value) is not None


def metadata_warnings(result, data, origin) -> list[str]:
    """Post-save display checks only. Never echo host-provided text into status."""
    notices = []
    server = result.get("server")
    if (not isinstance(server, dict) or server.get("fingerprint") != data["fingerprint"]
            or server.get("url") not in (origin, origin + "/")):
        notices.append("Own token saved; server display metadata missing or different (not used for routing)")
    if result.get("ok") is not True:
        notices.append("Own token saved; optional response acknowledgement missing or different")
    if not active_client(result.get("client")):
        notices.append("Own token saved; client record has revocation or expiry; delivery guard will refuse it")
    return notices


def active_client(client) -> bool:
    return (isinstance(client, dict) and valid_client_id(client.get("id"))
            and not client.get("revokedAt")
            and not client.get("expiresAt"))


def require_own_record(clients, binding) -> None:
    """Require exactly the stored active Docket record. Otherwise refuse.

    A matching record means the host listed one client, and that client is the
    stored id, with no revocation and no expiry. Names/kind/branding are display
    metadata, not identity: renaming an own device must not strand delivery. Any other
    list is a visible refusal. There is no fallback to another host, another
    client, or an unauthenticated call. This does not prove the host checked
    the bearer: password-free loopback can list clients without one.
    """
    if not isinstance(binding, dict):
        raise RecordRefusal("Docket client binding is missing; no fallback")
    expected_id = binding.get("clientId")
    if not valid_client_id(expected_id):
        raise RecordRefusal("Docket client binding does not name its stored client; no fallback")
    if not isinstance(clients, list) or len(clients) != 1 or not isinstance(clients[0], dict):
        raise RecordRefusal("OpenChamber did not return exactly Docket's own client record; no fallback")
    client = clients[0]
    if client.get("revokedAt") or client.get("expiresAt"):
        raise RecordRefusal("Docket client record is revoked or expired; no fallback")
    if client.get("id") != expected_id or not active_client(client):
        raise RecordRefusal("Docket client record does not match the stored active client; no fallback")


def intake(stream) -> str:
    # Hidden terminal intake; redirected stdin supports a pipe without argv/history.
    if stream.isatty():
        import getpass
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                return getpass.getpass("Connection link (hidden): ", stream=None).strip()
            except getpass.GetPassWarning:
                fail("hidden terminal input unavailable; use a secure stdin pipe")
    raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        fail()
    return raw.strip()
