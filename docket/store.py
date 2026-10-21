"""SQLite item store. One writer process is the scheduler; clients use HTTP.

The scheduler and the HTTP server share one connection. Every public method
holds ``_lock``, and status changes that can race a cancel go through a
conditional update so a stale object cannot resurrect a cancelled row.

Opening an older database only adds missing tables. Item rows are not rewritten.
"""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from docket.bounds import ANONYMOUS_ACTOR, actor_bucket
from docket.delivery_state import decode_receipt, receipt_projection
from docket.platform import (
    create_private_file,
    prepare_private_db,
    private_directory,
    read_private_bytes,
    state_root,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  fire_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  reason TEXT NOT NULL,
  context_json TEXT NOT NULL DEFAULT '{}',
  target_json TEXT NOT NULL,
  urgent INTEGER NOT NULL DEFAULT 0,
  created_by TEXT,
  late INTEGER NOT NULL DEFAULT 0,
  late_by_seconds REAL,
  delivered_at TEXT,
  cancelled_at TEXT,
  error TEXT,
  receipt_json TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  firing_at TEXT,
  spec_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_due ON items(status, fire_at);
CREATE TABLE IF NOT EXISTS occurrences (
  id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  status TEXT NOT NULL,
  scheduled_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  late INTEGER NOT NULL DEFAULT 0,
  late_by_seconds REAL,
  delivered_at TEXT,
  error TEXT,
  receipt_json TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  firing_at TEXT,
  coalesced INTEGER NOT NULL DEFAULT 0,
  detail_json TEXT,
  UNIQUE(item_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_occ_item ON occurrences(item_id, seq);
CREATE INDEX IF NOT EXISTS idx_occ_status ON occurrences(status, updated_at);
CREATE TABLE IF NOT EXISTS delivery_meta (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  writer_format INTEGER NOT NULL,
  caller TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_requests (
  unit_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  occurrence_id TEXT UNIQUE,
  caller TEXT NOT NULL,
  request_id TEXT UNIQUE,
  provenance TEXT NOT NULL,
  frozen_json TEXT,
  digest TEXT,
  send_started_at TEXT,
  dispatch_state TEXT NOT NULL DEFAULT 'never',
  facts_json TEXT NOT NULL DEFAULT '{}',
  issue_json TEXT,
  check_issue_json TEXT,
  checked_at TEXT,
  phase TEXT NOT NULL DEFAULT 'prepared',
  stop_requested INTEGER NOT NULL DEFAULT 0,
  manual_used INTEGER NOT NULL DEFAULT 0,
  manual_pending INTEGER NOT NULL DEFAULT 0,
  revision INTEGER NOT NULL DEFAULT 0,
  CHECK ((occurrence_id IS NULL AND unit_id=item_id)
         OR (occurrence_id IS NOT NULL AND occurrence_id=unit_id))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_one_shot
  ON delivery_requests(item_id) WHERE occurrence_id IS NULL;
"""

# Not terminal. The default pending view includes every one of these.
OUTSTANDING = ("pending", "firing", "admitted", "ambiguous")
CANCELLABLE = OUTSTANDING


def home() -> Path:
    root = state_root()
    _private_dir(root)
    return root


def token_path() -> Path:
    return home() / "token"


def load_token() -> str:
    path = token_path()
    # Exclusive creation prevents a world-readable creation window. Existing
    # tokens are verified by handle, never followed through a symlink.
    value = secrets.token_urlsafe(32)
    try:
        create_private_file(path, (value + "\n").encode("utf-8"))
        return value
    except FileExistsError:
        return read_private_bytes(path, max_bytes=4096).decode("utf-8").strip()


def server_path() -> Path:
    return home() / "server.json"


@dataclass
class Item:
    id: str
    kind: str
    status: str
    fire_at: datetime
    created_at: datetime
    updated_at: datetime
    reason: str
    context: dict
    target: dict
    urgent: bool
    created_by: str | None
    late: bool
    late_by_seconds: float | None
    delivered_at: datetime | None
    cancelled_at: datetime | None
    error: str | None
    receipt: dict | None
    attempts: int
    next_attempt_at: datetime | None
    firing_at: datetime | None
    spec: dict | None
    capacity_reserved: bool = False
    receipt_malformed: bool = False

    def public(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "fire_at": _iso(self.fire_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "reason": self.reason,
            "context": self.context,
            "target": self.target,
            "urgent": self.urgent,
            "created_by": self.created_by,
            "created_by_source": (
                "anonymous" if actor_bucket(self.created_by) == ANONYMOUS_ACTOR else "self-reported"
            ),
            "late": self.late,
            "late_by_seconds": self.late_by_seconds,
            "delivered_at": _iso(self.delivered_at) if self.delivered_at else None,
            "cancelled_at": _iso(self.cancelled_at) if self.cancelled_at else None,
            "error": self.error,
            "receipt": self.receipt,
            "attempts": self.attempts,
            "next_attempt_at": _iso(self.next_attempt_at) if self.next_attempt_at else None,
            "spec": self.spec,
        }


@dataclass
class Occurrence:
    id: str
    item_id: str
    seq: int
    status: str
    scheduled_at: datetime
    created_at: datetime
    updated_at: datetime
    late: bool
    late_by_seconds: float | None
    delivered_at: datetime | None
    error: str | None
    receipt: dict | None
    attempts: int
    next_attempt_at: datetime | None
    firing_at: datetime | None
    coalesced: int
    detail: dict | None
    receipt_malformed: bool = False

    def public(self) -> dict:
        return {
            "id": self.id,
            "item_id": self.item_id,
            "seq": self.seq,
            "status": self.status,
            "scheduled_at": _iso(self.scheduled_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "late": self.late,
            "late_by_seconds": self.late_by_seconds,
            "delivered_at": _iso(self.delivered_at) if self.delivered_at else None,
            "error": self.error,
            "receipt": self.receipt,
            "attempts": self.attempts,
            "next_attempt_at": _iso(self.next_attempt_at) if self.next_attempt_at else None,
            "coalesced": self.coalesced,
            "detail": self.detail,
        }


class Store:
    def __init__(self, path: Path):
        self.path = path
        # Refuse symlinks/hardlinks/unprivate files before sqlite can read or mutate the file.
        prepare_private_db(path)
        self._lock = threading.RLock()
        # Ownership is deliberately not durable: a matching port is not a server identity.
        self._binding_owners: set[str] = set()
        self._caller_identity: str | None = None
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        try:
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            had_delivery_meta = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_meta'"
            ).fetchone() is not None
            self._db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
            meta = self._db.execute("SELECT * FROM delivery_meta WHERE singleton=1").fetchone()
            if not meta:
                if had_delivery_meta or self._db.execute("SELECT 1 FROM delivery_requests LIMIT 1").fetchone():
                    raise ValueError("delivery caller metadata missing; refusing caller rotation")
                self._db.execute("INSERT INTO delivery_meta VALUES (1,1,?)", ("docket-" + secrets.token_hex(16),))
            caller = self._delivery_caller()
            self._caller_identity = caller
            if self._db.execute(
                "SELECT 1 FROM delivery_requests WHERE caller<>? LIMIT 1", (caller,)
            ).fetchone():
                raise ValueError("delivery caller metadata does not match existing requests")
            self._commit()
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _delivery_caller(self) -> str:
        """Caller creation is startup-only; missing/malformed identity is never repaired."""
        meta = self._db.execute("SELECT * FROM delivery_meta WHERE singleton=1").fetchone()
        if not meta:
            raise ValueError("delivery caller metadata missing; refusing caller rotation")
        if meta["writer_format"] != 1:
            raise ValueError("unsupported delivery writer format")
        if not isinstance(meta["caller"], str) or re.fullmatch(r"docket-[0-9a-f]{32}", meta["caller"]) is None:
            raise ValueError("malformed delivery caller metadata; refusing caller rotation")
        if self._caller_identity is not None and meta["caller"] != self._caller_identity:
            raise ValueError("delivery caller metadata changed during this process; refusing caller rotation")
        return meta["caller"]

    def _check_request_caller(self, request) -> str:
        caller = self._delivery_caller()
        if request and request["caller"] != caller:
            raise ValueError("delivery caller metadata does not match existing requests")
        return caller

    def get_request(self, unit_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM delivery_requests WHERE unit_id=?", (unit_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["binding_owned"] = unit_id in self._binding_owners
            for key, column in (("frozen", "frozen_json"), ("facts", "facts_json"),
                                ("issue", "issue_json"), ("check_issue", "check_issue_json")):
                result[key] = json.loads(result.pop(column)) if row[column] else None
            return result

    def reserve_request(self, item: Item, occ: Occurrence | None = None) -> dict:
        """Reserve once. An attempted legacy row never acquires a replacement id/body."""
        unit = occ or item
        with self._lock:
            existing = self.get_request(unit.id)
            caller = self._check_request_caller(existing)
            if existing:
                return existing
            table = "occurrences" if occ else "items"
            row = self._db.execute(f"SELECT * FROM {table} WHERE id=?", (unit.id,)).fetchone()
            if row is None:
                raise ValueError("missing delivery unit")
            receipt, _malformed = decode_receipt(row["receipt_json"])
            receipt = receipt if isinstance(receipt, dict) else {}
            pristine = (row["status"] == "pending" and row["attempts"] == 0
                        and row["receipt_json"] is None and row["firing_at"] is None
                        and row["delivered_at"] is None)
            aid = "msg_" + secrets.token_hex(16) if pristine else (receipt.get("admission_id") or receipt.get("inbox_id"))
            if not isinstance(aid, str):
                aid = None  # No usable identity; retain the original receipt and never mint a replacement.
            facts = {}
            if receipt.get("state") in ("admitted", "consumed"):
                facts["admitted"] = True
            if receipt.get("state") == "consumed" or row["status"] == "delivered":
                facts["consumed"] = True
            self._db.execute(
                """INSERT INTO delivery_requests
                   (unit_id,item_id,occurrence_id,caller,request_id,provenance,dispatch_state,facts_json,stop_requested)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (unit.id, item.id, occ.id if occ else None, caller, aid,
                 "reserved" if pristine else "legacy", "never" if pristine else "possible",
                 json.dumps(facts), int(item.status == "cancelled")),
            )
            self._commit()
            return self.get_request(unit.id)

    def freeze_request(self, unit_id: str, frozen: dict) -> dict:
        """Only this method writes immutable fields, exactly once, before sending."""
        raw = json.dumps(frozen, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with self._lock:
            found = self.get_request(unit_id)
            self._check_request_caller(found)
            if not found or found["provenance"] == "legacy":
                raise ValueError("legacy envelope cannot be reconstructed")
            if found["frozen"] is not None:
                if found["digest"] != hashlib.sha256(raw.encode()).hexdigest():
                    raise ValueError("immutable request conflict")
                return found
            self._db.execute(
                """UPDATE delivery_requests SET frozen_json=?,digest=?,provenance='frozen',revision=revision+1
                   WHERE unit_id=? AND frozen_json IS NULL AND stop_requested=0""",
                (raw, hashlib.sha256(raw.encode()).hexdigest(), unit_id),
            )
            self._commit()
            if self.get_request(unit_id)["frozen"] is not None:
                self._binding_owners.add(unit_id)
            return self.get_request(unit_id)

    def begin_send(self, unit_id: str, now: datetime) -> bool:
        """Commit uncertainty before I/O; caller also holds its in-process unit gate."""
        with self._lock:
            req = self.get_request(unit_id)
            self._check_request_caller(req)
            if not req or not req["frozen"] or req["dispatch_state"] == "possible" or req["stop_requested"]:
                return False
            parent = self.get(req["item_id"])
            unit = self.get_occurrence(req["occurrence_id"]) if req["occurrence_id"] else parent
            if (not parent or parent.status not in ("pending", "firing") or not unit
                    or unit.status != "firing" or any(req["facts"].values())):
                return False
            if req["occurrence_id"] and (parent.spec or {}).get("open_occurrence_id") != unit.id:
                return False
            self._db.execute(
                """UPDATE delivery_requests SET send_started_at=?,dispatch_state='possible',phase='sending',
                   manual_pending=0,revision=revision+1 WHERE unit_id=?""", (_iso(now), unit_id),
            )
            self._commit()
            return True

    def note_request(self, unit_id: str, *, facts=None, diagnostic=None,
                     phase=None, checked_at=None, check_issue=None) -> None:
        """Merge positive evidence, never overwrite identity or erase a positive fact."""
        with self._lock:
            req = self.get_request(unit_id)
            if not req:
                return
            merged = dict(req["facts"])
            merged.update({key: True for key, value in (facts or {}).items()
                           if value is True and key in ("admitted", "claimed", "injected", "observed", "consumed", "cancelled", "historicalUncertainty", "failed_before_claim")})
            self._db.execute(
                """UPDATE delivery_requests SET facts_json=?,issue_json=?,dispatch_state=?,phase=?,
                   checked_at=?,check_issue_json=?,revision=revision+1 WHERE unit_id=?""",
                (json.dumps(merged), json.dumps(diagnostic) if diagnostic else None,
                 req["dispatch_state"], phase or req["phase"],
                 _iso(checked_at) if checked_at else req["checked_at"],
                 json.dumps(check_issue) if check_issue else None, unit_id),
            )
            self._commit()

    def authorize_retry(self, unit_id: str, now: datetime) -> str | None:
        from docket.delivery_state import retry_refusal
        with self._lock:
            req = self.get_request(unit_id)
            refusal = retry_refusal(req)
            if refusal:
                return refusal
            parent = self.get(req["item_id"])
            occ = self.get_occurrence(req["occurrence_id"]) if req["occurrence_id"] else None
            if not parent or parent.status == "cancelled":
                return "cancelled_identity"
            if occ and (parent.spec or {}).get("open_occurrence_id") != occ.id:
                return "schedule_advanced"
            if parent.kind in ("recurring", "watch") and parent.status not in ("pending", "firing"):
                return "schedule_finished"
            unit = occ or parent
            if unit.status not in ("pending", "ambiguous", "undeliverable"):
                return "not_ready"
            if req["manual_pending"]:
                return None
            self._db.execute(
                "UPDATE delivery_requests SET manual_used=1,manual_pending=1,revision=revision+1 WHERE unit_id=?",
                (unit_id,),
            )
            table = "occurrences" if occ else "items"
            self._db.execute(f"UPDATE {table} SET status='pending',next_attempt_at=?,updated_at=? WHERE id=?",
                             (_iso(now), _iso(now), unit.id))
            if occ:
                self._db.execute("UPDATE items SET next_attempt_at=?,updated_at=? WHERE id=?",
                                 (_iso(now), _iso(now), parent.id))
            self._commit()
            return None

    def invalidate_binding(self, unit_id: str) -> None:
        with self._lock:
            self._binding_owners.discard(unit_id)

    def stopped_unresolved_requests(self) -> list[dict]:
        with self._lock:
            ids = [row[0] for row in self._db.execute(
                "SELECT unit_id FROM delivery_requests WHERE stop_requested=1 AND dispatch_state='possible'"
            ).fetchall()]
            return [req for ident in ids if not (req := self.get_request(ident))["facts"].get("consumed")]

    def insert(self, item: Item) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO items (
                     id, kind, status, fire_at, created_at, updated_at, reason,
                     context_json, target_json, urgent, created_by, late,
                     late_by_seconds, delivered_at, cancelled_at, error,
                     receipt_json, attempts, next_attempt_at, firing_at, spec_json
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                _row(item),
            )
            self._commit()

    def get(self, item_id: str) -> Item | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            return _item(row) if row else None

    def list(self, status: str | None = None) -> list[Item]:
        with self._lock:
            if status:
                rows = self._db.execute(
                    "SELECT * FROM items WHERE status=? ORDER BY fire_at, id", (status,)
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM items ORDER BY fire_at, id"
                ).fetchall()
            return [_item(row) for row in rows]

    def active_items(self) -> list[Item]:
        with self._lock:
            reserved = {row["item_id"] for row in self._db.execute(
                "SELECT item_id,facts_json FROM delivery_requests WHERE dispatch_state='possible'"
            ).fetchall() if not any(json.loads(row["facts_json"]).get(key) for key in ("consumed", "cancelled"))}
            rows = self._db.execute(
                """SELECT * FROM items
                   ORDER BY fire_at, id"""
            ).fetchall()
            found = []
            for row in rows:
                item = _item(row)
                item.capacity_reserved = item.id in reserved
                if item.status in OUTSTANDING or item.capacity_reserved:
                    found.append(item)
            return found

    def panel_items(self, status: str) -> tuple[list[Item], bool]:
        """Fixed read windows; the extra row detects truncation without a count scan."""
        if status not in ("outstanding", "terminal"):
            raise ValueError("unsupported panel status")
        limit = 200 if status == "outstanding" else 40
        predicate = "IN" if status == "outstanding" else "NOT IN"
        order = "fire_at, id" if status == "outstanding" else "updated_at DESC, id DESC"
        with self._lock:
            marks = ",".join("?" * len(OUTSTANDING))
            rows = self._db.execute(
                f"SELECT * FROM items WHERE status {predicate} ({marks}) ORDER BY {order} LIMIT ?",
                (*OUTSTANDING, limit + 1),
            ).fetchall()
            return [_item(row) for row in rows[:limit]], len(rows) > limit

    def panel_occurrences(self) -> tuple[list[Occurrence], bool]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM occurrences ORDER BY updated_at DESC, id DESC LIMIT 41"
            ).fetchall()
            return [_occurrence(row) for row in rows[:40]], len(rows) > 40

    def due_ids(self, now: datetime) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                """SELECT id FROM items
                   WHERE status='pending' AND fire_at<=?
                     AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                   ORDER BY fire_at, id""",
                (_iso(now), _iso(now)),
            ).fetchall()
            return [row["id"] for row in rows]

    def claim(self, item_id: str, now: datetime, *, max_attempts: int | None = None) -> bool:
        return self._claim_delivery("items", item_id, now, max_attempts)

    def _claim_delivery(self, table: str, unit_id: str, now: datetime, max_attempts: int | None) -> bool:
        """Claim + counter increment + manual authorization consumption commit together."""
        with self._lock:
            unit = self.get_occurrence(unit_id) if table == "occurrences" else self.get(unit_id)
            req = self.get_request(unit_id)
            parent = self.get(unit.item_id) if table == "occurrences" and unit else unit
            if not unit or unit.status != "pending" or not parent or parent.status == "cancelled":
                return False
            manual = bool(req and req["manual_pending"])
            if req and req["stop_requested"]:
                return False
            if max_attempts is not None and unit.attempts >= max_attempts and not manual:
                return False
            cur = self._db.execute(
                f"""UPDATE {table}
                    SET status='firing', firing_at=?, updated_at=?, attempts=attempts+1
                    WHERE id=? AND status='pending'""",
                (_iso(now), _iso(now), unit_id),
            )
            if cur.rowcount and manual:
                self._db.execute(
                    "UPDATE delivery_requests SET manual_pending=0,revision=revision+1 WHERE unit_id=?", (unit_id,),
                )
            self._commit()
            return cur.rowcount == 1

    def reclaim_ids(self, now: datetime, *, lease_seconds: float) -> list[str]:
        with self._lock:
            cutoff = _iso(_shift(now, -lease_seconds))
            rows = self._db.execute(
                """SELECT id FROM items
                   WHERE status='firing' AND (firing_at IS NULL OR firing_at<=?)""",
                (cutoff,),
            ).fetchall()
            return [row["id"] for row in rows]

    def save(self, item: Item) -> None:
        with self._lock:
            self._update_item(item)
            self._commit()

    def cas_item(self, item: Item, expect: tuple[str, ...]) -> bool:
        """Write ``item`` only if its current status is one of ``expect``."""
        if not expect:
            return False
        with self._lock:
            marks = ",".join("?" * len(expect))
            cur = self._db.execute(
                f"""UPDATE items SET
                      kind=?, status=?, fire_at=?, updated_at=?, reason=?,
                      context_json=?, target_json=?, urgent=?, created_by=?, late=?,
                      late_by_seconds=?, delivered_at=?, cancelled_at=?, error=?,
                      receipt_json=?, attempts=?, next_attempt_at=?, firing_at=?,
                      spec_json=?
                    WHERE id=? AND status IN ({marks})""",
                _update_values(item) + tuple(expect),
            )
            self._commit()
            return cur.rowcount == 1

    def cas_occurrence(self, occ: Occurrence, expect: tuple[str, ...]) -> bool:
        if not expect:
            return False
        with self._lock:
            marks = ",".join("?" * len(expect))
            cur = self._db.execute(
                f"""UPDATE occurrences SET
                      status=?, updated_at=?, late=?, late_by_seconds=?, delivered_at=?,
                      error=?, receipt_json=?, attempts=?, next_attempt_at=?, firing_at=?,
                      coalesced=?, detail_json=?
                    WHERE id=? AND status IN ({marks})""",
                _occ_update_values(occ) + tuple(expect),
            )
            self._commit()
            return cur.rowcount == 1

    def open_summaries(self) -> dict[str, dict]:
        """Sanitized outstanding occurrences, including inconsistent/missing pointers."""
        from docket.delivery_state import issue
        with self._lock:
            rows = self._db.execute(
                """SELECT id, item_id, status, receipt_json, error
                   FROM occurrences
                   WHERE status IN ('pending', 'firing', 'admitted', 'ambiguous')"""
            ).fetchall()
            found: dict[str, dict] = {}
            for row in rows:
                raw, malformed = decode_receipt(row["receipt_json"])
                receipt = receipt_projection(raw, malformed=malformed)
                code = row["error"].partition(":")[0] if isinstance(row["error"], str) else "unavailable"
                found[row["item_id"]] = {
                    "id": row["id"],
                    "status": row["status"],
                    "error": issue(code)["detail"] if row["error"] else None,
                    "delivery_state": receipt["state"],
                    "admission_id": receipt["admission_id"],
                    "receipt_issue": receipt["issue"],
                }
            return found

    def list_outstanding(self) -> list[Item]:
        with self._lock:
            marks = ",".join("?" * len(OUTSTANDING))
            rows = self._db.execute(
                f"""SELECT * FROM items
                    WHERE status IN ({marks})
                    ORDER BY fire_at, id""",
                OUTSTANDING,
            ).fetchall()
            return [_item(row) for row in rows]

    def cancel(self, item_id: str, now: datetime, *, receipt_state: str | None = None,
               error: str | None = None, admission_id: str | None = None,
               clear_error: bool = False) -> str:
        """Return 'cancelled', 'missing', or 'terminal'.

        Optional receipt fields are written in the same commit as the status
        change. A sending receipt must not be left looking live after cancel,
        and a consumed or lost admission must stay visible on the item the
        panel reads. This does not insert an occurrence or mint an id.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                return "missing"
            if row["status"] not in CANCELLABLE:
                return "terminal"
            if receipt_state or error or admission_id or clear_error:
                self._stamp_cancel_receipt(
                    item_id, state=receipt_state, error=error,
                    admission_id=admission_id, clear_error=clear_error,
                )
            marks = ",".join("?" * len(CANCELLABLE))
            self._db.execute(
                f"""UPDATE items SET status='cancelled', cancelled_at=?, updated_at=?
                    WHERE id=? AND status IN ({marks})""",
                (_iso(now), _iso(now), item_id, *CANCELLABLE),
            )
            self._db.execute(
                f"""UPDATE occurrences
                    SET status='cancelled', updated_at=?
                    WHERE item_id=? AND status IN ({marks})""",
                (_iso(now), item_id, *CANCELLABLE),
            )
            self._db.execute(
                "UPDATE delivery_requests SET stop_requested=1,manual_pending=0,revision=revision+1 WHERE item_id=?",
                (item_id,),
            )
            self._commit()
            return "cancelled"

    def note_outstanding_error(self, item_id: str, now: datetime, error: str) -> bool:
        """Record a failed cancel attempt without changing status or receipt ids."""
        with self._lock:
            marks = ",".join("?" * len(CANCELLABLE))
            cur = self._db.execute(
                f"""UPDATE items SET error=?, updated_at=?
                    WHERE id=? AND status IN ({marks})""",
                (error, _iso(now), item_id, *CANCELLABLE),
            )
            self._commit()
            return cur.rowcount == 1

    def _stamp_cancel_receipt(self, item_id: str, *, state: str | None, error: str | None,
                              admission_id: str | None, clear_error: bool) -> None:
        """Caller holds ``_lock``. Does not commit."""
        self._merge_receipt(
            "items", "id", item_id, state=state, error=error,
            admission_id=admission_id, clear_error=clear_error,
        )
        marks = ",".join("?" * len(CANCELLABLE))
        rows = self._db.execute(
            f"""SELECT id FROM occurrences
                WHERE item_id=? AND status IN ({marks})""",
            (item_id, *CANCELLABLE),
        ).fetchall()
        for row in rows:
            self._merge_receipt(
                "occurrences", "id", row["id"], state=state, error=error,
                admission_id=admission_id, clear_error=clear_error,
            )

    def _merge_receipt(self, table: str, column: str, ident: str, *, state: str | None,
                       error: str | None, admission_id: str | None, clear_error: bool) -> None:
        if table not in ("items", "occurrences") or column != "id":
            return
        row = self._db.execute(
            f"SELECT receipt_json FROM {table} WHERE {column}=?", (ident,)
        ).fetchone()
        if row is None:
            return
        receipt, _malformed = decode_receipt(row["receipt_json"])
        if not isinstance(receipt, dict):
            receipt = {}
        if admission_id:
            receipt["admission_id"] = admission_id
        if state:
            receipt["state"] = state
        assignments = ["receipt_json=?"]
        values: list = [json.dumps(receipt)]
        if clear_error:
            assignments.append("error=?")
            values.append(None)
        elif error is not None:
            assignments.append("error=?")
            values.append(error)
        values.append(ident)
        self._db.execute(
            f"UPDATE {table} SET {', '.join(assignments)} WHERE {column}=?",
            tuple(values),
        )

    def attach_occurrence(self, item: Item, occ: Occurrence) -> Occurrence | None:
        """Insert the occurrence and point the item at it, in one commit.

        Returns None if the item was cancelled or otherwise left the outstanding
        states before the insert. The caller must not deliver in that case.
        """
        with self._lock:
            current = self._db.execute(
                "SELECT status FROM items WHERE id=?", (item.id,)
            ).fetchone()
            if current is None or current["status"] not in ("pending", "firing"):
                return None
            existing = self._db.execute(
                "SELECT * FROM occurrences WHERE item_id=? AND seq=?",
                (item.id, occ.seq),
            ).fetchone()
            spec = dict(item.spec or {})
            if existing:
                found = _occurrence(existing)
                spec["open_occurrence_id"] = found.id
                spec["next_seq"] = max(int(spec.get("next_seq") or 1), found.seq + 1)
                item.spec = spec
                self._update_item(item)
                self._commit()
                return found
            self._insert_occurrence(occ)
            spec["open_occurrence_id"] = occ.id
            spec["next_seq"] = occ.seq + 1
            item.spec = spec
            self._update_item(item)
            self._commit()
            return occ

    def get_occurrence(self, occ_id: str) -> Occurrence | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM occurrences WHERE id=?", (occ_id,)
            ).fetchone()
            return _occurrence(row) if row else None

    def open_occurrence(self, item: Item) -> Occurrence | None:
        occ_id = (item.spec or {}).get("open_occurrence_id")
        if not occ_id:
            return None
        return self.get_occurrence(occ_id)

    def save_occurrence(self, occ: Occurrence) -> None:
        with self._lock:
            self._update_occurrence(occ)
            self._commit()

    def claim_occurrence(self, occ_id: str, now: datetime, *, max_attempts: int | None = None) -> bool:
        return self._claim_delivery("occurrences", occ_id, now, max_attempts)

    def reclaim_occurrence_ids(self, now: datetime, *, lease_seconds: float) -> list[str]:
        with self._lock:
            cutoff = _iso(_shift(now, -lease_seconds))
            rows = self._db.execute(
                """SELECT id FROM occurrences
                   WHERE status='firing' AND (firing_at IS NULL OR firing_at<=?)""",
                (cutoff,),
            ).fetchall()
            return [row["id"] for row in rows]

    def list_occurrences(self, item_id: str | None = None, status: str | None = None,
                         limit: int = 200) -> list[Occurrence]:
        with self._lock:
            clauses = []
            args: list = []
            if item_id:
                clauses.append("item_id=?")
                args.append(item_id)
            if status:
                clauses.append("status=?")
                args.append(status)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            order = "seq" if item_id else "updated_at DESC"
            rows = self._db.execute(
                f"SELECT * FROM occurrences{where} ORDER BY {order} LIMIT ?",
                (*args, limit),
            ).fetchall()
            return [_occurrence(row) for row in rows]

    def occurrence_counts(self) -> dict[str, dict[str, int]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT item_id, status, COUNT(*) AS n FROM occurrences GROUP BY item_id, status"
            ).fetchall()
            counts: dict[str, dict[str, int]] = {}
            for row in rows:
                counts.setdefault(row["item_id"], {})[row["status"]] = row["n"]
            return counts

    def count_occurrences(self, item_id: str, status: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM occurrences WHERE item_id=? AND status=?",
                (item_id, status),
            ).fetchone()
            return int(row["n"])

    def previous_occurrence(self, item_id: str, seq: int) -> Occurrence | None:
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM occurrences
                   WHERE item_id=? AND seq<?
                   ORDER BY seq DESC LIMIT 1""",
                (item_id, seq),
            ).fetchone()
            return _occurrence(row) if row else None

    def _update_item(self, item: Item) -> None:
        self._db.execute(
            """UPDATE items SET
                 kind=?, status=?, fire_at=?, updated_at=?, reason=?,
                 context_json=?, target_json=?, urgent=?, created_by=?, late=?,
                 late_by_seconds=?, delivered_at=?, cancelled_at=?, error=?,
                 receipt_json=?, attempts=?, next_attempt_at=?, firing_at=?,
                 spec_json=?
               WHERE id=?""",
            (
                item.kind, item.status, _iso(item.fire_at), _iso(item.updated_at),
                item.reason, json.dumps(item.context), json.dumps(item.target),
                int(item.urgent), item.created_by, int(item.late),
                item.late_by_seconds,
                _iso(item.delivered_at) if item.delivered_at else None,
                _iso(item.cancelled_at) if item.cancelled_at else None,
                item.error,
                json.dumps(item.receipt) if item.receipt is not None else None,
                item.attempts,
                _iso(item.next_attempt_at) if item.next_attempt_at else None,
                _iso(item.firing_at) if item.firing_at else None,
                json.dumps(item.spec) if item.spec is not None else None,
                item.id,
            ),
        )

    def _commit(self) -> None:
        try:
            # Ancillary protection must not turn durable success into an error.
            # SQLite can create sidecars later inside commit: verified private
            # inheritance and startup recovery cover those, not a post-commit seal.
            _seal_db(self.path)
            self._db.commit()
        except BaseException:
            # Do not leave a failed mutation pending for a later call to commit.
            self._db.rollback()
            raise

    def _insert_occurrence(self, occ: Occurrence) -> None:
        self._db.execute(
            """INSERT INTO occurrences (
                 id, item_id, seq, status, scheduled_at, created_at, updated_at,
                 late, late_by_seconds, delivered_at, error, receipt_json,
                 attempts, next_attempt_at, firing_at, coalesced, detail_json
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            _occ_row(occ),
        )

    def _update_occurrence(self, occ: Occurrence) -> None:
        self._db.execute(
            """UPDATE occurrences SET
                 status=?, updated_at=?, late=?, late_by_seconds=?, delivered_at=?,
                 error=?, receipt_json=?, attempts=?, next_attempt_at=?, firing_at=?,
                 coalesced=?, detail_json=?
               WHERE id=?""",
            (
                occ.status, _iso(occ.updated_at), int(occ.late), occ.late_by_seconds,
                _iso(occ.delivered_at) if occ.delivered_at else None,
                occ.error,
                json.dumps(occ.receipt) if occ.receipt is not None else None,
                occ.attempts,
                _iso(occ.next_attempt_at) if occ.next_attempt_at else None,
                _iso(occ.firing_at) if occ.firing_at else None,
                occ.coalesced,
                json.dumps(occ.detail) if occ.detail is not None else None,
                occ.id,
            ),
        )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _shift(value: datetime, seconds: float) -> datetime:
    return value + timedelta(seconds=seconds)


def _row(item: Item) -> tuple:
    return (
        item.id, item.kind, item.status, _iso(item.fire_at), _iso(item.created_at),
        _iso(item.updated_at), item.reason, json.dumps(item.context),
        json.dumps(item.target), int(item.urgent), item.created_by, int(item.late),
        item.late_by_seconds,
        _iso(item.delivered_at) if item.delivered_at else None,
        _iso(item.cancelled_at) if item.cancelled_at else None,
        item.error,
        json.dumps(item.receipt) if item.receipt is not None else None,
        item.attempts,
        _iso(item.next_attempt_at) if item.next_attempt_at else None,
        _iso(item.firing_at) if item.firing_at else None,
        json.dumps(item.spec) if item.spec is not None else None,
    )


def _item(row: sqlite3.Row) -> Item:
    receipt, malformed = decode_receipt(row["receipt_json"])
    return Item(
        id=row["id"],
        kind=row["kind"],
        status=row["status"],
        fire_at=_parse(row["fire_at"]),
        created_at=_parse(row["created_at"]),
        updated_at=_parse(row["updated_at"]),
        reason=row["reason"],
        context=json.loads(row["context_json"]),
        target=json.loads(row["target_json"]),
        urgent=bool(row["urgent"]),
        created_by=row["created_by"],
        late=bool(row["late"]),
        late_by_seconds=row["late_by_seconds"],
        delivered_at=_parse(row["delivered_at"]),
        cancelled_at=_parse(row["cancelled_at"]),
        error=row["error"],
        receipt=receipt,
        receipt_malformed=malformed,
        attempts=row["attempts"],
        next_attempt_at=_parse(row["next_attempt_at"]),
        firing_at=_parse(row["firing_at"]),
        spec=json.loads(row["spec_json"]) if row["spec_json"] else None,
    )


def _occ_row(occ: Occurrence) -> tuple:
    return (
        occ.id, occ.item_id, occ.seq, occ.status, _iso(occ.scheduled_at),
        _iso(occ.created_at), _iso(occ.updated_at), int(occ.late), occ.late_by_seconds,
        _iso(occ.delivered_at) if occ.delivered_at else None,
        occ.error,
        json.dumps(occ.receipt) if occ.receipt is not None else None,
        occ.attempts,
        _iso(occ.next_attempt_at) if occ.next_attempt_at else None,
        _iso(occ.firing_at) if occ.firing_at else None,
        occ.coalesced,
        json.dumps(occ.detail) if occ.detail is not None else None,
    )


def _occurrence(row: sqlite3.Row) -> Occurrence:
    receipt, malformed = decode_receipt(row["receipt_json"])
    return Occurrence(
        id=row["id"],
        item_id=row["item_id"],
        seq=row["seq"],
        status=row["status"],
        scheduled_at=_parse(row["scheduled_at"]),
        created_at=_parse(row["created_at"]),
        updated_at=_parse(row["updated_at"]),
        late=bool(row["late"]),
        late_by_seconds=row["late_by_seconds"],
        delivered_at=_parse(row["delivered_at"]),
        error=row["error"],
        receipt=receipt,
        receipt_malformed=malformed,
        attempts=row["attempts"],
        next_attempt_at=_parse(row["next_attempt_at"]),
        firing_at=_parse(row["firing_at"]),
        coalesced=row["coalesced"] or 0,
        detail=json.loads(row["detail_json"]) if row["detail_json"] else None,
    )


def _update_values(item: Item) -> tuple:
    return (
        item.kind, item.status, _iso(item.fire_at), _iso(item.updated_at),
        item.reason, json.dumps(item.context), json.dumps(item.target),
        int(item.urgent), item.created_by, int(item.late),
        item.late_by_seconds,
        _iso(item.delivered_at) if item.delivered_at else None,
        _iso(item.cancelled_at) if item.cancelled_at else None,
        item.error,
        json.dumps(item.receipt) if item.receipt is not None else None,
        item.attempts,
        _iso(item.next_attempt_at) if item.next_attempt_at else None,
        _iso(item.firing_at) if item.firing_at else None,
        json.dumps(item.spec) if item.spec is not None else None,
        item.id,
    )


def _occ_update_values(occ: Occurrence) -> tuple:
    return (
        occ.status, _iso(occ.updated_at), int(occ.late), occ.late_by_seconds,
        _iso(occ.delivered_at) if occ.delivered_at else None,
        occ.error,
        json.dumps(occ.receipt) if occ.receipt is not None else None,
        occ.attempts,
        _iso(occ.next_attempt_at) if occ.next_attempt_at else None,
        _iso(occ.firing_at) if occ.firing_at else None,
        occ.coalesced,
        json.dumps(occ.detail) if occ.detail is not None else None,
        occ.id,
    )


def _private_dir(path: Path) -> None:
    private_directory(path)


def _seal_db(path: Path) -> None:
    """Owner-only database, WAL, and SHM. The directory is already private."""
    prepare_private_db(path, seal_sidecars=True)
