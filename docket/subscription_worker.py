"""Bounded deterministic event work inside the existing scheduler process.

The host adapter is trusted installed code, never selected by an event. It must
enforce the frozen profile at execution, not merely validate a local snapshot.
An intent is never retried: all later ticks reconcile its exact stable identity.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import secrets
import time
import threading

from docket.subscription_admission import AdmissionStore, BINDING_KEYS
from docket.subscriptions import SubscriptionRefusal, _json

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscription_runtime (
 delivery_id TEXT PRIMARY KEY, deadline REAL NOT NULL, next_at REAL NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_until REAL,
 terminal TEXT, reason TEXT NOT NULL DEFAULT '', finished_at REAL
);
CREATE TABLE IF NOT EXISTS subscription_runtime_history (
 sequence INTEGER PRIMARY KEY, delivery_id TEXT NOT NULL, at REAL NOT NULL,
 reason TEXT NOT NULL, attempt INTEGER NOT NULL
);
"""
MAX_ATTEMPTS = 8
DEADLINE_SECONDS = 3600
LEASE_SECONDS = 60
ELIGIBLE = "(terminal IS NULL OR (terminal='deadletter' AND delivery_id IN (SELECT delivery_id FROM subscription_admissions WHERE phase IN ('sending','uncertain','admitted','consumed'))))"


