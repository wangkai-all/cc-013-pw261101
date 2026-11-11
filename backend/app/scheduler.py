"""The scheduling core.

Fairness model
--------------
Every tick the scheduler walks accounts in round-robin order and dispatches at
most one item per account. Inside an account, items are ordered by task
priority (high -> low) and then submission time. Because the round-robin
cursor advances on every tick, an account that keeps adding high-priority
tasks cannot starve the other accounts: each tick still gives every account
a turn before it comes back.

Quota model
-----------
A ``request_logs`` row is inserted *before* the HTTP request leaves the
process, so in-flight requests, retries and requests that ultimately receive
429 all consume quota. Both the platform-wide quota and the per-account quota
are sliding windows computed from those timestamps.

Failure handling
----------------
* 429: pause according to the response scope (global vs account) for
  Retry-After seconds; the item goes back to the queue.
* 401: only that account's credentials are marked invalid; other accounts
  keep running. Re-authorizing clears the flag from the UI/API.
* 5xx / network error: exponential backoff, bounded retries.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from typing import Any

import httpx

from . import quota
from .db import DB

log = logging.getLogger("scheduler")

TICK_SEC = 0.25
MAX_ATTEMPTS = 8
BACKOFF_CAP = 30.0
WAIT_EVENT_INTERVAL = 5.0  # throttle repeated identical wait events

WAIT_REASONS = {
    "global_paused": "平台限流暂停中（全局 429）",
    "account_paused": "账号限流暂停中（账号 429）",
    "auth_invalid": "凭据失效，等待重新授权",
    "quota_global": "平台总额度已满，等待滑动窗口释放",
    "quota_account": "账号额度已满，等待滑动窗口释放",
    "concurrency": "并发数已达上限，等待在途请求完成",
    "backoff": "请求失败退避等待中",
}


def _date_range(start: str, end: str) -> list[str]:
    d0 = dt.date.fromisoformat(start)
    d1 = dt.date.fromisoformat(end)
    if d1 < d0:
        raise ValueError("date_end 在 date_start 之前")
    out, cur = [], d0
    while cur <= d1:
        out.append(cur.isoformat())
        cur += dt.timedelta(days=1)
    return out


class Scheduler:
    def __init__(self, db: DB, mock_base_url: str) -> None:
        self.db = db
        self.mock_base_url = mock_base_url.rstrip("/")
        self._task: asyncio.Task | None = None
        self._sync_task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self._in_flight: dict[int, asyncio.Task] = {}
        self._rr_cursor = 0
        # item_id -> (reason, last_event_ts) for throttled wait events
        self._last_wait: dict[int, tuple[str, float]] = {}
        # item_id -> ts when waiting started (for request_logs.wait_sec)
        self._waiting_since: dict[int, float] = {}

    # Lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._client = httpx.AsyncClient(timeout=10.0)
            self._task = asyncio.create_task(self._loop(), name="scheduler")
            self._sync_task = asyncio.create_task(
                self._config_sync_loop(), name="mock-config-sync"
            )
            asyncio.create_task(self._recover_stale())

    async def stop(self) -> None:
        for t in (self._task, self._sync_task):
            if t:
                t.cancel()
        await asyncio.gather(
            *[t for t in self._in_flight.values()], return_exceptions=True
        )
        if self._client:
            await self._client.aclose()
        self._task = self._sync_task = None

    async def _recover_stale(self) -> None:
        """After a restart, running items/tasks are impossible -> requeue."""
        now = time.time()

        def _do(c):
            cur = c.execute(
                "SELECT id FROM task_items WHERE status='running'"
            ).fetchall()
            c.execute(
                "UPDATE task_items SET status='pending', updated_at=? "
                "WHERE status='running'",
                (now,),
            )
            c.execute(
                "UPDATE tasks SET status='pending' WHERE status='running'"
            )
            return [r["id"] for r in cur]

        stale = await self.db.run(_do)
        for item_id in stale:
            await self._add_event(
                item_id, "status", "服务重启，未完成的日期重新入队", None
            )
        if stale:
            log.info("recovered %d stale items after restart", len(stale))

    # Main loop -----------------------------------------------------------
    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # never let the loop die
                log.exception("scheduler tick failed")
            await asyncio.sleep(TICK_SEC)

    async def _tick(self) -> None:
        settings = await self.db.settings()
        now = time.time()
        accounts = await self.db.all(
            "SELECT * FROM accounts ORDER BY id"
        )
        if not accounts:
            return

        n = len(accounts)
        order = [accounts[(self._rr_cursor + i) % n] for i in range(n)]
        self._rr_cursor = (self._rr_cursor + 1) % n

        max_conc = int(settings["max_concurrency"])
        g_limit = int(settings["global_quota_limit"])
        g_window = int(settings["global_quota_window_sec"])
        g_paused_until = float(settings.get("global_paused_until", 0))

        for acct in order:
            if len(self._in_flight) >= max_conc:
                reason = "concurrency"
                # record against whatever is waiting
                await self._note_waiting(reason)
                return

            item = await self._pick_item(acct["id"], now)
            if item is None:
                continue

            reason, wait_until = await self._gate(
                acct, item, settings, g_limit, g_window,
                g_paused_until, now,
            )
            if reason:
                self._waiting_since.setdefault(item["id"], now)
                await self._emit_wait(item, reason, wait_until, now)
                continue

            # dispatch
            wait_since = self._waiting_since.pop(item["id"], now)
            self._last_wait.pop(item["id"], None)
            log_id = await self._open_request_log(item, acct, now - wait_since)
            await self._set_item_status(item["id"], "running")
            await self._add_event(
                item["id"], "request",
                f"发起第 {item['attempts'] + 1} 次请求",
                {"log_id": log_id},
            )
            t = asyncio.create_task(
                self._run_item(item, acct, log_id, now),
                name=f"item-{item['id']}",
            )
            self._in_flight[item["id"]] = t
            t.add_done_callback(
                lambda _t, iid=item["id"]: self._in_flight.pop(iid, None)
            )

    async def _pick_item(self, account_id: str, now: float):
        return await self.db.one(
            """
            SELECT ti.*, t.priority AS task_priority, t.created_at AS task_created
            FROM task_items ti
            JOIN tasks t ON t.id = ti.task_id
            WHERE ti.account_id = ?
              AND ti.status = 'pending'
              AND ti.next_run_at <= ?
              AND t.status != 'cancelled'
            ORDER BY t.priority ASC, t.created_at ASC, ti.id ASC
            LIMIT 1
            """,
            (account_id, now),
        )

    async def _gate(self, acct, item, settings, g_limit, g_window,
                    g_paused_until, now):
        """Return (reason, wait_until_ts); (None, 0) means dispatchable."""
        if g_paused_until > now:
            return "global_paused", g_paused_until

        if not acct["auth_valid"]:
            return "auth_invalid", 0

        if acct["paused_until"] and acct["paused_until"] > now:
            return "account_paused", acct["paused_until"]

        g_used = await quota.global_used(self.db, now - g_window)
        if g_used >= g_limit:
            oldest = await quota.global_oldest(self.db, now - g_window)
            wait_until = (oldest or now) + g_window
            return "quota_global", wait_until

        a_window = acct["quota_window_sec"]
        a_used = await quota.account_used(self.db, acct["id"], now - a_window)
        if a_used >= acct["quota_limit"]:
            oldest = await quota.account_oldest(
                self.db, acct["id"], now - a_window
            )
            wait_until = (oldest or now) + a_window
            return "quota_account", wait_until

        if item["next_run_at"] > now:
            return "backoff", item["next_run_at"]

        return None, 0

    async def _note_waiting(self, reason: str) -> None:
        """Concurrency cap hit: annotate one queued item so the UI shows it."""
        row = await self.db.one(
            "SELECT id FROM task_items WHERE status='pending' "
            "ORDER BY id LIMIT 1"
        )
        if row:
            now = time.time()
            self._waiting_since.setdefault(row["id"], now)
            await self._emit_wait(row, reason, None, now)

    async def _emit_wait(self, item, reason: str, wait_until, now: float) -> None:
        last = self._last_wait.get(item["id"])
        if last and last[0] == reason and now - last[1] < WAIT_EVENT_INTERVAL:
            return
        self._last_wait[item["id"]] = (reason, now)
        data: dict[str, Any] = {"reason": reason}
        if wait_until:
            data["wait_until"] = round(wait_until, 3)
            data["wait_remaining_sec"] = round(max(0.0, wait_until - now), 2)
        await self._add_event(
            item["id"], "wait",
            WAIT_REASONS.get(reason, reason), data,
        )

    # Request execution ---------------------------------------------------
    async def _open_request_log(self, item, acct, wait_sec: float) -> int:
        return await self.db.execute(
            """
            INSERT INTO request_logs
              (item_id, task_id, account_id, date, attempt,
               started_at, wait_reason, wait_sec)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"], item["task_id"], acct["id"], item["date"],
                item["attempts"] + 1, time.time(),
                self._last_wait.get(item["id"], ("", 0))[0],
                round(max(0.0, wait_sec), 3),
            ),
        )

    async def _run_item(self, item, acct, log_id: int, started: float) -> None:
        body = {
            "account_id": acct["id"],
            "token": acct["token"],
            "date": item["date"],
            "request_id": f"req-{log_id}-{int(started * 1000)}",
        }
        try:
            assert self._client is not None
            resp = await self._client.post(
                f"{self.mock_base_url}/mock/report", json=body
            )
            status = resp.status_code
            try:
                payload = resp.json()
            except Exception:
                payload = {"raw": resp.text}
        except httpx.HTTPError as exc:
            await self._finish_log(log_id, None, error=str(exc))
            await self._handle_transport_error(item, str(exc))
            return

        await self._finish_log(log_id, status, payload)

        if status == 200:
            await self._handle_success(item, acct, payload)
        elif status == 429:
            await self._handle_429(item, acct, payload)
        elif status == 401:
            await self._handle_401(item, acct, payload)
        elif 500 <= status < 600:
            await self._handle_transport_error(
                item, f"平台错误 {status}: {payload.get('detail', '')}"
            )
        else:
            # Unexpected status: retry with backoff rather than burning the
            # whole item on one response.
            await self._handle_transport_error(
                item, f"意外的状态码 {status}: {payload.get('detail', '')}"
            )
        await self._recompute_task(item["task_id"])

    async def _finish_log(self, log_id: int, status, payload=None,
                          error: str | None = None) -> None:
        now = time.time()
        if payload is None:
            payload = {}
        await self.db.execute(
            """
            UPDATE request_logs SET
              finished_at=?, http_status=?, scope=?, retry_after=?,
              remaining_global=?, remaining_account=?, response_body=?, error=?
            WHERE id=?
            """,
            (
                now, status, payload.get("scope"), payload.get("retry_after"),
                payload.get("remaining_global"),
                payload.get("remaining_account"),
                json.dumps(payload, ensure_ascii=False)[:4000],
                error, log_id,
            ),
        )

    async def _handle_success(self, item, acct, payload) -> None:
        now = time.time()
        await self.db.execute(
            """
            INSERT INTO collected_data
              (account_id, date, item_id, collected_at, payload)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(account_id, date) DO UPDATE SET
              item_id=excluded.item_id,
              collected_at=excluded.collected_at,
              payload=excluded.payload
            """,
            (
                acct["id"], item["date"], item["id"], now,
                json.dumps(payload.get("data", payload), ensure_ascii=False),
            ),
        )
        await self.db.execute(
            "UPDATE task_items SET status='done', attempts=attempts+1, "
            "updated_at=?, last_error=NULL WHERE id=?",
            (now, item["id"]),
        )
        await self._add_event(
            item["id"], "status", "采集完成，数据已保存",
            {"remaining_global": payload.get("remaining_global"),
             "remaining_account": payload.get("remaining_account")},
        )

    async def _handle_429(self, item, acct, payload) -> None:
        now = time.time()
        retry_after = float(payload.get("retry_after", 5))
        scope = payload.get("scope", "account")
        if scope == "global":
            await self.db.set_setting(
                "global_paused_until", now + retry_after
            )
            msg = f"收到全局 429，整个平台暂停 {retry_after:.0f}s"
        else:
            await self.db.execute(
                "UPDATE accounts SET paused_until=?, pause_reason='rate_limited' "
                "WHERE id=?",
                (now + retry_after, acct["id"]),
            )
            msg = f"收到账号 429，{acct['id']} 暂停 {retry_after:.0f}s"
        await self._requeue(item, now + retry_after, now, msg,
                            {"scope": scope, "retry_after": retry_after})

    async def _handle_401(self, item, acct, payload) -> None:
        now = time.time()
        await self.db.execute(
            "UPDATE accounts SET auth_valid=0, pause_reason='auth_invalid' "
            "WHERE id=?",
            (acct["id"],),
        )
        # Keep the item queued with a gentle backoff; re-authorization clears
        # auth_valid and the item resumes without a restart.
        await self._requeue(item, now + 3.0, now,
                            "凭据失效（401），账号已暂停，等待重新授权",
                            None)

    async def _handle_transport_error(self, item, error: str) -> None:
        now = time.time()
        attempts = item["attempts"] + 1
        if attempts >= MAX_ATTEMPTS:
            await self._handle_permanent_error(item, error)
            return
        backoff = min(BACKOFF_CAP, 2 ** attempts)
        await self._add_event(
            item["id"], "wait", f"请求失败：{error}",
            {"backoff_sec": backoff},
        )
        await self._requeue(item, now + backoff, now,
                            f"传输错误，{backoff:.0f}s 后重试", None,
                            error=error)

    async def _handle_permanent_error(self, item, error: str) -> None:
        now = time.time()
        await self.db.execute(
            "UPDATE task_items SET status='failed', attempts=attempts+1, "
            "last_error=?, updated_at=? WHERE id=?",
            (error[:500], now, item["id"]),
        )
        await self._add_event(item["id"], "status",
                              f"任务失败：{error}", None)

    async def _requeue(self, item, next_run_at: float, now: float,
                       event_msg: str, event_data, error: str | None = None):
        # A task cancelled while this request was in flight never requeues:
        # stop sending new requests, keep whatever data already arrived.
        task = await self.db.one(
            "SELECT status FROM tasks WHERE id=?", (item["task_id"],)
        )
        if task and task["status"] == "cancelled":
            await self.db.execute(
                "UPDATE task_items SET status='cancelled', "
                "attempts=attempts+1, updated_at=? WHERE id=?",
                (now, item["id"]),
            )
            await self._add_event(
                item["id"], "status", "任务已取消，不再重试该日期", None
            )
            return
        await self.db.execute(
            "UPDATE task_items SET status='pending', attempts=attempts+1, "
            "next_run_at=?, last_error=?, updated_at=? WHERE id=?",
            (next_run_at, error, now, item["id"]),
        )
        await self._add_event(item["id"], "status", event_msg, event_data)

    async def _set_item_status(self, item_id: int, status: str) -> None:
        await self.db.execute(
            "UPDATE task_items SET status=?, updated_at=? WHERE id=?",
            (status, time.time(), item_id),
        )

    async def _add_event(self, item_id: int, kind: str, message: str,
                         data: dict | None) -> None:
        await self.db.execute(
            "INSERT INTO item_events(item_id, ts, kind, message, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (item_id, time.time(), kind, message,
             json.dumps(data, ensure_ascii=False) if data else None),
        )

    async def _recompute_task(self, task_id: int) -> None:
        row = await self.db.one(
            "SELECT status, COUNT(*) AS total, "
            "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done_n, "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_n, "
            "SUM(CASE WHEN status IN ('pending','running') THEN 1 ELSE 0 END) "
            "AS active_n FROM task_items WHERE task_id=?",
            (task_id,),
        )
        if not row:
            return
        if row["status"] == "cancelled":
            new_status = "cancelled"
        elif row["active_n"] and row["active_n"] > 0:
            new_status = "running"
        elif (row["done_n"] or 0) + (row["failed_n"] or 0) == row["total"]:
            new_status = "done" if not row["failed_n"] else "done_with_errors"
        else:
            new_status = "pending"
        await self.db.execute(
            "UPDATE tasks SET status=? WHERE id=?", (new_status, task_id)
        )

    # Task creation / cancellation ---------------------------------------
    async def create_task(self, account_ids: list[str], date_start: str,
                          date_end: str, priority: str, note: str | None):
        from .schemas import PRIORITIES

        if priority not in PRIORITIES:
            raise ValueError(f"未知优先级 {priority}")
        dates = _date_range(date_start, date_end)
        now = time.time()
        task_ids = []

        for acct_id in account_ids:
            acct = await self.db.one(
                "SELECT id FROM accounts WHERE id=?", (acct_id,)
            )
            if not acct:
                raise ValueError(f"账号不存在: {acct_id}")

            task_id = await self.db.execute(
                "INSERT INTO tasks(account_id, date_start, date_end, "
                "priority, status, note, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (acct_id, date_start, date_end, PRIORITIES[priority],
                 note, now),
            )
            task_ids.append(task_id)

            # skip dates already collected for this account (restart safety)
            placeholders = ",".join("?" for _ in dates)
            done_rows = await self.db.all(
                f"SELECT date FROM collected_data WHERE account_id=? "
                f"AND date IN ({placeholders})",
                (acct_id, *dates),
            )
            done_dates = {r["date"] for r in done_rows}
            for d in dates:
                if d in done_dates:
                    continue
                await self.db.execute(
                    "INSERT INTO task_items(task_id, account_id, date, "
                    "status, next_run_at, updated_at) "
                    "VALUES (?, ?, ?, 'pending', 0, ?)",
                    (task_id, acct_id, d, now),
                )
            await self._recompute_task(task_id)
        return task_ids

    async def cancel_task(self, task_id: int) -> None:
        now = time.time()
        row = await self.db.one("SELECT id FROM tasks WHERE id=?", (task_id,))
        if not row:
            raise ValueError("任务不存在")
        await self.db.execute(
            "UPDATE tasks SET status='cancelled' WHERE id=?", (task_id,)
        )
        await self.db.execute(
            "UPDATE task_items SET status='cancelled', updated_at=? "
            "WHERE task_id=? AND status='pending'",
            (now, task_id),
        )
        # running items finish their current in-flight request, then the
        # scheduler will not requeue them because the task is cancelled;
        # handle that by checking task status in _run_item via recompute.

    # Mock platform config sync ------------------------------------------
    async def _config_sync_loop(self) -> None:
        while True:
            try:
                await self.push_config_to_mock()
            except Exception:
                pass  # mock may still be starting; retry next round
            await asyncio.sleep(5)

    async def push_config_to_mock(self) -> bool:
        settings = await self.db.settings()
        accounts = await self.db.all("SELECT * FROM accounts")
        assert self._client is not None
        try:
            await self._client.post(
                f"{self.mock_base_url}/mock/admin/config",
                json={
                    "global_limit": int(settings["global_quota_limit"]),
                    "global_window_sec": int(
                        settings["global_quota_window_sec"]
                    ),
                    "account_window_sec": accounts[0]["quota_window_sec"]
                    if accounts else 10,
                },
            )
            await self._client.post(
                f"{self.mock_base_url}/mock/admin/accounts",
                json={
                    "accounts": [
                        {
                            "id": a["id"],
                            "token": a["token"],
                            "limit": a["quota_limit"],
                            "window_sec": a["quota_window_sec"],
                            "auth_valid": bool(a["auth_valid"]),
                        }
                        for a in accounts
                    ]
                },
            )
            return True
        except httpx.HTTPError:
            return False
