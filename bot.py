"""Discord reminder bot: type tasks in a channel, Claude picks the time, the bot pings you."""
from __future__ import annotations

import logging
import os
import re
from datetime import timedelta

import discord
import yaml
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import learning
from core import apply_plan, mark_done, ts
from db import DB, from_iso, utcnow
from planner import Planner
from windows import Rules, parse_hhmm

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["TODO_CHANNEL_ID"])
OWNER_ID = int(os.environ["OWNER_USER_ID"])
GUILD_ID = int(os.environ["GUILD_ID"]) if os.getenv("GUILD_ID") else None
DB_PATH = os.getenv("DB_PATH", "data/reminders.db")
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

with open(os.getenv("CONFIG_PATH", "config.yaml")) as f:
    CFG = yaml.safe_load(f)

RULES = Rules(CFG)
DBX = DB(DB_PATH)
PLANNER = Planner(os.environ["ANTHROPIC_API_KEY"], MODEL, CFG, RULES.tz)
REPING = CFG.get("reping", {})
REPING_MIN = int(REPING.get("interval_minutes", 30))
MAX_NUDGES = int(REPING.get("max_nudges", 2))


def history_text() -> str:
    return learning.summarize(DBX.events(), RULES.tz)


async def run_plan(text: str, about_ids=None):
    plan = await PLANNER.plan(text, RULES.now(), DBX.open_tasks(), history_text(), about_ids)
    lines, new_ids = apply_plan(DBX, RULES, plan, source_text=text)
    return plan.get("reply", ""), lines, new_ids


PARKED = timedelta(days=365)


async def replan_task(task_id: int, prompt: str, fallback_hours: float) -> list[str]:
    """Park the task (so it can't fire mid-thought), ask Claude for a new slot, fall back if it doesn't give one."""
    DBX.update(task_id, status="scheduled", notify_at=utcnow() + PARKED)
    lines = []
    try:
        _, lines, _ = await run_plan(prompt, about_ids=[task_id])
    except Exception:
        log.exception("replan failed for #%s", task_id)
    t = DBX.get(task_id)
    if t["status"] == "scheduled" and from_iso(t["notify_at"]) - utcnow() > PARKED / 2:
        when = RULES.finalize(RULES.now() + timedelta(hours=fallback_hours), False)
        DBX.reschedule(task_id, when, "fallback")
        lines.append(f"⏰ **{t['title']}** → {ts(when)}")
    return lines


# ---------- Buttons (persist across restarts via custom_id) ----------

ACTIONS = {
    "done": ("Done", discord.ButtonStyle.success, "✅"),
    "s30": ("30 min", discord.ButtonStyle.secondary, "⏰"),
    "s120": ("2 hrs", discord.ButtonStyle.secondary, "⏰"),
    "ai": ("Better time", discord.ButtonStyle.primary, "🧠"),
    "cancel": ("Drop it", discord.ButtonStyle.danger, "🗑️"),
}


class ReminderButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rb:(?P<act>[a-z0-9]+):(?P<id>[0-9]+)"):
    def __init__(self, act: str, task_id: int):
        label, style, emoji = ACTIONS[act]
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"rb:{act}:{task_id}"))
        self.act, self.task_id = act, task_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(match["act"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("These reminders aren't yours 🙂", ephemeral=True)
        task = DBX.get(self.task_id)
        if not task or task["status"] in ("done", "cancelled"):
            await interaction.response.edit_message(view=None)
            return await interaction.followup.send("That one's already closed.", ephemeral=True)

        title = task["title"]
        ping_at = from_iso(task["first_ping_at"])

        if self.act == "done":
            mark_done(DBX, task)
            return await interaction.response.edit_message(content=f"✅ ~~{title}~~", view=None)

        if self.act == "cancel":
            DBX.update(task["id"], status="cancelled")
            DBX.log(task["id"], "cancel", ping_at)
            return await interaction.response.edit_message(content=f"🗑️ ~~{title}~~ (dropped)", view=None)

        if self.act in ("s30", "s120"):
            mins = 30 if self.act == "s30" else 120
            when = RULES.finalize(RULES.now() + timedelta(minutes=mins), user_specified=False)
            DBX.log(task["id"], "snooze", ping_at)
            DBX.reschedule(task["id"], when, f"Snoozed {mins} min")
            return await interaction.response.edit_message(content=f"⏰ **{title}** snoozed → {ts(when)}", view=None)

        if self.act == "ai":
            await interaction.response.defer()
            DBX.log(task["id"], "snooze", ping_at)
            lines = await replan_task(
                task["id"],
                f"[system] He hit 'pick a better time' on task #{task['id']} '{title}' just now. "
                "Now is clearly bad. Reschedule it to the next genuinely good slot.",
                fallback_hours=2,
            )
            return await interaction.edit_original_response(content="\n".join(lines), view=None)


def reminder_view(task_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for act in ACTIONS:
        view.add_item(ReminderButton(act, task_id))
    return view


# ---------- Bot ----------

class ReminderBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_dynamic_items(ReminderButton)
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.tick.start()

    async def channel(self) -> discord.TextChannel:
        return self.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)

    async def on_ready(self):
        log.info("Logged in as %s, model %s", self.user, MODEL)

    async def on_message(self, msg: discord.Message):
        if msg.author.bot or msg.channel.id != CHANNEL_ID or msg.author.id != OWNER_ID:
            return
        text = msg.content.strip()
        if not text:
            return

        about_ids = None
        if msg.reference and msg.reference.message_id:
            linked = DBX.tasks_for_message(msg.reference.message_id)
            about_ids = [t["id"] for t in linked] or None

        async with msg.channel.typing():
            try:
                reply, lines, new_ids = await run_plan(text, about_ids)
            except Exception as e:
                log.exception("plan failed")
                return await msg.reply(f"⚠️ Couldn't reach Claude ({type(e).__name__}). Try again in a sec.")

        body = "\n".join(filter(None, [reply, *lines]))[:1990] or "👍"
        sent = await msg.reply(body, mention_author=False)
        for tid in new_ids:
            DBX.update(tid, confirm_message_id=sent.id)

    # ---- the clock: runs every 30 seconds ----
    @tasks.loop(seconds=30)
    async def tick(self):
        try:
            await self.send_due()
            await self.send_nudges()
            await self.maybe_morning_summary()
        except Exception:
            log.exception("tick failed")

    @tick.before_loop
    async def _wait(self):
        await self.wait_until_ready()

    async def send_due(self):
        now = utcnow()
        for t in DBX.due_tasks(now):
            ch = await self.channel()
            extra = f"\n> due {t['deadline']}" if t["deadline"] else ""
            m = await ch.send(f"<@{OWNER_ID}> ⏰ **{t['title']}**{extra}", view=reminder_view(t["id"]))
            DBX.update(t["id"], status="pinged", nudges=0, first_ping_at=now, last_ping_at=now, ping_message_id=m.id)

    async def send_nudges(self):
        now = utcnow()
        for t in DBX.pinged_tasks():
            last = from_iso(t["last_ping_at"])
            if now - last < timedelta(minutes=REPING_MIN):
                continue
            if t["nudges"] < MAX_NUDGES:
                if RULES.blocked_reason(now):  # wait out trading/quiet hours before nudging
                    continue
                ch = await self.channel()
                n = t["nudges"] + 1
                m = await ch.send(
                    f"<@{OWNER_ID}> 👋 Nudge {n}/{MAX_NUDGES}: **{t['title']}**", view=reminder_view(t["id"])
                )
                DBX.update(t["id"], nudges=n, last_ping_at=now, ping_message_id=m.id)
            else:
                # Ignored every ping: log it so Claude learns, then let Claude find a better slot.
                DBX.log(t["id"], "ignored", from_iso(t["first_ping_at"]))
                lines = await replan_task(
                    t["id"],
                    f"[system] Task #{t['id']} '{t['title']}' was pinged {MAX_NUDGES + 1} times with no "
                    f"response (first ping {from_iso(t['first_ping_at']).astimezone(RULES.tz):%a %I:%M %p}). "
                    "That time didn't work for him. Reschedule it to a better slot.",
                    fallback_hours=3,
                )
                if lines:
                    await (await self.channel()).send("No answer on that one, so I moved it:\n" + "\n".join(lines))

    async def maybe_morning_summary(self):
        now = RULES.now()
        hhmm = parse_hhmm(CFG.get("morning_summary", "09:00"))
        today = now.date().isoformat()
        if now.time() < hhmm or DBX.kv_get("last_summary") == today:
            return
        DBX.kv_set("last_summary", today)
        await (await self.channel()).send(self.summary_text(now))

    def summary_text(self, now) -> str:
        today, later, waiting = [], [], []
        for t in DBX.open_tasks():
            when = from_iso(t["notify_at"]).astimezone(RULES.tz)
            line = f"• **{t['title']}** — <t:{int(when.timestamp())}:t>"
            if t["deadline"]:
                line += f" (due {t['deadline']})"
            if t["status"] == "pinged":
                waiting.append(f"• **{t['title']}** (pinged, waiting on you)")
            elif when.date() == now.date():
                today.append(line)
            else:
                later.append(f"• **{t['title']}** — <t:{int(when.timestamp())}:f>")
        parts = [f"<@{OWNER_ID}> ☀️ **{now:%A}** plan"]
        parts += ["**Today**", *today] if today else ["Nothing scheduled for today."]
        if waiting:
            parts += ["**Still open**", *waiting]
        if later:
            parts += [f"**Coming up** ({len(later)})", *later[:8]]
        return "\n".join(parts)[:1990]


bot = ReminderBot()


@bot.tree.command(name="tasks", description="Show your open reminders")
async def tasks_cmd(interaction: discord.Interaction):
    rows = DBX.open_tasks()
    if not rows:
        return await interaction.response.send_message("No open reminders 🎉", ephemeral=True)
    lines = [f"`#{t['id']}` **{t['title']}** — {ts(from_iso(t['notify_at']))}" for t in rows]
    await interaction.response.send_message("\n".join(lines)[:1990], ephemeral=True)


@bot.tree.command(name="summary", description="Post today's plan now")
async def summary_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(bot.summary_text(RULES.now()))


@bot.tree.command(name="habits", description="What the bot has learned about when you act on pings")
async def habits_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(history_text()[:1990], ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)
