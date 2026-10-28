"""The Claude part: reads what you said plus your schedule/history and decides when to ping you."""
from __future__ import annotations

import json
import logging
from datetime import datetime

from anthropic import AsyncAnthropic

from db import from_iso

log = logging.getLogger("planner")

TOOL = {
    "name": "apply_plan",
    "description": "Create new reminders and/or change existing ones, plus a short reply to the user.",
    "input_schema": {
        "type": "object",
        "properties": {
            "new_tasks": {
                "type": "array",
                "description": "One entry per distinct thing to do. Empty if the message has no new tasks.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short imperative, e.g. 'Call the dentist'"},
                        "notify_at": {"type": "string", "description": "Local time, format YYYY-MM-DDTHH:MM"},
                        "user_specified_time": {
                            "type": "boolean",
                            "description": "True only if the user named an exact clock time for the reminder",
                        },
                        "deadline": {"type": ["string", "null"], "description": "Deadline if mentioned, e.g. 'Fri 5 PM'"},
                        "urgency": {"type": "integer", "minimum": 1, "maximum": 5},
                        "reason": {"type": "string", "description": "One short sentence on why this time"},
                    },
                    "required": ["title", "notify_at", "user_specified_time", "urgency", "reason"],
                },
            },
            "updates": {
                "type": "array",
                "description": "Changes to existing tasks (by id). Empty if none.",
                "items": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "integer"},
                        "action": {"type": "string", "enum": ["reschedule", "cancel", "done", "rename"]},
                        "notify_at": {"type": "string", "description": "For reschedule: YYYY-MM-DDTHH:MM local"},
                        "user_specified_time": {"type": "boolean"},
                        "new_title": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["task_id", "action"],
                },
            },
            "reply": {
                "type": "string",
                "description": "1-2 casual sentences back to the user. Don't list the times; the bot adds them.",
            },
        },
        "required": ["new_tasks", "updates", "reply"],
    },
}

SYSTEM = """You are the scheduling brain of a personal reminder bot for {name}, a busy college student who \
also trades futures. He types things he needs to do into Discord; you decide the best moment to ping him so \
he actually does them.

How to pick a time:
- If he names an exact time ("at 3pm", "in 20 min"), use it and set user_specified_time=true.
- Otherwise pick the moment he is most likely free AND able to act: avoid his blocked windows, quiet hours \
and busy blocks in the weekly schedule; respect when the task can physically happen (businesses are open \
roughly 9-5 on weekdays, a gym task fits his gym time, "when I get home" means after his day ends).
- Deadlines: ping with a real buffer before the deadline (hours to a day, based on how long the task takes). \
Urgent/near-deadline things go sooner.
- Vague "sometime" tasks: put them in a slot where he's historically responsive (see history), not right now.
- Spread pings out: avoid two reminders within ~15 min of each other unless they belong together; batch \
small related errands into one slot when it makes sense (still one task each).
- Use the response history: favor dayparts with high done rates, avoid ones he snoozes or ignores.
- Never schedule in the past.

Editing: if he says to move, cancel, rename or mark something done, use `updates` with the task id. \
Messages starting with [system] come from the bot itself (snoozes, ignored reminders); handle them by \
rescheduling the referenced task to a better time (cancel only if the deadline has clearly passed).
If a message isn't a task or an edit (e.g. "thanks"), return empty arrays and a short friendly reply.
Always answer by calling apply_plan."""


def _fmt_task(t, tz) -> str:
    when = from_iso(t["notify_at"]).astimezone(tz).strftime("%a %b %d %I:%M %p")
    extra = f", deadline {t['deadline']}" if t["deadline"] else ""
    return f"#{t['id']} [{t['status']}] {t['title']} -> {when}{extra}, urgency {t['urgency']}"


def _fmt_schedule(cfg: dict) -> str:
    lines = []
    for item in cfg.get("weekly_schedule", []) or []:
        days = item.get("days", "daily")
        days = ", ".join(days) if isinstance(days, list) else days
        tag = "BLOCKED (no pings)" if item.get("block") else "busy-ish"
        note = f" - {item['note']}" if item.get("note") else ""
        lines.append(f"- {item['name']}: {days} {item['start']}-{item['end']} [{tag}]{note}")
    q = cfg.get("quiet_hours")
    if q:
        lines.append(f"- Quiet hours (no pings): every day {q['start']}-{q['end']}")
    return "\n".join(lines) or "(none provided)"


class Planner:
    def __init__(self, api_key: str, model: str, cfg: dict, tz):
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model
        self.cfg = cfg
        self.tz = tz

    async def plan(self, text: str, now: datetime, open_tasks, history: str, about_ids: list[int] | None = None):
        context = f"""Current time: {now.strftime('%A %Y-%m-%d %H:%M')} ({self.cfg.get('timezone')})

Weekly schedule:
{_fmt_schedule(self.cfg)}

Extra notes about him:
{self.cfg.get('about_me') or '(none)'}

Open reminders:
{chr(10).join(_fmt_task(t, self.tz) for t in open_tasks) or '(none)'}

Response history (when he actually acts on pings):
{history}
"""
        if about_ids:
            context += f"\nHe is replying directly to the message about task(s): {', '.join('#' + str(i) for i in about_ids)}\n"
        context += f"\nMessage:\n{text}"

        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=1500,
            system=SYSTEM.format(name=self.cfg.get("name", "the user")),
            tools=[TOOL],
            tool_choice={"type": "tool", "name": "apply_plan"},
            messages=[{"role": "user", "content": context}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "apply_plan":
                log.info("plan: %s", json.dumps(block.input))
                return block.input
        raise RuntimeError("Claude did not return a plan")


def parse_local(s: str, tz) -> datetime:
    """'2026-10-28T14:30' (or with seconds / offset) -> aware datetime in tz."""
    dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)
