"""Fixed Tower lease and authoritative recorded-marker projection. No native access."""
import hashlib
import json
import re
from datetime import timedelta, timezone

from docket.bounds import check_limits, slot_after
from docket.checkins import roster, stamp
from docket.store import Item
from docket.parse import validate_session_id

SERIES = "dkt_tower_refresh"
ACTOR = "docket.tower-refresh"
REFRESH_TYPE = "docket.snapshot.refresh_due"
MARKER_TYPE = "lead.milestone.recorded"


def utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def event_only(item):
    return item.id == SERIES and (item.spec or {}).get("tower_refresh") is True


def canonical_time(value):
    """Validate representation, not freshness (the caller owns freshness)."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z", value):
        raise ValueError("invalid UTC timestamp")
    parsed = stamp(value)
    if utc(parsed) != value:
        raise ValueError("noncanonical timestamp")
    return parsed


def marker_id(session, directory, milestone_at, reference):
    value = dict(session=session, directory=directory, milestone_at=milestone_at,
                 milestone_ref_sha256=hashlib.sha256(reference.encode("utf-8")).hexdigest())
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "mil_" + hashlib.sha256(raw).hexdigest()


def validate_marker(marker):
    fields = {"id", "session", "directory", "milestone_at", "milestone_ref", "roster_state"}
    if not isinstance(marker, dict) or set(marker) != fields:
        raise ValueError("invalid marker fields")
    directory, ref = marker["directory"], marker["milestone_ref"]
    if (validate_session_id(marker["session"]) or not isinstance(directory, str)
            or not directory.startswith("/") or len(directory) > 4096 or "\x00" in directory
            or not isinstance(ref, str) or not ref.strip() or len(ref) > 1000
            or marker["roster_state"] not in ("active", "hold", "done")):
        raise ValueError("invalid marker values")
    canonical_time(marker["milestone_at"])
    if marker["id"] != marker_id(marker["session"], directory, marker["milestone_at"], ref):
        raise ValueError("marker identity mismatch")


def validate_response(payload, marker_projection):
    """Fail closed on malformed new CLI responses, without an age window."""
    try:
        fields = {"version", "source", "observed_at"} | ({"complete", "markers"} if marker_projection else {"changed", "schedule"})
        if not isinstance(payload, dict) or set(payload) != fields or type(payload["version"]) is not int or payload["version"] != 1:
            raise ValueError("invalid envelope")
        observed = canonical_time(payload["observed_at"])
        if len(json.dumps(payload).encode("utf-8")) > 65536:
            raise ValueError("oversized envelope")
        if marker_projection:
            if payload["source"] != "docket.project-markers" or payload["complete"] is not True or not isinstance(payload["markers"], list) or len(payload["markers"]) > 20:
                raise ValueError("incomplete projection")
            ids, sessions = set(), set()
            for marker in payload["markers"]:
                validate_marker(marker)
                if marker["id"] in ids or marker["session"] in sessions:
                    raise ValueError("duplicate marker")
                ids.add(marker["id"])
                sessions.add(marker["session"])
            return
        if payload["source"] != "docket.tower-refresh-schedule" or type(payload["changed"]) is not bool:
            raise ValueError("invalid schedule envelope")
        s = payload["schedule"]
        if s is None:
            if payload["changed"]:
                raise ValueError("absent schedule changed")
            return
        fields = {"item_id", "status", "purpose", "every_seconds", "anchor", "next_fire_at", "until", "lease_expired", "cancelled_at", "last_occurrence"}
        if (not isinstance(s, dict) or set(s) != fields or s["item_id"] != SERIES or s["purpose"] != "project_pages"
                or type(s["every_seconds"]) is not int or s["every_seconds"] != 900
                or s["status"] not in ("pending", "firing", "completed", "cancelled", "undeliverable", "expired")
                or type(s["lease_expired"]) is not bool):
            raise ValueError("invalid schedule values")
        anchor, until = canonical_time(s["anchor"]), canonical_time(s["until"])
        if until <= anchor or s["lease_expired"] != (until <= observed) or (s["status"] == "expired" and not s["lease_expired"]):
            raise ValueError("inconsistent lease")
        needs_next = s["status"] == "pending" and not s["lease_expired"]
        if needs_next:
            nxt = canonical_time(s["next_fire_at"])
            if nxt <= anchor or (nxt-anchor).total_seconds() % 900:
                raise ValueError("invalid next slot")
        elif s["next_fire_at"] is not None:
            raise ValueError("unexpected next slot")
        if s["status"] == "cancelled":
            canonical_time(s["cancelled_at"])
        elif s["cancelled_at"] is not None:
            raise ValueError("unexpected cancellation")
        occ = s["last_occurrence"]
        if occ is not None:
            if not isinstance(occ, dict) or set(occ) != {"id", "status", "scheduled_at", "queue_result"}:
                raise ValueError("invalid occurrence")
            if not isinstance(occ["id"], str) or not re.fullmatch(r"occ_[0-9a-f]{16}", occ["id"]):
                raise ValueError("invalid occurrence id")
            canonical_time(occ["scheduled_at"])
            outcomes = {"pending": (None,), "firing": (None, "uncertain"), "cancelled": (None, "uncertain"),
                        "enqueued": ("accepted",), "undeliverable": ("full", "disabled", "uncertain")}
            if not isinstance(occ["status"], str) or occ["status"] not in outcomes or occ["queue_result"] not in outcomes[occ["status"]]:
                raise ValueError("invalid occurrence outcome")
    except (TypeError, KeyError, UnicodeError, OverflowError) as exc:
        raise ValueError("invalid response") from exc


def markers(rows):
    result = []
    for sid, row in sorted(rows.items()):
        if len(row["directory"]) > 4096:
            raise ValueError("directory too long")
        if row.get("milestone_at") is None:
            continue
        at = utc(stamp(row["milestone_at"]))
        ref = row["milestone_ref"]
        result.append(dict(id=marker_id(sid, row["directory"], at, ref), session=sid,
                           directory=row["directory"], milestone_at=at,
                           milestone_ref=ref, roster_state=row["state"]))
    if len(result) > 20:
        raise ValueError("too many markers")
    return result


def projection(path, now):
    payload = dict(version=1, source="docket.project-markers", observed_at=utc(now),
                   complete=True, markers=markers(roster(path)))
    # Match the HTTP serializer, including escaping and whitespace.
    if len(json.dumps(payload).encode("utf-8")) > 65536:
        raise ValueError("projection too large")
    return payload


def observe(manager, rows, now):
    """Reserve durably before best effort enqueue; unknown reservations never resend."""
    if getattr(manager, "broken", False):
        return
    from docket.tower_publish import enabled
    if not enabled():
        return
    ledger = manager.state.setdefault("marker_observations", {})
    for marker in markers(rows):
        key = marker["id"]
        if key in ledger:
            continue
        ledger[key] = {"queue_result": "uncertain", "observed_at": utc(now)}
        manager.save()
        data = dict(version=1, lead_session=marker["session"], directory=marker["directory"],
                    milestone_at=marker["milestone_at"], milestone_ref=marker["milestone_ref"],
                    observed_at=utc(now), roster_state=marker["roster_state"],
                    provenance="recorded_marker_observation")
        ledger[key]["queue_result"] = manager.scheduler.tower.note_fixed(MARKER_TYPE, key, data, utc(now))
        manager.save()


def view(store, now, changed=False):
    item = store.get(SERIES)
    schedule = None
    if item:
        spec = item.spec
        expired = stamp(spec["until"]) <= now
        occ = store.previous_occurrence(SERIES, int(spec.get("next_seq") or 1))
        schedule = dict(item_id=SERIES, status=item.status, purpose="project_pages", every_seconds=900,
                        anchor=spec["anchor"], next_fire_at=utc(item.fire_at) if item.status == "pending" and not expired else None,
                        until=spec["until"], lease_expired=expired,
                        cancelled_at=utc(item.cancelled_at) if item.cancelled_at else None,
                        last_occurrence=None if occ is None else dict(id=occ.id, status=occ.status,
                            scheduled_at=utc(occ.scheduled_at), queue_result=(occ.receipt or {}).get("queue_result")))
    return dict(version=1, source="docket.tower-refresh-schedule", observed_at=utc(now), changed=changed, schedule=schedule)


class ClockError(ValueError):
    def __init__(self, status, code, detail):
        self.status, self.code, self.detail = status, code, detail


def mutate(scheduler, body, cancel=False):
    store, now = scheduler.store, scheduler.clock.now()
    item = store.get(SERIES)
    if cancel:
        if body:
            raise ClockError(400, "invalid", "cancel requires an empty object; remove all fields")
        changed = item is not None and item.status != "cancelled"
        if changed:
            item.status, item.cancelled_at, item.updated_at = "cancelled", now, now
            store.cas_item(item, ("pending", "firing", "completed", "undeliverable", "expired"))
            occ = store.open_occurrence(item)
            if occ and occ.status in ("pending", "firing"):
                scheduler._cancel_occurrence(occ, now)
        return view(store, now, changed)
    try:
        if set(body) != {"until"} or not isinstance(body["until"], str):
            raise ValueError()
        until = stamp(body["until"])
        if not now < until <= now + timedelta(days=90):
            raise ValueError()
    except (ValueError, TypeError):
        raise ClockError(400, "invalid", "provide only until: a timezone-aware future ISO deadline within 90 days") from None
    if item and item.status == "cancelled":
        raise ClockError(409, "schedule_cancelled", "explicitly cancelled; obtain fresh operator approval, do not recreate or renew")
    if item and until <= stamp(item.spec["until"]):
        return view(store, now)
    if item and item.status != "pending" and item.spec.get("finished") != "until":
        raise ClockError(409, "schedule_failed", "lease has an unrelated failure; inspect it before requesting operator repair")
    if not item or item.status != "pending":
        check_limits(store.active_items(), kind="recurring", session_id="", created_by=ACTOR)
    if item is None:
        from docket.tower_publish import enabled
        if not enabled():
            raise ClockError(503, "publisher_disabled", "Tower publisher is unconfigured; configure the existing publisher before ensure")
        item = Item(id=SERIES, kind="recurring", status="pending", fire_at=now+timedelta(seconds=900),
                    created_at=now, updated_at=now, reason="Tower project pages refresh", context={},
                    target={"adapter": "tower"}, urgent=False, created_by=ACTOR, late=False,
                    late_by_seconds=None, delivered_at=None, cancelled_at=None, error=None, receipt=None,
                    attempts=0, next_attempt_at=None, firing_at=None,
                    spec=dict(tower_refresh=True, every=900, anchor=utc(now), until=utc(until),
                              next_seq=1, open_occurrence_id=None))
        store.insert(item)
    else:
        prior = item.status
        item.spec = dict(item.spec, until=utc(until))
        item.spec.pop("finished", None)
        item.status, item.updated_at = "pending", now
        if prior != "pending":
            item.fire_at = slot_after(stamp(item.spec["anchor"]), 900, now)
            item.error, item.next_attempt_at = None, None
        store.cas_item(item, (prior,))
    return view(store, now, True)
