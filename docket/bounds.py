"""Explicit bounds for recurring items and watches.

Numbers are the product limits, not hints. Create rejects anything outside
them. A recurring item must name a count or an until. A watch must name an
until. Neither accepts a command, a script, or a principal notification.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from docket.parse import DURATION_RE, local_zone, parse_delay, validate_session_id
from docket.platform import fully_qualified_path

MIN_RECURRING_INTERVAL = 60
MAX_RECURRING_INTERVAL = 90 * 24 * 3600
MAX_RECURRING_COUNT = 100
MAX_RECURRING_HORIZON = 90 * 24 * 3600

MIN_POLL_SECONDS = 5
MIN_URL_POLL_SECONDS = 30
DEFAULT_POLL_SECONDS = 15
DEFAULT_URL_POLL_SECONDS = 60
MAX_POLL_SECONDS = 24 * 3600

MIN_STABLE_FOR = 10
DEFAULT_STABLE_FOR = 60
MAX_STABLE_FOR = 6 * 3600

MAX_WATCH_HORIZON = 7 * 24 * 3600
MAX_URL_HORIZON = 24 * 3600
MAX_WATCH_CHECKS = 130_000

MAX_PENDING_PER_TARGET = 20
MAX_RECURRING_PER_TARGET = 5
MAX_WATCHES_PER_TARGET = 8
MAX_RECURRING_PER_ACTOR = 5
MAX_WATCHES_PER_ACTOR = 8
MAX_PENDING_GLOBAL = 200
MAX_CREATED_BY = 200

# Omitted and blank setter labels share one bucket. A display name is
# self-reported; it is not an authenticated identity. Per-target and global
# caps do not depend on that label.
ANONYMOUS_ACTOR = "anonymous"

MAX_PATH = 1024
MAX_URL = 2000

CHECKS = (
    "file_appears",
    "file_stable",
    "process_ended",
    "url_answers",
    "session_idle",
)
URL_EXPECTS = ("2xx", "any")

# Top-level create fields. Context may still carry a "command" key as data;
# Docket never executes it.
FORBIDDEN_FIELDS = (
    "command",
    "script",
    "shell",
    "argv",
    "exec",
    "program",
    "notify",
    "notification",
    "phone",
)

ACTIVE = ("pending", "firing", "admitted", "ambiguous")


class LimitError(ValueError):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def public_limits() -> dict:
    return {
        "min_recurring_interval_seconds": MIN_RECURRING_INTERVAL,
        "max_recurring_interval_seconds": MAX_RECURRING_INTERVAL,
        "max_recurring_count": MAX_RECURRING_COUNT,
        "max_recurring_horizon_seconds": MAX_RECURRING_HORIZON,
        "min_poll_seconds": MIN_POLL_SECONDS,
        "min_url_poll_seconds": MIN_URL_POLL_SECONDS,
        "default_poll_seconds": DEFAULT_POLL_SECONDS,
        "default_url_poll_seconds": DEFAULT_URL_POLL_SECONDS,
        "min_stable_for_seconds": MIN_STABLE_FOR,
        "default_stable_for_seconds": DEFAULT_STABLE_FOR,
        "max_watch_horizon_seconds": MAX_WATCH_HORIZON,
        "max_url_horizon_seconds": MAX_URL_HORIZON,
        "max_watch_checks": MAX_WATCH_CHECKS,
        "max_pending_per_target": MAX_PENDING_PER_TARGET,
        "max_recurring_per_target": MAX_RECURRING_PER_TARGET,
        "max_watches_per_target": MAX_WATCHES_PER_TARGET,
        "max_recurring_per_actor": MAX_RECURRING_PER_ACTOR,
        "max_watches_per_actor": MAX_WATCHES_PER_ACTOR,
        "max_pending_global": MAX_PENDING_GLOBAL,
        "anonymous_actor": ANONYMOUS_ACTOR,
        "created_by": "self-reported",
        "checks": list(CHECKS),
        "url_expects": list(URL_EXPECTS),
    }


def reject_forbidden(body: dict) -> None:
    found = [key for key in FORBIDDEN_FIELDS if key in body]
    if found:
        raise ValueError(
            "docket does not run commands or notify the principal ("
            + ", ".join(found) + ")"
        )


def whole_seconds(value, *, what: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{what} is required")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    elif isinstance(value, str):
        seconds = _duration_seconds(value, what=what)
    else:
        raise ValueError(f"{what} must be seconds or a duration like 15m")
    if seconds != int(seconds):
        raise ValueError(f"{what} must be a whole number of seconds")
    return int(seconds)


def parse_deadline(value, *, now: datetime, what: str = "until") -> datetime:
    """A duration from now, or an ISO-8601 timestamp. Not a bare clock time."""
    if isinstance(value, str) and _looks_like_timestamp(value):
        return _parse_timestamp(value, what=what)
    if isinstance(value, str) and _looks_like_clock(value):
        raise ValueError(f"{what} must be a duration or an ISO-8601 timestamp, not a clock time")
    try:
        return parse_delay(value, now=now)
    except ValueError:
        if isinstance(value, str):
            return _parse_timestamp(value, what=what)
        raise ValueError(f"{what} must be a duration or an ISO-8601 timestamp") from None


def recurring_spec(body: dict, *, now: datetime) -> tuple[datetime, dict]:
    every = whole_seconds(body.get("every"), what="every")
    if every < MIN_RECURRING_INTERVAL:
        raise ValueError(f"every must be at least {MIN_RECURRING_INTERVAL} seconds")
    if every > MAX_RECURRING_INTERVAL:
        raise ValueError("every is too long")
    count = body.get("count")
    until_raw = body.get("until")
    if count is None and not until_raw:
        raise ValueError("recurring items need a count or an until")
    parsed_count = None
    if count is not None:
        if isinstance(count, bool) or not isinstance(count, int):
            raise ValueError("count must be an integer")
        if count < 1 or count > MAX_RECURRING_COUNT:
            raise ValueError(f"count must be from 1 to {MAX_RECURRING_COUNT}")
        parsed_count = count
    until = None
    if until_raw:
        until = parse_deadline(until_raw, now=now, what="until")
        if until > _utc(now) + timedelta(seconds=MAX_RECURRING_HORIZON):
            raise ValueError("until is too far ahead")
    if body.get("start") not in (None, ""):
        fire_at = parse_deadline(body.get("start"), now=now, what="start")
    else:
        fire_at = _utc(now) + timedelta(seconds=every)
    if fire_at > _utc(now) + timedelta(seconds=MAX_RECURRING_HORIZON):
        raise ValueError("start is too far ahead")
    if until is not None and fire_at > until:
        raise ValueError("start is after until, so the schedule would never fire")
    spec = {
        "every": every,
        "anchor": _iso(fire_at),
        "next_seq": 1,
        "open_occurrence_id": None,
    }
    if parsed_count is not None:
        spec["count"] = parsed_count
    if until is not None:
        spec["until"] = _iso(until)
    return fire_at, spec


def watch_spec(body: dict, *, now: datetime, capture_process) -> tuple[datetime, dict]:
    check = body.get("check")
    if check not in CHECKS:
        raise ValueError("check must be one of " + ", ".join(CHECKS))
    if not body.get("until"):
        raise ValueError("watches need an until")
    until = parse_deadline(body.get("until"), now=now, what="until")
    if until <= _utc(now):
        raise ValueError("until must be in the future")
    horizon = MAX_URL_HORIZON if check == "url_answers" else MAX_WATCH_HORIZON
    if until > _utc(now) + timedelta(seconds=horizon):
        raise ValueError(f"until is too far ahead for {check}")
    default_poll = DEFAULT_URL_POLL_SECONDS if check == "url_answers" else DEFAULT_POLL_SECONDS
    min_poll = MIN_URL_POLL_SECONDS if check == "url_answers" else MIN_POLL_SECONDS
    every = default_poll if body.get("every") in (None, "") else whole_seconds(body.get("every"), what="every")
    if every < min_poll:
        raise ValueError(f"every must be at least {min_poll} seconds for {check}")
    if every > MAX_POLL_SECONDS:
        raise ValueError("every is too long")
    spec: dict = {
        "check": check,
        "every": every,
        "until": _iso(until),
        "checks": 0,
        "latched": False,
        "open_occurrence_id": None,
        "next_seq": 1,
    }
    if check in ("file_appears", "file_stable"):
        spec["path"] = _absolute_path(body.get("path"))
    if check == "file_stable":
        stable = DEFAULT_STABLE_FOR if body.get("stable_for") in (None, "") else whole_seconds(
            body.get("stable_for"), what="stable_for",
        )
        if stable < MIN_STABLE_FOR or stable > MAX_STABLE_FOR:
            raise ValueError(
                f"stable_for must be from {MIN_STABLE_FOR} to {MAX_STABLE_FOR} seconds"
            )
        if _utc(now) + timedelta(seconds=stable) > until:
            raise ValueError("stable_for is longer than the watch window, so it cannot be met")
        first = _utc(now) + timedelta(seconds=every)
        if first > until:
            first = until
        if earliest_stable_met(first, every, stable) > until:
            raise ValueError(
                "file_stable cannot be observed before until; "
                "the first sample plus the stable window does not fit"
            )
        spec["stable_for"] = stable
    if check == "process_ended":
        pid = _pid(body.get("pid"))
        start = capture_process(pid)
        spec["pid"] = pid
        spec["process_start"] = start
    if check == "url_answers":
        spec["url"] = _http_url(body.get("url"))
        expect = body.get("expect") or "2xx"
        if expect not in URL_EXPECTS:
            raise ValueError("expect must be 2xx or any")
        spec["expect"] = expect
    if check == "session_idle":
        watched = body.get("watch_session") or body.get("session_id")
        bad = validate_session_id(watched or "")
        if bad:
            raise ValueError("watch_session " + bad)
        spec["watch_session"] = watched
    fire_at = _utc(now) + timedelta(seconds=every)
    if fire_at > until:
        fire_at = until
    if fire_at <= _utc(now):
        raise ValueError("until does not leave time for a check")
    return fire_at, spec


def check_limits(active, *, kind: str, session_id: str, created_by: str | None) -> None:
    rows = [item for item in active if item.status in ACTIVE or getattr(item, "capacity_reserved", False)]
    if len(rows) >= MAX_PENDING_GLOBAL:
        raise LimitError(f"docket already has {MAX_PENDING_GLOBAL} pending items")
    target_rows = [item for item in rows if (item.target or {}).get("session_id") == session_id]
    if len(target_rows) >= MAX_PENDING_PER_TARGET:
        raise LimitError(
            f"target {session_id} already has {MAX_PENDING_PER_TARGET} pending items"
        )
    if kind == "recurring":
        owned = [item for item in target_rows if item.kind == "recurring"]
        if len(owned) >= MAX_RECURRING_PER_TARGET:
            raise LimitError(
                f"target {session_id} already has {MAX_RECURRING_PER_TARGET} pending recurring items"
            )
    if kind == "watch":
        owned = [item for item in target_rows if item.kind == "watch"]
        if len(owned) >= MAX_WATCHES_PER_TARGET:
            raise LimitError(
                f"target {session_id} already has {MAX_WATCHES_PER_TARGET} pending watches"
            )
    # Always applied, including when the setter omitted a name. Distinct
    # self-reported labels are capped too, but they are not trusted as identity:
    # the target and global caps above do not use the label.
    bucket = actor_bucket(created_by)
    actor_rows = [item for item in rows if actor_bucket(item.created_by) == bucket]
    label = "anonymous setters" if bucket == ANONYMOUS_ACTOR else f"{bucket} (self-reported)"
    if kind == "recurring":
        owned = [item for item in actor_rows if item.kind == "recurring"]
        if len(owned) >= MAX_RECURRING_PER_ACTOR:
            raise LimitError(
                f"{label} already have {MAX_RECURRING_PER_ACTOR} pending recurring items"
                if bucket == ANONYMOUS_ACTOR
                else f"{label} already has {MAX_RECURRING_PER_ACTOR} pending recurring items"
            )
    if kind == "watch":
        owned = [item for item in actor_rows if item.kind == "watch"]
        if len(owned) >= MAX_WATCHES_PER_ACTOR:
            raise LimitError(
                f"{label} already have {MAX_WATCHES_PER_ACTOR} pending watches"
                if bucket == ANONYMOUS_ACTOR
                else f"{label} already has {MAX_WATCHES_PER_ACTOR} pending watches"
            )


def slot_after(anchor: datetime, every: int, now: datetime) -> datetime:
    """First slot strictly after now, on the anchor grid."""
    anchor = _utc(anchor)
    now = _utc(now)
    if now < anchor:
        return anchor
    elapsed = int((now - anchor).total_seconds())
    index = elapsed // every
    slot = anchor + timedelta(seconds=index * every)
    if slot <= now:
        return anchor + timedelta(seconds=(index + 1) * every)
    return slot


def skipped_between(every: int, delivered: datetime, nxt: datetime) -> int:
    """Slots strictly between the delivered slot and the next one."""
    span = int((_utc(nxt) - _utc(delivered)).total_seconds())
    if span <= every:
        return 0
    return max(0, span // every - 1)


def coalesced_in_window(
    every: int, delivered: datetime, now: datetime, anchor: datetime, until: datetime | None,
) -> int:
    """Slots skipped by a jump to the next future fire, clamped at until.

    A slot after the schedule expiry is not an owed wake, so it is not counted
    as coalesced and is not reported as remaining work.
    """
    if every <= 0:
        return 0
    nxt = slot_after(anchor, every, now)
    if until is not None and _utc(nxt) > _utc(until):
        span = int((_utc(until) - _utc(delivered)).total_seconds())
        if span < every:
            return 0
        return span // every
    return skipped_between(every, delivered, nxt)


def effective_remaining(spec: dict, seq: int, nxt: datetime | None, continues: bool) -> int | None:
    """Wakes still owed after this one. Zero once the series is finished."""
    count = spec.get("count")
    if count is None:
        return None
    if not continues:
        return 0
    count_left = max(0, int(count) - int(seq))
    until = spec.get("until")
    if not until or nxt is None:
        return count_left
    until_dt = _parse(until)
    if _utc(nxt) > until_dt:
        return 0
    every = int(spec.get("every") or 0)
    if every <= 0:
        return count_left
    span = int((until_dt - _utc(nxt)).total_seconds())
    return min(count_left, span // every + 1)


def earliest_stable_met(first: datetime, every: int, stable: int) -> datetime:
    """Earliest time a file_stable watch can meet, given polls on ``every``.

    The first sample only starts the window. A second sample is required, and
    it cannot happen before ``stable`` seconds have elapsed.
    """
    step = max(int(every), 1)
    steps = max(1, (int(stable) + step - 1) // step)
    return _utc(first) + timedelta(seconds=steps * step)


def actor_bucket(created_by: str | None) -> str:
    """Cap key for a setter label. Blank and omitted names share one bucket."""
    if not isinstance(created_by, str):
        return ANONYMOUS_ACTOR
    label = created_by.strip()
    if not label:
        return ANONYMOUS_ACTOR
    return label


def normalize_created_by(value) -> str | None:
    """Accept an omitted label, or a non-empty self-reported one.

    Empty text is rejected so it cannot be used as a distinct identity. The
    returned string is stored as the setter claimed it, not as a verified id.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("created_by must be a string")
    label = value.strip()
    if not label:
        raise ValueError("created_by must be a non-empty self-reported label, or omitted")
    if len(label) > MAX_CREATED_BY:
        raise ValueError("created_by is too long")
    return label


