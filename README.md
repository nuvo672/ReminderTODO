# Discord Reminder Bot

Type what you need to do in a Discord channel. Claude decides the best time to remind you, around your
schedule, deadlines and when you've actually responded to pings before. The bot pings you with buttons.

## How it works

1. You type in `#todo`: `need to email my professor about the exam and pick up dry cleaning before friday`
2. Claude splits it into tasks, picks a time for each, and the bot replies with when it'll ping you and why.
3. At that time you get `@you ⏰ Email professor about the exam` with buttons:
   **Done** · **30 min** · **2 hrs** · **Better time** (Claude re-picks) · **Drop it**
4. Ignore it and it nudges every 30 min (max 2). Still nothing → Claude moves it to a better slot and
   remembers that time didn't work.
5. Every day at 9:00 AM you get the day's plan.

Never pings during quiet hours (1–9 AM) or trading (9:30–11:30 weekdays), unless you name an exact time.

**Changing things:** reply to any bot message with plain English: "make that tonight", "cancel this",
"actually it's due thursday". Or just type "push the dentist thing to tomorrow".

**Slash commands:** `/tasks` (open reminders), `/summary` (today's plan now), `/habits` (what it has learned).

## Setup

### 1. Discord bot settings
In the [Developer Portal](https://discord.com/developers/applications) → your app:
- **Bot** tab: turn on **Message Content Intent**. Copy the token (Reset Token if needed).
- **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; permissions *View Channels, Send
  Messages, Read Message History, Embed Links*. Open the URL and add the bot to your server.

### 2. Get your IDs
Discord → Settings → Advanced → turn on **Developer Mode**. Then right-click to **Copy ID** on:
- the `#todo` channel → `TODO_CHANNEL_ID`
- yourself → `OWNER_USER_ID`
- your server → `GUILD_ID` (makes slash commands show up instantly)

Make sure notifications for that channel are on (or set to "All Messages") on your phone.

### 3. Edit `config.yaml`
Add your classes, gym, work, etc. under `weekly_schedule`. `block: true` = never ping then.

### 4. Deploy on Railway
1. Push this folder to a new **private** GitHub repo.
2. Railway → **New Project → Deploy from GitHub repo** → pick it.
3. Service → **Variables**: add everything from `.env.example` (token, API key, IDs,
   `DB_PATH=/data/reminders.db`).
4. Service → right-click → **Attach Volume**, mount path `/data` (keeps your tasks across redeploys).
5. Deploy. Logs should say `Logged in as ...`. Type a task in `#todo` to test.

Cost: Railway Hobby is ~$5/mo; Claude API usage is a few cents a day (one short call per message).

### Run locally instead
```bash
pip install -r requirements.txt
cp .env.example .env   # fill it in, set DB_PATH=data/reminders.db
python bot.py
```

## Files
- `bot.py` — Discord bot, buttons, 30-second clock (pings, nudges, morning summary)
- `planner.py` — the Claude prompt and structured `apply_plan` tool
- `core.py` — applies Claude's plan to the database
- `windows.py` — quiet hours / blocked windows, enforced in code
- `learning.py` — turns Done/Snooze/ignored history into stats Claude uses
- `db.py` — SQLite storage
- `test_logic.py` — offline tests (`python test_logic.py`)
