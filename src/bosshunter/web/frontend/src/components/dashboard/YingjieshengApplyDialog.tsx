import { useState } from 'react'
import { Button } from '@/components/ui/button'
import type { Job } from '@/hooks/useDashboard'

interface Preview {
  confirmation_token: string
  job_id: string
  title: string
  company: string
  url: string
}

interface Props {
  job: Job
  onClose: () => void
  onApplied: () => void
}

export function YingjieshengApplyDialog({ job, onClose, onApplied }: Props) {
  const [preview, setPreview] = useState<Preview | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')

  const post = async (path: string, body: Record<string, unknown>) => {
    const response = await fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.error || '申请操作失败')
    return data
  }

  const close = () => {
    if (busy) return
    if (preview) {
      void post('/api/yingjiesheng/applications/cancel', { confirmation_token: preview.confirmation_token }).catch(() => {})
    }
    onClose()
  }

  const prepare = async () => {
    setBusy(true)
    setNotice('')
    try {
      const result = await post('/api/yingjiesheng/applications/prepare', { job_id: job.id })
      setPreview(result as Preview)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '职位检查失败')
    } finally {
      setBusy(false)
    }
  }

  const confirm = async () => {
    if (!preview || busy || preview.job_id !== job.id) return
    if (!window.confirm(`确认立即申请“${preview.company}｜${preview.title}”？确认后应届生平台可能直接提交申请，无法在本站撤销。`)) return
    setBusy(true)
    setNotice('')
    const token = preview.confirmation_token
    setPreview(null)
    try {
      await post('/api/yingjiesheng/applications/confirm', {
        job_id: job.id, confirmation_token: token, confirmed: true,
      })
      onApplied()
      onClose()
    } catch (error) {
      setNotice(`${error instanceof Error ? error.message : '申请结果未知'}。请打开原岗位人工核对，避免重复申请。`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div role="dialog" aria-modal="true" aria-label="应届生申请确认" className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
      <div className="w-full max-w-lg rounded-3xl border border-card-border bg-card p-6 shadow-2xl">
        <h3 className="text-lg font-black">单岗位申请</h3>
        <p className="mt-1 text-sm text-muted">{job.company}｜{job.title}</p>
        <p className="mt-4 rounded-xl bg-warning-soft p-3 text-xs leading-5 text-warning">
          “立即申请”可能直接提交。先检查页面，再由你逐岗确认；外链岗位请在原网站手动处理。
        </p>
        {preview ? <div className="mt-4 rounded-xl border border-card-border p-3 text-sm">
          <p>已检查：{preview.company}｜{preview.title}</p>
          <a href={preview.url} target="_blank" rel="noopener noreferrer" className="mt-2 block text-xs text-primary underline">查看原岗位</a>
        </div> : <Button className="mt-4" disabled={busy} onClick={() => void prepare()}>{busy ? '检查中...' : '检查申请条件'}</Button>}
        {notice && <p role="alert" className="mt-3 text-xs text-warning">{notice}</p>}
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="secondary" disabled={busy} onClick={close}>取消</Button>
          {preview && <Button disabled={busy} onClick={() => void confirm()}>{busy ? '处理中...' : '确认并立即申请'}</Button>}
        </div>
      </div>
    </div>
  )
}
