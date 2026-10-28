"""Offline tests: time windows, plan application, learning stats, and the Claude call (mocked)."""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import yaml

import learning
from core import apply_plan, mark_done
from db import DB, utcnow
from planner import Planner
from windows import Rules

CFG = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "config.yaml")))
R = Rules(CFG)
TZ = R.tz


def local(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


def test_windows():
    # Wed Oct 28 2026
    assert R.blocked_reason(local(2026, 10, 28, 3, 0)) == "quiet hours"
    assert R.blocked_reason(local(2026, 10, 28, 10, 0)) == "Trading (morning session)"
    assert R.blocked_reason(local(2026, 10, 31, 10, 0)) is None  # Saturday, no trading block
    assert R.blocked_reason(local(2026, 10, 28, 14, 0)) is None
    assert R.next_allowed(local(2026, 10, 28, 10, 7)) == local(2026, 10, 28, 11, 30)
    assert R.next_allowed(local(2026, 10, 29, 2, 0)) == local(2026, 10, 29, 9, 0)
    # midnight-crossing window
    r2 = Rules({"timezone": "America/New_York", "quiet_hours": {"start": "23:00", "end": "07:00"}})
    assert r2.blocked_reason(local(2026, 10, 28, 23, 30)) == "quiet hours"
    assert r2.blocked_reason(local(2026, 10, 28, 6, 59)) == "quiet hours"
    assert r2.blocked_reason(local(2026, 10, 28, 7, 0)) is None
    print("windows ok")


def test_apply_and_learning():
    d = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
    future = (R.now() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)  # trading-hours if weekday
    plan = {
        "new_tasks": [
            {"title": "Email professor", "notify_at": future.strftime("%Y-%m-%dT%H:%M"),
             "user_specified_time": False, "urgency": 3, "reason": "after class"},
            {"title": "Call at exact time", "notify_at": future.strftime("%Y-%m-%dT%H:%M"),
             "user_specified_time": True, "urgency": 2, "reason": "you said so"},
            {"title": "Past time", "notify_at": "2020-01-01T10:00", "user_specified_time": False,
             "urgency": 1, "reason": "x"},
        ],
        "updates": [],
        "reply": "Got it",
    }
    lines, ids = apply_plan(d, R, plan)
    assert len(ids) == 3 and len(lines) == 3
    t1, t2, t3 = (d.get(i) for i in ids)
    from db import from_iso
    w1 = from_iso(t1["notify_at"]).astimezone(TZ)
    assert R.blocked_reason(w1) is None, "non-explicit time must be moved out of blocks"
    assert from_iso(t2["notify_at"]).astimezone(TZ) == future, "explicit time kept"
    assert from_iso(t3["notify_at"]) > utcnow() - timedelta(minutes=1), "past times clamped"

    # updates
    lines, _ = apply_plan(d, R, {"new_tasks": [], "reply": "", "updates": [
        {"task_id": ids[1], "action": "cancel"},
        {"task_id": ids[2], "action": "rename", "new_title": "Renamed"},
        {"task_id": ids[0], "action": "reschedule", "notify_at": (future + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M")},
        {"task_id": 999, "action": "cancel"},
    ]})
    assert d.get(ids[1])["status"] == "cancelled"
    assert d.get(ids[2])["title"] == "Renamed"
    assert len(lines) == 3

    # due + done + learning
    now = utcnow()
    d.update(ids[2], notify_at=now - timedelta(minutes=1))
    assert [t["id"] for t in d.due_tasks(now)] == [ids[2]]
    ping = local(2026, 10, 27, 15, 0)  # Tuesday afternoon
    for i in range(4):
        tid = d.add_task(f"t{i}", ping)
        d.update(tid, status="pinged", first_ping_at=ping)
        if i < 3:
            mark_done(d, d.get(tid))
            # fake that it was done 10 minutes after ping
            d.conn.execute("UPDATE events SET at=?, minutes_to_act=10 WHERE task_id=?", (ping.isoformat(), tid))
        else:
            d.log(tid, "ignored", ping)
    d.conn.commit()
    summary = learning.summarize(d.events(), TZ)
    assert "weekday late afternoon 15-18: 4 pings -> 75% done" in summary, summary
    print("apply/learning ok\n", summary)


def test_planner_mock():
    p = Planner("sk-test", "claude-sonnet-5-5", CFG, TZ)
    fake = SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="apply_plan",
                                                    input={"new_tasks": [], "updates": [], "reply": "hi"})])
    p.client.messages.create = AsyncMock(return_value=fake)
    out = asyncio.run(p.plan("thanks", R.now(), [], "none"))
    assert out["reply"] == "hi"
    kwargs = p.client.messages.create.call_args.kwargs
    assert kwargs["tool_choice"] == {"type": "tool", "name": "apply_plan"}
    assert "Trading (morning session)" in kwargs["messages"][0]["content"]
    print("planner ok")


if __name__ == "__main__":
    test_windows()
    test_apply_and_learning()
    test_planner_mock()
    print("ALL TESTS PASSED")
