"""Time parsing, identifiers, and the wake notice.

A fired item is a notice. Nothing in an item is executed.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

SESSION_RE = re.compile(r"^ses_[A-Za-z0-9]+$")
DURATION_RE = re.compile(
    r"^\s*(?:in\s+)?(?:(\d+)\s*h(?:ours?)?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?\s*(?:(\d+)\s*s(?:ec(?:ond)?s?)?)?\s*$",
    re.IGNORECASE,
)
CLOCK_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")

LATE_GRACE = timedelta(seconds=5)
# A clock time this recently past still means "today", and may be reported late.
CLOCK_PAST_WINDOW = timedelta(seconds=60)
MAX_REASON = 2000
MAX_CONTEXT_BYTES = 8192

IMPLEMENTED_KINDS = ("timer", "alarm", "recurring", "watch")
RESERVED_KINDS = ()


def new_id() -> str:
    return "dkt_" + uuid.uuid4().hex[:16]


def new_occurrence_id() -> str:
    return "occ_" + uuid.uuid4().hex[:16]


def new_admission_id() -> str:
    """Stable OpenCode prompt id. The host dedupes a retry of the same id."""
    return "msg_" + uuid.uuid4().hex


def require_bool(value, name: str) -> bool:
    """Accept only a JSON boolean. The string ``false`` is not false."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be true or false")


def validate_session_id(value: str) -> str | None:
    if not isinstance(value, str) or not SESSION_RE.match(value):
        return "session id must look like ses_…"
    return None


def parse_delay(value, *, now: datetime) -> datetime:
    """Return an aware UTC fire time from a relative delay."""
    seconds = _delay_seconds(value)
    if seconds < 0:
        raise ValueError("delay must not be negative")
    if seconds > 366 * 24 * 3600:
        raise ValueError("delay is too far ahead")
    return _utc(now) + timedelta(seconds=seconds)


def local_zone() -> tzinfo:
    """The machine zone, from /etc/localtime, without a tzdata install."""
    path = Path("/etc/localtime")
    try:
        with path.open("rb") as handle:
            return ZoneInfo.from_file(handle, key="local")
    except OSError:
        return datetime.now().astimezone().tzinfo or timezone.utc


def parse_alarm(value: str, *, now: datetime, zone: tzinfo | None = None) -> datetime:
    """Return an aware UTC fire time for an absolute alarm.

    ``HH:MM`` and ``HH:MM:SS`` are local clock times. A time still ahead, or at
    most a minute past, is today; an older clock time is the next day. A full
    timestamp is used as given, including one already past (it fires late).
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("alarm time is required")
    text = value.strip()
    clock = CLOCK_RE.match(text)
    if clock:
        zone = zone or local_zone()
        local_now = _utc(now).astimezone(zone)
        hour, minute, second = int(clock.group(1)), int(clock.group(2)), int(clock.group(3) or 0)
        if hour > 23 or minute > 59 or second > 59:
            raise ValueError("clock time is not a valid time of day")
        candidate = local_now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if candidate < local_now - CLOCK_PAST_WINDOW:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("alarm time must be HH:MM or an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone or local_zone())
    return parsed.astimezone(timezone.utc)


def _delay_seconds(value) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError("delay is required")
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        raise ValueError("delay must be seconds or a duration like 20m")
    text = value.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    match = DURATION_RE.match(text)
    if not match or not any(match.groups()):
        raise ValueError("delay must be seconds or a duration like 20m or 'in 20 minutes'")
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def check_reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason is required")
    if len(reason) > MAX_REASON:
        raise ValueError("reason is too long")
    return reason.strip()


def check_context(context) -> dict:
    if context is None:
        return {}
    if not isinstance(context, dict):
        raise ValueError("context must be an object")
    raw = json.dumps(context, separators=(",", ":"), default=str)
    if len(raw.encode()) > MAX_CONTEXT_BYTES:
        raise ValueError("context is too large")
    return context


def is_late(fire_at: datetime, now: datetime, *, grace: timedelta = LATE_GRACE) -> bool:
    return _utc(now) > _utc(fire_at) + grace


def late_by_seconds(fire_at: datetime, now: datetime) -> float:
    return max(0.0, (_utc(now) - _utc(fire_at)).total_seconds())


def format_span(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} second" + ("" if seconds == 1 else "s")
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        text = f"{minutes} minute" + ("" if minutes == 1 else "s")
        if sec:
            text += f" {sec} second" + ("" if sec == 1 else "s")
        return text
    hours, minutes = divmod(minutes, 60)
    text = f"{hours} hour" + ("" if hours == 1 else "s")
    if minutes:
        text += f" {minutes} minute" + ("" if minutes == 1 else "s")
    return text


def wake_text(*, kind: str, item_id: str, reason: str, fire_at: datetime,
              now: datetime, late: bool, urgent: bool, created_by: str | None,
              context: dict, zone: tzinfo | None = None,
              occurrence_id: str | None = None, coalesced: int = 0,
              check: str | None = None, summary: dict | None = None,
              next_fire: datetime | None = None, remaining: int | None = None,
              series_continues: bool = False,
              previous_undeliverable: str | None = None) -> str:
    """Notice text. The reason and context are quoted data, not instructions to run.

    One-shot timers and alarms omit the extra lines, so their text stays the
    milestone 1 notice. Repeats carry an occurrence id. That id, not the item
    id, is what a later delivery uses to tell a repeat from a retry.
    """
    zone = zone or local_zone()
    when = _utc(fire_at).astimezone(zone).isoformat(timespec="seconds")
    lines = [f"Docket {kind} fired."]
    if check:
        lines.append(f"Check: {check}")
    lines.extend([
        f"Reason: {reason}",
        f"Scheduled: {when}",
    ])
    if late:
        lines.append(
            "Late: yes, " + format_span(late_by_seconds(fire_at, now))
            + " after the scheduled time."
        )
    else:
        lines.append("Late: no")
    if coalesced:
        noun = "interval" if coalesced == 1 else "intervals"
        lines.append(
            f"Coalesced: {coalesced} earlier {noun} were not delivered separately, "
            "so this wake is not repeated for each one."
        )
    if previous_undeliverable:
        lines.append(f"Previous undeliverable: {previous_undeliverable}")
    lines.append("Urgent: yes" if urgent else "Urgent: no")
    if created_by:
        lines.append(f"Set by: {created_by} (self-reported)")
    else:
        lines.append("Set by: anonymous (not named)")
    if occurrence_id:
        lines.append(f"Occurrence: {occurrence_id}")
    lines.append(f"Item: {item_id}")
    if kind == "recurring":
        if series_continues and next_fire is not None:
            nxt = _utc(next_fire).astimezone(zone).isoformat(timespec="seconds")
            lines.append(f"Next: {nxt}")
        else:
            lines.append("Next: none (schedule complete)")
        if remaining is not None:
            lines.append(f"Remaining: {remaining}")
        lines.append(f"Series: cancel {item_id} to stop later repeats.")
    if summary:
        lines.append("Observed: " + json.dumps(summary, ensure_ascii=False, default=str))
    if context:
        lines.append("Context: " + json.dumps(context, ensure_ascii=False, default=str))
    lines.append("")
    lines.append("This is a wake for that session. Docket did not run anything.")
    return "\n".join(lines)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(timezone.utc)
