"""Opt-in lead check-ins. No commands, new transport, or background loop.

Only IDs reserved in the private ledger are owned. Watch specs are immutable
intent; observations and existing scheduler receipt fields may still advance.
"""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from docket.bounds import LimitError, check_limits, watch_spec
from docket.checks import Outcome
from docket.parse import new_id, validate_session_id
from docket.store import Item

IDLE = 30 * 60
MILESTONE = 3 * 3600
CADENCE = 60
PROMPTS = {
    "idle": "You've been idle with your goal unfinished. Continue, or if you're stuck, tell the chief in one plain sentence.",
    "milestone": "There has been no recorded visible milestone for about three hours. Tell the chief in one plain sentence whether to continue, shrink or shelve, and why.",
}


def stamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs timezone")
    return parsed.astimezone(timezone.utc)


def private_read(path):
    """Refuse links, non-owned files and non-private files/directories."""
    for candidate in (path.parent, path):
        view = candidate.lstat()
        if (stat.S_ISLNK(view.st_mode) or view.st_uid != os.getuid()
                or view.st_mode & 0o077):
            raise ValueError("check-in path must be owner-only and not a symlink")
    if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > 256_000:
        raise ValueError("invalid check-in file")
    return path.read_text()


def roster(path):
    return parse_roster(private_read(path))


def parse_roster(text):
    """Shared reader/writer roster validation without separate storage."""
    rows = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        sid = row.get("session")
        if (not isinstance(sid, str) or validate_session_id(sid)
                or sid in rows or row.get("state") not in ("active", "hold", "done")
                or not isinstance(row.get("directory"), str)
                or not row["directory"].startswith("/") or "\x00" in row["directory"]):
            raise ValueError("invalid check-in roster line")
        at, ref = row.get("milestone_at"), row.get("milestone_ref")
        if at is not None:
            stamp(at)
            if not isinstance(ref, str) or not ref.strip() or len(ref) > 1000:
                raise ValueError("milestone requires a reference")
        elif ref is not None:
            raise ValueError("milestone reference requires a timestamp")
        rows[sid] = row
    if len(rows) > 20:
        raise ValueError("check-in roster is limited to 20 leads")
    return rows


