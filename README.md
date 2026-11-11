# 多账号广告数据采集调度台

一套调度台同时服务多个广告账号：每个账号有独立额度，所有账号共享平台总额度。
支持账号轮询调度、滑动窗口配额、429/401 分级暂停、任务取消、重启断点恢复，
并配有本地模拟广告平台和可视化控制台。

## 架构

```
┌──────────────┐   HTTP    ┌─────────────────────────────┐
│ React 控制台 │ ────────▶ │ FastAPI 调度台 (端口 9000)   │
│ (Vite)       │           │  · 轮询调度器 (asyncio)      │
└──────────────┘           │  · 滑动窗口双重额度门控      │
                           │  · SQLite 状态持久化         │
                           └──────────────┬──────────────┘
                                          │ HTTP
                           ┌──────────────▼──────────────┐
                           │ 模拟广告平台 (端口 9001)     │
                           │  · 可调小额度                │
                           │  · 主动触发全局/账号 429     │
                           │  · 凭据失效 / 重新授权       │
                           └─────────────────────────────┘
```

| 目录 | 说明 |
| --- | --- |
| `backend/app/scheduler.py` | 调度核心：账号轮询、账号内优先级+提交时间排序、额度门控、429/401 处理、取消、重启恢复 |
| `backend/app/quota.py` | 滑动窗口额度统计（数据来源于 `request_logs`，在途/重试/429 请求都计数） |
| `backend/app/db.py` | SQLite 建表与串行化访问 |
| `backend/app/mock_server.py` | 本地模拟广告平台（独立进程） |
| `backend/app/main.py` | REST API + 模拟接口管理代理 + 前端静态资源托管 |
| `frontend/` | React + TypeScript + Vite 控制台 |
| `demo.py` | 端到端验收脚本（独立端口、临时库，覆盖全部调度保证） |

## 快速开始

### 1. 后端

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run_dev.sh          # 同时启动模拟平台 :9001 与调度台 :9000
```

### 2. 前端

开发模式（热更新，代理 `/api` 到 9000）：

```bash
cd frontend
npm install
npm run dev           # 打开 http://localhost:5173
```

生产构建（构建后由 FastAPI 在 `http://127.0.0.1:9000/` 直接托管）：

```bash
cd frontend && npm run build
```

## 调度语义

- **账号轮询防饥饿**：每个调度 tick 按轮询游标遍历账号，每账号最多派发一个
  日期项；账号内部按任务优先级（高→低）、再按提交时间排序。游标每 tick
  前进，因此某账号持续追加高优先级任务也不会饿死其他账号。
- **滑动窗口双额度**：请求**发出前**即写入请求日志，所以在途请求、重试请求、
  最终收到 429 的请求都占额度。平台总额度与账号额度都按滑动时间窗口实时计算。
- **429 分级暂停**：按响应体中的 `scope` 与 `Retry-After`：
  - `global`：整个平台暂停到指定时刻，所有账号等待；
  - `account`：仅该账号暂停，其他账号继续。
- **401 凭据隔离**：只标记该账号失效并暂停其队列，其他账号不受影响；
  调用「重新授权」（平台签发新 token）后自动恢复，无需重启。
- **取消**：待办项立即标记取消，在途请求跑完但不再重试/补发；已落库的数据
  仍可查看。
- **重启恢复**：任务、日期项、已采集数据都在 SQLite 中。重启后未完成项重新
  入队；新建任务时自动跳过该账号已采集过的日期，避免重复采集。

## 模拟平台能做什么

控制台「模拟平台控制」面板，或直接调用 API：

```bash
# 调小平台总额度，制造排队
curl -X PATCH localhost:9000/api/settings -d '{"global_quota_limit":4}' \
  -H 'Content-Type: application/json'

# 主动触发全局 429（Retry-After=5s）
curl -X POST localhost:9000/api/mock/force-429 \
  -d '{"scope":"global","retry_after":5,"duration_sec":60}' \
  -H 'Content-Type: application/json'

# 只让 acct-b 触发账号级 429
curl -X POST localhost:9000/api/mock/force-429 \
  -d '{"scope":"account","account_id":"acct-b","retry_after":5}' \
  -H 'Content-Type: application/json'

# 让 acct-c 凭据失效 / 重新授权
curl -X POST localhost:9000/api/mock/invalidate \
  -d '{"account_id":"acct-c","invalid":true}' -H 'Content-Type: application/json'
curl -X POST localhost:9000/api/accounts/acct-c/reauth
```

## 页面怎么判断调度是否生效

- **额度水位面板**：平台/各账号窗口内已用量与剩余量实时刷新，暂停状态有倒计时。
- **任务队列**：状态（排队中/采集中/已完成/已取消）、进度、优先级一目了然，
  可直接取消。
- **任务抽屉**：
  - 「按日期」页签可展开每个日期项的完整事件流——每次等待原因与剩余等待
    时间、每次请求、每次状态变更都带时间戳；
  - 「请求明细」页签展示每次请求的发起时间、HTTP 状态、第几次尝试、等待
    原因/时长、429 作用域与 Retry-After、响应时的剩余全局/账号额度、完整
    返回 JSON。
- **采集结果**：按账号/日期保存，点击日期可查看原始返回；取消的任务已收到
  的数据同样在这里。

## 一键验收

```bash
python3 -I demo.py
```

脚本在独立端口（9100/9101）和临时数据库上运行，覆盖：

1. 三组账号同时采集，18 个日期全部落库；
2. 滑动窗口在大/小两种额度下均不越限，重试与 429 请求计入额度；
3. 防饥饿：A 持续高优先级任务时，B 的普通任务仍被交替调度；
4. 全局 429 暂停全平台、账号 429 只停一个账号，Retry-After 到期自动恢复；
5. 凭据失效只隔离单账号，重新授权后自动续跑；
6. 取消后不再发新请求，已收数据保留；
7. 杀掉服务再重启，从断点续采且每个已完成日期不重复请求。

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/tasks` | 创建任务（多账号、日期范围、优先级） |
| GET | `/api/tasks` / `/api/tasks/{id}/items` | 任务与日期项 |
| POST | `/api/tasks/{id}/cancel` | 取消任务 |
| GET | `/api/items/{id}/events` | 日期项事件流（等待/请求/状态） |
| GET | `/api/requests` | 请求明细（时间、额度、等待原因、返回体） |
| GET | `/api/quota` | 滑动窗口额度快照 |
| GET/PATCH | `/api/accounts`、`/api/settings` | 账号与调度参数 |
| POST | `/api/accounts/{id}/reauth` | 重新授权 |
| POST | `/api/mock/force-429`、`/api/mock/invalidate`、`/api/mock/config` | 模拟平台控制 |
| GET | `/api/data`、`/api/data/{id}` | 采集结果 |
