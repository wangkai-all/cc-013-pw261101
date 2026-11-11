import { useState } from 'react'
import type { Account } from '../api'
import { api } from '../api'

export function TaskForm({
  accounts,
  onCreated,
}: {
  accounts: Account[]
  onCreated: () => void
}) {
  const today = new Date().toISOString().slice(0, 10)
  const [selected, setSelected] = useState<string[]>(accounts.map((a) => a.id))
  const [start, setStart] = useState(today)
  const [end, setEnd] = useState(today)
  const [priority, setPriority] = useState('normal')
  const [note, setNote] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const toggle = (id: string) =>
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]))

  const submit = async () => {
    setError('')
    if (!selected.length) {
      setError('至少选择一个账号')
      return
    }
    if (end < start) {
      setError('结束日期不能早于开始日期')
      return
    }
    setBusy(true)
    try {
      await api.post('/api/tasks', {
        account_ids: selected,
        date_start: start,
        date_end: end,
        priority,
        note: note || null,
      })
      setNote('')
      onCreated()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="panel">
      <h2>新建采集任务</h2>
      <div className="stack">
        <div>
          <label>账号（可多选，每个账号生成一个独立任务）</label>
          <div className="checkbox-list">
            {accounts.map((a) => (
              <label key={a.id}>
                <input
                  type="checkbox"
                  checked={selected.includes(a.id)}
                  onChange={() => toggle(a.id)}
                />
                {a.name}
                {a.auth_valid === 0 && <span className="badge failed">失效</span>}
              </label>
            ))}
          </div>
        </div>
        <div className="grid grid-3">
          <div>
            <label>开始日期</label>
            <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
          </div>
          <div>
            <label>结束日期（含）</label>
            <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
          </div>
          <div>
            <label>优先级</label>
            <select value={priority} onChange={(e) => setPriority(e.target.value)}>
              <option value="high">高</option>
              <option value="normal">普通</option>
              <option value="low">低</option>
            </select>
          </div>
        </div>
        <div className="row">
          <div style={{ flex: 1 }}>
            <label>备注</label>
            <input
              style={{ width: '100%' }}
              value={note}
              placeholder="可选"
              onChange={(e) => setNote(e.target.value)}
            />
          </div>
          <button onClick={submit} disabled={busy} style={{ marginTop: 16 }}>
            {busy ? '提交中…' : '提交任务'}
          </button>
        </div>
        {error && <div className="banner error">{error}</div>}
      </div>
    </div>
  )
}
