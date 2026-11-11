"""Sliding-window quota accounting.

Quota is derived from ``request_logs``: a row is inserted *before* the HTTP
request is sent, so in-flight and retried requests are already counted. Every
attempt (including retries and requests that later receive 429) consumes quota.
"""
from __future__ import annotations

from .db import DB


async def global_used(db: DB, window_start: float) -> int:
    row = await db.one(
        "SELECT COUNT(*) AS n FROM request_logs WHERE started_at >= ?",
        (window_start,),
    )
    return row["n"]


async def account_used(db: DB, account_id: str, window_start: float) -> int:
    row = await db.one(
        "SELECT COUNT(*) AS n FROM request_logs "
        "WHERE account_id = ? AND started_at >= ?",
        (account_id, window_start),
    )
    return row["n"]


async def global_oldest(db: DB, window_start: float) -> float | None:
    """Timestamp of the oldest request inside the window (drives wait time)."""
    row = await db.one(
        "SELECT MIN(started_at) AS ts FROM request_logs WHERE started_at >= ?",
        (window_start,),
    )
    return row["ts"] if row and row["ts"] is not None else None


async def account_oldest(
    db: DB, account_id: str, window_start: float
) -> float | None:
    row = await db.one(
        "SELECT MIN(started_at) AS ts FROM request_logs "
        "WHERE account_id = ? AND started_at >= ?",
        (account_id, window_start),
    )
    return row["ts"] if row and row["ts"] is not None else None


async def snapshot(db: DB, settings: dict, accounts: list) -> dict:
    """Live remaining-quota snapshot for the API / UI."""
    import time

    now = time.time()
    g_window = int(settings["global_quota_window_sec"])
    g_limit = int(settings["global_quota_limit"])
    g_used = await global_used(db, now - g_window)
    out = {
        "global": {
            "limit": g_limit,
            "window_sec": g_window,
            "used": g_used,
            "remaining": max(0, g_limit - g_used),
        },
        "accounts": {},
    }
    for a in accounts:
        start = now - a["quota_window_sec"]
        used = await account_used(db, a["id"], start)
        out["accounts"][a["id"]] = {
            "limit": a["quota_limit"],
            "window_sec": a["quota_window_sec"],
            "used": used,
            "remaining": max(0, a["quota_limit"] - used),
        }
    return out
