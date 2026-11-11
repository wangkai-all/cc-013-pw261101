"""Main FastAPI application: scheduling console + REST API."""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import quota
from .db import DB, now_ts
from .scheduler import Scheduler
from .schemas import (
    AccountUpdate,
    Force429,
    Invalidate,
    MockConfig,
    SettingsUpdate,
    TaskCreate,
)

DB_PATH = os.environ.get(
    "SCHED_DB", str(Path(__file__).resolve().parent.parent / "data" / "state.db")
)
MOCK_BASE = os.environ.get("MOCK_BASE_URL", "http://127.0.0.1:9001")

db = DB(DB_PATH)
scheduler = Scheduler(db, MOCK_BASE)
_proxy_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _proxy_client
    _proxy_client = httpx.AsyncClient(timeout=10.0)
    scheduler.start()
    yield
    await scheduler.stop()
    if _proxy_client:
        await _proxy_client.aclose()
    await db.close()


app = FastAPI(title="Ad Data Collection Scheduler", lifespan=lifespan)


def row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()} if row is not None else None


# ---------------------------------------------------------------- Tasks
@app.post("/api/tasks")
async def create_task(req: TaskCreate):
    try:
        ids = await scheduler.create_task(
            req.account_ids, req.date_start, req.date_end,
            req.priority, req.note,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"task_ids": ids}


