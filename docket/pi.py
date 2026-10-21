"""Offline-draft pi-a2a external-v1 boundary. No mailbox or transcript access.

Only trusted private installation configuration selects launch files/state.
Published contract: pi-a2a 0.2.0, ceafe6f8f2c3e1d9d2e85845feac96f13dc30853.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from docket.delivery import DeliveryAdapter, DeliveryError, Receipt, Validation
from docket.platform import child_options, read_private_bytes, state_root


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


class MachineRunner:
    """One direct child, bounded caller waits and at most one unresolved I/O pair.

    30s is Docket's local exchange policy, not Pi's separate 10s input-EOF
    bound or a published latency SLA. Only daemon I/O threads own pipe close.
    Inherited handles can outlive the child: later calls fail closed until the
    exact child is reaped and its I/O clears. No descendant discovery/termination.
    """

    def __init__(self, *, timeout=30, cleanup=0.5):
        self.timeout = timeout
        self.cleanup = cleanup
        self._lock = threading.Lock()
        self._active = None

    def __call__(self, argv, frame, env):
        if not self._lock.acquire(blocking=False):
            raise OSError("companion exchange already active")
        try:
            if self._active is not None:
                child, threads = self._active
                if any(thread.is_alive() for thread in threads) or child.poll() is None:
                    raise OSError("companion exchange unresolved")
                self._active = None
            deadline = time.monotonic() + self.timeout
            output = bytearray()
            errors = []
            options = {**child_options(), "stdin": subprocess.PIPE}
            # No Popen context manager: its pipe close/wait path is unbounded.
            child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env=env, shell=False, bufsize=0, **options)

            def read():
                try:
                    while len(output) <= 8192:
                        raw = child.stdout.read(8193 - len(output))
                        if not raw:
                            break
                        output.extend(raw)
                except (OSError, ValueError):
                    errors.append(True)
                finally:
                    try:
                        child.stdout.close()
                    except OSError:
                        errors.append(True)

            def write():
                try:
                    remaining = memoryview(frame)
                    while remaining:
                        count = child.stdin.write(remaining)
                        if not count:
                            raise OSError("short write")
                        remaining = remaining[count:]
                except (OSError, ValueError):
                    errors.append(True)
                finally:
                    try:
                        child.stdin.close()  # Single frame, then EOF; owned by this thread.
                    except OSError:
                        errors.append(True)

            threads = [threading.Thread(target=read, daemon=True), threading.Thread(target=write, daemon=True)]
            self._active = child, threads
            try:
                for thread in threads:
                    thread.start()
                child.wait(timeout=max(0, deadline - time.monotonic()))
                for thread in threads:
                    thread.join(timeout=max(0, deadline - time.monotonic()))
                if any(thread.is_alive() for thread in threads):
                    raise subprocess.TimeoutExpired("companion", self.timeout)
                if errors or len(output) > 8192:
                    raise OSError("companion exchange incomplete")
                return child.returncode, bytes(output)
            finally:
                cleanup_deadline = time.monotonic() + self.cleanup
                if child.poll() is None:
                    try:
                        child.kill()  # Only this Popen child, never descendants.
                    except OSError:
                        pass
                try:
                    child.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
                except (OSError, subprocess.TimeoutExpired):
                    pass  # Retain the active record; subsequent calls fail closed.
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(timeout=max(0, cleanup_deadline - time.monotonic()))
                if not any(thread.is_alive() for thread in threads) and child.poll() is not None:
                    self._active = None
        finally:
            self._lock.release()


class PiAdapter(DeliveryAdapter):
    name = "pi"

    def __init__(self, *, config=None, runner=None):
        # This path is never accepted from a reminder/API request.
        self.config = config if config is not None else state_root() / "pi-companion.json"
        self.runner = runner or MachineRunner()

    def binding_snapshot(self):
        try:
            value = _json(read_private_bytes(self.config, max_bytes=8192))
            if (not isinstance(value, dict) or set(value) != {"v", "node", "cli", "state"}
                    or type(value["v"]) is not int or value["v"] != 1):
                raise ValueError
            for key in ("node", "cli", "state"):
                path = value[key]
                if (not isinstance(path, str) or not Path(path).is_absolute()
                        or any(ord(c) < 32 for c in path) or os.path.normpath(path) != path):
                    raise ValueError
            if Path(value["cli"]).name != "cli.mjs":
                raise ValueError
            return {"adapter": "pi", "mode": "pi-draft", "namespace_verified": False,
                    "node": value["node"], "cli": value["cli"], "state": value["state"]}
        except (OSError, ValueError, TypeError, UnicodeError):
            raise DeliveryError("backend_unverifiable", not_dispatched=True) from None

    def host_status(self):
        return {"adapter": "pi", "mode": "pi-draft"}

    @staticmethod
    def validate_target_id(value):
        if (not isinstance(value, str) or not value.startswith("pi/") or len(value) <= 3
                or len(value.encode("utf-8")) > 1024
                or any(c.isspace() or ord(c) < 32 for c in value)):
            return "Pi requires an existing pi/<id> target"
        return None

    def _call(self, frame, binding=None):
        current = self.binding_snapshot()
        if binding is not None and binding != current:
            raise DeliveryError("backend_unverifiable", not_dispatched=True)
        try:
            raw = (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            if len(raw) > 262144:
                raise ValueError
            # No NODE_OPTIONS/NODE_PATH injection; no credentials or model settings
            # are required by the companion. OS launcher environment is retained.
            env = {key: value for key, value in os.environ.items()
                   if key in ("PATH", "HOME", "USERPROFILE", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT")}
            env["PI_A2A_STATE"] = current["state"]
            rc, stdout = self.runner([current["node"], current["cli"], "machine"], raw, env)
            if (not isinstance(stdout, bytes) or len(stdout) > 8192
                    or not stdout.endswith(b"\n") or stdout.count(b"\n") != 1):
                raise ValueError
            result = _json(stdout)
            if (not isinstance(result, dict) or type(result.get("v")) is not int
                    or result["v"] != 1 or type(result.get("ok")) is not bool):
                raise ValueError
            if result["ok"]:
                if rc != 0:
                    raise ValueError
                return result
            codes = {"INVALID_JSON": 2, "SCHEMA": 2, "INPUT_LIMIT": 2, "INPUT_TIMEOUT": 2,
                     "UNKNOWN": 3, "UNAVAILABLE": 3, "UNSUPPORTED_TARGET": 3,
                     "UNSUPPORTED_SECURITY": 3, "CAPACITY": 3, "CONFLICT": 4,
                     "CANCELLED": 4, "TOO_LATE": 4, "A2A_BUSY": 5, "STORAGE_UNCERTAIN": 6}
            if (set(result) != {"v", "ok", "error", "retry"}
                    or result.get("retry") != "same_key_only"
                    or codes.get(result.get("error")) != rc):
                raise ValueError
            return result
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError, subprocess.TimeoutExpired):
            raise DeliveryError("acceptance_unknown") from None

    def validate(self, target, *, expected_binding=None):
        try:
            if self.validate_target_id(target.get("session_id")) or target.get("adapter") != "pi":
                return Validation.bad("bad_target", "")
            if target.get("directory"):
                return Validation.bad("unsupported_capability", "")
            snapshot = self.binding_snapshot()
            if ((expected_binding is not None and expected_binding != snapshot)
                    or (target.get("provider_binding") is not None and target["provider_binding"] != snapshot)):
                raise DeliveryError("backend_unverifiable", not_dispatched=True)
            cap = self._call({"v": 1, "op": "capabilities"}, snapshot).get("capabilities")
            required = {"protocol": 1, "contract": "pi-a2a-external-v1", "expectReply": False,
                        "maxCaller": 48, "maxRequestId": 96, "maxTextBytes": 32768,
                        "maxInputBytes": 262144, "maxOutputBytes": 8192,
                        "maxRetainedIdentities": 50000, "maxActivePayloads": 128,
                        "maxStateBytes": 134217728,
                        "capacityScope": "shared_ledger", "inspection": "read_only",
                        "targetSnapshot": True, "reservation": False}
            if (not isinstance(cap, dict) or any(type(cap.get(k)) is not type(v) or cap[k] != v for k, v in required.items())
                    or set(cap.get("operations", [])) != {"capabilities", "lookup", "send", "status", "cancel"}
                    or set(cap.get("delivery", [])) != {"queue", "steer"}):
                return Validation.bad("unsupported_capability", "")
            found = self._call({"v": 1, "op": "lookup", "to": target["session_id"]}, snapshot).get("target")
            if (not isinstance(found, dict) or found.get("to") != target["session_id"]
                    or any(type(found.get(k)) is not bool for k in ("exists", "live", "capable", "snapshot", "reservation"))
                    or found["snapshot"] is not True or found["reservation"] is not False):
                raise DeliveryError("unavailable")
            if not found["exists"]:
                return Validation.bad("bad_target", "")
            if not found["capable"]:
                return Validation.bad("unsupported_capability", "")
            if not found["live"]:
                return Validation.bad("unavailable", "")
            return Validation.good()
        except (DeliveryError, TypeError, ValueError) as exc:
            return Validation.bad(exc.code if isinstance(exc, DeliveryError) else "unavailable", "")

    def prepare_body(self, caller, request_id, text, urgent, target):
        if (not re.fullmatch(r"docket-[0-9a-f]{32}", caller)
                or not re.fullmatch(r"msg_[0-9a-f]{32}", request_id)
                or not text.strip() or len(text.encode("utf-8")) > 32768):
            raise DeliveryError("unsupported_capability", not_dispatched=True)
        return {"v": 1, "op": "send", "caller": caller, "id": request_id,
                "to": target["session_id"], "text": text,
                "delivery": "steer" if urgent else "queue", "expectReply": False}

    def _frame(self, frozen, op):
        body = json.loads(frozen["body"])
        expected = self.prepare_body(frozen["caller"], body["id"], body["text"],
                                     frozen["urgent"], frozen["target"])
        if body != expected:
            raise DeliveryError("conflict", not_dispatched=True)
        # Frozen body already includes to; status/cancel never accept payload fields.
        if op == "send":
            return body
        return {"v": 1, "op": op, "caller": body["caller"], "id": body["id"]}

    def _receipt(self, frozen, op):
        response = self._call(self._frame(frozen, op), frozen["binding"])
        if not response["ok"]:
            error = response["error"]
            if error == "UNKNOWN" and op == "status":
                return Receipt(None, "queue", state="missing")
            if error == "TOO_LATE" and op == "cancel":
                return Receipt(None, "queue", state="too_late", raw={"facts": {"claimed": True}})
            raise DeliveryError("conflict" if error == "CONFLICT" else "acceptance_unknown")
        receipt = response.get("receipt")
        if not isinstance(receipt, dict):
            raise DeliveryError("acceptance_unknown")
        facts = receipt.get("facts")
        keys = ("admitted", "claimed", "injected", "observed", "consumed", "historicalUncertainty")
        state = receipt.get("state")
        evidence = {"not_checked", "missing", "unsafe", "changed", "partial_header", "session_mismatch",
                    "oversized_record", "invalid_record", "partial_record", "scanning", "absent", "unreadable",
                    "saved_exact", "deadline", "owner_unavailable"}
        if (not isinstance(facts, dict) or any(type(facts.get(k)) is not bool for k in keys)
                or state not in ("admitted", "injected", "consumed", "cancelled", "uncertain", "failed")
                or type(receipt.get("cancelAvailable")) is not bool
                or receipt.get("retry") != "same_key_only" or receipt.get("evidence") not in evidence):
            raise DeliveryError("acceptance_unknown")
        consumed = state == "consumed" and facts["consumed"] and receipt.get("evidence") == "saved_exact"
        if (facts["consumed"] != consumed or (state == "consumed" and not consumed)
                or (facts["claimed"] and receipt["cancelAvailable"])
                or (facts["injected"] and not facts["claimed"])
                or (facts["observed"] and not facts["injected"])
                or (state in ("admitted", "injected") and not facts["admitted"])
                or (state == "injected" and not facts["injected"])
                or (facts["consumed"] and not all(facts[k] for k in ("claimed", "injected", "observed")))
                or (any(facts[k] for k in ("claimed", "injected", "observed", "consumed")) and not facts["admitted"])):
            raise DeliveryError("acceptance_unknown")
        facts = dict(facts)
        if state == "cancelled":
            if facts["claimed"] or facts["consumed"]:
                raise DeliveryError("acceptance_unknown")
            facts["cancelled"] = True  # Unknown tombstone need not imply admitted.
        mapped = ("consumed" if consumed else "failed" if state == "failed" and not facts["claimed"]
                  else "missing" if state == "cancelled"
                  else "uncertain" if state in ("uncertain", "failed")
                  else "admitted" if facts["admitted"] else "uncertain")
        body = json.loads(frozen["body"])
        return Receipt(body["id"], body["delivery"], admission_id=body["id"],
                       state=mapped, consumed=consumed, raw={"facts": facts})

    def deliver_frozen(self, frozen):
        return self._receipt(frozen, "send")

    def status_frozen(self, frozen):
        return self._receipt(frozen, "status")

    def cancel_frozen(self, frozen):
        return self._receipt(frozen, "cancel")
