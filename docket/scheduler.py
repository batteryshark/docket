"""Claims due items and hands them to a delivery adapter.

The scheduler never runs item text. A failure to deliver is recorded; it is
not turned into a command.

Admission is not consumption. A 200 from the host means the prompt was
admitted. ``delivered`` is recorded only after that stable id is in the
session transcript. Missing or unreadable evidence after possible dispatch
is uncertain and status-only. Only proven pre-dispatch failures can retry,
using the original frozen body and the same daemon's unchanged binding.

A recurring item keeps one parent row and a distinct occurrence for each wake.
The admission id belongs to that occurrence. A later repeat is a new
occurrence, not a retry. An admitted occurrence does not advance the series
and does not mint another wake. A sleep that covers many slots delivers that
one owed occurrence, marked late, and jumps to the next future slot.

A watch polls on its own interval. The condition is latched into one
occurrence when it first becomes true, then delivered even if the condition
later flips. An outage is not a match.
"""

from __future__ import annotations

import sys
import json
import threading
import time
from datetime import datetime, timedelta, timezone

from docket.bounds import (
    MAX_WATCH_CHECKS,
    coalesced_in_window,
    effective_remaining,
    schedule_finished,
    slot_after,
    slot_allowed,
)
from docket.checks import URL_DEADLINE, CheckExecutor, Checks
from docket.delivery import DeliveryError, Receipt
from docket.parse import is_late, late_by_seconds, new_occurrence_id, wake_text
from docket.store import Occurrence
from docket.delivery_state import issue, project, retry_refusal
from docket.checkins import Checkins

LEASE_SECONDS = 60
MAX_ATTEMPTS = 8
TARGET_DEAD = ("bad_target", "archived", "wrong_target")
OPEN = ("pending", "firing", "admitted", "ambiguous")


