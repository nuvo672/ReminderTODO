"""Turns your Done / Snooze / ignored history into a short summary Claude reads when picking times."""
from __future__ import annotations

from collections import defaultdict
from zoneinfo import ZoneInfo

from db import from_iso

DAYPARTS = [
    ("early morning 6-9", 6, 9),
    ("morning 9-12", 9, 12),
    ("early afternoon 12-15", 12, 15),
    ("late afternoon 15-18", 15, 18),
    ("evening 18-21", 18, 21),
    ("night 21-24", 21, 24),
    ("late night 0-6", 0, 6),
]


def _daypart(hour: int) -> str:
    for name, a, b in DAYPARTS:
        if a <= hour < b:
            return name
    return "late night 0-6"


def summarize(events, tz: ZoneInfo, min_samples: int = 3) -> str:
    buckets = defaultdict(lambda: {"done": 0, "snooze": 0, "ignored": 0, "mins": []})
    for e in events:
        ping = from_iso(e["ping_at"]).astimezone(tz)
        key = ("weekday" if ping.weekday() < 5 else "weekend", _daypart(ping.hour))
        b = buckets[key]
        if e["action"] in b:
            b[e["action"]] += 1
        if e["action"] == "done" and e["minutes_to_act"] is not None:
            b["mins"].append(e["minutes_to_act"])

    lines = []
    for (kind, part), b in sorted(buckets.items()):
        n = b["done"] + b["snooze"] + b["ignored"]
        if n < min_samples:
            continue
        done_rate = round(100 * b["done"] / n)
        avg = f", avg {round(sum(b['mins']) / len(b['mins']))} min to done" if b["mins"] else ""
        lines.append(
            f"- {kind} {part}: {n} pings -> {done_rate}% done, {b['snooze']} snoozed, {b['ignored']} ignored{avg}"
        )
    if not lines:
        return "Not enough history yet. Use the weekly schedule and common sense."
    return "\n".join(lines)
