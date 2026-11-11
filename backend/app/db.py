"""SQLite persistence layer.

A single connection guarded by a lock; all access goes through a thread so the
asyncio loop never blocks on disk I/O.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  token TEXT NOT NULL,
  quota_limit INTEGER NOT NULL,
  quota_window_sec INTEGER NOT NULL,
  auth_valid INTEGER NOT NULL DEFAULT 1,
  paused_until REAL,
  pause_reason TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL,
  date_start TEXT NOT NULL,
  date_end TEXT NOT NULL,
  priority INTEGER NOT NULL,
  status TEXT NOT NULL,
  note TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_acct ON tasks(account_id, status);

CREATE TABLE IF NOT EXISTS task_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  account_id TEXT NOT NULL,
  date TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_run_at REAL NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_pick
  ON task_items(account_id, status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_items_task ON task_items(task_id);

CREATE TABLE IF NOT EXISTS request_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER,
  task_id INTEGER,
  account_id TEXT NOT NULL,
  date TEXT,
  attempt INTEGER,
  started_at REAL NOT NULL,
  finished_at REAL,
  http_status INTEGER,
  wait_reason TEXT,
  wait_sec REAL,
  scope TEXT,
  retry_after REAL,
  remaining_global INTEGER,
  remaining_account INTEGER,
  response_body TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_req_started ON request_logs(started_at);
CREATE INDEX IF NOT EXISTS idx_req_acct_started
  ON request_logs(account_id, started_at);

CREATE TABLE IF NOT EXISTS item_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  message TEXT,
  data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_item ON item_events(item_id, id);

CREATE TABLE IF NOT EXISTS collected_data (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL,
  date TEXT NOT NULL,
  item_id INTEGER,
  collected_at REAL NOT NULL,
  payload TEXT NOT NULL,
  UNIQUE(account_id, date)
);
"""

DEFAULT_SETTINGS = {
    "global_quota_limit": "20",
    "global_quota_window_sec": "10",
    "max_concurrency": "3",
    # epoch seconds until which the whole platform is paused (global 429)
    "global_paused_until": "0",
}

DEFAULT_ACCOUNTS = [
    ("acct-a", "广告账号 A", "tok-a", 8, 10),
    ("acct-b", "广告账号 B", "tok-b", 6, 10),
    ("acct-c", "广告账号 C", "tok-c", 6, 10),
]


class DB:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        self._loop = asyncio.get_event_loop()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            for key, value in DEFAULT_SETTINGS.items():
                self._conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                    (key, value),
                )
            for acct_id, name, token, limit, window in DEFAULT_ACCOUNTS:
                self._conn.execute(
                    "INSERT OR IGNORE INTO accounts"
                    "(id, name, token, quota_limit, quota_window_sec)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (acct_id, name, token, limit, window),
                )
            self._conn.commit()

    async def run(self, fn):
        """Run a synchronous DB function on a worker thread."""
        return await asyncio.to_thread(self._run_locked, fn)

    def _run_locked(self, fn):
        with self._lock:
            result = fn(self._conn)
            self._conn.commit()
            return result

    # Convenience helpers -------------------------------------------------
    async def all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return await self.run(lambda c: c.execute(sql, tuple(params)).fetchall())

    async def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return await self.run(lambda c: c.execute(sql, tuple(params)).fetchone())

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        def _do(c):
            cur = c.execute(sql, tuple(params))
            return cur.lastrowid
        return await self.run(_do)

    async def settings(self) -> dict[str, str]:
        rows = await self.all("SELECT key, value FROM settings")
        return {r["key"]: r["value"] for r in rows}

    async def set_setting(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    async def close(self) -> None:
        def _do(c):
            c.close()
        await asyncio.to_thread(_do, self._conn)


def now_ts() -> float:
    return time.time()
