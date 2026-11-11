import type { Account, QuotaSnap } from '../api'

function Meter({ used, limit }: { used: number; limit: number }) {
  const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0
  const cls = pct >= 100 ? 'full' : pct >= 75 ? 'warn' : ''
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <div className={`meter ${cls}`} style={{ flex: 1 }}>
        <i style={{ width: `${pct}%` }} />
      </div>
      <span className="mono muted" style={{ minWidth: 64, textAlign: 'right' }}>
        {used}/{limit}
      </span>
    </div>
  )
}

export function QuotaPanel({
  quota,
  accounts,
  now,
  onReauth,
  onClearPause,
}: {
  quota: QuotaSnap
  accounts: Account[]
  now: number
  onReauth: (id: string) => void
  onClearPause: () => void
}) {
  const pausedRemain = Math.max(0, quota.global_paused_until - now)
  return (
    <div className="panel">
      <h2>
        额度水位（滑动窗口 {quota.global.window_sec}s）
        {pausedRemain > 0 && (
          <button className="small danger" style={{ marginLeft: 10 }} onClick={onClearPause}>
            平台暂停中 {pausedRemain.toFixed(0)}s · 手动解除
          </button>
        )}
      </h2>
      <div className="stack">
        <div>
          <label>
            平台总额度 · 剩余 {quota.global.remaining}（窗口内已发 {quota.global.used}）
          </label>
          <Meter used={quota.global.used} limit={quota.global.limit} />
        </div>
        {accounts.map((a) => {
          const q = quota.accounts[a.id]
          if (!q) return null
          const authBad = a.auth_valid === 0
          const pauseRemain = a.paused_until ? Math.max(0, a.paused_until - now) : 0
          return (
            <div key={a.id}>
              <label>
                {a.name}（{a.id}）窗口 {q.window_sec}s · 剩余 {q.remaining}
                {authBad && <span className="badge failed" style={{ marginLeft: 8 }}>凭据失效</span>}
                {pauseRemain > 0 && (
                  <span className="badge running" style={{ marginLeft: 8 }}>
                    限流暂停 {pauseRemain.toFixed(0)}s
                  </span>
                )}
                {authBad && (
                  <button className="small" style={{ marginLeft: 8 }} onClick={() => onReauth(a.id)}>
                    重新授权
                  </button>
                )}
              </label>
              <Meter used={q.used} limit={q.limit} />
            </div>
          )
        })}
      </div>
    </div>
  )
}