def schedule_finished(spec: dict, seq: int, nxt: datetime) -> str | None:
    count = spec.get("count")
    if count is not None and seq >= int(count):
        return "count"
    until = spec.get("until")
    if until and _utc(nxt) > _parse(until):
        return "until"
    return None


def slot_allowed(spec: dict, fire_at: datetime) -> bool:
    count = spec.get("count")
    seq = int(spec.get("next_seq") or 1)
    if count is not None and seq > int(count):
        return False
    until = spec.get("until")
    if until and _utc(fire_at) > _parse(until):
        return False
    return True


def _duration_seconds(value: str, *, what: str) -> float:
    text = value.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    match = DURATION_RE.match(text)
    if not match or not any(match.groups()):
        raise ValueError(f"{what} must be seconds or a duration like 15m")
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _looks_like_timestamp(value: str) -> bool:
    text = value.strip()
    return "T" in text or text.endswith("Z") or text.count("-") >= 2


def _looks_like_clock(value: str) -> bool:
    return bool(re.match(r"^\s*\d{1,2}:\d{2}", value))


def _parse_timestamp(value: str, *, what: str) -> datetime:
    text = value.strip()
    if _looks_like_clock(text) and "T" not in text:
        raise ValueError(f"{what} must be a duration or an ISO-8601 timestamp, not a clock time")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{what} must be a duration or an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_zone())
    return parsed.astimezone(timezone.utc)


def _absolute_path(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path is required")
    path = value.strip()
    if "\x00" in path:
        raise ValueError("path is invalid")
    if len(path) > MAX_PATH:
        raise ValueError("path is too long")
    if not fully_qualified_path(path):
        raise ValueError("path must be absolute")
    return path


def _http_url(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("url is required")
    url = value.strip()
    if len(url) > MAX_URL:
        raise ValueError("url is too long")
    if any(ch in url for ch in ("\x00", "\n", "\r")):
        raise ValueError("url is invalid")
    match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", url)
    if not match or match.group(1).lower() not in ("http", "https"):
        raise ValueError("url must be http or https")
    if "@" in _authority(url):
        raise ValueError("url must not carry a username or password")
    host = _hostname(url)
    if not host:
        raise ValueError("url must include a host")
    return url


def _authority(url: str) -> str:
    rest = url.split("://", 1)[1]
    return rest.split("/", 1)[0]


def _hostname(url: str) -> str:
    authority = _authority(url)
    if "@" in authority:
        return ""
    host = authority
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end > 1 else ""
    return host.split(":")[0]


def _pid(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("pid must be an integer greater than 1")
    if value <= 1 or value > 2**31 - 1:
        raise ValueError("pid must be an integer greater than 1")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