class Checkins:
    def __init__(self, scheduler, root=None):
        self.scheduler = scheduler
        self.root = root or scheduler.store.path.parent
        self.roster_path = self.root / "lead-checkins.jsonl"
        self.ledger_path = self.root / "lead-checkins-state.json"
        self.next_tick = None
        self.error = None
        self.state = {"leads": {}, "owned": {}}
        self.broken = False
        try:
            self.state = json.loads(private_read(self.ledger_path))
            if not isinstance(self.state["leads"], dict) or not isinstance(self.state["owned"], dict):
                raise ValueError("invalid ledger")
        except FileNotFoundError:
            if any(self.marked(item) for item in scheduler.store.list_outstanding()):
                self.broken = True
                self.error = "check-in ledger missing with outstanding intent; dispatch suppressed"
        except Exception:
            self.broken = True
            self.error = "check-in ledger unreadable; dispatch suppressed"
            self.state = {"leads": {}, "owned": {}}
        for lead in self.state["leads"].values():
            lead["idle_since"] = None  # never infer continuous idle across restart
        from docket.delivery_stall import DeliveryStall
        self.delivery_stall = DeliveryStall(self)

    def save(self):
        # Reserve intent before inserting a watch. A crash may lose a nudge,
        # never ownership or a stable admission. Never overwrite a linked file.
        tmp = self.ledger_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.ledger_path)

    def marked(self, item):
        return bool((item.spec or {}).get("lead_checkin"))

    def snapshot(self, sid):
        row = roster(self.roster_path).get(sid)
        if row is None or row["state"] != "active":
            return None
        adapter = self.scheduler.adapter
        goal = adapter.session_goal(sid)
        if not isinstance(goal, dict) or goal.get("status") not in ("active", "blocked"):
            return None
        identity = self.goal_identity(goal)
        if identity is None:
            return None
        state = adapter.session_state(sid)
        if state not in ("idle", "active"):
            return None
        if not adapter.validate({"session_id": sid, "directory": row["directory"]}).ok:
            return None
        return row, identity, state

    @staticmethod
    def goal_identity(goal):
        # Filled from native identity, never outcome or session activity times.
        identity, created = goal.get("id"), goal.get("createdAt")
        if (not isinstance(identity, str) or not identity or type(created) is not int
                or not 0 < created < 10**15):
            return None
        return [identity, created]

    def stop(self, item):
        intent = self.state["owned"].get(item.id)
        if intent is None or intent != (item.spec or {}).get("lead_checkin"):
            return False  # labels/specs supplied by others never confer ownership
        store = self.scheduler.store
        occ = store.open_occurrence(item)
        # The newer core owns cancellation and exact frozen-envelope evidence.
        # Never use the older ID-only lookup or infer safety from a missing receipt.
        result = self.scheduler.stop(item)
        req = store.get_request((occ or item).id)
        safe = result in ("not_sent", "retracted") or bool(req["facts"].get("consumed"))
        slot = self.state["leads"][intent["session"]]["slots"][intent["kind"]]
        slot["blocked"] = not safe
        if req["facts"].get("consumed"):
            delay = IDLE if intent["kind"] == "idle" else MILESTONE
            slot["after"] = (self.scheduler.clock.now() + timedelta(seconds=delay)).isoformat()
        self.save()
        return safe

    def allowed(self, item, now, *, matching=False):
        """Fresh roster AND host gate, including latched sends and retries."""
        if not self.marked(item):
            return True
        try:
            if self.broken:
                return False
            intent = self.state["owned"].get(item.id)
            if not intent or intent != (item.spec or {}).get("lead_checkin"):
                return False
            sid, kind = intent["session"], intent["kind"]
            lead = self.state["leads"][sid]
            snap = self.snapshot(sid)
            if snap is None:
                return False
            row, identity, activity = snap
            marker = [row.get("milestone_at"), row.get("milestone_ref")]
            if (identity != intent["goal"] or row["directory"] != item.target["directory"]
                    or marker != intent["marker"] or lead["goal"] != identity):
                return False
            if kind == "idle":
                gap = not lead.get("sample_at") or (now - stamp(lead["sample_at"])).total_seconds() > 2 * CADENCE
                if activity != "idle" or gap:
                    lead["idle_since"] = None
                    self.save()
                    return False
                since = lead.get("idle_since")
                return since is not None and (now - stamp(since)).total_seconds() >= IDLE
            return (now - stamp(intent["baseline"])).total_seconds() >= MILESTONE
        except Exception:
            return False

    def evaluate(self, item, now):
        return Outcome("met" if self.allowed(item, now, matching=True) else "waiting")

    def tick(self, now):
        if self.broken or self.next_tick and now < self.next_tick:
            return
        self.next_tick = now + timedelta(seconds=CADENCE)
        try:
            self.limited = False
            self.reconcile(now)
            self.error = "check-in watch capacity reached; excess leads wait" if self.limited else None
        except Exception:
            # No file contents, session metadata or credentials in diagnostics.
            self.error = "check-in reconciliation unavailable; dispatch suppressed"
            self.broken = True

        if not self.broken:
            try:
                self.delivery_stall.tick(now)
            except Exception:
                self.delivery_stall.samples.clear()
                self.delivery_stall.error = "delivery-stall ledger unavailable; publication suppressed"
            if self.delivery_stall.error:
                self.error = "; ".join(filter(None, (self.error, self.delivery_stall.error)))

    def reconcile(self, now):
        try:
            rows = roster(self.roster_path)
        except Exception:
            rows = {}
        store = self.scheduler.store
        for sid in set(rows) | set(self.state["leads"]):
            lead = self.state["leads"].setdefault(sid, {"slots": {}})
            try:
                snap = self.snapshot(sid) if sid in rows else None
            except Exception:
                snap = None
            row, identity, activity = snap if snap else ({}, None, None)
            marker = [row.get("milestone_at"), row.get("milestone_ref")]
            changed = (lead.get("goal") != identity or lead.get("marker") != marker
                       or lead.get("directory") != row.get("directory"))
            for kind, slot in lead["slots"].items():
                item = store.get(slot["id"])
                if item and item.status == "cancelled":
                    occ = store.open_occurrence(item)
                    req = store.get_request((occ or item).id)
                    if req and not any(req["facts"].get(key) for key in ("cancelled", "consumed")):
                        slot["blocked"] = True
                if item and item.status in ("pending", "firing", "admitted", "ambiguous"):
                    if not snap or changed or kind == "idle" and activity != "idle":
                        slot["blocked"] = not self.stop(item)
                if item and item.status == "delivered" and not slot.get("consumed"):
                    slot["consumed"] = True
                    slot["after"] = ((item.delivered_at or now) + timedelta(seconds=IDLE if kind == "idle" else MILESTONE)).isoformat()
            if not snap:
                lead["idle_since"] = None
                continue
            if changed:
                lead.update(goal=identity, marker=marker, directory=row["directory"],
                            baseline=now.isoformat(), idle_since=None)
                for slot in lead["slots"].values():
                    slot.pop("after", None)
                    slot.pop("consumed", None)
            if lead.get("sample_at") and (now - stamp(lead["sample_at"])).total_seconds() > 2 * CADENCE:
                lead["idle_since"] = None
            lead["sample_at"] = now.isoformat()
            if activity != "idle":
                lead["idle_since"] = None
            elif lead.get("idle_since") is None:
                lead["idle_since"] = now.isoformat()
            baseline = max(stamp(lead["baseline"]), stamp(row["milestone_at"])) if row.get("milestone_at") else stamp(lead["baseline"])
            for kind in PROMPTS:
                slot = lead["slots"].get(kind)
                if slot:
                    item = store.get(slot["id"])
                    if slot.get("blocked") or item is None:
                        continue  # reserved but missing or uncertain: never replay
                    if item.status in ("pending", "firing", "admitted", "ambiguous", "undeliverable"):
                        continue
                    if slot.get("after") and now < stamp(slot["after"]):
                        continue
                if kind == "idle" and activity != "idle":
                    continue
                try:
                    check_limits(store.active_items(), kind="watch", session_id=sid, created_by="lead-checkins")
                except LimitError:
                    self.limited = True
                    continue
                intent = {"session": sid, "kind": kind, "goal": identity,
                          "marker": marker, "baseline": baseline.isoformat()}
                fire, spec = watch_spec({"check": "session_idle", "watch_session": sid,
                                         "every": CADENCE, "until": 86400}, now=now, capture_process=None)
                if kind == "milestone" and slot and slot.get("after"):
                    intent["baseline"] = (stamp(slot["after"]) - timedelta(seconds=MILESTONE)).isoformat()
                spec["lead_checkin"] = intent
                ident = new_id()
                if slot:
                    self.state["owned"].pop(slot["id"], None)
                lead["slots"][kind] = {"id": ident}
                self.state["owned"][ident] = intent
                self.save()
                store.insert(Item(id=ident, kind="watch", status="pending", fire_at=fire,
                                  created_at=now, updated_at=now, reason=PROMPTS[kind], context={},
                                  target={"adapter": "opencode", "session_id": sid, "directory": row["directory"]},
                                  urgent=False, created_by="lead-checkins", late=False, late_by_seconds=None,
                                  delivered_at=None, cancelled_at=None, error=None, receipt=None,
                                  attempts=0, next_attempt_at=None, firing_at=None, spec=spec))
        if self.state["leads"]:
            self.save()
