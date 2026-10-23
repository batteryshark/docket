"""Explicit isolated native host bridge; never discover or resume a global host.

The trusted binding directory must be outside every model/tool writable mount.
Runtime plugin counters live in native durable plugin storage, not this directory.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import base64
import fcntl
import math
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

from docket.delivery import Receipt
from docket.pairing import json_request
from docket.wake_profiles import ProfileRefusal, prepare as prepare_profile

BINDING_KEYS = {"target", "owner_generation", "profile_id", "profile_version",
                "baseline_version", "model", "role"}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class EventHostBusy(RuntimeError):
    """Retryable before send intent; never a permanent profile refusal."""


def configured_event_adapter(host_config, *, sandbox=None):
    config = dict(host_config)
    sandbox_path = config.get("sandbox_config")
    if sandbox_path is not None:
        if not isinstance(sandbox_path, str) or not Path(sandbox_path).is_absolute():
            raise ProfileRefusal("explicit private sandbox configuration required")
        private_read(Path(sandbox_path))
    if sandbox is None and sandbox_path is not None:
        # Fixed installed constructor, never a config-selected module/command.
        try:
            from docket.sandbox_native import configured_sandbox
        except ImportError:
            raise EventHostBusy("installed concrete handler backend unavailable") from None
        sandbox = configured_sandbox(sandbox_path)
    return EventHost(config, sandbox=sandbox)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def private_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        s = os.fstat(fd)
        if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077
                or s.st_nlink != 1 or s.st_size > 131072):
            raise ProfileRefusal("untrusted host binding file")
        raw = os.read(fd, 131073)
        if len(raw) > 131072:
            raise ProfileRefusal("host binding too large")
        return json.loads(raw)
    finally:
        os.close(fd)


class EventHost:
    def __init__(self, installed_host_config, *, request=None, sandbox=None):
        c = dict(installed_host_config)
        if (not {"version", "url", "binding_root"} <= set(c)
                or set(c) - {"version", "url", "binding_root", "server_auth_file", "sandbox_config"}
                or c["version"] != 1):
            raise ProfileRefusal("invalid installed host configuration")
        url = urlparse(c["url"])
        if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
                or url.path or url.query or url.fragment or url.username):
            raise ProfileRefusal("explicit loopback host URL required")
        self.root = Path(c["binding_root"])
        if not self.root.is_absolute() or self.root.is_symlink():
            raise ProfileRefusal("explicit trusted binding root required")
        s = self.root.stat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077:
            raise ProfileRefusal("host binding root must be private")
        self.url = c["url"]
        self.auth_file = Path(c["server_auth_file"]) if c.get("server_auth_file") else None
        if self.auth_file is not None and not self.auth_file.is_absolute():
            raise ProfileRefusal("explicit private server authentication path required")
        self.request = request or self._request
        self.sandbox = sandbox
        if c.get("sandbox_config") is not None:
            if not isinstance(c["sandbox_config"], str) or not Path(c["sandbox_config"]).is_absolute():
                raise ProfileRefusal("explicit private sandbox configuration required")
            private_read(Path(c["sandbox_config"]))  # Provenance/private bounds before composition.

    def _request(self, method, path, body=None):
        if self.auth_file is None:
            return json_request(self.url, method, path, body, timeout=10)
        fd = os.open(self.auth_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            s = os.fstat(fd)
            if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077
                    or s.st_nlink != 1 or not 1 <= s.st_size <= 1024):
                raise ProfileRefusal("untrusted server authentication file")
            password = os.read(fd, 1025).strip()
        finally:
            os.close(fd)
        if not password or len(password) > 1024 or b"\n" in password or b"\r" in password:
            raise ProfileRefusal("invalid private server authentication")
        # Only transient memory; never put headers/passwords into receipts or bindings.
        headers = {"Authorization": "Basic " + base64.b64encode(b"opencode:" + password).decode(),
                   "Content-Type": "application/json"}
        req = Request(self.url + path, data=None if body is None else canonical(body).encode(),
                      method=method, headers=headers)
        try:
            response = build_opener(_NoRedirect).open(req, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(1048577)
            if len(raw) > 1048576:
                raise ProfileRefusal("native response bound exceeded")
            try:
                result = json.loads(raw) if raw else None
            except ValueError:
                result = None
            return response.status, result

    def _installation(self):
        return private_read(self.root / "installation.json")

    def prepare(self, config, event_data, admission_id):
        """config selects only an exact installed profile ID/version; not event input."""
        i = self._installation()
        action = "handler" if "handler_class" in i["baseline"]["target"] else "owner"
        if config != {"action": action, "baseline": i["baseline"], "profile": i["profile"]}:
            raise ProfileRefusal("installed profile selection mismatch")
        p = prepare_profile(i["baseline"], i["profile"], event_data,
                            available_tools=set(i["available_tools"]), remaining_budget=i["remaining_budget"], action=action)
        if not re.fullmatch(r"msg_[A-Za-z0-9]+", admission_id):
            raise ProfileRefusal("invalid stable native admission ID")
        p["admission_id"] = admission_id
        self.validate(p)
        return p

    def validate(self, prepared):
        i = self._installation()
        expected = prepare_profile(i["baseline"], i["profile"], prepared["event_data"],
                                   available_tools=set(i["available_tools"]), remaining_budget=i["remaining_budget"],
                                   action=prepared.get("action", "owner"))
        expected["admission_id"] = prepared["admission_id"]
        if canonical(expected) != canonical(prepared):
            raise ProfileRefusal("prepared record differs from current installed policy/context")
        if prepared.get("action") == "handler":
            owner = private_read(self.root / "owner.json")
            if (owner.get("stopped") is not False or owner.get("revoked") is not False
                    or owner.get("target") != prepared["target"]
                    or owner.get("owner_generation") != prepared["owner_generation"]):
                raise ProfileRefusal("handler class stopped/revoked or deployment changed")
            if self.sandbox is None:
                raise EventHostBusy("installed concrete handler backend unavailable")
            if not all(callable(getattr(self.sandbox, name, None)) for name in ("launch", "lookup", "status", "receipt")):
                raise EventHostBusy("installed handler lifecycle recovery API unavailable")
            return {**{k: prepared[k] for k in BINDING_KEYS}, "stopped": False, "revoked": False}
        directory = prepared["target"]["directory"]
        query = "?" + urlencode({"location[directory]": directory})
        code, _ = self.request("GET", "/api/integration" + query)
        if code != 200:
            raise EventHostBusy("native location plugin readiness unavailable")
        code, plugins = self.request("GET", "/api/plugin" + query)
        if (code != 200 or not isinstance(plugins, dict) or not isinstance(plugins.get("data"), list)
                or not any(x.get("id") == "docket-events" and x.get("state", {}).get("status") == "active"
                           for x in plugins["data"])):
            raise ProfileRefusal("native enforcement plugin not active")
        owner = private_read(self.root / "owner.json")
        if (owner.get("stopped") is not False or owner.get("revoked") is not False
                or any(owner.get(k) != prepared[k] for k in
                       ("target", "owner_generation", "goal_id", "goal_generation"))):
            raise ProfileRefusal("owner stopped/revoked or generation changed")
        code, body = self.request("GET", "/api/session/" + prepared["target"]["session_id"])
        s = body.get("data", {}) if isinstance(body, dict) else {}
        goal = s.get("metadata", {}).get("openchamber", {}).get("goal", {})
        if (code != 200 or s.get("agent") != prepared["role"]
                or s.get("location", {}).get("directory") != prepared["target"]["directory"]
                or s.get("time", {}).get("archived")
                or goal.get("id") != prepared["goal_id"]
                or type(goal.get("createdAt")) is not type(prepared["goal_generation"])
                or goal.get("createdAt") != prepared["goal_generation"]
                or goal.get("status") != "active"):
            raise ProfileRefusal("native owner/goal identity changed")
        with self._owner_lock(prepared):
            old = self._current_binding(prepared)
            if old and old["prepared"]["admission_id"] != prepared["admission_id"]:
                self._terminal_witness(old)
        return {**{k: prepared[k] for k in BINDING_KEYS}, "stopped": False, "revoked": False}

    @contextmanager
    def _owner_lock(self, prepared):
        session = prepared["target"]["session_id"]
        if not re.fullmatch(r"ses_[A-Za-z0-9]+", session):
            raise ProfileRefusal("invalid native owner identity")
        fd = os.open(self.root / (session + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            s = os.fstat(fd)
            if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077 or s.st_nlink != 1:
                raise ProfileRefusal("untrusted native owner lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise EventHostBusy("owner binding busy; retry before admission") from None
            yield
        finally:
            os.close(fd)

    def _current_binding(self, prepared):
        try:
            return private_read(self.root / (prepared["target"]["session_id"] + ".json"))
        except FileNotFoundError:
            return None

    def _terminal_witness(self, binding):
        """Exact transcript turn boundary, NOT an arbitrary idle observation."""
        p = binding["prepared"]
        session = p["target"]["session_id"]
        code, body = self.request("GET", f"/api/session/{session}/context")
        messages = body.get("data") if isinstance(body, dict) else None
        if code != 200 or not isinstance(messages, list):
            raise EventHostBusy("bound native turn terminal evidence unavailable")
        indices = [n for n, m in enumerate(messages) if m.get("id") == p["admission_id"]]
        if len(indices) != 1:
            raise EventHostBusy("bound native admission not in current terminal context")
        index = indices[0]
        user = messages[index]
        if user.get("type") != "user" or user.get("text") != p["briefing"]:
            raise ProfileRefusal("native binding admission conflict")
        terminal = None
        for m in messages[index + 1:]:
            if m.get("type") in ("user", "synthetic", "shell"):
                raise EventHostBusy("native turn contains another input; reconcile ownership")
            if m.get("type") == "idle":
                terminal = terminal or m
        if not terminal or terminal.get("outcome") not in ("succeeded", "failed", "interrupted") or not terminal.get("id"):
            raise EventHostBusy("bound native turn has no terminal execution marker")
        code, body = self.request("GET", "/api/session/active")
        active = body.get("data") if isinstance(body, dict) else None
        if code != 200 or not isinstance(active, (dict, list)) or session in active:
            raise EventHostBusy("native owner still active or activity unavailable")
        if isinstance(active, list) and any(isinstance(x, dict) and x.get("sessionID", x.get("id")) == session for x in active):
            raise EventHostBusy("native owner still active")
        code, body = self.request("GET", f"/api/session/{session}/inbox")
        if code != 200 or not isinstance(body, dict) or body.get("data") != []:
            raise EventHostBusy("native owner inbox not empty or unavailable")
        return {"admission_id": p["admission_id"], "body_sha256": hashlib.sha256(p["briefing"].encode()).hexdigest(),
                "terminal_id": terminal["id"], "outcome": terminal["outcome"],
                "terminal_time": terminal.get("time"), "not_active": True, "empty_inbox": True}

    def archive_terminal(self, prepared, stable_id):
        """Operator handoff: archive an exact closed turn, leave pointer/counters intact.

        No new admission is invented and nothing is dispatched or unlinked. The
        later authenticated queue delivery still replaces the pointer under its
        normal fresh terminal check and per-owner lock.
        """
        if stable_id != prepared["admission_id"] or not re.fullmatch(r"msg_[A-Za-z0-9]+", stable_id):
            raise ProfileRefusal("archive admission identity mismatch")
        with self._owner_lock(prepared):
            binding = self._current_binding(prepared)
            if binding is None or binding.get("prepared") != prepared:
                raise ProfileRefusal("archive requires exact currently bound preparation")
            witness = self._terminal_witness(binding)
            archive = self.root / ("archive-" + stable_id + ".json")
            self._exclusive_record(archive, {"binding": binding, "terminal_witness": witness})
            fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return {"archive": str(archive), "terminal_witness": witness, "current_binding_preserved": True}

    def _exclusive_record(self, path, value):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if private_read(path) != value:
                raise ProfileRefusal("native archive identity conflict") from None
            return False
        with os.fdopen(fd, "w") as f:
            f.write(canonical(value))
            f.flush()
            os.fsync(f.fileno())
        return True

    def _handler_path(self, admission_id):
        if not re.fullmatch(r"msg_[A-Za-z0-9]+", admission_id):
            raise ProfileRefusal("invalid native admission identity")
        return self.root / ("handler-" + admission_id + ".json")

    def _save_handler(self, path, record):
        temporary = self.root / (".handler-" + secrets.token_hex(16))
        self._exclusive_record(temporary, record)
        try:
            os.replace(temporary, path)
            fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)

    def _handler_binding(self, prepared, record):
        binding = record.get("sandbox_binding")
        lookup = getattr(self.sandbox, "lookup", None)
        if callable(lookup):
            binding = lookup(record["delivery_id"])
        if binding is None:
            return None  # Existing intent is uncertain, NEVER call launch to recover it.
        expected_hash = hashlib.sha256(json.dumps(prepared, sort_keys=True).encode()).hexdigest()
        if (binding.get("prepared_hash") != expected_hash
                or binding.get("admission_id") != prepared["admission_id"]
                or binding.get("delivery_id") != record["delivery_id"]
                or binding.get("generation") != record["generation"]):
            raise ProfileRefusal("sandbox lifecycle binding conflict")
        return binding

    def _handler_status(self, prepared, stable_id, *, terminal_receipt=False):
        if self.sandbox is None:
            return "unavailable"
        try:
            record = private_read(self._handler_path(stable_id))
        except FileNotFoundError:
            return "missing"
        if record.get("prepared_hash") != digest(prepared) or record.get("admission_id") != stable_id:
            return "unavailable"
        binding = self._handler_binding(prepared, record)
        if binding is None:
            return "unavailable"
        # Receipt export is strictly read-only: only an already-pruned saved
        # receipt can pass the checks below. Do not poll or clean running work.
        state = "handler_finished" if terminal_receipt else self.sandbox.status(binding)
        if state != "handler_finished":
            return state if state in ("admitted", "consumed", "missing", "unavailable") else "unavailable"
        receipt_method = getattr(self.sandbox, "receipt", None)
        if not callable(receipt_method):
            return "unavailable"
        receipt = receipt_method(binding)
        native = receipt.get("native_terminal") or {}
        if (receipt.get("phase") != "pruned" or receipt.get("consumed") is not True
                or receipt.get("trusted_completion") is not True
                or not isinstance(receipt.get("output_manifest"), list)
                or any(receipt.get(k) != binding.get(k) for k in ("environment_id", "generation", "admission_id"))
                or native.get("session_id") != binding.get("session_id")
                or native.get("admission_id") != stable_id
                or native.get("body_sha256") != hashlib.sha256(prepared["briefing"].encode()).hexdigest()
                or not native.get("terminal_id") or native.get("outcome") not in ("succeeded", "failed", "interrupted")
                or native.get("not_active") is not True or native.get("empty_inbox") is not True):
            return "unavailable"
        if terminal_receipt:
            return Receipt(stable_id, "queue", stable_id, state="handler_finished", consumed=True,
                           raw={"handler_terminal": receipt})
        return "handler_finished"

    def terminal_receipt(self, prepared, stable_id):
        """Read-only structured proof through the SAME exact terminal checks."""
        if prepared.get("action") != "handler" or stable_id != prepared.get("admission_id"):
            return None
        result = self._handler_status(prepared, stable_id, terminal_receipt=True)
        return result if isinstance(result, Receipt) else None

    def _dispatch_handler(self, prepared, stable_id, lifecycle):
        if lifecycle is None:
            raise ProfileRefusal("handler requires trusted separate runtime lifecycle")
        self.validate(prepared)
        path = self._handler_path(stable_id)
        record = {"version": 1, "prepared_hash": digest(prepared), "admission_id": stable_id,
                  "delivery_id": lifecycle["delivery_id"], "generation": lifecycle["generation"],
                  "phase": "launch_intent"}
        try:
            old = private_read(path)
        except FileNotFoundError:
            old = None
        if old is not None:
            if any(old.get(k) != record[k] for k in ("prepared_hash", "admission_id", "delivery_id", "generation")):
                raise ProfileRefusal("handler admission identity conflict")
            status = self._handler_status(prepared, stable_id)
            if status not in ("admitted", "consumed", "handler_finished"):
                raise EventHostBusy("handler intent exists; status-only reconciliation required")
            if status == "handler_finished":
                receipt = self.terminal_receipt(prepared, stable_id)
                if receipt is None:
                    raise EventHostBusy("handler terminal proof unavailable")
                return receipt
            return Receipt(stable_id, "queue", stable_id, state=status, consumed=status != "admitted")
        if not self._exclusive_record(path, record):
            raise EventHostBusy("handler admission concurrently claimed; reconcile exact identity")
        # Persist the pointer before allocation. Sandbox persists its own exact
        # allocation intent before creating compute. Neither intent is replayed.
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        existing = self.sandbox.lookup(lifecycle["delivery_id"])
        if existing is not None:
            record.update(phase="bound", sandbox_binding=existing)
            self._handler_binding(prepared, record)
            self._save_handler(path, record)
            state = self._handler_status(prepared, stable_id)
            if state not in ("admitted", "consumed", "handler_finished"):
                raise EventHostBusy("existing handler allocation requires status-only reconciliation")
            if state == "handler_finished":
                receipt = self.terminal_receipt(prepared, stable_id)
                if receipt is None:
                    raise EventHostBusy("handler terminal proof unavailable")
                return receipt
            return Receipt(stable_id, "queue", stable_id, state=state, consumed=state != "admitted")
        binding = self.sandbox.launch(prepared, lifecycle["delivery_id"], lifecycle=lifecycle)
        record.update(phase="bound", sandbox_binding=binding)
        self._handler_binding(prepared, record)
        self._save_handler(path, record)
        return Receipt(stable_id, "queue", stable_id, state="admitted")

    def _binding(self, prepared, delivery_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", delivery_id):
            raise ProfileRefusal("invalid delivery ID")
        binding = {"version": 1, "delivery_id": delivery_id, "prepared": prepared,
                   "installation_hash": digest(self._installation())}
        path = self.root / (prepared["target"]["session_id"] + ".json")
        with self._owner_lock(prepared):
            old = self._current_binding(prepared)
            if old == binding:
                return
            if old:
                if old["prepared"]["admission_id"] == prepared["admission_id"]:
                    raise ProfileRefusal("native binding identity conflict")
                witness = self._terminal_witness(old)
                oldid = old["prepared"]["admission_id"]
                if not re.fullmatch(r"msg_[A-Za-z0-9]+", oldid):
                    raise ProfileRefusal("invalid archived admission identity")
                self._exclusive_record(self.root / ("archive-" + oldid + ".json"),
                                       {"binding": old, "terminal_witness": witness})
                temporary = self.root / (".binding-" + secrets.token_hex(16))
                self._exclusive_record(temporary, binding)
                try:
                    if private_read(path) != old:
                        raise EventHostBusy("owner binding changed; reconcile exact admission")
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                self._exclusive_record(path, binding)
            fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def dispatch(self, prepared, stable_id, *, lifecycle=None):
        if lifecycle is not None:
            if not isinstance(lifecycle, dict) or set(lifecycle) != {"delivery_id", "deadline", "lease_until", "generation"}:
                raise ProfileRefusal("invalid trusted lifecycle fields")
            for key in ("delivery_id", "generation"):
                if not isinstance(lifecycle[key], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", lifecycle[key]):
                    raise ProfileRefusal("invalid trusted lifecycle identity")
            try:
                values = [lifecycle[k] for k in ("deadline", "lease_until")]
                if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
                    raise ValueError
                deadline, lease = (datetime.fromtimestamp(v, timezone.utc) for v in values)
                now = datetime.now(timezone.utc)
                if not now < lease <= deadline <= now + timedelta(days=7):
                    raise ValueError
            except (AttributeError, TypeError, ValueError, OverflowError, OSError):
                raise ProfileRefusal("invalid trusted lifecycle bounds") from None
        if stable_id != prepared["admission_id"]:
            raise ProfileRefusal("admission identity mismatch")
        if prepared.get("action") == "handler":
            return self._dispatch_handler(prepared, stable_id, lifecycle)
        prior = self.reconcile(prepared, stable_id)
        if prior in ("admitted", "consumed"):
            return Receipt(prepared["admission_id"], "queue", prepared["admission_id"],
                           state=prior, consumed=prior == "consumed")
        if prior != "missing":
            raise ProfileRefusal("native admission uncertain; reconcile exact ID/body")
        self.validate(prepared)
        self._binding(prepared, lifecycle["delivery_id"] if lifecycle else stable_id)
        session = prepared["target"]["session_id"]
        provider, model = prepared["model"].split("/", 1)
        status, _ = self.request("POST", f"/api/session/{session}/model",
                                 {"model": {"providerID": provider, "id": model}})
        if status not in (200, 204):
            raise ProfileRefusal("supported native model selection refused")
        self.validate(prepared)
        status, _ = self.request("POST", f"/api/session/{session}/prompt",
                                 {"id": prepared["admission_id"], "text": prepared["briefing"],
                                  "delivery": "queue"})
        if status not in (200, 409):
            raise ProfileRefusal("native admission uncertain; reconcile exact ID/body")
        outcome = self.reconcile(prepared, stable_id)
        if outcome not in ("admitted", "consumed"):
            raise ProfileRefusal("native acceptance unconfirmed; reconcile exact ID/body")
        return Receipt(prepared["admission_id"], "queue", prepared["admission_id"],
                       state=outcome, consumed=outcome == "consumed")

    def reconcile(self, prepared, stable_id):
        if stable_id != prepared["admission_id"]:
            return "unavailable"
        if prepared.get("action") == "handler":
            try:
                return self._handler_status(prepared, stable_id)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                return "unavailable"
        session, identity = prepared["target"]["session_id"], prepared["admission_id"]
        try:
            code, body = self.request("GET", f"/api/session/{session}/message/{identity}")
            if code == 200:
                data = body.get("data", {})
                return "consumed" if (data.get("id") == identity and data.get("type") == "user"
                                      and data.get("text") == prepared["briefing"]) else "unavailable"
            if code != 404:
                return "unavailable"
            code, body = self.request("GET", f"/api/session/{session}/inbox")
            if code != 200 or not isinstance(body.get("data"), list):
                return "unavailable"
            rows = [x for x in body["data"] if x.get("id") == identity]
            if not rows:
                return "missing"
            row = rows[0]
            return "admitted" if (row.get("type") == "user" and
                                   row.get("payload", {}).get("text") == prepared["briefing"]) else "unavailable"
        except (OSError, ValueError, TypeError, KeyError):
            return "unavailable"

    status = reconcile