@app.get("/api/tasks")
async def list_tasks(account_id: str | None = None):
    sql = "SELECT * FROM tasks"
    params: list = []
    if account_id:
        sql += " WHERE account_id=?"
        params.append(account_id)
    sql += " ORDER BY id DESC"
    rows = await db.all(sql, params)
    out = []
    for r in rows:
        d = row_to_dict(r)
        counts = await db.one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done_n, "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_n, "
            "SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled_n, "
            "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running_n, "
            "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_n "
            "FROM task_items WHERE task_id=?",
            (r["id"],),
        )
        d.update(row_to_dict(counts))
        out.append(d)
    return out


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: int):
    try:
        await scheduler.cancel_task(task_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    return {"task_id": task_id, "status": "cancelled"}


@app.get("/api/tasks/{task_id}/items")
async def task_items(task_id: int):
    rows = await db.all(
        "SELECT * FROM task_items WHERE task_id=? ORDER BY date", (task_id,)
    )
    return [row_to_dict(r) for r in rows]


@app.get("/api/items/{item_id}/events")
async def item_events(item_id: int):
    rows = await db.all(
        "SELECT * FROM item_events WHERE item_id=? ORDER BY id", (item_id,)
    )
    out = []
    for r in rows:
        d = row_to_dict(r)
        if d.get("data"):
            try:
                d["data"] = json.loads(d["data"])
            except json.JSONDecodeError:
                pass
        out.append(d)
    return out


# ------------------------------------------------------------- Accounts
@app.get("/api/accounts")
async def list_accounts():
    rows = await db.all("SELECT * FROM accounts ORDER BY id")
    return [row_to_dict(r) for r in rows]


@app.patch("/api/accounts/{account_id}")
async def update_account(account_id: str, req: AccountUpdate):
    row = await db.one("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not row:
        raise HTTPException(404, "账号不存在")
    name = req.name if req.name is not None else row["name"]
    limit = req.quota_limit if req.quota_limit is not None else row["quota_limit"]
    window = (
        req.quota_window_sec if req.quota_window_sec is not None
        else row["quota_window_sec"]
    )
    await db.execute(
        "UPDATE accounts SET name=?, quota_limit=?, quota_window_sec=? "
        "WHERE id=?",
        (name, limit, window, account_id),
    )
    await scheduler.push_config_to_mock()
    return {"ok": True}


@app.post("/api/accounts/{account_id}/reauth")
async def reauth_account(account_id: str):
    """Re-authorize: complete re-auth on the platform, which issues a fresh
    token, then resume the account with it."""
    row = await db.one("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not row:
        raise HTTPException(404, "账号不存在")
    try:
        assert _proxy_client is not None
        resp = await _proxy_client.post(
            f"{MOCK_BASE}/mock/admin/reauth",
            json={"account_id": account_id},
        )
        resp.raise_for_status()
        token = resp.json()["token"]
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"平台重新授权失败: {exc}") from None
    await db.execute(
        "UPDATE accounts SET auth_valid=1, paused_until=NULL, "
        "pause_reason=NULL, token=? WHERE id=?",
        (token, account_id),
    )
    return {"ok": True, "token": token}


# ------------------------------------------------------------- Settings
@app.get("/api/settings")
async def get_settings():
    s = await db.settings()
    return {k: float(v) if "." in v else int(v) for k, v in s.items()}


@app.patch("/api/settings")
async def update_settings(req: SettingsUpdate):
    for field in ("global_quota_limit", "global_quota_window_sec",
                  "max_concurrency"):
        value = getattr(req, field)
        if value is not None:
            await db.set_setting(field, value)
    await scheduler.push_config_to_mock()
    return await get_settings()


@app.post("/api/global-pause/clear")
async def clear_global_pause():
    await db.set_setting("global_paused_until", 0)
    return {"ok": True}


# ---------------------------------------------------------------- Quota
@app.get("/api/quota")
async def get_quota():
    settings = await db.settings()
    accounts = await db.all("SELECT * FROM accounts ORDER BY id")
    snap = await quota.snapshot(db, settings, [row_to_dict(a) for a in accounts])
    snap["global_paused_until"] = float(
        settings.get("global_paused_until", 0)
    )
    snap["now"] = now_ts()
    return snap


# ------------------------------------------------------------------ Data
@app.get("/api/data")
async def list_data(account_id: str | None = None):
    sql = "SELECT id, account_id, date, item_id, collected_at FROM collected_data"
    params: list = []
    if account_id:
        sql += " WHERE account_id=?"
        params.append(account_id)
    sql += " ORDER BY account_id, date"
    rows = await db.all(sql, params)
    return [row_to_dict(r) for r in rows]


@app.get("/api/data/{data_id}")
async def get_data(data_id: int):
    row = await db.one(
        "SELECT * FROM collected_data WHERE id=?", (data_id,)
    )
    if not row:
        raise HTTPException(404, "数据不存在")
    d = row_to_dict(row)
    d["payload"] = json.loads(d["payload"])
    return d


# ----------------------------------------------------------- Request log
@app.get("/api/requests")
async def list_requests(limit: int = 100, account_id: str | None = None):
    sql = "SELECT * FROM request_logs"
    params: list = []
    if account_id:
        sql += " WHERE account_id=?"
        params.append(account_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = await db.all(sql, params)
    out = []
    for r in rows:
        d = row_to_dict(r)
        if d.get("response_body"):
            try:
                d["response_body"] = json.loads(d["response_body"])
            except json.JSONDecodeError:
                pass
        out.append(d)
    return out


# ------------------------------------------------------- Mock admin proxy
@app.get("/api/mock/state")
async def mock_state():
    return await _proxy("GET", "/admin/state")


@app.post("/api/mock/config")
async def mock_config(req: MockConfig):
    return await _proxy("POST", "/admin/config", json=req.model_dump(
        exclude_none=True
    ))


@app.post("/api/mock/force-429")
async def mock_force_429(req: Force429):
    return await _proxy("POST", "/admin/force-429",
                        json=req.model_dump(exclude_none=True))


@app.post("/api/mock/invalidate")
async def mock_invalidate(req: Invalidate):
    result = await _proxy("POST", "/admin/invalidate",
                          json=req.model_dump())
    if req.invalid:
        # mirror credential state into the scheduler DB; the gate will stop
        # sending requests before the platform is hit again.
        await db.execute(
            "UPDATE accounts SET auth_valid=0, pause_reason='auth_invalid' "
            "WHERE id=?",
            (req.account_id,),
        )
    else:
        # Re-authorization via the UI: ask the platform to re-enable the
        # credential and issue a fresh token, then resume with it.
        token = None
        try:
            assert _proxy_client is not None
            resp = await _proxy_client.post(
                f"{MOCK_BASE}/mock/admin/reauth",
                json={"account_id": req.account_id},
            )
            resp.raise_for_status()
            token = resp.json().get("token")
        except httpx.HTTPError:
            pass
        if token:
            await db.execute(
                "UPDATE accounts SET auth_valid=1, paused_until=NULL, "
                "pause_reason=NULL, token=? WHERE id=?",
                (token, req.account_id),
            )
        else:
            await db.execute(
                "UPDATE accounts SET auth_valid=1, paused_until=NULL, "
                "pause_reason=NULL WHERE id=?",
                (req.account_id,),
            )
        await scheduler.push_config_to_mock()
    return result


@app.post("/api/mock/reset")
async def mock_reset():
    result = await _proxy("POST", "/admin/reset")
    # restore canonical tokens & validity in scheduler DB as well
    accounts = await db.all("SELECT id FROM accounts")
    for a in accounts:
        suffix = a["id"].split("-")[-1]
        await db.execute(
            "UPDATE accounts SET auth_valid=1, paused_until=NULL, "
            "pause_reason=NULL, token=? WHERE id=?",
            (f"tok-{suffix}", a["id"]),
        )
    await db.set_setting("global_paused_until", 0)
    await scheduler.push_config_to_mock()
    return result


async def _proxy(method: str, path: str, json=None):
    try:
        assert _proxy_client is not None
        resp = await _proxy_client.request(
            method, f"{MOCK_BASE}/mock{path}", json=json
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"模拟接口不可用: {exc}") from None
    if resp.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse(resp.json(), status_code=resp.status_code)
    return JSONResponse({"raw": resp.text}, status_code=resp.status_code)


@app.get("/api/health")
async def health():
    return {"ok": True, "in_flight": len(scheduler._in_flight)}


# Static frontend (production build) -------------------------------------
_frontend_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _frontend_dist.exists():
    app.mount(
        "/",
        StaticFiles(directory=str(_frontend_dist), html=True),
        name="frontend",
    )
