import { useEffect, useState } from 'react'
import { api, type CollectedRow } from '../api'

export function DataPanel({ refreshKey }: { refreshKey: number }) {
  const [rows, setRows] = useState<CollectedRow[]>([])
  const [open, setOpen] = useState<number | null>(null)
  const [payload, setPayload] = useState<unknown>(null)

  useEffect(() => {
    api.get<CollectedRow[]>('/api/data').then(setRows)
  }, [refreshKey])

  const show = async (id: number) => {
    if (open === id) {
      setOpen(null)
      return
    }
    setOpen(id)
    const full = await api.get<{ payload: unknown }>(`/api/data/${id}`)
    setPayload(full.payload)
  }

  const grouped = rows.reduce<Record<string, CollectedRow[]>>((m, r) => {
    ;(m[r.account_id] ??= []).push(r)
    return m
  }, {})

  return (
    <div className="panel">
      <h2>采集结果（按账号 / 日期保存，取消后仍可查看）</h2>
      {Object.keys(grouped).length === 0 && <div className="muted">暂无数据</div>}
      {Object.entries(grouped).map(([acct, list]) => (
        <div key={acct} style={{ marginBottom: 12 }}>
          <label>
            {acct} · {list.length} 天
          </label>
          <div className="row">
            {list.map((r) => (
              <button
                key={r.id}
                className={`small ${open === r.id ? '' : 'secondary'}`}
                onClick={() => show(r.id)}
              >
                {r.date}
              </button>
            ))}
          </div>
          {open && (
            <div key={open} style={{ marginTop: 8 }}>
              {rows.find((r) => r.id === open)?.account_id === acct && (
                <pre className="json">{JSON.stringify(payload, null, 2)}</pre>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}
