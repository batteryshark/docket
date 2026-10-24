"""Bounded enrolled-roster view; reads existing volatile evidence only."""
import hashlib
import json

from docket.checkins import roster
from docket.parse import validate_session_id
from docket.tower_clock import canonical_time, utc


def validate_response(payload):
    try:
        if (not isinstance(payload, dict)
                or set(payload) != {"version", "source", "observed_at", "complete", "leads"}
                or type(payload["version"]) is not int or payload["version"] != 1
                or payload["source"] != "docket.project-index" or payload["complete"] is not True
                or not isinstance(payload["leads"], list) or len(payload["leads"]) > 20
                or len(json.dumps(payload).encode("utf-8")) > 65536):
            raise ValueError("invalid index envelope")
        observed = canonical_time(payload["observed_at"])
        sessions = set()
        for row in payload["leads"]:
            if not isinstance(row, dict) or set(row) != {"session", "directory", "roster_state", "activity"}:
                raise ValueError("invalid index entry")
            sid, directory = row["session"], row["directory"]
            if (not isinstance(sid, str) or validate_session_id(sid) or sid in sessions
                    or not isinstance(directory, str) or not directory.startswith("/")
                    or len(directory) > 4096 or "\x00" in directory
                    or row["roster_state"] not in ("active", "hold", "done")):
                raise ValueError("invalid index values")
            sessions.add(sid)
            activity = row["activity"]
            if activity == {"state": "unknown"}:
                continue
            if (not isinstance(activity, dict)
                    or set(activity) != {"state", "sample_at", "age_seconds", "basis"}
                    or activity["state"] != "busy" or activity["basis"] != "docket.running_sample"
                    or row["roster_state"] != "active" or type(activity["age_seconds"]) is not int):
                raise ValueError("invalid activity")
            age = (observed - canonical_time(activity["sample_at"])).total_seconds()
            if not 0 <= age <= 120 or activity["age_seconds"] != int(age):
                raise ValueError("invalid sample age")
    except (KeyError, TypeError, UnicodeError, OverflowError) as exc:
        raise ValueError("invalid index response") from exc


def activity(manager, sid, row, now):
    unknown = {"state": "unknown"}
    try:
        stall = manager.delivery_stall
        if (row["state"] != "active" or manager.broken or manager.error or stall.error):
            return unknown
        lead = manager.state["leads"][sid]
        goal = lead["goal"]
        if (lead["directory"] != row["directory"] or not isinstance(goal, list)
                or len(goal) != 2 or not isinstance(goal[0], str) or not goal[0]
                or type(goal[1]) is not int or not 0 < goal[1] < 10**15):
            return unknown
        key, sampled, _since = stall.samples[sid]
        expected = hashlib.sha256(json.dumps([sid, row["directory"], goal], sort_keys=True).encode()).hexdigest()
        age = (now - sampled).total_seconds()
        if key != expected or not 0 <= age <= 120:
            return unknown
        return dict(state="busy", sample_at=utc(sampled), age_seconds=int(age), basis="docket.running_sample")
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return unknown


def projection(manager, now):
    rows = roster(manager.roster_path)
    payload = dict(version=1, source="docket.project-index", observed_at=utc(now), complete=True,
                   leads=[dict(session=sid, directory=row["directory"], roster_state=row["state"],
                               activity=activity(manager, sid, row, now))
                          for sid, row in sorted(rows.items())])
    validate_response(payload)
    return payload
