"""Admission state contract; the opt-in event worker owns adapter invocation.

Existing DeliveryAdapter.reconcile/Receipt shapes are reused. The existing host's
deliver(resume=True) cannot enforce atomic stop precedence/profile narrowing, so
this module deliberately cannot send. Trusted snapshots below are local
preconditions, NOT a host fence. All mutation methods are internal adapter seams,
never producer HTTP operations. Only explicit construction installs these tables.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import secrets
import hashlib

from docket.delivery import Receipt
from docket.subscriptions import SubscriptionRefusal, SubscriptionStore, _json

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscription_admissions (
 delivery_id TEXT PRIMARY KEY, generation TEXT NOT NULL,
 admission_id TEXT UNIQUE NOT NULL, binding_json TEXT NOT NULL,
 phase TEXT NOT NULL CHECK(phase IN ('ready','sending','admitted','consumed','uncertain','lost')),
 blocked INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscription_admission_history (
 sequence INTEGER PRIMARY KEY, delivery_id TEXT NOT NULL,
 generation TEXT NOT NULL, phase TEXT NOT NULL, blocked INTEGER NOT NULL,
 reason TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscription_handler_terminals (
 delivery_id TEXT PRIMARY KEY, generation TEXT NOT NULL,
 receipt_json TEXT NOT NULL, recorded_at TEXT NOT NULL
);
"""
BINDING_KEYS = ("target", "owner_generation", "profile_id", "profile_version",
                "baseline_version", "model", "role")


