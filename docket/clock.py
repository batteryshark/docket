"""Clocks the scheduler can be tested against."""

from datetime import datetime, timezone


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    def __init__(self, now: datetime):
        if now.tzinfo is None:
            raise ValueError("clock time must be timezone-aware")
        self._now = now.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        from datetime import timedelta
        self._now += timedelta(seconds=seconds)