class EventStore(AdmissionStore):
    def __init__(self, path):
        super().__init__(path)
        with self._lock:
            self._db.executescript(SCHEMA)
            self._commit()

    def due_events(self, now):
        with self._transaction():
            # Also adopts accepted rows from the earlier source-only candidate.
            for row in self._db.execute("SELECT d.id,e.accepted_at FROM subscription_deliveries d JOIN subscription_events e ON e.id=d.event_id").fetchall():
                accepted = datetime.fromisoformat(row["accepted_at"]).timestamp()
                self._db.execute("INSERT OR IGNORE INTO subscription_runtime(delivery_id,deadline,next_at) VALUES (?,?,?)",
                                 (row["id"], accepted + DEADLINE_SECONDS, accepted))
            return [row[0] for row in self._db.execute(
                "SELECT delivery_id FROM subscription_runtime WHERE " + ELIGIBLE + " AND next_at<=? "
                "AND (lease_until IS NULL OR lease_until<=?) ORDER BY next_at,delivery_id LIMIT 4", (now, now))]

    def claim_event(self, delivery_id, now):
        owner = secrets.token_hex(16)
        with self._transaction():
            changed = self._db.execute(
                "UPDATE subscription_runtime SET lease_owner=?,lease_until=? WHERE delivery_id=? AND " + ELIGIBLE + " "
                "AND next_at<=? AND (lease_until IS NULL OR lease_until<=?)",
                (owner, now + LEASE_SECONDS, delivery_id, now, now)).rowcount
        return owner if changed else None

    def event_work(self, delivery_id):
        with self._lock:
            row = self._db.execute("SELECT d.*,e.event_json,r.deadline,r.attempts,r.lease_until FROM subscription_deliveries d "
                                   "JOIN subscription_events e ON e.id=d.event_id JOIN subscription_runtime r ON r.delivery_id=d.id WHERE d.id=?",
                                   (delivery_id,)).fetchone()
            return dict(row)

    def save_prepared(self, delivery_id, owner, prepared, now):
        """Persist only a fully host-prepared frozen briefing before admission."""
        with self._transaction():
            lease = self._db.execute("SELECT * FROM subscription_runtime WHERE delivery_id=?", (delivery_id,)).fetchone()
            if lease["lease_owner"] != owner or lease["lease_until"] <= now:
                raise SubscriptionRefusal("preparation lease superseded", code="conflict")
            row = self._db.execute("SELECT * FROM subscription_deliveries WHERE id=?", (delivery_id,)).fetchone()
            config = json.loads(row["config_json"])
            payload = json.loads(self._db.execute("SELECT event_json FROM subscription_events WHERE id=?", (row["event_id"],)).fetchone()[0])["payload"]
            if (not isinstance(prepared, dict) or not all(k in prepared for k in BINDING_KEYS)
                    or ("baseline" not in config and any(prepared.get(k) != config[k] for k in BINDING_KEYS))
                    or prepared.get("action", "owner") != config.get("action", "owner")
                    or prepared.get("event_data") != payload
                    or prepared.get("admission_id") != row["admission_id"]
                    or not isinstance(prepared.get("briefing"), str)
                    or not 0 < len(prepared["briefing"].encode()) <= 16384
                    or len(_json(prepared).encode()) > 65536):
                raise SubscriptionRefusal("host preparation binding invalid", code="forbidden")
            encoded = _json(prepared)
            if row["prepared_json"] is not None and row["prepared_json"] != encoded:
                raise SubscriptionRefusal("frozen preparation changed", code="conflict")
            self._db.execute("UPDATE subscription_deliveries SET prepared_json=?,prepared_at=?,status='prepared' WHERE id=? AND status IN ('accepted','prepared')",
                             (encoded, datetime.fromtimestamp(now, timezone.utc).isoformat(), delivery_id))

    def finish_event_tick(self, delivery_id, owner, now, *, reason, terminal=None, retry=False):
        # Reasons are fixed codes from this module; never raw adapter errors/data.
        with self._transaction():
            row = self._db.execute("SELECT * FROM subscription_runtime WHERE delivery_id=? AND lease_owner=?", (delivery_id, owner)).fetchone()
            if row is None:
                return
            attempts = row["attempts"] + int(retry)
            if row["terminal"] == "deadletter" and terminal is None:
                terminal, reason = row["terminal"], row["reason"]
            if retry and attempts >= MAX_ATTEMPTS:
                terminal, reason = "deadletter", "retry_exhausted"
            next_at = now + (min(300, 2 ** min(attempts, 9)) if retry else 5)
            if terminal == "deadletter":
                next_at = now + 60
            self._db.execute("UPDATE subscription_runtime SET attempts=?,next_at=?,terminal=?,reason=?,lease_owner=NULL,lease_until=NULL,finished_at=? WHERE delivery_id=? AND lease_owner=?",
                             (attempts, next_at, terminal, reason, now if terminal else None, delivery_id, owner))
            self._db.execute("INSERT INTO subscription_runtime_history(delivery_id,at,reason,attempt) VALUES (?,?,?,?)",
                             (delivery_id, now, reason, attempts))

    def inspect_events(self, delivery_id=None, *, deadletters=False):
        """Operator metadata only: no payload, briefing, template or credentials."""
        with self._lock:
            rows = self._db.execute(
                "SELECT d.id,d.event_id,d.subscription_id,d.admission_id,d.status,"
                "a.generation,a.phase,a.blocked,r.deadline,r.attempts,r.next_at,r.terminal,r.reason "
                "FROM subscription_deliveries d LEFT JOIN subscription_admissions a ON a.delivery_id=d.id "
                "LEFT JOIN subscription_runtime r ON r.delivery_id=d.id "
                "WHERE (? IS NULL OR d.id=?) AND (?=0 OR r.terminal='deadletter') ORDER BY d.rowid DESC LIMIT 100",
                (delivery_id, delivery_id, int(deadletters))).fetchall()
            result = [dict(row) for row in rows]
            if delivery_id and result:
                raw = self._db.execute("SELECT prepared_json FROM subscription_deliveries WHERE id=?", (delivery_id,)).fetchone()[0]
                prepared = json.loads(raw) if raw else {}
                result[0]["frozen_binding"] = {key: prepared[key] for key in (*BINDING_KEYS, "goal_id", "goal_generation", "action") if key in prepared}
                result[0]["evidence"] = [dict(row) for row in self._db.execute(
                    "SELECT at,reason,attempt FROM subscription_runtime_history WHERE delivery_id=? ORDER BY sequence DESC LIMIT 50", (delivery_id,))]
                result[0]["admission_evidence"] = [dict(row) for row in self._db.execute(
                    "SELECT at,phase,blocked,reason FROM subscription_admission_history WHERE delivery_id=? ORDER BY sequence DESC LIMIT 50", (delivery_id,))]
                terminal = self.handler_terminal(delivery_id)
                if terminal:
                    result[0]["handler_terminal"] = {key: terminal["native_terminal"][key] for key in
                                                    ("terminal_id", "outcome", "body_sha256", "not_active", "empty_inbox")}
            return result

    def event_capacity(self, prepared):
        """Four active event runs globally, one per frozen owner identity."""
        with self._lock:
            rows = self._db.execute("SELECT a.binding_json FROM subscription_admissions a JOIN subscription_runtime r ON r.delivery_id=a.delivery_id "
                                    "WHERE a.phase IN ('sending','uncertain','admitted','consumed') AND (r.terminal IS NULL OR r.terminal='deadletter')").fetchall()
            return len(rows) < 4 and not any(json.loads(row[0])["target"] == prepared["target"] for row in rows)

    def lease_valid(self, delivery_id, owner, now):
        row = self._db.execute("SELECT lease_owner,lease_until FROM subscription_runtime WHERE delivery_id=?", (delivery_id,)).fetchone()
        return row is not None and row["lease_owner"] == owner and row["lease_until"] > now