class AdmissionStore(SubscriptionStore):
    def __init__(self, path):
        super().__init__(path)
        with self._lock:
            self._db.executescript(SCHEMA)
            self._commit()

    def _delivery(self, delivery_id):
        row = self._db.execute(
            "SELECT d.*,s.config_json AS installed_json,s.revoked AS subscription_revoked,"
            "p.revoked AS publisher_revoked FROM subscription_deliveries d "
            "JOIN event_subscriptions s ON s.id=d.subscription_id "
            "JOIN subscription_publishers p ON p.id=s.publisher_id WHERE d.id=?",
            (delivery_id,)).fetchone()
        if row is None or row["prepared_json"] is None:
            raise SubscriptionRefusal("delivery not prepared")
        return row

    @staticmethod
    def _refusal(delivery, snapshot):
        if delivery["subscription_revoked"] or delivery["publisher_revoked"]:
            return "installed subscription or publisher revoked"
        if delivery["config_json"] != delivery["installed_json"]:
            return "installed configuration changed"
        if not isinstance(snapshot, dict) or set(snapshot) != set(BINDING_KEYS) | {"stopped", "revoked"}:
            return "trusted owner/profile snapshot unavailable"
        if snapshot["stopped"] is not False or snapshot["revoked"] is not False:
            return "owner stopped or owner/profile revoked"
        prepared = json.loads(delivery["prepared_json"])
        if any(snapshot[key] != prepared[key] for key in BINDING_KEYS):
            return "owner/profile/version binding changed"
        return None

    def prepare_admission(self, delivery_id, *, trusted_snapshot):
        """Persist immutable prepared binding/generation, no external effects."""
        with self._transaction():
            delivery = self._delivery(delivery_id)
            reason = self._refusal(delivery, trusted_snapshot)
            if reason:
                # A retained candidate cannot be unblocked by a later clear snapshot.
                old = self._db.execute("SELECT * FROM subscription_admissions WHERE delivery_id=?", (delivery_id,)).fetchone()
                if old:
                    self._change(old, old["phase"], reason, blocked=True)
                else:
                    raise SubscriptionRefusal(reason, code="forbidden")
            else:
                self._db.execute(
                    "INSERT OR IGNORE INTO subscription_admissions VALUES (?,?,?,?,?,0,?)",
                    (delivery_id, secrets.token_hex(16), delivery["admission_id"],
                     delivery["prepared_json"], "ready", "candidate only; host fence required"))
                old = self._row(delivery_id)
                if old["binding_json"] != delivery["prepared_json"]:
                    self._change(old, old["phase"], "prepared binding changed", blocked=True)
            return self._public(self._row(delivery_id))

    def _row(self, delivery_id, generation=None):
        row = self._db.execute("SELECT * FROM subscription_admissions WHERE delivery_id=?", (delivery_id,)).fetchone()
        if row is None or (generation is not None and row["generation"] != generation):
            raise SubscriptionRefusal("admission generation unavailable", code="conflict")
        return row

    @staticmethod
    def _public(row):
        return {key: row[key] for key in ("delivery_id", "generation", "admission_id", "phase", "blocked", "reason")}

    def state(self, delivery_id):
        with self._lock:
            return self._public(self._row(delivery_id))

    def handler_terminal(self, delivery_id):
        """Separate trusted terminal evidence; plain consumption is not finish."""
        with self._lock:
            row = self._db.execute("SELECT receipt_json FROM subscription_handler_terminals WHERE delivery_id=?", (delivery_id,)).fetchone()
            return json.loads(row[0]) if row else None

    @staticmethod
    def _terminal_proof(row, receipt):
        prepared = json.loads(row["binding_json"])
        proof = receipt.raw.get("handler_terminal") if isinstance(receipt.raw, dict) else None
        if prepared.get("action") != "handler" or not isinstance(proof, dict):
            return None
        terminal = proof.get("native_terminal")
        if (not isinstance(terminal, dict) or proof.get("phase") != "pruned"
                or proof.get("consumed") is not True or proof.get("trusted_completion") is not True
                or proof.get("delivery_id") != row["delivery_id"]
                or proof.get("admission_id") != row["admission_id"] or proof.get("generation") != row["generation"]
                or not isinstance(proof.get("environment_id"), str) or not proof["environment_id"]
                or not isinstance(proof.get("session_id"), str) or not proof["session_id"].startswith("ses_")
                or terminal.get("session_id") != proof["session_id"]
                or terminal.get("admission_id") != row["admission_id"]
                or terminal.get("body_sha256") != hashlib.sha256(prepared["briefing"].encode()).hexdigest()
                or not isinstance(terminal.get("terminal_id"), str) or not terminal["terminal_id"].startswith("msg_")
                or terminal.get("outcome") not in ("succeeded", "failed", "interrupted")
                or terminal.get("not_active") is not True or terminal.get("empty_inbox") is not True
                or not isinstance(proof.get("output_manifest"), list)):
            return None
        # Explicit evidence projection: never save a raw grant or host error.
        return {**{key: proof[key] for key in ("delivery_id", "admission_id", "generation", "environment_id", "session_id",
                  "phase", "consumed", "trusted_completion", "output_manifest")},
                "native_terminal": {key: terminal[key] for key in ("session_id", "admission_id", "body_sha256",
                  "terminal_id", "outcome", "not_active", "empty_inbox")}}

    def _change(self, row, phase, reason, *, blocked=None):
        blocked = row["blocked"] if blocked is None else int(blocked)
        if (row["phase"], row["reason"], row["blocked"]) == (phase, reason, blocked):
            return
        self._db.execute("UPDATE subscription_admissions SET phase=?,blocked=?,reason=? WHERE delivery_id=? AND generation=?",
                         (phase, blocked, reason, row["delivery_id"], row["generation"]))
        self._db.execute("INSERT INTO subscription_admission_history(delivery_id,generation,phase,blocked,reason,at) VALUES (?,?,?,?,?,?)",
                         (row["delivery_id"], row["generation"], phase, blocked, reason,
                          datetime.now(timezone.utc).isoformat()))
        if phase in ("admitted", "consumed"):
            self._db.execute("UPDATE subscription_deliveries SET status='admitted' WHERE id=?",
                             (row["delivery_id"],))

    def record_send_intent(self, delivery_id, generation, *, trusted_snapshot, claim_guard=None):
        """Local crash-point contract only, NOT execution permission.

        claimed is true once. No send implementation exists here. A future fenced
        host adapter must independently enforce stop/narrowing at execution time.
        Never steal/reset this claim on age or a missing host message.
        """
        claimed = False
        with self._transaction():
            row = self._row(delivery_id, generation)
            delivery = self._delivery(delivery_id)
            reason = self._refusal(delivery, trusted_snapshot)
            if row["binding_json"] != delivery["prepared_json"]:
                reason = "prepared binding changed"
            if reason:
                self._change(row, row["phase"], reason, blocked=True)
            elif not row["blocked"] and row["phase"] == "ready" and (claim_guard is None or claim_guard()):
                self._change(row, "sending", "send intent only; external admission not established")
                claimed = True
            return {**self._public(self._row(delivery_id)), "claimed": claimed,
                    "execution_authorized": False}

    def record_receipt(self, delivery_id, generation, receipt: Receipt):
        """Record trusted adapter evidence, not event-supplied success prose."""
        with self._transaction():
            row = self._row(delivery_id, generation)
            handler_finished = isinstance(receipt, Receipt) and receipt.state == "handler_finished"
            if row["phase"] == "consumed" and not handler_finished:
                return self._public(row)
            if row["phase"] not in ("sending", "uncertain", "admitted", "consumed"):
                raise SubscriptionRefusal("no matching send intent", code="conflict")
            valid = (isinstance(receipt, Receipt) and receipt.admission_id == row["admission_id"]
                     and receipt.inbox_id == row["admission_id"] and receipt.delivery == "queue"
                     and receipt.state in ("admitted", "consumed", "handler_finished") and type(receipt.consumed) is bool
                     and (receipt.state == "admitted" or receipt.consumed))
            proof = self._terminal_proof(row, receipt) if valid and handler_finished else None
            if handler_finished:
                valid = valid and proof is not None
            if valid and handler_finished:
                encoded = _json(proof)
                old = self._db.execute("SELECT receipt_json FROM subscription_handler_terminals WHERE delivery_id=?", (delivery_id,)).fetchone()
                if old and old[0] != encoded:
                    raise SubscriptionRefusal("handler terminal evidence conflict", code="conflict")
                self._db.execute("INSERT OR IGNORE INTO subscription_handler_terminals VALUES (?,?,?,?)",
                                 (delivery_id, generation, encoded, datetime.now(timezone.utc).isoformat()))
            if valid:
                self._change(row, "consumed" if receipt.consumed else "admitted",
                             "exact handler terminal receipt" if handler_finished else "exact adapter receipt")
            else:
                self._change(row, row["phase"] if row["phase"] in ("admitted", "consumed") else "uncertain",
                             "invalid or mismatched adapter receipt")
            return self._public(self._row(delivery_id))

    def observe(self, delivery_id, generation, outcome):
        """Reconcile exact admission lookup; missing never authorizes a resend."""
        with self._transaction():
            row = self._row(delivery_id, generation)
            if row["phase"] == "consumed":
                return self._public(row)  # A stale lookup cannot undo consumption.
            if outcome in ("admitted", "consumed") and row["phase"] != "ready":
                self._change(row, outcome, "exact admission lookup")
            elif outcome == "missing" and row["phase"] == "admitted":
                self._change(row, "lost", "confirmed admission no longer present; no replay")
            elif row["phase"] == "admitted":
                self._change(row, "admitted", "lookup unavailable; prior admission remains confirmed")
            elif row["phase"] not in ("ready", "lost"):
                self._change(row, "uncertain", "admission unresolved; no replay")
            elif outcome != "missing" and row["phase"] == "ready":
                self._change(row, "uncertain", "unexpected pre-send evidence; reconcile ownership")
            return self._public(self._row(delivery_id))

    def reconcile_candidate(self, delivery_id, generation, adapter, *, enabled=False):
        """Opt-in READ-only DeliveryAdapter use. Never calls deliver/cancel/resume.

        Disabled by default; tests supply deterministic fake adapters. No installed
        adapter discovery, credential loading or live scheduling occurs here.
        """
        if enabled is not True:
            raise SubscriptionRefusal("candidate reconciliation disabled", code="forbidden")
        with self._lock:
            row = self._row(delivery_id, generation)
            if row["phase"] == "consumed":
                return self._public(row)
            target = json.loads(row["binding_json"])["target"]
            admission_id = row["admission_id"]
        try:
            valid = adapter.validate(target)
            outcome = adapter.reconcile(target, admission_id) if valid.ok else "unavailable"
        except Exception:
            outcome = "unavailable"  # Never log host bodies/credentials/errors.
        return self.observe(delivery_id, generation, outcome)
