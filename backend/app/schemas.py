"""Pydantic request/response models."""
from __future__ import annotations

from pydantic import BaseModel, Field

PRIORITIES = {"high": 0, "normal": 1, "low": 2}
PRIORITY_LABELS = {0: "high", 1: "normal", 2: "low"}


class TaskCreate(BaseModel):
    account_ids: list[str] = Field(min_length=1)
    date_start: str  # YYYY-MM-DD inclusive
    date_end: str
    priority: str = "normal"
    note: str | None = None


class CancelResp(BaseModel):
    task_id: int
    status: str


class SettingsUpdate(BaseModel):
    global_quota_limit: int | None = None
    global_quota_window_sec: int | None = None
    max_concurrency: int | None = None


class AccountUpdate(BaseModel):
    name: str | None = None
    quota_limit: int | None = None
    quota_window_sec: int | None = None


class MockConfig(BaseModel):
    global_limit: int | None = None
    global_window_sec: int | None = None
    default_account_limit: int | None = None
    account_window_sec: int | None = None


class Force429(BaseModel):
    scope: str  # "global" | "account"
    account_id: str | None = None
    retry_after: int = 3
    duration_sec: int = 30


class Invalidate(BaseModel):
    account_id: str
    invalid: bool = True
