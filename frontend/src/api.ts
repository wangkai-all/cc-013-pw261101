export type Priority = 0 | 1 | 2

export interface Account {
  id: string
  name: string
  token: string
  quota_limit: number
  quota_window_sec: number
  auth_valid: number
  paused_until: number | null
  pause_reason: string | null
}

export interface Task {
  id: number
  account_id: string
  date_start: string
  date_end: string
  priority: Priority
  status: string
  note: string | null
  created_at: number
  total: number
  done_n: number
  failed_n: number
  cancelled_n: number
  running_n: number
  pending_n: number
}

export interface TaskItem {
  id: number
  task_id: number
  account_id: string
  date: string
  status: string
  attempts: number
  next_run_at: number
  last_error: string | null
  updated_at: number
}

export interface ItemEvent {
  id: number
  item_id: number
  ts: number
  kind: string
  message: string
  data: Record<string, unknown> | null
}

export interface RequestLog {
  id: number
  item_id: number | null
  task_id: number | null
  account_id: string
  date: string | null
  attempt: number | null
  started_at: number
  finished_at: number | null
  http_status: number | null
  wait_reason: string | null
  wait_sec: number | null
  scope: string | null
  retry_after: number | null
  remaining_global: number | null
  remaining_account: number | null
  response_body: unknown
  error: string | null
}

export interface QuotaSnap {
  global: { limit: number; window_sec: number; used: number; remaining: number }
  accounts: Record<
    string,
    { limit: number; window_sec: number; used: number; remaining: number }
  >
  global_paused_until: number
  now: number
}

export interface CollectedRow {
  id: number
  account_id: string
  date: string
  item_id: number | null
  collected_at: number
}

export interface Settings {
  global_quota_limit: number
  global_quota_window_sec: number
  max_concurrency: number
  global_paused_until: number
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!resp.ok) {
    const text = await resp.text()
    throw new Error(`${resp.status} ${text}`)
  }
  return resp.json()
}

export const api = {
  get: <T,>(path: string) => req<T>(path),
  post: <T,>(path: string, body?: unknown) =>
    req<T>(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined }),
  patch: <T,>(path: string, body: unknown) =>
    req<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
}

export const PRIORITY_LABEL: Record<number, string> = {
  0: '高',
  1: '普通',
  2: '低',
}

export const STATUS_LABEL: Record<string, string> = {
  pending: '排队中',
  running: '采集中',
  done: '已完成',
  done_with_errors: '完成(含失败)',
  cancelled: '已取消',
  failed: '失败',
}
