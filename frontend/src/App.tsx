import { useCallback, useEffect, useState } from 'react'
import {
  api,
  type Account,
  type QuotaSnap,
  type Settings,
  type Task,
} from './api'
import { QuotaPanel } from './components/QuotaPanel'
import { TaskForm } from './components/TaskForm'
import { TaskTable } from './components/TaskTable'
import { TaskDrawer } from './components/TaskDrawer'
import { MockControl } from './components/MockControl'
import { DataPanel } from './components/DataPanel'

const POLL_MS = 2000

export default function App() {
  const [accounts, setAccounts] = useState<Account[]>([])
  const [tasks, setTasks] = useState<Task[]>([])
  const [quota, setQuota] = useState<QuotaSnap | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [openTask, setOpenTask] = useState<Task | null>(null)
  const [dataKey, setDataKey] = useState(0)
  const [now, setNow] = useState(Date.now() / 1000)

  const refresh = useCallback(async () => {
    const [a, t, q, s] = await Promise.all([
      api.get<Account[]>('/api/accounts'),
      api.get<Task[]>('/api/tasks'),
      api.get<QuotaSnap>('/api/quota'),
      api.get<Settings>('/api/settings'),
    ])
    setAccounts(a)
    setTasks(t)
    setQuota(q)
    setSettings(s)
    setNow(Date.now() / 1000)
    setDataKey((k) => k + 1)
  }, [])

  useEffect(() => {
    refresh()
    const timer = setInterval(refresh, POLL_MS)
    return () => clearInterval(timer)
  }, [refresh])

  const cancel = async (id: number) => {
    await api.post(`/api/tasks/${id}/cancel`)
    refresh()
  }

  const reauth = async (id: string) => {
    await api.post(`/api/accounts/${id}/reauth`)
    refresh()
  }

  const clearPause = async () => {
    await api.post('/api/global-pause/clear')
    refresh()
  }

  if (!quota || !settings) return <div className="app">加载中…</div>

  const activeCount = tasks.filter((t) =>
    ['pending', 'running'].includes(t.status),
  ).length

  return (
    <div className="app">
      <h1>广告数据采集调度台</h1>
      <p className="subtitle">
        多账号共享一套调度 · 账号轮询防饥饿 · 滑动窗口双重额度 · 429/401 分级暂停 · 状态持久化与重启恢复
        {activeCount > 0 && <span className="badge running" style={{ marginLeft: 10 }}>{activeCount} 个任务进行中</span>}
      </p>

      <div className="grid grid-2" style={{ marginBottom: 16 }}>
        <QuotaPanel
          quota={quota}
          accounts={accounts}
          now={now}
          onReauth={reauth}
          onClearPause={clearPause}
        />
        <MockControl accounts={accounts} settings={settings} onChange={refresh} />
      </div>

      <div style={{ marginBottom: 16 }}>
        <TaskForm accounts={accounts} onCreated={refresh} />
      </div>

      <div style={{ marginBottom: 16 }}>
        <TaskTable tasks={tasks} onOpen={setOpenTask} onCancel={cancel} />
      </div>

      <DataPanel refreshKey={dataKey} />

      {openTask && <TaskDrawer task={openTask} onClose={() => setOpenTask(null)} />}
    </div>
  )
}
