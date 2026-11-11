import { useState } from 'react'
import type { Account, Settings } from '../api'
import { api } from '../api'

export function MockControl({
  accounts,
  settings,
  onChange,
}: {
  accounts: Account[]
  settings: Settings
  onChange: () => void
}) {
  const [scope, setScope] = useState<'global' | 'account'>('global')
  const [acctId, setAcctId] = useState(accounts[0]?.id ?? '')
  const [retryAfter, setRetryAfter] = useState(4)
  const [gLimit, setGLimit] = useState(settings.global_quota_limit)
  const [maxConc, setMaxConc] = useState(settings.max_concurrency)
  const [msg, setMsg] = useState('')

  const flash = (m: string) => {
    setMsg(m)
    setTimeout(() => setMsg(''), 2500)
  }

  const force429 = async () => {
    await api.post('/api/mock/force-429', {
      scope,
      account_id: scope === 'account' ? acctId : null,
      retry_after: retryAfter,
      duration_sec: 60,
    })
    flash(`已触发${scope === 'global' ? '全局' : '账号'} 429，Retry-After=${retryAfter}s`)
    onChange()
  }

  const invalidate = async (id: string, invalid: boolean) => {
    await api.post('/api/mock/invalidate', { account_id: id, invalid })
    flash(`${id} 凭据已${invalid ? '失效' : '恢复'}`)
    onChange()
  }

  const saveSettings = async () => {
    await api.patch('/api/settings', {
      global_quota_limit: gLimit,
      max_concurrency: maxConc,
    })
    flash('调度参数已更新并同步到模拟接口')
    onChange()
  }

  return (
    <div className="panel">
      <h2>模拟平台控制 & 调度参数</h2>
      <div className="stack">
        <div className="grid grid-2">
          <div>
            <label>平台总额度（每窗口）</label>
            <input
              type="number"
              min={1}
              value={gLimit}
              onChange={(e) => setGLimit(Number(e.target.value))}
            />
          </div>
          <div>
            <label>最大并发</label>
            <input
              type="number"
              min={1}
              value={maxConc}
              onChange={(e) => setMaxConc(Number(e.target.value))}
            />
          </div>
        </div>
        <button className="secondary" onClick={saveSettings}>
          保存参数（可调小额度制造排队）
        </button>

        <div style={{ height: 1, background: 'var(--border)' }} />

        <div>
          <label>主动触发限流 429</label>
          <div className="row">
            <select value={scope} onChange={(e) => setScope(e.target.value as 'global' | 'account')}>
              <option value="global">全局作用域（暂停整个平台）</option>
              <option value="account">账号作用域（只暂停一个账号）</option>
            </select>
            {scope === 'account' && (
              <select value={acctId} onChange={(e) => setAcctId(e.target.value)}>
                {accounts.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.id}
                  </option>
                ))}
              </select>
            )}
            <label style={{ margin: 0 }}>Retry-After</label>
            <input
              type="number"
              min={1}
              style={{ width: 80 }}
              value={retryAfter}
              onChange={(e) => setRetryAfter(Number(e.target.value))}
            />
            <button onClick={force429}>触发 429</button>
          </div>
        </div>

        <div>
          <label>凭据状态（模拟 token 失效 / 重新授权）</label>
          <table>
            <tbody>
              {accounts.map((a) => (
                <tr key={a.id}>
                  <td>{a.id}</td>
                  <td>
                    {a.auth_valid === 1 ? (
                      <span className="badge done">有效</span>
                    ) : (
                      <span className="badge failed">已失效</span>
                    )}
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    <button className="small danger" onClick={() => invalidate(a.id, true)}>
                      令其失效
                    </button>{' '}
                    <button className="small secondary" onClick={() => invalidate(a.id, false)}>
                      恢复
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {msg && <div className="banner warn">{msg}</div>}
      </div>
    </div>
  )
}
