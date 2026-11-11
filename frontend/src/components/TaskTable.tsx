import type { Task } from '../api'
import { PRIORITY_LABEL, STATUS_LABEL } from '../api'

export function TaskTable({
  tasks,
  onOpen,
  onCancel,
}: {
  tasks: Task[]
  onOpen: (t: Task) => void
  onCancel: (id: number) => void
}) {
  return (
    <div className="panel">
      <h2>任务队列（账号轮询 · 账号内按优先级/提交时间排序）</h2>
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>账号</th>
            <th>日期范围</th>
            <th>优先级</th>
            <th>状态</th>
            <th>进度</th>
            <th>备注</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((t) => {
            const active = ['pending', 'running'].includes(t.status)
            return (
              <tr key={t.id} className="clickable" onClick={() => onOpen(t)}>
                <td className="mono">#{t.id}</td>
                <td>{t.account_id}</td>
                <td className="mono">
                  {t.date_start} ~ {t.date_end}
                </td>
                <td>{PRIORITY_LABEL[t.priority]}</td>
                <td>
                  <span className={`badge ${t.status}`}>
                    {STATUS_LABEL[t.status] ?? t.status}
                  </span>
                </td>
                <td>
                  {t.done_n}/{t.total}
                  {t.failed_n > 0 && (
                    <span className="muted">（失败 {t.failed_n}）</span>
                  )}
                  {t.cancelled_n > 0 && (
                    <span className="muted">（取消 {t.cancelled_n}）</span>
                  )}
                </td>
                <td className="muted">{t.note ?? '-'}</td>
                <td onClick={(e) => e.stopPropagation()}>
                  <button
                    className="small danger"
                    disabled={!active}
                    onClick={() => onCancel(t.id)}
                  >
                    取消
                  </button>
                </td>
              </tr>
            )
          })}
          {tasks.length === 0 && (
            <tr>
              <td colSpan={8} className="muted" style={{ textAlign: 'center' }}>
                暂无任务
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
