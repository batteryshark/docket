"""Claims due items and hands them to a delivery adapter.

The scheduler never runs item text. A failure to deliver is recorded; it is
not turned into a command.

Admission is not consumption. A 200 from the host means the prompt was
admitted. ``delivered`` is recorded only after that stable id is in the
session transcript. A confirmed admission that later vanishes is
``undeliverable`` (lost), not delivered, and it is not sent again. An
unconfirmed send is retried with the same id. A conflict whose id cannot be
found is ``ambiguous`` and is not given a new id.

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
from docket.delivery import (
    AMBIGUOUS,
    CONSUMED,
    FAILED,
    DeliveryError,
    retract_admission,
    scrub,
)
from docket.parse import is_late, late_by_seconds, new_admission_id, new_occurrence_id, wake_text
from docket.store import Occurrence

LEASE_SECONDS = 60
MAX_ATTEMPTS = 8
TARGET_DEAD = ("bad_target", "archived", "wrong_target")
OPEN = ("pending", "firing", "admitted", "ambiguous")


class Scheduler:
    def __init__(self, store, adapter, clock, checks=None, check_executor=None):
        self.store = store
        self.adapter = adapter
        self.clock = clock
        self.checks = checks or Checks(adapter)
        self.check_executor = check_executor or CheckExecutor()
        self._inflight: dict[str, str] = {}
        self._inflight_cv = threading.Condition()

    def tick(self) -> list[str]:
        now = self.clock.now()
        touched: list[str] = []
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
        if item is None or item.status != "firing":
            return False
        state = (item.receipt or {}).get("state")
        aid = (item.receipt or {}).get("admission_id")
        item.updated_at = now
        if state == "consumed":
            item.status = "delivered"
            item.delivered_at = item.delivered_at or now
            item.error = None
            self.store.cas_item(item, ("firing",))
            return True
        if state == "admitted" and aid:
            item.status = "admitted"
            item.error = None
            self.store.cas_item(item, ("firing",))
            return True
        if not aid:
            item.status = "ambiguous"
            item.error = "ambiguous: firing claim had no stable admission id"
            item.next_attempt_at = None
            self.store.cas_item(item, ("firing",))
            _log(f"ambiguous {item.id} no admission id")
            return True
        item.status = "pending"
        item.next_attempt_at = now
        self.store.cas_item(item, ("firing",))
        return True

    def _reclaim_occurrence(self, occ_id: str, now) -> bool:
        occ = self.store.get_occurrence(occ_id)
        if occ is None or occ.status != "firing":
            return False
        state = (occ.receipt or {}).get("state")
        aid = (occ.receipt or {}).get("admission_id")
        occ.updated_at = now
        parent = self.store.get(occ.item_id)
        if state == "consumed":
            occ.status = "delivered"
            occ.delivered_at = occ.delivered_at or now
            occ.error = None
            if self.store.cas_occurrence(occ, ("firing",)) and parent is not None:
                self._after_occurrence(parent, occ, now)
            return True
        if state == "admitted" and aid:
            occ.status = "admitted"
            occ.error = None
            self.store.cas_occurrence(occ, ("firing",))
            self._hold_parent(parent, now, occ)
            return True
        if not aid:
            occ.status = "ambiguous"
            occ.error = "ambiguous: firing claim had no stable admission id"
            occ.next_attempt_at = None
            self.store.cas_occurrence(occ, ("firing",))
            self._hold_parent(parent, now, occ)
            _log(f"ambiguous {occ.id} no admission id")
            return True
        occ.status = "pending"
        occ.next_attempt_at = now
        self.store.cas_occurrence(occ, ("firing",))
        if parent is not None and parent.status == "pending":
            parent.next_attempt_at = now
            parent.updated_at = now
            self.store.cas_item(parent, ("pending",))
        return True

    def _fire(self, item_id: str) -> bool:
        now = self.clock.now()
        if not self.store.claim(item_id, now):
            return False
        item = self.store.get(item_id)
        if item is None or item.status != "firing":
            return False
        aid = self._ensure_item_id(item)
        if aid is None:
            return False
        item = self.store.get(item_id) or item
        decision = self._decide(item, None, aid)
        if decision != "send":
            return True
        try:
            check = self.adapter.validate(item.target)
        except DeliveryError as exc:
            self._fail(item, exc)
            return True
        if not check.ok:
            if check.code == "unavailable":
                self._fail(item, DeliveryError(check.code, check.detail, retryable=True))
            else:
                self._undeliverable(item, check.code, check.detail)
            return True
        if not self._mark_sending_item(item, aid):
            return False
        self._enter_send(item.id, aid)
        try:
            late = is_late(item.fire_at, now)
            text = wake_text(
                kind=item.kind,
                item_id=item.id,
                reason=item.reason,
                fire_at=item.fire_at,
                now=now,
                late=late,
                urgent=item.urgent,
                created_by=item.created_by,
                context=item.context,
            )
            try:
                sent = self.adapter.deliver(item.target, text, urgent=item.urgent, admission_id=aid)
            except DeliveryError as exc:
                if exc.code == "conflict":
                    return self._conflict_item(item, aid)
                self._fail(item, exc)
                return True
            item.late = late
            item.late_by_seconds = late_by_seconds(item.fire_at, now) if late else None
            return self._after_send_item(item, sent)
        finally:
            self._leave_send(item.id)

    def _fire_recurring(self, item) -> bool:
        now = self.clock.now()
        spec = dict(item.spec or {})
        occ_id = spec.get("open_occurrence_id")
        if occ_id:
            occ = self.store.get_occurrence(occ_id)
            if occ is None:
                spec["open_occurrence_id"] = None
                item.spec = spec
                item.updated_at = now
                self.store.cas_item(item, ("pending", "firing"))
                return True
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
        item.error = outcome.detail or None
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
            spec = dict(item.spec or {})
            spec["open_occurrence_id"] = None
            item.spec = spec
            item.updated_at = self.clock.now()
            self.store.cas_item(item, ("pending", "firing"))
            return True
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
        now = self.clock.now()
        if item.status == "cancelled" or occ.status == "cancelled":
            return False
        if occ.status == "pending":
            if not self.store.claim_occurrence(occ.id, now):
                return False
            occ = self.store.get_occurrence(occ.id)
        elif occ.status == "firing" and self._lease_fresh(occ.firing_at, now):
            return False
        elif occ.status != "firing":
            return self._after_occurrence(item, occ, now)
        if occ is None:
            return False
        aid = self._ensure_occ_id(item, occ)
        if aid is None:
            return False
        occ = self.store.get_occurrence(occ.id) or occ
        item = self.store.get(item.id) or item
        if self._decide(item, occ, aid) != "send":
            return True
        try:
            check = self.adapter.validate(item.target)
        except DeliveryError as exc:
            self._fail_occurrence(item, occ, exc)
            return True
        if not check.ok:
            retryable = check.code == "unavailable"
            self._fail_occurrence(
                item, occ, DeliveryError(check.code, check.detail, retryable=retryable),
            )
            return True
        if not self._mark_sending_occ(occ, aid):
            return False
        self._enter_send(item.id, aid)
        try:
            plan = self._plan(item, occ, now)
            late = is_late(occ.scheduled_at, now)
            text = wake_text(
                kind=item.kind,
                item_id=item.id,
                reason=item.reason,
                fire_at=occ.scheduled_at,
                now=now,
                late=late,
                urgent=item.urgent,
                created_by=item.created_by,
                context=item.context,
                occurrence_id=occ.id,
                coalesced=plan["coalesced"],
                check=(item.spec or {}).get("check"),
                summary=(occ.detail or {}).get("summary") or (item.spec or {}).get("summary"),
                next_fire=plan["next_fire"],
                remaining=plan["remaining"],
                series_continues=plan["continues"],
                previous_undeliverable=plan["previous"],
            )
            try:
                sent = self.adapter.deliver(item.target, text, urgent=item.urgent, admission_id=aid)
            except DeliveryError as exc:
                if exc.code == "conflict":
                    return self._conflict_occurrence(item, occ, aid)
                self._fail_occurrence(item, occ, exc)
                return True
            return self._after_send_occurrence(item, occ, sent, now)
        finally:
            self._leave_send(item.id)

    def _ensure_item_id(self, item) -> str | None:
        aid = (item.receipt or {}).get("admission_id")
        if aid:
            return aid
        aid = new_admission_id()
        item.receipt = {"admission_id": aid, "state": "prepared"}
        item.updated_at = self.clock.now()
        if not self.store.cas_item(item, ("firing",)):
            return None
        return aid

    def _ensure_occ_id(self, item, occ) -> str | None:
        aid = (occ.receipt or {}).get("admission_id")
        if aid:
            return aid
        aid = new_admission_id()
        occ.receipt = {"admission_id": aid, "state": "prepared"}
        occ.updated_at = self.clock.now()
        if not self.store.cas_occurrence(occ, ("firing",)):
            return None
        return aid

    def _mark_sending_item(self, item, aid: str) -> bool:
        fresh = self.store.get(item.id) or item
        if fresh.status != "firing":
            return False
        receipt = dict(fresh.receipt or {})
        receipt["admission_id"] = aid
        receipt["state"] = "sending"
        fresh.receipt = receipt
        fresh.updated_at = self.clock.now()
        return self.store.cas_item(fresh, ("firing",))

    def _mark_sending_occ(self, occ, aid: str) -> bool:
        fresh = self.store.get_occurrence(occ.id) or occ
        if fresh.status != "firing":
            return False
        receipt = dict(fresh.receipt or {})
        receipt["admission_id"] = aid
        receipt["state"] = "sending"
        fresh.receipt = receipt
        fresh.updated_at = self.clock.now()
        return self.store.cas_occurrence(fresh, ("firing",))

    def _decide(self, item, occ, aid: str) -> str:
        """Return send, or handle an existing id and return wait.

        A confirmed admission is never posted again. A missing unconfirmed id
        may be posted: the host returns the original admission if it landed.
        """
        receipt = (occ.receipt if occ is not None else item.receipt) or {}
        state = receipt.get("state")
        if state == "admitted":
            if occ is not None:
                self._reconcile_occurrence(item, occ)
            else:
                self._reconcile_item(item)
            return "wait"
        if state == "ambiguous":
            if occ is not None:
                self._reconcile_occurrence(item, occ)
            else:
                self._reconcile_item(item)
            return "wait"
        if state == "consumed":
            self._consume(item, occ, aid, receipt.get("delivery"))
            return "wait"
        found = self._lookup(item, occ, aid)
        if found == "stop":
            return "wait"
        if found == "consumed":
            self._consume(item, occ, aid, receipt.get("delivery"))
            return "wait"
        if found == "admitted":
            self._note_admitted(item, occ, aid, receipt.get("delivery"))
            return "wait"
        if found == "unavailable":
            return "wait"
        return "send"

    def _lookup(self, item, occ, aid: str) -> str:
        try:
            return self.adapter.reconcile(item.target, aid)
        except DeliveryError as exc:
            if occ is not None:
                self._fail_occurrence(item, occ, exc)
            else:
                self._fail(item, exc)
            return "stop"
        except Exception as exc:
            log_scheduler_error(exc)
            held = occ or item
            held.error = _short(exc)
            held.updated_at = self.clock.now()
            if occ is not None:
                self.store.cas_occurrence(occ, OPEN)
            else:
                self.store.cas_item(item, OPEN)
            return "unavailable"

    def _after_send_item(self, item, sent) -> bool:
        current = self.store.get(item.id)
        aid = sent.admission_id or sent.inbox_id
        if current is not None and current.status == "cancelled":
            self._retract_cancelled(item, aid)
            return True
        if sent.consumed:
            return self._consume(item, None, aid, sent.delivery)
        found = self._lookup_quiet(item.target, aid)
        if found == "consumed":
            return self._consume(item, None, aid, sent.delivery)
        # A 200 is admission even if the inbox row is not visible yet.
        return self._note_admitted(item, None, aid, sent.delivery)

    def _after_send_occurrence(self, item, occ, sent, now) -> bool:
        current = self.store.get(item.id)
        aid = sent.admission_id or sent.inbox_id
        if current is not None and current.status == "cancelled":
            self._retract_cancelled(item, aid)
            self._cancel_occurrence(occ, self.clock.now())
            return True
        if sent.consumed:
            return self._consume(item, occ, aid, sent.delivery, now=now)
        found = self._lookup_quiet(item.target, aid)
        if found == "consumed":
            return self._consume(item, occ, aid, sent.delivery, now=now)
        return self._note_admitted(item, occ, aid, sent.delivery)

    def _retract_cancelled(self, item, aid: str | None) -> None:
        """Drop a host prompt after cancel won the race. Record a failed drop.

        A missing id was either retracted or never accepted. A consumed or
        still-held id is written onto the cancelled row so a later reader
        does not treat the removal as clean. This does not admit again.
        """
        if not aid or not item.target:
            return
        outcome = retract_admission(
            self.adapter, item.target, aid, confirmed=False,
        )
        if outcome == FAILED:
            self._note_cancel_truth(
                item, aid, FAILED, "retraction failed: the host still holds this admission",
            )
        elif outcome == AMBIGUOUS:
            self._note_cancel_truth(
                item, aid, AMBIGUOUS, "retraction uncertain: the host could not be checked after cancel",
            )
        elif outcome == CONSUMED:
            self._note_cancel_truth(
                item, aid, CONSUMED, "not removed: the host already consumed this admission",
            )

    def _note_cancel_truth(self, item, aid: str, state: str, error: str) -> None:
        fresh = self.store.get(item.id)
        if fresh is None or fresh.status != "cancelled":
            return
        receipt = dict(fresh.receipt or {})
        receipt["admission_id"] = aid
        receipt["state"] = state
        fresh.receipt = receipt
        fresh.error = error
        fresh.updated_at = self.clock.now()
        self.store.cas_item(fresh, ("cancelled",))

    def _lookup_quiet(self, target, aid: str) -> str:
        try:
            return self.adapter.reconcile(target, aid)
        except Exception as exc:
            log_scheduler_error(exc)
            return "admitted"

    def _conflict_item(self, item, aid: str) -> bool:
        found = self._lookup_quiet(item.target, aid)
        if found == "consumed":
            return self._consume(item, None, aid, (item.receipt or {}).get("delivery"))
        if found == "admitted":
            return self._note_admitted(item, None, aid, (item.receipt or {}).get("delivery"))
        return self._ambiguous(item, None, aid, "host reported a conflict and the admission id was not found")

    def _conflict_occurrence(self, item, occ, aid: str) -> bool:
        found = self._lookup_quiet(item.target, aid)
        if found == "consumed":
            return self._consume(item, occ, aid, (occ.receipt or {}).get("delivery"))
        if found == "admitted":
            return self._note_admitted(item, occ, aid, (occ.receipt or {}).get("delivery"))
        return self._ambiguous(item, occ, aid, "host reported a conflict and the admission id was not found")

    def _reconcile_item(self, item) -> bool:
        aid = (item.receipt or {}).get("admission_id")
        if not aid:
            return False
        if item.status == "ambiguous":
            found = self._lookup_quiet(item.target, aid)
            if found == "consumed":
                return self._consume(item, None, aid, (item.receipt or {}).get("delivery"))
            if found == "admitted":
                return self._note_admitted(item, None, aid, (item.receipt or {}).get("delivery"))
            return False
        found = self._lookup_quiet(item.target, aid)
        if found == "consumed":
            return self._consume(item, None, aid, (item.receipt or {}).get("delivery"))
        if found in ("admitted", "unavailable"):
            return False
        return self._lose(item, None, aid)

    def _reconcile_occurrence(self, item, occ) -> bool:
        aid = (occ.receipt or {}).get("admission_id")
        if not aid:
            return False
        if occ.status == "ambiguous":
            found = self._lookup_quiet(item.target, aid)
            if found == "consumed":
                return self._consume(item, occ, aid, (occ.receipt or {}).get("delivery"))
            if found == "admitted":
                return self._note_admitted(item, occ, aid, (occ.receipt or {}).get("delivery"))
            self._hold_parent(item, self.clock.now(), occ)
            return False
        found = self._lookup_quiet(item.target, aid)
        if found == "consumed":
            return self._consume(item, occ, aid, (occ.receipt or {}).get("delivery"))
        if found in ("admitted", "unavailable"):
            self._hold_parent(item, self.clock.now(), occ)
            return False
        return self._lose(item, occ, aid)

    def _note_admitted(self, item, occ, aid: str, delivery: str | None) -> bool:
        now = self.clock.now()
        receipt = {
            "admission_id": aid,
            "inbox_id": aid,
            "delivery": delivery,
            "state": "admitted",
        }
        host_status = getattr(self.adapter, "host_status", None)
        if callable(host_status):
            receipt["host"] = host_status()
        if occ is None:
            fresh = self.store.get(item.id) or item
            if fresh.status == "cancelled":
                self.adapter.cancel_admission(item.target, aid)
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
                self.adapter.cancel_admission(item.target, aid)
                return True
            _log(f"admitted {fresh.id} {delivery or 'queue'}")
            return True
        occ = self.store.get_occurrence(occ.id) or occ
        if occ.status == "cancelled":
            self.adapter.cancel_admission(item.target, aid)
            return True
        occ.status = "admitted"
        occ.updated_at = now
        occ.error = None
        occ.next_attempt_at = None
        occ.delivered_at = None
        occ.receipt = receipt
        if not self.store.cas_occurrence(occ, ("firing", "admitted", "pending", "ambiguous")):
            self.adapter.cancel_admission(item.target, aid)
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
        host_status = getattr(self.adapter, "host_status", None)
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

    def _lose(self, item, occ, aid: str) -> bool:
        detail = "admitted prompt is neither in the inbox nor in the transcript"
        if occ is None:
            fresh = self.store.get(item.id) or item
            if fresh.status == "cancelled":
                return True
            fresh.status = "undeliverable"
            fresh.updated_at = self.clock.now()
            fresh.error = f"lost: {detail}"
            fresh.next_attempt_at = None
            receipt = dict(fresh.receipt or {})
            receipt["admission_id"] = aid
            receipt["state"] = "lost"
            fresh.receipt = receipt
            self.store.cas_item(fresh, ("admitted", "firing", "ambiguous"))
            _log(f"undeliverable {fresh.id} {fresh.error}")
            return True
        occ = self.store.get_occurrence(occ.id) or occ
        occ.status = "undeliverable"
        occ.updated_at = self.clock.now()
        occ.error = f"lost: {detail}"
        occ.next_attempt_at = None
        receipt = dict(occ.receipt or {})
        receipt["admission_id"] = aid
        receipt["state"] = "lost"
        occ.receipt = receipt
        if not self.store.cas_occurrence(occ, ("admitted", "firing", "ambiguous")):
            return False
        _log(f"undeliverable {occ.id} {occ.error}")
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

    def _finish_occurrence(self, item, occ, now, *, receipt: dict) -> None:
        """Compatibility for tests that still call the old completion helper."""
        aid = (receipt or {}).get("admission_id") or (receipt or {}).get("inbox_id") or "msg_recovered"
        delivery = (receipt or {}).get("delivery")
        if (receipt or {}).get("state") == "admitted":
            self._note_admitted(item, occ, aid, delivery)
            return
        self._consume(item, occ, aid, delivery, now=now)

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
            previous_text = f"{previous.id} ({previous.error or 'undeliverable'})"
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

    def _finish_delivered(self, item, now, *, receipt: dict) -> None:
        aid = (receipt or {}).get("admission_id") or (receipt or {}).get("inbox_id") or "msg_recovered"
        if (receipt or {}).get("state") == "admitted":
            self._note_admitted(item, None, aid, (receipt or {}).get("delivery"))
            return
        self._consume(item, None, aid, (receipt or {}).get("delivery"), now=now)

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
    text = scrub(f"{type(exc).__name__}: {exc}")
    _log("scheduler error: " + text[:300])


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
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return text[:180]


def _log(message: str) -> None:
    print(f"docket: {message}", file=sys.stderr, flush=True)
