#!/usr/bin/env python3
"""End-to-end acceptance demo for the multi-account ad collection scheduler.

Runs its own mock platform + scheduler API on separate ports with a fresh
temporary SQLite database, then walks through every guarantee in the spec:

  1. Three accounts collecting at once; every date lands in storage.
  2. Sliding-window quotas (global + per-account) are never exceeded;
     retries/429 requests count against quota.
  3. Round-robin fairness: a normal-priority account keeps getting turns
     while another account keeps piling on high-priority work.
  4. 429 with scope=global pauses the whole platform; scope=account pauses
     only that account; Retry-After is honored.
  5. Invalid credentials pause only that account; re-authorization resumes.
  6. Cancellation stops new requests but keeps data already received.
  7. Service restart resumes pending work and never re-collects a finished
     date.

Usage:  python3 -I demo.py
Exit code is non-zero if any check fails.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
VENV_PY = BACKEND / ".venv" / "bin" / "python3"
MOCK_PORT = 9101
API_PORT = 9100
BASE = f"http://127.0.0.1:{API_PORT}"

PASS, FAIL = "✅ PASS", "❌ FAIL"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL}  {name}" + (f" — {detail}" if detail else ""))
    return ok


def api(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise AssertionError(
            f"{method} {path} -> {exc.code}: {exc.read().decode()[:300]}"
        ) from None


def wait_ready(proc: subprocess.Popen, path: str, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"process {proc.args} exited early")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}{path}", timeout=2):
                return
        except Exception:
            time.sleep(0.3)
    raise TimeoutError(f"server not ready on {path}")


def start_servers(db_path: str):
    mock = subprocess.Popen(
        [VENV_PY, "-m", "uvicorn", "app.mock_server:root",
         "--host", "127.0.0.1", "--port", str(MOCK_PORT)],
        cwd=BACKEND, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    env = {
        **os.environ,
        "SCHED_DB": db_path,
        "MOCK_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}",
    }
    api_proc = subprocess.Popen(
        [VENV_PY, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(API_PORT)],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    wait_ready(api_proc, "/api/health")
    return mock, api_proc


def stop(proc: subprocess.Popen):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def wait_until(predicate, timeout: float, interval: float = 0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def wait_tasks(note_prefix: str, timeout: float = 45.0):
    """Block until all tasks whose note starts with the prefix settle."""
    def settled():
        return all(
            t["status"] not in ("pending", "running")
            for t in api("GET", "/api/tasks")
            if t["note"] and t["note"].startswith(note_prefix)
        )
    assert wait_until(settled, timeout), f"tasks {note_prefix} did not settle"


def item_events_for(task_id: int):
    """All item-event messages for a task, across its items."""
    out = []
    for it in api("GET", f"/api/tasks/{task_id}/items"):
        out.extend(api("GET", f"/api/items/{it['id']}/events"))
    return out


def window_violations(rows, g_limit, a_limits, window=10.0):
    """Replay request timestamps through the sliding windows."""
    gw: deque[float] = deque()
    aw: dict[str, deque[float]] = defaultdict(deque)
    bad = []
    for r in sorted(rows, key=lambda x: x["started_at"]):
        t = r["started_at"]
        while gw and t - gw[0] > window:
            gw.popleft()
        gw.append(t)
        if len(gw) > g_limit:
            bad.append(("global", len(gw)))
        q = aw[r["account_id"]]
        while q and t - q[0] > window:
            q.popleft()
        q.append(t)
        if len(q) > a_limits[r["account_id"]]:
            bad.append((r["account_id"], len(q)))
    return bad


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="sched-demo-")
    db_path = os.path.join(tmp, "demo.db")
    print(f"临时数据库: {db_path}\n")

    mock, api_proc = start_servers(db_path)
    try:
        api("POST", "/api/mock/reset")
        api("PATCH", "/api/settings", {
            "global_quota_limit": 20, "max_concurrency": 3,
        })
        for acct in ("acct-a", "acct-b", "acct-c"):
            api("PATCH", f"/api/accounts/{acct}", {"quota_limit": 8})

        # ------------------------------------------------ 1. 基础三账号
        print("【1】三组账号同时采集（每账号 6 天，共 18 个日期）")
        api("POST", "/api/tasks", {
            "account_ids": ["acct-a", "acct-b", "acct-c"],
            "date_start": "2030-01-01", "date_end": "2030-01-06",
            "priority": "normal", "note": "demo-1",
        })
        ok = wait_until(
            lambda: len(api("GET", "/api/data")) >= 18, timeout=40
        )
        data = api("GET", "/api/data")
        per_acct = Counter(d["account_id"] for d in data)
        check("18 个日期全部入库", ok and len(data) >= 18, str(dict(per_acct)))

        # ------------------------------------------ 2. 滑动窗口不越限
        print("\n【2】滑动窗口额度校验（重试/429 请求同样计数）")
        rows = api("GET", "/api/requests?limit=500")
        bad = window_violations(rows, 20, {a: 8 for a in per_acct})
        check("全局/账号窗口均未越限", not bad, f"越限点: {bad}" if bad else "0 次")
        check("每条请求都记录了剩余额度",
              all(r["remaining_global"] is not None for r in rows
                  if r["http_status"] == 200))

        # 收紧额度后再次验证
        api("PATCH", "/api/settings", {"global_quota_limit": 4})
        for acct in ("acct-a", "acct-b", "acct-c"):
            api("PATCH", f"/api/accounts/{acct}", {"quota_limit": 2})
        time.sleep(6)  # 等待配置同步 + 旧窗口滑出
        t2 = api("POST", "/api/tasks", {
            "account_ids": ["acct-a", "acct-b", "acct-c"],
            "date_start": "2030-02-01", "date_end": "2030-02-05",
            "priority": "normal", "note": "demo-2",
        })["task_ids"]
        time.sleep(18)
        rows2 = [r for r in api("GET", "/api/requests?limit=500")
                 if str(r["date"]).startswith("2030-02")]
        bad2 = window_violations(rows2, 4, {a: 2 for a in per_acct})
        check("小额度（全局4/账号2）下仍不越限",
              not bad2, f"请求数={len(rows2)} 越限点={bad2}")
        # wait reasons live on the per-item event stream
        wait_msgs = [
            e["message"] for tid in t2 for e in item_events_for(tid)
            if e["kind"] == "wait"
        ]
        check("出现过额度等待事件",
              any("额度" in m for m in wait_msgs),
              f"等待事件 {len(wait_msgs)} 条")
        wait_tasks("demo-2")

        # --------------------------------------------- 3. 轮询防饥饿
        # Fairness needs headroom: generous quotas so nothing blocks on
        # rate limits, concurrency=1 so only one request is in flight.
        print("\n【3】防饥饿：A 持续高优先级，B 普通任务仍有执行机会")
        wait_tasks("demo-2")
        api("PATCH", "/api/settings", {
            "global_quota_limit": 50, "max_concurrency": 1,
        })
        for acct in ("acct-a", "acct-b", "acct-c"):
            api("PATCH", f"/api/accounts/{acct}", {"quota_limit": 30})
        api("POST", "/api/mock/config", {"global_limit": 50})
        time.sleep(11)  # let any previous sliding window fully slide out
        api("POST", "/api/tasks", {
            "account_ids": ["acct-a"],
            "date_start": "2030-03-01", "date_end": "2030-03-30",
            "priority": "high", "note": "demo-3-a",
        })
        time.sleep(1)  # small head start; A's 30 items cannot finish in 1s
        api("POST", "/api/tasks", {
            "account_ids": ["acct-b"],
            "date_start": "2030-03-01", "date_end": "2030-03-10",
            "priority": "normal", "note": "demo-3-b",
        })
        time.sleep(12)
        rows3 = sorted(
            (r for r in api("GET", "/api/requests?limit=500")
             if str(r["date"]).startswith("2030-03")),
            key=lambda r: r["id"],
        )
        b_rows = [r for r in rows3 if r["account_id"] == "acct-b"]
        a_rows = [r for r in rows3 if r["account_id"] == "acct-a"]
        check("B（普通）拿到了执行机会", len(b_rows) >= 1,
              f"A 请求 {len(a_rows)} 次，B 请求 {len(b_rows)} 次")
        first_b_idx = next(
            (i for i, r in enumerate(rows3) if r["account_id"] == "acct-b"), None
        )
        interleaved = first_b_idx is not None and any(
            r["account_id"] == "acct-a" for r in rows3[first_b_idx + 1:]
        )
        check("B 执行后调度又回到 A（交替而非独占）", interleaved,
              "顺序: " + " ".join(r["account_id"][-1].upper() for r in rows3))
        wait_tasks("demo-3")

        # --------------------------------------------- 4. 429 作用域
        # Fresh mock state so forced-429 is the ONLY thing throttling us.
        print("\n【4】429 作用域与 Retry-After")
        api("PATCH", "/api/settings", {"max_concurrency": 3})
        api("POST", "/api/mock/reset")
        api("PATCH", "/api/settings", {"global_quota_limit": 50})
        for acct in ("acct-a", "acct-b", "acct-c"):
            api("PATCH", f"/api/accounts/{acct}", {"quota_limit": 30})
        time.sleep(6)
        api("POST", "/api/mock/force-429", {
            "scope": "global", "retry_after": 3, "duration_sec": 20,
        })
        api("POST", "/api/tasks", {
            "account_ids": ["acct-a", "acct-b"],
            "date_start": "2030-04-01", "date_end": "2030-04-03",
            "priority": "normal", "note": "demo-4g",
        })
        time.sleep(7)
        g429 = [r for r in api("GET", "/api/requests?limit=500")
                if str(r["date"]).startswith("2030-04")]
        check("全局 429：两个账号都被限流",
              {r["scope"] for r in g429 if r["http_status"] == 429} == {"global"}
              and len({r["account_id"] for r in g429}) == 2,
              f"429 账号: {sorted({r['account_id'] for r in g429 if r['http_status'] == 429})}")
        q = api("GET", "/api/quota")
        check("平台进入暂停状态", q["global_paused_until"] > q["now"])
        check("Retry-After 到期后自动恢复并完成",
              wait_until(lambda: all(
                  t["status"] not in ("pending", "running")
                  for t in api("GET", "/api/tasks")
                  if t["note"] == "demo-4g"), timeout=35))

        api("POST", "/api/mock/reset")
        time.sleep(6)
        api("POST", "/api/mock/force-429", {
            "scope": "account", "account_id": "acct-b",
            "retry_after": 4, "duration_sec": 20,
        })
        api("POST", "/api/tasks", {
            "account_ids": ["acct-a", "acct-b"],
            "date_start": "2030-05-01", "date_end": "2030-05-03",
            "priority": "normal", "note": "demo-4a",
        })
        time.sleep(8)
        may = [r for r in api("GET", "/api/requests?limit=500")
               if str(r["date"]).startswith("2030-05")]
        check("账号 429：仅 acct-b 被限流",
              all(r["account_id"] == "acct-b"
                  for r in may if r["http_status"] == 429)
              and any(r["account_id"] == "acct-a" and r["http_status"] == 200
                      for r in may))
        wait_tasks("demo-4a", timeout=40)

        # --------------------------------------------- 5. 凭据失效隔离
        print("\n【5】凭据失效只暂停该账号，重新授权后恢复")
        api("POST", "/api/mock/reset")
        time.sleep(6)
        # Invalidate directly at the platform: the scheduler still believes
        # the token is valid, so it sends a request, observes the real 401,
        # and isolates the account itself.
        mock_url = f"http://127.0.0.1:{MOCK_PORT}/mock/admin/invalidate"
        req = urllib.request.Request(
            mock_url, method="POST",
            data=json.dumps({"account_id": "acct-c", "invalid": True}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req).read()
        api("POST", "/api/tasks", {
            "account_ids": ["acct-a", "acct-c"],
            "date_start": "2030-06-01", "date_end": "2030-06-03",
            "priority": "normal", "note": "demo-5",
        })
        time.sleep(8)
        jun = [r for r in api("GET", "/api/requests?limit=500")
               if str(r["date"]).startswith("2030-06")]
        check("C 返回 401，A 不受影响继续 200",
              any(r["account_id"] == "acct-c" and r["http_status"] == 401
                  for r in jun)
              and all(r["http_status"] != 401
                      for r in jun if r["account_id"] == "acct-a")
              and any(r["account_id"] == "acct-a" and r["http_status"] == 200
                      for r in jun))
        accts = {a["id"]: a for a in api("GET", "/api/accounts")}
        check("仅 C 被调度器标记凭据失效",
              accts["acct-c"]["auth_valid"] == 0
              and accts["acct-a"]["auth_valid"] == 1)
        api("POST", "/api/accounts/acct-c/reauth")
        check("重新授权后 C 的任务全部完成", wait_until(lambda: all(
            t["status"] not in ("pending", "running")
            for t in api("GET", "/api/tasks") if t["note"] == "demo-5"
        ), timeout=30))

        # --------------------------------------------- 6. 取消
        print("\n【6】取消任务：不发新请求，已收数据保留")
        api("POST", "/api/mock/reset")  # flush stale mock-side windows
        api("PATCH", "/api/settings", {"global_quota_limit": 2})
        for acct in ("acct-a", "acct-b", "acct-c"):
            api("PATCH", f"/api/accounts/{acct}", {"quota_limit": 1})
        time.sleep(6)
        tid = api("POST", "/api/tasks", {
            "account_ids": ["acct-b"],
            "date_start": "2030-07-01", "date_end": "2030-07-10",
            "priority": "normal", "note": "demo-6",
        })["task_ids"][0]
        # cancel only after at least one date has actually been collected
        got_data = wait_until(lambda: any(
            d["date"].startswith("2030-07")
            for d in api("GET", "/api/data?account_id=acct-b")
        ), timeout=20)
        api("POST", f"/api/tasks/{tid}/cancel")
        before = len([r for r in api("GET", "/api/requests?limit=500")
                      if str(r["date"]).startswith("2030-07")])
        time.sleep(8)
        after = len([r for r in api("GET", "/api/requests?limit=500")
                     if str(r["date"]).startswith("2030-07")])
        check("取消前已有请求完成且取消后请求数不再增长",
              got_data and before == after,
              f"取消时 {before} 个，之后 {after} 个")
        kept = [d for d in api("GET", "/api/data?account_id=acct-b")
                if d["date"].startswith("2030-07")]
        check("已收到的数据仍然可查", len(kept) > 0,
              f"保留 {len(kept)} 天数据")

        # --------------------------------------------- 7. 重启恢复
        print("\n【7】服务重启：从断点继续，不重复采集已完成日期")
        api("POST", "/api/mock/reset")
        api("PATCH", "/api/settings", {"global_quota_limit": 3})
        for acct in ("acct-a", "acct-b", "acct-c"):
            api("PATCH", f"/api/accounts/{acct}", {"quota_limit": 2})
        time.sleep(6)
        tid7 = api("POST", "/api/tasks", {
            "account_ids": ["acct-a"],
            "date_start": "2030-08-01", "date_end": "2030-08-08",
            "priority": "normal", "note": "demo-7",
        })["task_ids"][0]
        time.sleep(4)
        done_before = sum(
            i["status"] == "done"
            for i in api("GET", f"/api/tasks/{tid7}/items")
        )
        print(f"  · 重启前已完成 {done_before}/8 个日期，现在重启调度服务…")
        stop(api_proc)
        _, api_proc = start_servers(db_path)  # 同一个 SQLite 文件
        api("GET", "/api/health")
        finished = wait_until(lambda: all(
            i["status"] == "done"
            for i in api("GET", f"/api/tasks/{tid7}/items")
        ), timeout=60)
        items = api("GET", f"/api/tasks/{tid7}/items")
        check("重启后任务最终全部完成",
              finished and all(i["status"] == "done" for i in items))
        aug200 = Counter(
            r["date"] for r in api("GET", "/api/requests?limit=500")
            if str(r["date"]).startswith("2030-08") and r["http_status"] == 200
        )
        check("每个日期恰好一次成功请求（无重复采集）",
              len(aug200) == 8 and all(v == 1 for v in aug200.values()),
              str(dict(sorted(aug200.items()))))

    finally:
        stop(api_proc)
        stop(mock)

    print("\n" + "=" * 64)
    failures = [r for r in results if not r[1]]
    print(f"验收结果：{len(results) - len(failures)}/{len(results)} 项通过")
    if failures:
        print("\n失败项：")
        for name, _, detail in failures:
            print(f"  - {name} {detail}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
