"""SQLite storage for tasks and the event log the bot learns from."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    notify_at        TEXT NOT NULL,          -- UTC ISO
    deadline         TEXT,                   -- free text from Claude, e.g. "Fri 5 PM"
    urgency          INTEGER DEFAULT 3,      -- 1 low .. 5 critical
    reason           TEXT,                   -- why Claude picked this time
    status           TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled | pinged | done | cancelled
    nudges           INTEGER NOT NULL DEFAULT 0,
    first_ping_at    TEXT,
    last_ping_at     TEXT,
    ping_message_id  INTEGER,
    confirm_message_id INTEGER,
    source_text      TEXT,
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id   INTEGER,
    action    TEXT NOT NULL,     -- done | snooze | ignored | cancel
    at        TEXT NOT NULL,     -- UTC ISO of the action
    ping_at   TEXT,              -- UTC ISO of the first ping it responds to
    minutes_to_act REAL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class DB:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- tasks ----
    def add_task(self, title, notify_at: datetime, deadline=None, urgency=3, reason=None, source_text=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO tasks (title, notify_at, deadline, urgency, reason, source_text, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (title, iso(notify_at), deadline, urgency, reason, source_text, iso(utcnow())),
        )
        self.conn.commit()
        return cur.lastrowid

    def get(self, task_id: int):
        return self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def update(self, task_id: int, **fields):
        if not fields:
            return
        for k, v in list(fields.items()):
            if isinstance(v, datetime):
                fields[k] = iso(v)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))
        self.conn.commit()

    def reschedule(self, task_id: int, notify_at: datetime, reason=None):
        self.update(task_id, notify_at=notify_at, status="scheduled", nudges=0,
                    first_ping_at=None, last_ping_at=None, **({"reason": reason} if reason else {}))

    def open_tasks(self):
        return self.conn.execute(
            "SELECT * FROM tasks WHERE status IN ('scheduled','pinged') ORDER BY notify_at"
        ).fetchall()

    def due_tasks(self, now: datetime):
        return self.conn.execute(
            "SELECT * FROM tasks WHERE status='scheduled' AND notify_at<=? ORDER BY notify_at", (iso(now),)
        ).fetchall()

    def pinged_tasks(self):
        return self.conn.execute("SELECT * FROM tasks WHERE status='pinged'").fetchall()

    def tasks_for_message(self, message_id: int):
        return self.conn.execute(
            "SELECT * FROM tasks WHERE confirm_message_id=? OR ping_message_id=?", (message_id, message_id)
        ).fetchall()

    # ---- events (learning) ----
    def log(self, task_id, action, ping_at: datetime | None, at: datetime | None = None):
        at = at or utcnow()
        minutes = (at - ping_at).total_seconds() / 60 if ping_at else None
        self.conn.execute(
            "INSERT INTO events (task_id, action, at, ping_at, minutes_to_act) VALUES (?,?,?,?,?)",
            (task_id, action, iso(at), iso(ping_at) if ping_at else None, minutes),
        )
        self.conn.commit()

    def events(self, limit=500):
        return self.conn.execute(
            "SELECT * FROM events WHERE ping_at IS NOT NULL ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # ---- kv ----
    def kv_get(self, k):
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None

    def kv_set(self, k, v):
        self.conn.execute("INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        self.conn.commit()
