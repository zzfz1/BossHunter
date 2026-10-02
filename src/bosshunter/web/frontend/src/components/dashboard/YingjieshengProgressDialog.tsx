import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import type { Job } from '@/hooks/useDashboard'

type ProgressStatus = 'waiting' | 'reviewing' | 'interview' | 'offer' | 'rejected' | 'withdrawn'

interface ProgressEvent {
  status: ProgressStatus
  label: string
  next_check_date: string
  created_at: string
}

interface Progress {
  source: 'manual'
  latest: ProgressEvent | null
  events: ProgressEvent[]
  check_due: boolean
}

export function YingjieshengProgressDialog({ job, onClose }: { job: Job; onClose: () => void }) {
  const [progress, setProgress] = useState<Progress | null>(null)
  const [status, setStatus] = useState<ProgressStatus>('waiting')
  const [nextDate, setNextDate] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')

  useEffect(() => {
    let current = true
    void fetch(`/api/yingjiesheng/progress/${encodeURIComponent(job.id)}`, { cache: 'no-store' })
      .then(async response => {
        const data = await response.json()
        if (!response.ok) throw new Error(data.error || '读取进度失败')
        if (current) setProgress(data as Progress)
      })
      .catch(error => { if (current) setNotice(error instanceof Error ? error.message : '读取进度失败') })
    return () => { current = false }
  }, [job.id])

  const save = async () => {
    if (!window.confirm(`确认你已在应届生平台人工查看“${job.company}｜${job.title}”的投递进度，并将本地记录更新为所选状态？`)) return
    setBusy(true)
    setNotice('')
    try {
      const response = await fetch('/api/yingjiesheng/progress', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_id: job.id, status, next_check_date: nextDate, confirmed: true }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.error || '记录进度失败')
      setProgress(data as Progress)
      setNotice('已记录人工核对结果。工作台不会读取或发送平台消息。')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '记录进度失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div role="dialog" aria-modal="true" aria-label="应届生投递进度" className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
      <div className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-3xl border border-card-border bg-card p-6 shadow-2xl">
        <div className="flex items-start justify-between gap-3">
          <div><h3 className="text-lg font-black">投递进度</h3><p className="mt-1 text-sm text-muted">{job.company}｜{job.title}</p></div>
          <Button variant="secondary" size="sm" onClick={onClose}>关闭</Button>
        </div>
        <p className="mt-4 rounded-xl bg-surface p-3 text-xs leading-5 text-muted">
          这里记录你在平台亲自查看的结果。进度不会自动从应届生网站同步。先打开个人中心查看，再选择状态。
        </p>
        <a href="https://q.yingjiesheng.com/pc/personal" target="_blank" rel="noopener noreferrer" className="mt-3 inline-block text-sm font-bold text-primary underline">打开应届生个人中心</a>
        {progress?.check_due && <p className="mt-3 rounded-xl bg-warning-soft p-3 text-xs font-bold text-warning">已到你设置的检查日期，请前往平台查看最新进度。</p>}
        {progress?.latest && <p className="mt-3 text-sm">上次记录：{progress.latest.label}{progress.latest.next_check_date ? ` · 下次检查 ${progress.latest.next_check_date}` : ''}</p>}
        <label htmlFor="yingjiesheng-progress-status" className="mt-4 block text-xs font-bold text-muted">人工核对后的状态</label>
        <select id="yingjiesheng-progress-status" value={status} onChange={event => setStatus(event.target.value as ProgressStatus)} className="mt-1 w-full rounded-xl border border-card-border bg-card p-2 text-sm">
          <option value="waiting">待反馈</option><option value="reviewing">筛选中</option><option value="interview">面试中</option><option value="offer">已录用</option><option value="rejected">未通过</option><option value="withdrawn">已撤回</option>
        </select>
        <label htmlFor="yingjiesheng-progress-date" className="mt-3 block text-xs font-bold text-muted">下次检查日期（可选）</label>
        <input id="yingjiesheng-progress-date" type="date" value={nextDate} onChange={event => setNextDate(event.target.value)} className="mt-1 w-full rounded-xl border border-card-border bg-card p-2 text-sm" />
        <Button className="mt-4" disabled={busy} onClick={() => void save()}>{busy ? '保存中...' : '记录人工核对结果'}</Button>
        {notice && <p role="status" className="mt-3 text-xs text-muted">{notice}</p>}
        {!!progress?.events.length && <div className="mt-4 border-t border-card-border pt-3 text-xs text-muted">
          <p className="font-bold">记录历史</p>
          {progress.events.map((event, index) => <p key={`${event.created_at}-${index}`} className="mt-1">{event.created_at} · {event.label}</p>)}
        </div>}
      </div>
    </div>
  )
}
