"""SQLite persistence: users and their watch subscriptions.

State survives restarts (needed on cloud hosts that redeploy). Each subscriber
is a Telegram chat_id; each subscription is (chat_id, port, cycle_type, date).
"""

import sqlite3
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        with _LOCK:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id    INTEGER NOT NULL,
                    port_id    TEXT    NOT NULL,
                    cycle_type TEXT    NOT NULL,
                    date       TEXT    NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_count INTEGER,          -- last availableCount seen (-1 = not offered)
                    notified   INTEGER DEFAULT 0, -- 1 while user already alerted for current opening
                    UNIQUE(chat_id, port_id, cycle_type, date)
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            self.conn.commit()

    # ---- subscriptions -------------------------------------------------
    def add_subscription(self, chat_id, port_id, cycle_type, date) -> bool:
        """Returns True if newly created, False if it already existed."""
        with _LOCK:
            try:
                self.conn.execute(
                    "INSERT INTO subscriptions(chat_id, port_id, cycle_type, date, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (chat_id, port_id, cycle_type, date, int(time.time())),
                )
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def list_subscriptions(self, chat_id):
        with _LOCK:
            return self.conn.execute(
                "SELECT * FROM subscriptions WHERE chat_id=? ORDER BY date, port_id",
                (chat_id,),
            ).fetchall()

    def all_subscriptions(self):
        with _LOCK:
            return self.conn.execute("SELECT * FROM subscriptions").fetchall()

    def delete_subscription(self, chat_id, sub_id) -> bool:
        with _LOCK:
            cur = self.conn.execute(
                "DELETE FROM subscriptions WHERE id=? AND chat_id=?", (sub_id, chat_id)
            )
            self.conn.commit()
            return cur.rowcount > 0

    def clear_subscriptions(self, chat_id) -> int:
        with _LOCK:
            cur = self.conn.execute(
                "DELETE FROM subscriptions WHERE chat_id=?", (chat_id,)
            )
            self.conn.commit()
            return cur.rowcount

    def update_state(self, sub_id, last_count, notified):
        with _LOCK:
            self.conn.execute(
                "UPDATE subscriptions SET last_count=?, notified=? WHERE id=?",
                (last_count, notified, sub_id),
            )
            self.conn.commit()

    def distinct_chat_ids(self):
        with _LOCK:
            rows = self.conn.execute(
                "SELECT DISTINCT chat_id FROM subscriptions"
            ).fetchall()
            return [r["chat_id"] for r in rows]

    # ---- meta ----------------------------------------------------------
    def set_meta(self, key, value):
        with _LOCK:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            self.conn.commit()

    def get_meta(self, key, default=None):
        with _LOCK:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else default
