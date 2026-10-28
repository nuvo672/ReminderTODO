"""Time rules: quiet hours, blocked windows (trading, class...), and finding the next allowed slot.

These are enforced in code, so even if Claude picks a bad time, a ping never lands in a blocked window
(unless you explicitly asked for that exact time).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_hhmm(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


def _expand_days(days) -> set[int]:
    """Accepts ["mon","wed"], "weekdays", "weekends", "daily"."""
    if days in (None, "daily", "everyday", "all"):
        return set(range(7))
    if days == "weekdays":
        return {0, 1, 2, 3, 4}
    if days == "weekends":
        return {5, 6}
    if isinstance(days, str):
        days = [d.strip() for d in days.split(",")]
    return {DAY_KEYS.index(d.lower()[:3]) for d in days}


def _in_window(dt: datetime, start: time, end: time, days: set[int]) -> bool:
    t = dt.time()
    if start <= end:
        return dt.weekday() in days and start <= t < end
    # Window crosses midnight (e.g. 23:00-07:00): the late part belongs to the start day,
    # the early part belongs to the previous day.
    if t >= start:
        return dt.weekday() in days
    if t < end:
        return (dt.weekday() - 1) % 7 in days
    return False


class Rules:
    def __init__(self, cfg: dict):
        self.tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
        q = cfg.get("quiet_hours") or {}
        self.quiet = (parse_hhmm(q["start"]), parse_hhmm(q["end"])) if q else None
        self.blocks = []
        for item in cfg.get("weekly_schedule", []) or []:
            if item.get("block"):
                self.blocks.append(
                    (item["name"], parse_hhmm(item["start"]), parse_hhmm(item["end"]), _expand_days(item.get("days")))
                )

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def blocked_reason(self, dt: datetime) -> str | None:
        dt = dt.astimezone(self.tz)
        if self.quiet and _in_window(dt, self.quiet[0], self.quiet[1], set(range(7))):
            return "quiet hours"
        for name, start, end, days in self.blocks:
            if _in_window(dt, start, end, days):
                return name
        return None

    def next_allowed(self, dt: datetime, step_minutes: int = 5) -> datetime:
        dt = dt.astimezone(self.tz)
        for _ in range(int(7 * 24 * 60 / step_minutes)):
            if not self.blocked_reason(dt):
                return dt
            dt += timedelta(minutes=step_minutes)
            # snap to the step grid so we land on clean times like 11:30, not 11:33
            dt = dt.replace(second=0, microsecond=0, minute=(dt.minute // step_minutes) * step_minutes)
        return dt

    def finalize(self, dt: datetime, user_specified: bool) -> datetime:
        """Clamp to the future and, unless you asked for that exact time, out of blocked windows."""
        now = self.now()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        if dt <= now:
            dt = now + timedelta(minutes=1)
        if not user_specified:
            dt = self.next_allowed(dt)
        return dt.replace(second=0, microsecond=0)
