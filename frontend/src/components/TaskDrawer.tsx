import { useEffect, useState } from 'react'
import { api, STATUS_LABEL, type ItemEvent, type RequestLog, type Task, type TaskItem } from '../api'
import { fmtTime, fmtTimeMs, fmtDuration, waitReason } from '../format'

export function TaskDrawer({ task, onClose }: { task: Task; onClose: () => void }) {
  const [tab, setTab] = useState<'items' | 'requests'>('items')
  const [items, setItems] = useState<TaskItem[]>([])
  const [logs, setLogs] = useState<RequestLog[]>([])
  const [expanded, setExpanded] = useState<number | null>(null)
  const [events, setEvents] = useState<Record<number, ItemEvent[]>>({})

  const load = async () => {
    const [it, lg] = await Promise.all([
      api.get<TaskItem[]>(`/api/tasks/${task.id}/items`),
      api.get<RequestLog[]>(`/api/requests?limit=200&account_id=${task.account_id}`),
    ])
    setItems(it)
    setLogs(lg.filter((l) => l.task_id === task.id))
  }

  useEffect(() => {
    load()
    const timer = setInterval(load, 2000)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.id])

  const toggle = async (itemId: number) => {
    if (expanded === itemId) {
      setExpanded(null)
      return
    }
    setExpanded(itemId)
    if (!events[itemId]) {
      const ev = await api.get<ItemEvent[]>(`/api/items/${itemId}/events`)
      setEvents((m) => ({ ...m, [itemId]: ev }))
    }
  }

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <div className="drawer">
        <h3>
          <span>
            任务 #{task.id} · {task.account_id} · {task.date_start} ~ {task.date_end}
          </span>
          <button className="small secondary" onClick={onClose}>
            关闭
          </button>
        </h3>
        <div className="tabs">
          <button className={tab === 'items' ? 'active' : ''} onClick={() => setTab('items')}>
            按日期（{items.length}）
          </button>
          <button className={tab === 'requests' ? 'active' : ''} onClick={() => setTab('requests')}>
            请求明细（{logs.length}）
          </button>
        </div>

        {tab === 'items' && (
          <div className="stack">
            {items.map((it) => (
              <div key={it.id} className="panel" style={{ padding: 10 }}>
                <div className="row" style={{ justifyContent: 'space-between' }}>
                  <span className="mono" style={{ cursor: 'pointer' }} onClick={() => toggle(it.id)}>
                    {expanded === it.id ? '▼' : '▶'} {it.date}
                  </span>
                  <span className="row">
                    <span className={`badge ${it.status}`}>
                      {STATUS_LABEL[it.status] ?? it.status}
                    </span>
                    <span className="pill">尝试 {it.attempts} 次</span>
                  </span>
                </div>
                {it.last_error && <div className="banner error" style={{ margin: '8px 0 0' }}>{it.last_error}</div>}
                {expanded === it.id && (
                  <div style={{ marginTop: 10 }}>
                    {(events[it.id] ?? []).map((e) => (
                      <div key={e.id} className={`event ${e.kind}`}>
                        <span className="mono muted">{fmtTime(e.ts)}</span>{' '}
                        <strong>{e.kind === 'wait' ? '等待' : e.kind === 'request' ? '请求' : '状态'}</strong>{' '}
                        {e.message}
                        {e.data && (
                          <pre className="json">{JSON.stringify(e.data, null, 2)}</pre>
                        )}
                      </div>
                    ))}
                    {!events[it.id] && <div className="muted">加载中…</div>}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {tab === 'requests' && (
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>日期</th>
                <th>HTTP</th>
                <th>等待原因</th>
                <th>剩余(全局/账号)</th>
                <th>返回</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((l) => (
                <tr key={l.id}>
                  <td className="mono">{fmtTimeMs(l.started_at)}</td>
                  <td className="mono">{l.date}</td>
                  <td>
                    <span className={`badge ${l.http_status === 200 ? 'done' : l.http_status === 429 ? 'done_with_errors' : 'failed'}`}>
                      {l.http_status ?? '-'}
                    </span>
                    {l.attempt && <span className="pill">第{l.attempt}次</span>}
                  </td>
                  <td className="muted">
                    {waitReason(l.wait_reason)}
                    {l.wait_sec ? ` ${l.wait_sec}s` : ''}
                    {l.scope && (
                      <div className="pill" style={{ marginTop: 2 }}>
                        {l.scope === 'global' ? '全局' : '账号'} 429 · {l.retry_after}s
                      </div>
                    )}
                  </td>
                  <td className="mono">
                    {l.remaining_global ?? '-'} / {l.remaining_account ?? '-'}
                  </td>
                  <td>
                    <details>
                      <summary className="muted" style={{ cursor: 'pointer' }}>
                        {l.finished_at ? fmtDuration(l.started_at, l.finished_at) : '在途'}
                        {l.error ? ' · 错误' : ''}
                      </summary>
                      <pre className="json">{JSON.stringify(l.response_body ?? l.error, null, 2)}</pre>
                    </details>
                  </td>
                </tr>
              ))}
              {logs.length === 0 && (
                <tr>
                  <td colSpan={6} className="muted" style={{ textAlign: 'center' }}>
                    该任务还没有发出请求
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </>
  )
}
