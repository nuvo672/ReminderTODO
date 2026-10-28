"""Applies Claude's plan to the database. Kept separate from Discord so it can be tested on its own."""
from __future__ import annotations

import logging

from db import DB
from planner import parse_local
from windows import Rules

log = logging.getLogger("core")


def ts(dt) -> str:
    """Discord timestamp: renders in your local time and shows 'in 2 hours'."""
    u = int(dt.timestamp())
    return f"<t:{u}:f> (<t:{u}:R>)"


def apply_plan(db: DB, rules: Rules, plan: dict, source_text: str | None = None):
    """Returns (lines describing what happened, list of new task ids)."""
    lines, new_ids = [], []

    for t in plan.get("new_tasks", []):
        try:
            when = rules.finalize(parse_local(t["notify_at"], rules.tz), t.get("user_specified_time", False))
        except Exception:
            log.exception("bad time from Claude: %s", t)
            when = rules.finalize(rules.now(), False)
        tid = db.add_task(t["title"], when, t.get("deadline"), int(t.get("urgency", 3)), t.get("reason"), source_text)
        new_ids.append(tid)
        why = f" — {t['reason']}" if t.get("reason") else ""
        lines.append(f"🗓️ **{t['title']}** → {ts(when)}{why}")

    for u in plan.get("updates", []):
        task = db.get(int(u["task_id"]))
        if not task:
            continue
        action = u["action"]
        if action == "reschedule" and u.get("notify_at"):
            when = rules.finalize(parse_local(u["notify_at"], rules.tz), u.get("user_specified_time", False))
            db.reschedule(task["id"], when, u.get("reason"))
            why = f" — {u['reason']}" if u.get("reason") else ""
            lines.append(f"🔁 **{task['title']}** moved → {ts(when)}{why}")
        elif action == "cancel":
            db.update(task["id"], status="cancelled")
            db.log(task["id"], "cancel", None)
            lines.append(f"🗑️ Cancelled **{task['title']}**")
        elif action == "done":
            mark_done(db, task)
            lines.append(f"✅ Marked done: **{task['title']}**")
        elif action == "rename" and u.get("new_title"):
            db.update(task["id"], title=u["new_title"])
            lines.append(f"✏️ Renamed **{task['title']}** → **{u['new_title']}**")

    return lines, new_ids


def mark_done(db: DB, task):
    from db import from_iso

    db.update(task["id"], status="done")
    db.log(task["id"], "done", from_iso(task["first_ping_at"]))