class Scheduler:
    def __init__(self, store, adapter, clock, checks=None, check_executor=None, *, pi_adapter=None):
        self.store = store
        self.adapter = adapter
        self._pi_adapter = pi_adapter
        self.clock = clock
        self.checks = checks or Checks(adapter)
        self.check_executor = check_executor or CheckExecutor()
        self._inflight: dict[str, str] = {}
        self._inflight_cv = threading.Condition()
        self._gates: dict[str, threading.Lock] = {}
        self._gates_lock = threading.Lock()
        self._next_checks = {}
        self.checkins = Checkins(self)
        self._checkins_error = None

    def _gate(self, item_id):
        # Parent gate also serializes recurrence advancement and exact-unit recovery.
        with self._gates_lock:
            return self._gates.setdefault(item_id, threading.Lock())

    def adapter_for_target(self, target):
        """Two fixed local adapters; never change the shared default or use request argv."""
        if target.get("adapter") == "pi":
            with self._gates_lock:
                if self._pi_adapter is None:
                    from docket.pi import PiAdapter
                    self._pi_adapter = PiAdapter()
                return self._pi_adapter
        if target.get("adapter") not in (None, "opencode"):
            raise DeliveryError("unsupported_capability", not_dispatched=True)
        return self.adapter

    def _snapshot(self, target=None):
        adapter = self.adapter_for_target(target) if target is not None else self.adapter
        method = getattr(adapter, "binding_snapshot", None)
        if callable(method):
            return method()
        # Compatibility for deterministic in-process adapters, not a namespace claim.
        return {"adapter": adapter.name, "namespace_verified": False}

    def _note_receipt(self, unit_id, result, *, phase):
        facts = dict(result.raw.get("facts", {}))
        prior = self.store.get_request(unit_id)["facts"]
        # A provider snapshot cannot retract Docket's retained irreversible evidence.
        contradictory_cancel = facts.get("cancelled") and any(
            prior.get(key) for key in ("claimed", "injected", "consumed"))
        if contradictory_cancel:
            facts.pop("cancelled")
        self.store.note_request(unit_id, facts=facts, diagnostic=(
            issue("acceptance_unknown", phase=phase, certainty="unknown") if contradictory_cancel else None))
        return "uncertain" if contradictory_cancel else result.state

    def _status(self, item, occ, req):
        frozen = req["frozen"]
        target = frozen["target"] if frozen else item.target
        adapter = self.adapter_for_target(target)
        if frozen:
            if self._snapshot(target) != frozen["binding"]:
                raise DeliveryError("backend_unverifiable")
            method = getattr(adapter, "status_frozen", None)
            if callable(method):
                result = method(frozen)
                if isinstance(result, Receipt):
                    state = self._note_receipt((occ or item).id, result, phase="status")
                    if result.state == "failed" and any(req["facts"].get(k) for k in ("claimed", "injected", "consumed")):
                        return "uncertain"
                    return state
                return result
        # Legacy evidence may locate an ID but cannot recover a complete envelope.
        if not req["request_id"]:
            raise DeliveryError("legacy_identity_unknown")
        return adapter.reconcile(target, req["request_id"])

    def _reconcile_unit(self, item, occ=None, *, gate_held=False):
        unit = occ or item
        now = self.clock.now()
        if self._next_checks.get(unit.id, now) > now:
            return False
        gate = self._gate(item.id)
        if not gate_held and not gate.acquire(blocking=False):
            return False
        try:
            self._next_checks[unit.id] = now + timedelta(seconds=30)
            req = self.store.reserve_request(item, occ)
            try:
                found = self._status(item, occ, req)
                if found not in ("consumed", "admitted", "missing", "uncertain", "failed"):
                    raise DeliveryError("unavailable")
            except Exception as exc:
                code = exc.code if isinstance(exc, DeliveryError) else "unavailable"
                self.store.note_request(unit.id, diagnostic=req["issue"],
                                        checked_at=self.clock.now(), check_issue=issue(code, phase="status", certainty="unknown"))
                return False
            if found == "consumed":
                if not req["frozen"] and not req["facts"].get("consumed"):
                    self.store.note_request(unit.id, diagnostic=req["issue"], checked_at=now,
                                            check_issue=issue("legacy_envelope_unknown", phase="status", certainty="unknown"))
                    return False
                self.store.note_request(unit.id, facts={"admitted": True, "consumed": True},
                                        phase="consumed", checked_at=self.clock.now())
                return self._consume(item, occ, req["request_id"], (unit.receipt or {}).get("delivery"))
            if found == "failed":
                return self._provider_failed(item, occ)
            if found == "admitted":
                self.store.note_request(unit.id, facts={"admitted": True}, phase="admitted", checked_at=self.clock.now())
                if item.status != "cancelled":
                    return self._note_admitted(item, occ, req["request_id"], (unit.receipt or {}).get("delivery"))
                return True
            if found == "uncertain":
                self.store.note_request(unit.id, diagnostic=issue("acceptance_unknown", phase="status", certainty="unknown"), checked_at=now)
                if item.status != "cancelled" and not self.store.get_request(unit.id)["facts"].get("consumed"):
                    self._ambiguous(item, occ, req["request_id"], issue("acceptance_unknown")["detail"])
                return True
            # A missing snapshot never changes dispatch certainty or authorizes replay.
            if self.store.get_request(unit.id)["facts"].get("cancelled"):
                return True
            if req["dispatch_state"] == "possible" or req["provenance"] == "legacy":
                code = "lost" if req["facts"].get("admitted") else "acceptance_unknown"
                self.store.note_request(unit.id, diagnostic=issue(code, phase="status", certainty="unknown"),
                                        checked_at=self.clock.now())
                if item.status != "cancelled":
                    self._ambiguous(item, occ, req["request_id"], issue(code)["detail"])
            else:
                self.store.note_request(unit.id, diagnostic=req["issue"], checked_at=self.clock.now())
            return True
        finally:
            if not gate_held:
                gate.release()

    def recover(self, item, occ, action):
        gate = self._gate(item.id)
        if not gate.acquire(blocking=False):
            return 409, "in_flight"
        try:
            unit = occ or item
            req = self.store.reserve_request(item, occ)
            if action == "retry":
                refusal = project(self.store, item, occ)["recovery"]["refusal_code"]
                if refusal:
                    return 409, refusal
                if req["frozen"]:
                    try:
                        same = self._snapshot(req["frozen"]["target"]) == req["frozen"]["binding"]
                    except Exception:
                        same = False
                    if not same:
                        self.store.invalidate_binding(unit.id)
                        return 409, "backend_unverifiable"
                from docket.bounds import check_limits, LimitError
                try:
                    check_limits([row for row in self.store.active_items() if row.id != item.id],
                                 kind=item.kind, session_id=item.target["session_id"], created_by=item.created_by)
                except LimitError:
                    return 409, "limits"
                refusal = self.store.authorize_retry(unit.id, self.clock.now())
                return (409, refusal) if refusal else (202, None)
            if not req["request_id"]:
                return 409, "legacy_identity_unknown"
            self._next_checks.pop(unit.id, None)
            self._reconcile_unit(item, occ, gate_held=True)
            req = self.store.get_request(unit.id)
            return (503, req["check_issue"]["code"]) if req["check_issue"] else (200, None)
        finally:
            gate.release()

    def stop(self, item):
        """Durable local stop first. Never confuse a missing snapshot with retraction."""
        occ = self.store.open_occurrence(item)
        unit = occ or item
        req = self.store.reserve_request(item, occ)
        self.store.cancel(item.id, self.clock.now())
        # Re-read AFTER durable stop: begin_send may have won between reserve and cancel.
        # Once stop is committed, begin_send cannot subsequently win.
        req = self.store.get_request(unit.id)
        if req["dispatch_state"] != "possible" and req["provenance"] != "legacy":
            self.store.note_request(unit.id, facts={"cancelled": True}, phase="cancelled")
            return "not_sent"
        gate = self._gate(item.id)
        if not gate.acquire(blocking=False):
            return "uncertain"
        try:
            return self._retract_stopped(item, occ)
        finally:
            gate.release()

    def _retract_stopped(self, item, occ):
        unit = occ or item
        req = self.store.get_request(unit.id)
        if not req or not req["frozen"]:
            return "uncertain"
        try:
            found = self._status(item, occ, req)
            if found == "consumed":
                self.store.note_request(unit.id, facts={"admitted": True, "consumed": True}, phase="consumed")
                return "too_late"
            if self.store.get_request(unit.id)["facts"].get("consumed"):
                return "too_late"
            adapter = self.adapter_for_target(req["frozen"]["target"])
            method = getattr(adapter, "cancel_frozen", None)
            if callable(method):
                result = method(req["frozen"])
                if isinstance(result, Receipt):
                    self._note_receipt(unit.id, result, phase="cancel")
                    facts = self.store.get_request(unit.id)["facts"]
                    if facts.get("consumed"):
                        return "too_late"
                    if facts.get("cancelled"):
                        return "retracted"
                    if result.state == "too_late":
                        # Irreversible claim is NOT proof of saved intake.
                        self.store.note_request(unit.id, diagnostic=issue("acceptance_unknown", phase="cancel", certainty="unknown"))
                        return "uncertain"
            else:
                adapter.cancel_admission(req["frozen"]["target"], req["request_id"])
            after = self._status(item, occ, req)
            if after == "consumed":
                self.store.note_request(unit.id, facts={"admitted": True, "consumed": True}, phase="consumed")
                return "too_late"
        except Exception:
            pass
        self.store.note_request(unit.id, diagnostic=issue("acceptance_unknown", phase="cancel", certainty="unknown"))
        return "uncertain"

    def _render(self, item, occ, now):
        plan = self._plan(item, occ, now) if occ else None
        scheduled = occ.scheduled_at if occ else item.fire_at
        extra = {} if not occ else {
            "occurrence_id": occ.id, "coalesced": plan["coalesced"],
            "check": (item.spec or {}).get("check"),
            "summary": (occ.detail or {}).get("summary") or (item.spec or {}).get("summary"),
            "next_fire": plan["next_fire"], "remaining": plan["remaining"],
            "series_continues": plan["continues"], "previous_undeliverable": plan["previous"],
        }
        return wake_text(kind=item.kind, item_id=item.id, reason=item.reason, fire_at=scheduled,
                         now=now, late=is_late(scheduled, now), urgent=item.urgent,
                         created_by=item.created_by, context=item.context, **extra)

    def _deliver_unit(self, item, occ=None):
        unit = occ or item
        gate = self._gate(item.id)
        if not gate.acquire(blocking=False):
            return False
        try:
            parent = self.store.get(item.id)
            unit = self.store.get_occurrence(unit.id) if occ else parent
            if not parent or not unit or parent.status == "cancelled" or unit.status != "pending":
                return False
            item, occ = parent, unit if occ else None
            req = self.store.reserve_request(item, occ)
            refusal = retry_refusal(req)
            # Automatic sends may use the remaining original budget after a manual authorization.
            if refusal == "retry_budget":
                refusal = None
            if refusal:
                self.store.note_request(unit.id, diagnostic=issue(refusal, certainty="unknown"))
                self._ambiguous(item, occ, req["request_id"], issue(refusal)["detail"])
                return True
            manual = bool(req["manual_pending"])
            if unit.attempts >= MAX_ATTEMPTS and not manual:
                return False
            now = self.clock.now()
            claimed = (self.store.claim_occurrence(unit.id, now, max_attempts=MAX_ATTEMPTS) if occ
                       else self.store.claim(unit.id, now, max_attempts=MAX_ATTEMPTS))
            if not claimed:
                return False
            unit = self.store.get_occurrence(unit.id) if occ else self.store.get(unit.id)
            if occ:
                occ = unit
            else:
                item = unit
            try:
                # Freeze text/body/target before validation so a later validation retry
                # cannot change time-derived text. Binding discovery may itself be unavailable.
                target = req["frozen"]["target"] if req["frozen"] else item.target
                adapter = self.adapter_for_target(target)
                snapshot = self._snapshot(target)
                if not req["frozen"]:
                    body = {"id": req["request_id"], "text": self._render(item, occ, now),
                            "delivery": "steer" if item.urgent else "queue", "resume": True}
                    prepare = getattr(adapter, "prepare_body", None)
                    if callable(prepare):
                        body = prepare(req["caller"], req["request_id"], body["text"], item.urgent, item.target)
                    frozen = {"v": 1, "caller": req["caller"], "target": item.target,
                              "urgent": item.urgent, "binding": snapshot,
                              # Matches both urllib and the paired JSON transport exactly.
                              "body": json.dumps(body, sort_keys=True)}
                    req = self.store.freeze_request(unit.id, frozen)
                elif snapshot != req["frozen"]["binding"]:
                    self.store.invalidate_binding(unit.id)
                    raise DeliveryError("backend_unverifiable")
                if req["stop_requested"] or not req["frozen"]:
                    return False
                validate = getattr(adapter, "validate_frozen", None)
                check = validate(req["frozen"]) if callable(validate) else adapter.validate(req["frozen"]["target"])
                if not check.ok:
                    raise DeliveryError(check.code, retryable=check.code == "unavailable")
            except Exception as exc:
                code = exc.code if isinstance(exc, DeliveryError) else "unavailable"
                if code == "backend_unverifiable":
                    self.store.invalidate_binding(unit.id)
                diagnostic = issue(code)
                self.store.note_request(unit.id, diagnostic=diagnostic)
                safe = DeliveryError(code, retryable=code == "unavailable" and not manual)
                if occ:
                    self._fail_occurrence(item, occ, safe)
                else:
                    self._fail(item, safe)
                return True
            # Fresh eligibility after preflight, before the irreversible send marker.
            # Keep the core's frozen binding/body, dispatch certainty and gates intact.
            if not self.checkins.allowed(item, self.clock.now()):
                self.checkins.stop(item)
                return True
            if not self.store.begin_send(unit.id, now):
                return False
            self._enter_send(item.id, req["request_id"])
            try:
                frozen = req["frozen"]
                method = getattr(adapter, "deliver_frozen", None)
                if callable(method):
                    sent = method(frozen)
                else:
                    body = json.loads(frozen["body"])
                    sent = adapter.deliver(frozen["target"], body["text"], urgent=frozen["urgent"],
                                                admission_id=req["request_id"])
                if (sent.admission_id or sent.inbox_id) != req["request_id"]:
                    raise DeliveryError("conflict")
                facts = sent.raw.get("facts", {"admitted": True, "consumed": sent.consumed})
                self.store.note_request(unit.id, facts=facts,
                                        phase="consumed" if sent.consumed else "admitted")
                if self.store.get(item.id).status == "cancelled":
                    self._retract_stopped(item, occ)
                elif sent.consumed:
                    self._consume(item, occ, req["request_id"], sent.delivery)
                elif sent.state == "failed":
                    self._provider_failed(item, occ)
                elif facts.get("admitted") and sent.state != "uncertain":
                    self._note_admitted(item, occ, req["request_id"], sent.delivery)
                else:
                    self.store.note_request(unit.id, diagnostic=issue("acceptance_unknown", phase="send", certainty="unknown"))
                    self._ambiguous(item, occ, req["request_id"], issue("acceptance_unknown")["detail"])
            except Exception as exc:
                code = exc.code if isinstance(exc, DeliveryError) and exc.code == "conflict" else "acceptance_unknown"
                self.store.note_request(unit.id, diagnostic=issue(code, phase="send", certainty="unknown"))
                self._ambiguous(item, occ, req["request_id"], issue(code)["detail"])
            finally:
                self._leave_send(item.id)
            return True
        finally:
            gate.release()

    def _provider_failed(self, item, occ=None):
        """A documented terminal pre-claim receipt, not authority to resend."""
        unit = occ or item
        req = self.store.get_request(unit.id)
        if any(req["facts"].get(k) for k in ("claimed", "injected", "consumed")):
            return False  # Never downgrade stronger historical facts.
        self.store.note_request(unit.id, facts={"failed_before_claim": True}, phase="failed",
                                diagnostic=issue("provider_failed", phase="status",
                                                 certainty="admitted" if req["facts"].get("admitted") else "unknown"))
        fresh = self.store.get_occurrence(unit.id) if occ else self.store.get(unit.id)
        if fresh.status == "cancelled":
            return True
        fresh.status = "undeliverable"
        fresh.updated_at = self.clock.now()
        fresh.next_attempt_at = None
        fresh.error = "provider_failed: " + issue("provider_failed")["detail"]
        fresh.receipt = {"admission_id": req["request_id"], "state": "failed",
                         "delivery": json.loads(req["frozen"]["body"])["delivery"]}
        allowed = ("firing", "pending", "admitted", "ambiguous")
        saved = self.store.cas_occurrence(fresh, allowed) if occ else self.store.cas_item(fresh, allowed)
        if saved and occ:
            self._after_occurrence(item, fresh, self.clock.now())
        return saved

    def tick(self) -> list[str]:
        now = self.clock.now()
        touched: list[str] = []
        for req in self.store.stopped_unresolved_requests():
            parent = self.store.get(req["item_id"])
            occ = self.store.get_occurrence(req["occurrence_id"]) if req["occurrence_id"] else None
            if parent and self._reconcile_unit(parent, occ):
                touched.append(req["unit_id"])
        for item in self.store.list_outstanding():
            if item.status in ("admitted", "ambiguous"):
                if self._reconcile_item(item):
                    touched.append(item.id)
                continue
            if item.kind in ("recurring", "watch") and item.status == "pending":
                occ = self.store.open_occurrence(item)
                if occ is not None and occ.status in ("admitted", "ambiguous"):
                    if self._reconcile_occurrence(item, occ):
                        touched.append(item.id)
        for occ_id in self.store.reclaim_occurrence_ids(now, lease_seconds=LEASE_SECONDS):
            if self._reclaim_occurrence(occ_id, now):
                touched.append(occ_id)
        for item_id in self.store.reclaim_ids(now, lease_seconds=LEASE_SECONDS):
            if self._reclaim(item_id, now):
                touched.append(item_id)
        due = []
        for item_id in self.store.due_ids(self.clock.now()):
            item = self.store.get(item_id)
            if item is None or item.status != "pending":
                continue
            due.append(item)
        # Timers, alarms, recurring wakes, and already-latched watches deliver
        # before any read-only check runs. A slow URL or file probe must not
        # hold those wakes.
        priority = [item for item in due if not _needs_check(item)]
        watches = [item for item in due if _needs_check(item)]
        for item in priority:
            if self._touch_due(item):
                touched.append(item.id)
        submitted = []
        for item in watches:
            try:
                ready = self.check_executor.take(item.id)
            except Exception as exc:
                log_scheduler_error(exc)
                self._backoff_unexpected(item, exc)
                touched.append(item.id)
                continue
            if ready is not None:
                if self._apply_watch_outcome(item, ready, self.clock.now()):
                    touched.append(item.id)
                continue
            spec = dict(item.spec or {})
            now = self.clock.now()
            if self.check_executor.submit(
                item.id,
                lambda spec=spec, now=now: self.checks.evaluate(spec, now),
            ):
                submitted.append(item.id)
            # A full backlog is left due. It is not queued again and does not block.
        if submitted and not priority:
            self.check_executor.wait(submitted, timeout=URL_DEADLINE + 1)
            for item_id in submitted:
                item = self.store.get(item_id)
                if item is None or item.status != "pending":
                    continue
                try:
                    ready = self.check_executor.take(item_id)
                except Exception as exc:
                    log_scheduler_error(exc)
                    self._backoff_unexpected(item, exc)
                    touched.append(item_id)
                    continue
                if ready is None:
                    continue
                if self._apply_watch_outcome(item, ready, self.clock.now()):
                    touched.append(item_id)
        # Ordinary due work first; missing private roster leaves this opt-in off.
        self.checkins.tick(self.clock.now())
        if self.checkins.error != self._checkins_error:
            self._checkins_error = self.checkins.error
            if self.checkins.error:
                _log(self.checkins.error)
        return touched

    def sending(self, item_id: str) -> bool:
        with self._inflight_cv:
            return item_id in self._inflight

    def wait_sending(self, item_id: str, timeout: float) -> bool:
        """Block until ``item_id`` leaves deliver, or the timeout expires."""
        deadline = time.monotonic() + timeout
        with self._inflight_cv:
            while item_id in self._inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._inflight_cv.wait(remaining)
            return True

    def _enter_send(self, item_id: str, aid: str) -> None:
        with self._inflight_cv:
            self._inflight[item_id] = aid
            self._inflight_cv.notify_all()

    def _leave_send(self, item_id: str) -> None:
        with self._inflight_cv:
            self._inflight.pop(item_id, None)
            self._inflight_cv.notify_all()

    def _touch_due(self, item) -> bool:
        try:
            if item.kind == "watch":
                return self._deliver_latched(item)
            if item.kind == "recurring":
                return self._fire_recurring(item)
            return self._fire(item.id)
        except Exception as exc:
            log_scheduler_error(exc)
            fresh = self.store.get(item.id)
            if fresh is not None and fresh.kind in ("timer", "alarm") and fresh.status == "firing":
                self._fail(fresh, DeliveryError(
                    "unavailable", f"fire failed: {exc}", retryable=True,
                ))
            elif fresh is not None and fresh.status == "pending":
                # Do not clear a firing occurrence here. A fresh lease must
                # not be admitted again; reclaim retries only after it expires.
                self._backoff_unexpected(fresh, exc)
            return True

    def _reclaim(self, item_id: str, now) -> bool:
        item = self.store.get(item_id)
        return self._reclaim_unit(item, None, now) if item else False

    def _reclaim_unit(self, item, occ, now):
        unit = occ or item
        gate = self._gate(item.id)
        if not gate.acquire(blocking=False):
            return False
        try:
            req = self.store.reserve_request(item, occ)
            if req["facts"].get("consumed"):
                return self._consume(item, occ, req["request_id"], None)
            code = retry_refusal(req)
            spent_claim = (req["dispatch_state"] == "never" and not any(req["facts"].values())
                           and not req["manual_pending"]
                           and (req["manual_used"] or unit.attempts >= MAX_ATTEMPTS))
            if spent_claim:
                # A committed manual claim spends that single attempt, even if the
                # process dies before preflight. Close it truthfully, never strand
                # it pending or restore authorization/counters after restart. The
                # last automatic claim has the same terminal boundary.
                code = "retry_budget" if req["manual_used"] else "attempt_budget"
                self.store.note_request(unit.id, diagnostic=issue(code, phase="restart"))
                error = DeliveryError(code)
                if occ:
                    self._fail_occurrence(item, occ, error)
                else:
                    self._fail(item, error)
                return True
            if req["dispatch_state"] == "possible" or code:
                code = code or "acceptance_unknown"
                self.store.note_request(unit.id, diagnostic=issue(code, phase="restart", certainty="unknown"))
                return self._ambiguous(item, occ, req["request_id"], issue(code)["detail"])
            unit.status = "pending"
            unit.next_attempt_at = now
            unit.updated_at = now
            if occ:
                changed = self.store.cas_occurrence(unit, ("firing",))
                item.next_attempt_at = now
                self.store.cas_item(item, ("pending",))
                return changed
            return self.store.cas_item(unit, ("firing",))
        finally:
            gate.release()


    def _reclaim_occurrence(self, occ_id: str, now) -> bool:
        occ = self.store.get_occurrence(occ_id)
        parent = self.store.get(occ.item_id) if occ else None
        return self._reclaim_unit(parent, occ, now) if parent else False


    def _fire(self, item_id: str) -> bool:
        item = self.store.get(item_id)
        return self._deliver_unit(item) if item else False


    def _fire_recurring(self, item) -> bool:
        now = self.clock.now()
        spec = dict(item.spec or {})
        occ_id = spec.get("open_occurrence_id")
        if occ_id:
            occ = self.store.get_occurrence(occ_id)
            if occ is None:
                return self._ambiguous(item, None, None, issue("legacy_identity_unknown")["detail"])
            if occ.status in ("delivered", "undeliverable"):
                return self._advance_recurring(item, occ, now)
            if occ.status == "cancelled":
                return False
            if occ.status == "admitted":
                return self._reconcile_occurrence(item, occ)
            if occ.status == "ambiguous":
                return self._reconcile_occurrence(item, occ)
            if occ.next_attempt_at and _utc(occ.next_attempt_at) > _utc(now):
                item.next_attempt_at = occ.next_attempt_at
                item.updated_at = now
                self.store.cas_item(item, ("pending",))
                return False
            if occ.status == "firing" and self._lease_fresh(occ.firing_at, now):
                return False
            return self._deliver_occurrence(item, occ)
        if not slot_allowed(spec, item.fire_at):
            self._complete_recurring(item, now, "until" if spec.get("until") else "count")
            return True
        seq = int(spec.get("next_seq") or 1)
        occ = Occurrence(
            id=new_occurrence_id(),
            item_id=item.id,
            seq=seq,
            status="pending",
            scheduled_at=item.fire_at,
            created_at=now,
            updated_at=now,
            late=False,
            late_by_seconds=None,
            delivered_at=None,
            error=None,
            receipt=None,
            attempts=0,
            next_attempt_at=None,
            firing_at=None,
            coalesced=0,
            detail={"kind": "recurring"},
        )
        item.spec = spec
        occ = self.store.attach_occurrence(item, occ)
        if occ is None:
            return False
        item = self.store.get(item.id) or item
        return self._deliver_occurrence(item, occ)

    def _poll_watch(self, item) -> bool:
        """Evaluate and apply one watch. Tests and the waiting path use this."""
        now = self.clock.now()
        spec = dict(item.spec or {})
        if spec.get("open_occurrence_id"):
            item.spec = spec
            return self._deliver_latched(item)
        return self._apply_watch_outcome(item, self.checks.evaluate(spec, now), now)

    def _apply_watch_outcome(self, item, outcome, now) -> bool:
        item = self.store.get(item.id) or item
        if item.status != "pending":
            return False
        spec = dict(item.spec or {})
        if spec.get("open_occurrence_id"):
            return self._deliver_latched(item)
        if self.checkins.marked(item):
            outcome = self.checkins.evaluate(item, now)
        until = _parse(spec.get("until"))
        spec["checks"] = int(spec.get("checks") or 0) + 1
        if outcome.observations is not None:
            spec["observations"] = outcome.observations
        item.spec = spec
        item.updated_at = now
        if outcome.kind == "met":
            return self._latch_watch(item, spec, outcome, now)
        if outcome.kind == "terminal":
            code, _, detail = outcome.detail.partition(": ")
            self._undeliverable(item, code or "watch", detail or outcome.detail)
            return True
        expired = until is not None and _utc(now) >= until
        if expired or spec["checks"] >= MAX_WATCH_CHECKS:
            self._expire_watch(item, now, outcome.detail or "expired before the condition was met")
            return True
        every = max(int(spec.get("every") or 1), 1)
        nxt = _utc(now) + timedelta(seconds=every)
        if until is not None and nxt > until:
            nxt = until
        if nxt <= _utc(now):
            self._expire_watch(item, now, outcome.detail or "expired before the condition was met")
            return True
        item.status = "pending"
        item.fire_at = nxt
        # An unmet condition is normal waiting, not a delivery validation error.
        # Keep unavailable checks visible; delivery validation happens after latch.
        item.error = None if outcome.kind == "waiting" else outcome.detail or None
        item.next_attempt_at = None
        self.store.cas_item(item, ("pending",))
        return True

    def _latch_watch(self, item, spec, outcome, now) -> bool:
        spec["latched"] = True
        spec["summary"] = outcome.summary or {}
        item.spec = spec
        occ = Occurrence(
            id=new_occurrence_id(),
            item_id=item.id,
            seq=int(spec.get("next_seq") or 1),
            status="pending",
            scheduled_at=item.fire_at,
            created_at=now,
            updated_at=now,
            late=False,
            late_by_seconds=None,
            delivered_at=None,
            error=None,
            receipt=None,
            attempts=0,
            next_attempt_at=None,
            firing_at=None,
            coalesced=0,
            detail={"check": spec.get("check"), "summary": outcome.summary or {}},
        )
        occ = self.store.attach_occurrence(item, occ)
        if occ is None:
            return False
        item = self.store.get(item.id) or item
        return self._deliver_occurrence(item, occ)

    def _deliver_latched(self, item) -> bool:
        occ = self.store.open_occurrence(item)
        if occ is None:
            return self._ambiguous(item, None, None, issue("legacy_identity_unknown")["detail"])
        if occ.status in ("delivered", "undeliverable"):
            return self._after_occurrence(item, occ, self.clock.now())
        if occ.status in ("admitted", "ambiguous"):
            return self._reconcile_occurrence(item, occ)
        if occ.next_attempt_at and _utc(occ.next_attempt_at) > _utc(self.clock.now()):
            item.next_attempt_at = occ.next_attempt_at
            item.updated_at = self.clock.now()
            self.store.cas_item(item, ("pending",))
            return False
        return self._deliver_occurrence(item, occ)

    def _deliver_occurrence(self, item, occ) -> bool:
        return self._deliver_unit(item, occ)

    def _lookup_quiet(self, target, aid: str) -> str:
        try:
            return self.adapter_for_target(target).reconcile(target, aid)
        except Exception as exc:
            log_scheduler_error(exc)
            return "unavailable"

    def _reconcile_item(self, item) -> bool:
        return self._reconcile_unit(item)

    def _reconcile_occurrence(self, item, occ) -> bool:
        return self._reconcile_unit(item, occ)

    def _note_admitted(self, item, occ, aid: str, delivery: str | None) -> bool:
        now = self.clock.now()
        receipt = {
            "admission_id": aid,
            "inbox_id": aid,
            "delivery": delivery,
            "state": "admitted",
        }
        host_status = getattr(self.adapter_for_target(item.target), "host_status", None)
        if callable(host_status):
            receipt["host"] = host_status()
        if occ is None:
            fresh = self.store.get(item.id) or item
            if fresh.status == "cancelled":
                self._retract_stopped(item, None)
                return True
            fresh.status = "admitted"
            fresh.updated_at = now
            fresh.error = None
            fresh.next_attempt_at = None
            fresh.delivered_at = None
            fresh.receipt = receipt
            fresh.late = item.late
            fresh.late_by_seconds = item.late_by_seconds
            if not self.store.cas_item(fresh, ("firing", "admitted", "pending", "ambiguous")):
                self._retract_stopped(item, None)
                return True
            _log(f"admitted {fresh.id} {delivery or 'queue'}")
            return True
        occ = self.store.get_occurrence(occ.id) or occ
        if occ.status == "cancelled":
            self._retract_stopped(item, occ)
            return True
        occ.status = "admitted"
        occ.updated_at = now
        occ.error = None
        occ.next_attempt_at = None
        occ.delivered_at = None
        occ.receipt = receipt
        if not self.store.cas_occurrence(occ, ("firing", "admitted", "pending", "ambiguous")):
            self._retract_stopped(item, occ)
            return True
        parent = self.store.get(item.id) or item
        self._hold_parent(parent, now, occ)
        _log(f"admitted {occ.id} {delivery or 'queue'}")
        return True

    def _consume(self, item, occ, aid: str, delivery: str | None, now=None) -> bool:
        now = now or self.clock.now()
        receipt = {
            "admission_id": aid,
            "inbox_id": aid,
            "delivery": delivery,
            "state": "consumed",
        }
        host_status = getattr(self.adapter_for_target(item.target), "host_status", None)
        if callable(host_status):
            receipt["host"] = host_status()
        if occ is None:
            fresh = self.store.get(item.id) or item
            if fresh.status == "cancelled":
                return True
            late = is_late(fresh.fire_at, now)
            fresh.status = "delivered"
            fresh.delivered_at = now
            fresh.updated_at = now
            fresh.error = None
            fresh.next_attempt_at = None
            fresh.receipt = receipt
            fresh.late = late
            fresh.late_by_seconds = late_by_seconds(fresh.fire_at, now) if late else None
            if not self.store.cas_item(fresh, ("firing", "admitted", "pending", "ambiguous")):
                return False
            _log(f"delivered {fresh.id} {fresh.kind} {delivery or 'recovered'} late={fresh.late}")
            return True
        occ = self.store.get_occurrence(occ.id) or occ
        if occ.status == "cancelled":
            return True
        late = is_late(occ.scheduled_at, now)
        plan = self._plan(item, occ, now)
        occ.status = "delivered"
        occ.delivered_at = now
        occ.updated_at = now
        occ.error = None
        occ.next_attempt_at = None
        occ.receipt = receipt
        occ.late = late
        occ.late_by_seconds = late_by_seconds(occ.scheduled_at, now) if late else None
        occ.coalesced = plan["coalesced"]
        if not self.store.cas_occurrence(occ, ("firing", "admitted", "pending", "ambiguous")):
            return False
        _log(
            f"delivered {occ.id} {item.kind} {delivery or 'recovered'} "
            f"late={occ.late} coalesced={occ.coalesced}"
        )
        fresh = self.store.get(item.id) or item
        return self._after_occurrence(fresh, occ, self.clock.now())


    def _ambiguous(self, item, occ, aid: str, detail: str) -> bool:
        now = self.clock.now()
        error = f"ambiguous: {detail}"
        receipt_state = {"admission_id": aid, "state": "ambiguous"}
        if occ is None:
            fresh = self.store.get(item.id) or item
            if fresh.status == "cancelled":
                return True
            fresh.status = "ambiguous"
            fresh.updated_at = now
            fresh.error = error
            fresh.next_attempt_at = None
            fresh.receipt = {**(fresh.receipt or {}), **receipt_state}
            self.store.cas_item(fresh, ("firing", "pending", "admitted", "ambiguous"))
            _log(f"ambiguous {fresh.id} {detail}")
            return True
        occ = self.store.get_occurrence(occ.id) or occ
        occ.status = "ambiguous"
        occ.updated_at = now
        occ.error = error
        occ.next_attempt_at = None
        occ.receipt = {**(occ.receipt or {}), **receipt_state}
        self.store.cas_occurrence(occ, ("firing", "pending", "admitted", "ambiguous"))
        self._hold_parent(self.store.get(item.id) or item, now, occ)
        _log(f"ambiguous {occ.id} {detail}")
        return True

    def _hold_parent(self, parent, now, occ) -> None:
        if parent is None or parent.status not in ("pending", "firing"):
            return
        parent.updated_at = now
        parent.error = occ.error
        parent.next_attempt_at = now
        if parent.status == "firing":
            parent.status = "pending"
            self.store.cas_item(parent, ("firing",))
        else:
            self.store.cas_item(parent, ("pending",))

    def _fail_occurrence(self, item, occ, exc: DeliveryError) -> None:
        now = self.clock.now()
        occ = self.store.get_occurrence(occ.id) or occ
        occ.updated_at = now
        fresh = self.store.get(item.id) or item
        if fresh.status == "cancelled" or occ.status == "cancelled":
            self._cancel_occurrence(occ, now)
            return
        if (occ.receipt or {}).get("state") == "admitted":
            self._note_admitted(fresh, occ, occ.receipt["admission_id"], occ.receipt.get("delivery"))
            return
        dead = exc.code in TARGET_DEAD
        if dead or not exc.retryable or occ.attempts >= MAX_ATTEMPTS:
            occ.status = "undeliverable"
            occ.error = f"{exc.code}: {exc.detail}" if exc.detail else exc.code
            occ.next_attempt_at = None
            if not self.store.cas_occurrence(occ, ("firing", "pending", "admitted")):
                return
            self._after_occurrence(fresh, occ, now)
            _log(f"undeliverable {occ.id} {occ.error}")
            return
        delay = min(60, 2 ** max(occ.attempts, 1))
        occ.status = "pending"
        occ.error = exc.detail
        occ.next_attempt_at = now + timedelta(seconds=delay)
        if not self.store.cas_occurrence(occ, ("firing", "pending")):
            return
        fresh.status = "pending"
        fresh.updated_at = now
        fresh.error = exc.detail
        fresh.next_attempt_at = occ.next_attempt_at
        self.store.cas_item(fresh, ("pending", "firing"))
        _log(f"retry {occ.id} in {delay}s")

    def _after_occurrence(self, item, occ, now) -> bool:
        if item.kind == "watch":
            return self._finish_watch(item, occ, now)
        return self._advance_recurring(item, occ, now)

    def _finish_watch(self, item, occ, now) -> bool:
        item = self.store.get(item.id) or item
        if item.status == "cancelled":
            return False
        item.updated_at = now
        item.next_attempt_at = None
        item.late = occ.late
        item.late_by_seconds = occ.late_by_seconds
        item.attempts = 0
        if occ.status == "delivered":
            item.status = "delivered"
            item.delivered_at = now
            item.error = None
            item.receipt = occ.receipt
        else:
            item.status = "undeliverable"
            item.error = occ.error
            item.receipt = occ.receipt
        spec = dict(item.spec or {})
        spec["open_occurrence_id"] = None
        item.spec = spec
        self.store.cas_item(item, ("pending", "firing", "admitted", "ambiguous"))
        return True

    def _advance_recurring(self, item, occ, now) -> bool:
        item = self.store.get(item.id) or item
        if item.status == "cancelled":
            return False
        spec = dict(item.spec or {})
        spec["open_occurrence_id"] = None
        item.spec = spec
        item.updated_at = now
        item.next_attempt_at = None
        item.attempts = 0
        item.late = occ.late
        item.late_by_seconds = occ.late_by_seconds
        item.error = None
        if occ.status == "undeliverable" and _dead_error(occ.error):
            item.status = "undeliverable"
            item.error = occ.error
            self.store.cas_item(item, ("pending", "firing", "admitted", "ambiguous"))
            _log(f"undeliverable {item.id} {item.error}")
            return True
        every = int(spec.get("every") or 0)
        anchor = _parse(spec.get("anchor")) or occ.scheduled_at
        nxt = slot_after(anchor, every, now) if every else now + timedelta(seconds=60)
        reason = schedule_finished(spec, occ.seq, nxt)
        if reason:
            self._complete_recurring(item, now, reason)
            return True
        item.status = "pending"
        item.fire_at = nxt
        self.store.cas_item(item, ("pending", "firing", "admitted", "ambiguous"))
        return True

    def _complete_recurring(self, item, now, reason: str) -> None:
        item = self.store.get(item.id) or item
        if item.status == "cancelled":
            return
        delivered = self.store.count_occurrences(item.id, "delivered")
        spec = dict(item.spec or {})
        spec["open_occurrence_id"] = None
        spec["finished"] = reason
        item.spec = spec
        item.updated_at = now
        item.next_attempt_at = None
        if delivered:
            item.status = "completed"
            item.error = None
        else:
            item.status = "undeliverable"
            item.error = item.error or f"schedule ended ({reason}) with no delivered occurrence"
        self.store.cas_item(item, ("pending", "firing", "admitted", "ambiguous"))
        _log(f"{item.status} {item.id} {reason}")

    def _plan(self, item, occ, now) -> dict:
        spec = item.spec or {}
        previous = self.store.previous_occurrence(item.id, occ.seq)
        previous_text = None
        if previous is not None and previous.status == "undeliverable":
            code = (previous.error or "").partition(":")[0]
            previous_text = f"{previous.id} ({issue(code)['detail']})"
        if item.kind != "recurring":
            return {
                "coalesced": 0,
                "next_fire": None,
                "remaining": None,
                "continues": False,
                "previous": previous_text,
            }
        every = int(spec.get("every") or 0)
        anchor = _parse(spec.get("anchor")) or occ.scheduled_at
        until = _parse(spec.get("until"))
        nxt = slot_after(anchor, every, now) if every else None
        coalesced = coalesced_in_window(
            every, occ.scheduled_at, now, anchor, until,
        ) if every else 0
        reason = schedule_finished(spec, occ.seq, nxt) if nxt is not None else "count"
        continues = not reason
        return {
            "coalesced": coalesced,
            "next_fire": None if reason else nxt,
            "remaining": effective_remaining(spec, occ.seq, nxt, continues),
            "continues": continues,
            "previous": previous_text,
        }

    def _expire_watch(self, item, now, detail: str) -> None:
        item = self.store.get(item.id) or item
        if item.status == "cancelled":
            return
        item.status = "expired"
        item.updated_at = now
        item.error = detail or "expired before the condition was met"
        item.next_attempt_at = None
        spec = dict(item.spec or {})
        spec["open_occurrence_id"] = None
        item.spec = spec
        self.store.cas_item(item, ("pending", "firing"))
        _log(f"expired {item.id} {item.error}")

    def _cancel_occurrence(self, occ, now) -> None:
        occ.status = "cancelled"
        occ.updated_at = now
        occ.next_attempt_at = None
        self.store.cas_occurrence(occ, OPEN)

    def _undeliverable(self, item, code: str, detail: str) -> None:
        now = self.clock.now()
        fresh = self.store.get(item.id) or item
        if fresh.status == "cancelled":
            return
        fresh.status = "undeliverable"
        fresh.updated_at = now
        fresh.error = f"{code}: {detail}" if detail else code
        fresh.next_attempt_at = None
        self.store.cas_item(fresh, ("pending", "firing", "admitted", "ambiguous"))
        _log(f"undeliverable {fresh.id} {fresh.error}")

    def _fail(self, item, exc: DeliveryError) -> None:
        now = self.clock.now()
        fresh = self.store.get(item.id) or item
        if fresh.status == "cancelled":
            return
        fresh.updated_at = now
        if (fresh.receipt or {}).get("state") == "admitted":
            return
        if not exc.retryable or fresh.attempts >= MAX_ATTEMPTS:
            self._undeliverable(fresh, exc.code, exc.detail)
            return
        delay = min(60, 2 ** max(fresh.attempts, 1))
        fresh.status = "pending"
        fresh.error = exc.detail
        fresh.next_attempt_at = now + timedelta(seconds=delay)
        if not self.store.cas_item(fresh, ("firing", "pending")):
            return
        _log(f"retry {fresh.id} in {delay}s")

    def _backoff_unexpected(self, item, exc: Exception) -> None:
        now = self.clock.now()
        fresh = self.store.get(item.id) or item
        if fresh.status != "pending":
            return
        spec = dict(fresh.spec or {})
        spec["tick_errors"] = int(spec.get("tick_errors") or 0) + 1
        fresh.spec = spec
        fresh.updated_at = now
        fresh.error = _short(exc)
        if spec["tick_errors"] >= MAX_ATTEMPTS:
            fresh.status = "undeliverable"
            fresh.next_attempt_at = None
            self.store.cas_item(fresh, ("pending",))
            _log(f"undeliverable {fresh.id} {fresh.error}")
            return
        fresh.status = "pending"
        fresh.next_attempt_at = now + timedelta(seconds=30)
        self.store.cas_item(fresh, ("pending",))
        _log(f"backoff {fresh.id} {fresh.error}")

    def _lease_fresh(self, firing_at, now) -> bool:
        if firing_at is None:
            return False
        return _utc(firing_at) > _utc(now) - timedelta(seconds=LEASE_SECONDS)


def log_scheduler_error(exc: BaseException) -> None:
    """Log a scheduler failure without a credential or a request body."""
    _log("scheduler error: delivery operation failed; check delivery status")


def _needs_check(item) -> bool:
    return item.kind == "watch" and not (item.spec or {}).get("open_occurrence_id")


def _dead_error(error: str | None) -> bool:
    text = error or ""
    return text.startswith(TARGET_DEAD)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _short(exc: Exception) -> str:
    return issue("unavailable")["detail"]


def _log(message: str) -> None:
    print(f"docket: {message}", file=sys.stderr, flush=True)
