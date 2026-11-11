export function fmtTime(ts: number | null | undefined): string {
  if (!ts) return '-'
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function fmtTimeMs(ts: number | null | undefined): string {
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  return (
    d.toLocaleTimeString('zh-CN', { hour12: false }) +
    '.' +
    String(d.getMilliseconds()).padStart(3, '0')
  )
}

export function fmtDuration(start: number, end: number | null): string {
  if (!end) return '-'
  const ms = Math.round((end - start) * 1000)
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

const WAIT_REASON: Record<string, string> = {
  global_paused: '平台限流暂停',
  account_paused: '账号限流暂停',
  auth_invalid: '凭据失效待授权',
  quota_global: '平台总额度已满',
  quota_account: '账号额度已满',
  concurrency: '并发上限',
  backoff: '失败退避',
}

export function waitReason(code: string | null | undefined): string {
  if (!code) return '-'
  return WAIT_REASON[code] ?? code
}