class EventWorker:
    def __init__(self, store, adapter):
        self.store, self.adapter = store, adapter
        self._thread = None
        self._batch_result = []
        self._batch_error = None
        self._executor_lock = threading.Lock()
        self._closed = False

    def scheduler_tick(self, now):
        """One bounded batch in flight, no in-memory work backlog."""
        with self._executor_lock:
            if self._closed or (self._thread is not None and self._thread.is_alive()):
                return []
            touched, self._batch_result = self._batch_result, []
            if self._batch_error is not None:
                error, self._batch_error = self._batch_error, None
                raise error
            def run():
                try:
                    result = self.tick(now)
                    with self._executor_lock:
                        self._batch_result = result
                except Exception as error:
                    with self._executor_lock:
                        self._batch_error = error
            self._thread = threading.Thread(target=run, name="docket-events", daemon=True)
            self._thread.start()
            return touched

    def close(self, timeout=5):
        """Bound shutdown; false means caller must not close the shared database.

        A stalled adapter thread is daemon-owned, not a detached new sender. Its
        durable intent remains status-only for the next process on termination.
        """
        with self._executor_lock:
            self._closed = True
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def tick(self, now):
        now = now.timestamp() if isinstance(now, datetime) else now
        batch_started = time.monotonic()
        def current_time():
            return now + time.monotonic() - batch_started
        touched = []
        for delivery_id in self.store.due_events(now):
            claim_now = current_time()
            owner = self.store.claim_event(delivery_id, claim_now)
            if owner is None:
                continue
            touched.append(delivery_id)
            try:
                self._tick(delivery_id, owner, current_time(), current_time=current_time)
            except ValueError as error:
                try:
                    state = self.store.state(delivery_id)
                except SubscriptionRefusal:
                    state = None
                if state and state["phase"] != "ready":
                    self.store.observe(delivery_id, state["generation"], "unavailable")
                    self.store.finish_event_tick(delivery_id, owner, current_time(), reason="dispatch_unresolved_status_only", retry=True)
                else:
                    # Only a known installed-validator refusal gets a more
                    # specific public code; never disclose arbitrary adapter text.
                    from docket.wake_profiles import ProfileRefusal
                    reason = "profile_or_owner_refused"
                    if isinstance(error, ProfileRefusal) and str(error) == "profile widens installed tool policy":
                        reason = "profile_widens_installed_tool_policy"
                    self.store.finish_event_tick(delivery_id, owner, current_time(), reason=reason, terminal="deadletter")
            except Exception:
                # If intent exists, even failure storing its response must only
                # lead to exact-ID lookup on restart, never dispatch again.
                self.store.finish_event_tick(delivery_id, owner, current_time(), reason="adapter_unavailable", retry=True)
        return touched

    def _tick(self, delivery_id, owner, now, *, current_time):
        work = self.store.event_work(delivery_id)
        try:
            state = self.store.state(delivery_id)
        except SubscriptionRefusal:
            state = None
        prepared = json.loads(work["prepared_json"]) if work["prepared_json"] else None
        if state and state["phase"] != "ready":
            # Reconcile even revoked/expired work to preserve possible effects.
            outcome = self.adapter.reconcile(prepared, work["admission_id"])
            if outcome == "handler_finished":
                reader = getattr(self.adapter, "terminal_receipt", None)
                receipt = reader(prepared, work["admission_id"]) if callable(reader) else None
                self.store.record_receipt(delivery_id, state["generation"], receipt)
                if self.store.handler_terminal(delivery_id) is not None:
                    return self.store.finish_event_tick(delivery_id, owner, now, reason="handler_finished", terminal="handler_finished")
                return self.store.finish_event_tick(delivery_id, owner, now, reason="handler_terminal_unavailable_status_only")
            state = self.store.observe(delivery_id, state["generation"], outcome)
            if state["phase"] == "lost":
                return self.store.finish_event_tick(delivery_id, owner, now, reason="admission_lost", terminal="deadletter")
            if state["phase"] == "consumed" and prepared.get("action", "owner") == "owner":
                return self.store.finish_event_tick(delivery_id, owner, now, reason="owner_consumed", terminal="consumed")
            if now >= work["deadline"]:
                return self.store.finish_event_tick(delivery_id, owner, now, reason="deadline_effect_unresolved", terminal="deadletter")
            return self.store.finish_event_tick(delivery_id, owner, now, reason="status_only")
        if now >= work["deadline"]:
            return self.store.finish_event_tick(delivery_id, owner, now, reason="deadline_before_dispatch", terminal="deadletter")
        if prepared is None:
            prepared = self.adapter.prepare(json.loads(work["config_json"]), json.loads(work["event_json"])["payload"], work["admission_id"])
            self.store.save_prepared(delivery_id, owner, prepared, current_time())
        snapshot = self.adapter.validate(prepared)
        state = self.store.prepare_admission(delivery_id, trusted_snapshot=snapshot)
        if state["blocked"]:
            return self.store.finish_event_tick(delivery_id, owner, now, reason="profile_or_owner_refused", terminal="deadletter")
        if not self.store.event_capacity(prepared):
            return self.store.finish_event_tick(delivery_id, owner, now, reason="active_capacity")
        dispatch_now = current_time()
        intent = self.store.record_send_intent(delivery_id, state["generation"], trusted_snapshot=snapshot,
                                               claim_guard=lambda: self.store.lease_valid(delivery_id, owner, dispatch_now)
                                               and dispatch_now < work["deadline"] and self.store.event_capacity(prepared))
        if intent["blocked"]:
            return self.store.finish_event_tick(delivery_id, owner, now, reason="profile_or_owner_refused", terminal="deadletter")
        if intent["claimed"]:
            receipt = self.adapter.dispatch(prepared, work["admission_id"], lifecycle={
                "delivery_id": delivery_id, "deadline": work["deadline"],
                "lease_until": work["lease_until"], "generation": state["generation"]})
            self.store.record_receipt(delivery_id, state["generation"], receipt)
            if self.store.handler_terminal(delivery_id) is not None:
                return self.store.finish_event_tick(delivery_id, owner, now, reason="handler_finished", terminal="handler_finished")
        self.store.finish_event_tick(delivery_id, owner, now, reason="exact_intent_recorded")
