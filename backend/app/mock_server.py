"""Local mock of the advertising platform API.

Runs as its own uvicorn process. State is in-memory (rate-limit windows do
not need to survive restarts for the demo). Admin endpoints allow shrinking
quotas, forcing 429s with an explicit scope/Retry-After, and invalidating
credentials.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Mock Ad Platform")
# Served under /mock via the mounted root app below.
root = FastAPI(title="Mock Ad Platform Root")
root.mount("/mock", app)
_lock = threading.Lock()


class _State:
    def __init__(self) -> None:
        self.global_limit = 20
        self.global_window = 10.0
        self.account_window = 10.0
        # account_id -> {"token", "limit", "window", "auth_valid", "hits"}
        self.accounts: dict[str, dict] = {}
        self.global_hits: Deque[float] = deque()
        # active forced-429 rules: (scope, account_id|None, until_ts, retry_after)
        self.forced: list[dict] = []

    def prune(self, now: float) -> None:
        while self.global_hits and now - self.global_hits[0] > self.global_window:
            self.global_hits.popleft()
        for a in self.accounts.values():
            hits: Deque[float] = a["hits"]
            while hits and now - hits[0] > a["window"]:
                hits.popleft()
        self.forced = [f for f in self.forced if f["until"] > now]


state = _State()


def _acct_default(acct_id: str) -> dict:
    return {
        "token": f"tok-{acct_id}",
        "limit": 6,
        "window": state.account_window,
        "auth_valid": True,
        "hits": deque(),
    }


class ReportReq(BaseModel):
    account_id: str
    token: str
    date: str
    request_id: str | None = None


class ConfigReq(BaseModel):
    global_limit: int | None = None
    global_window_sec: float | None = None
    default_account_limit: int | None = None
    account_window_sec: float | None = None


class AccountsReq(BaseModel):
    accounts: list[dict]


class Force429Req(BaseModel):
    scope: str = Field(pattern="^(global|account)$")
    account_id: str | None = None
    retry_after: int = 3
    duration_sec: int = 30


class InvalidateReq(BaseModel):
    account_id: str
    invalid: bool = True


class ReauthReq(BaseModel):
    account_id: str


def _too_many(scope: str, retry_after: float, detail: str,
              remaining_global: int, remaining_account: int) -> JSONResponse:
    # Body carries scope + retry_after so the scheduler can decide whether to
    # pause the whole platform or just this account.
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": str(int(retry_after))},
        content={
            "detail": detail,
            "scope": scope,
            "retry_after": int(retry_after),
            "remaining_global": remaining_global,
            "remaining_account": remaining_account,
        },
    )


@app.post("/report")
def report(req: ReportReq):
    now = time.time()
    with _lock:
        state.prune(now)
        acct = state.accounts.get(req.account_id)
        if acct is None:
            # auto-register unknown accounts so demos stay self-contained
            acct = _acct_default(req.account_id)
            state.accounts[req.account_id] = acct

        if not acct["auth_valid"] or req.token != acct["token"]:
            raise HTTPException(status_code=401, detail="凭据失效或 token 不匹配")

        # forced 429 rules (admin-triggered)
        for rule in state.forced:
            if rule["scope"] == "global":
                return _too_many(
                    "global", rule["retry_after"], "管理员触发的全局限流",
                    max(0, state.global_limit - len(state.global_hits)),
                    max(0, acct["limit"] - len(acct["hits"])),
                )
            if (rule["scope"] == "account"
                    and rule["account_id"] == req.account_id):
                return _too_many(
                    "account", rule["retry_after"],
                    f"管理员触发的账号限流: {req.account_id}",
                    max(0, state.global_limit - len(state.global_hits)),
                    max(0, acct["limit"] - len(acct["hits"])),
                )

        # sliding-window platform quota
        if len(state.global_hits) >= state.global_limit:
            oldest = state.global_hits[0]
            retry_after = max(1, int(oldest + state.global_window - now) + 1)
            return _too_many(
                "global", retry_after, "平台总额度已用尽",
                0, max(0, acct["limit"] - len(acct["hits"])),
            )

        # sliding-window account quota
        if len(acct["hits"]) >= acct["limit"]:
            oldest = acct["hits"][0]
            retry_after = max(1, int(oldest + acct["window"] - now) + 1)
            return _too_many(
                "account", retry_after,
                f"账号额度已用尽: {req.account_id}",
                max(0, state.global_limit - len(state.global_hits)), 0,
            )

        # request accepted -> counts against both windows
        state.global_hits.append(now)
        acct["hits"].append(now)
        remaining_global = state.global_limit - len(state.global_hits)
        remaining_account = acct["limit"] - len(acct["hits"])

        return {
            "request_id": req.request_id,
            "account_id": req.account_id,
            "date": req.date,
            "remaining_global": remaining_global,
            "remaining_account": remaining_account,
            "scope": None,
            "retry_after": None,
            "data": {
                "date": req.date,
                "impressions": 12000 + (hash(req.request_id or "") % 9000),
                "clicks": 300 + (hash(req.date) % 400),
                "spend": round(850.5 + (hash(req.account_id) % 300), 2),
            },
        }


@app.get("/admin/state")
def admin_state():
    now = time.time()
    with _lock:
        state.prune(now)
        return {
            "global": {
                "limit": state.global_limit,
                "window_sec": state.global_window,
                "used": len(state.global_hits),
            },
            "accounts": {
                aid: {
                    "limit": a["limit"],
                    "window_sec": a["window"],
                    "used": len(a["hits"]),
                    "auth_valid": a["auth_valid"],
                    "token": a["token"],
                }
                for aid, a in state.accounts.items()
            },
            "forced_429": state.forced,
        }


@app.post("/admin/config")
def admin_config(cfg: ConfigReq):
    with _lock:
        if cfg.global_limit is not None:
            state.global_limit = cfg.global_limit
        if cfg.global_window_sec is not None:
            state.global_window = cfg.global_window_sec
        if cfg.account_window_sec is not None:
            state.account_window = cfg.account_window_sec
            for a in state.accounts.values():
                a["window"] = cfg.account_window_sec
        if cfg.default_account_limit is not None:
            for a in state.accounts.values():
                a["limit"] = cfg.default_account_limit
    return {"ok": True}


@app.post("/admin/accounts")
def admin_accounts(req: AccountsReq):
    with _lock:
        for item in req.accounts:
            aid = item["id"]
            existing = state.accounts.get(aid)
            if existing:
                # The platform owns credentials: quota config sync may update
                # limits/windows but must never overwrite a rotated token or
                # an auth_valid flag changed via /admin/invalidate.
                existing["limit"] = item.get("limit", existing["limit"])
                existing["window"] = item.get(
                    "window_sec", existing["window"]
                )
            else:
                state.accounts[aid] = {
                    "token": item.get("token", f"tok-{aid}"),
                    "limit": item.get("limit", 6),
                    "window": item.get("window_sec", state.account_window),
                    "auth_valid": item.get("auth_valid", True),
                    "hits": deque(),
                }
    return {"ok": True, "n": len(state.accounts)}


@app.post("/admin/force-429")
def admin_force_429(req: Force429Req):
    if req.scope == "account" and not req.account_id:
        raise HTTPException(400, "账号级限流需要 account_id")
    now = time.time()
    with _lock:
        state.forced.append({
            "scope": req.scope,
            "account_id": req.account_id,
            "until": now + req.duration_sec,
            "retry_after": req.retry_after,
        })
    return {"ok": True, "active": state.forced}


@app.post("/admin/invalidate")
def admin_invalidate(req: InvalidateReq):
    with _lock:
        acct = state.accounts.get(req.account_id)
        if acct is None:
            acct = _acct_default(req.account_id)
            state.accounts[req.account_id] = acct
        acct["auth_valid"] = not req.invalid
        # Rotate the token to a deterministic, re-fetchable value; the
        # scheduler's reauth endpoint reads it back via /admin/state.
        if req.invalid:
            acct["token"] = f"rotated-{req.account_id}"
    return {"ok": True, "auth_valid": acct["auth_valid"],
            "token": acct["token"]}


@app.post("/admin/reauth")
def admin_reauth(req: ReauthReq):
    """Simulate the user completing re-authorization on the platform:
    credentials become valid again and a fresh token is issued."""
    with _lock:
        acct = state.accounts.get(req.account_id)
        if acct is None:
            acct = _acct_default(req.account_id)
            state.accounts[req.account_id] = acct
        acct["auth_valid"] = True
        acct["token"] = f"fresh-{req.account_id}"
    return {"ok": True, "auth_valid": True, "token": acct["token"]}


@app.post("/admin/reset")
def admin_reset():
    with _lock:
        for aid, a in state.accounts.items():
            a["hits"].clear()
            a["auth_valid"] = True
            a["token"] = f"tok-{aid.split('-')[-1]}"
        state.global_hits.clear()
        state.forced.clear()
    return {"ok": True}
