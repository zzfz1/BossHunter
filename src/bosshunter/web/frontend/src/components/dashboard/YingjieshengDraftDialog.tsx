import { useState } from 'react'
import { Button } from '@/components/ui/button'
import type { Job } from '@/hooks/useDashboard'

interface Props {
  job: Job
  onClose: () => void
}

function safeUrl(value: string): string | null {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && !url.username && !url.password ? url.toString() : null
  } catch {
    return null
  }
}

export function YingjieshengDraftDialog({ job, onClose }: Props) {
  const [kind, setKind] = useState<'greeting' | 'follow_up'>('greeting')
  const [context, setContext] = useState('')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const url = safeUrl(job.url || '')

  const generate = async () => {
    setBusy(true)
    setNotice('')
    setText('')
    try {
      const response = await fetch('/api/yingjiesheng/drafts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_id: job.id, kind, context: kind === 'follow_up' ? context : '' }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.error || '草稿生成失败')
      setText(String(data.text || ''))
      setNotice('草稿已生成。请检查内容，再复制到平台手动发送。')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '草稿生成失败')
    } finally {
      setBusy(false)
    }
  }

  const copy = async () => {
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
      setNotice('已复制。请在平台核对收件人和内容后手动发送。')
    } catch {
      setNotice('复制失败，请手动选择草稿内容。')
    }
  }

  return (
    <div role="dialog" aria-modal="true" aria-label="应届生沟通草稿" className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
      <div className="max-h-[90vh] w-full max-w-xl overflow-y-auto rounded-3xl border border-card-border bg-card p-6 shadow-2xl">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h3 className="text-lg font-black">沟通草稿</h3>
            <p className="mt-1 text-sm text-muted">{job.company}｜{job.title}</p>
          </div>
          <Button variant="secondary" size="sm" onClick={onClose}>关闭</Button>
        </div>
        <p className="mt-4 rounded-xl bg-surface p-3 text-xs leading-5 text-muted">
          草稿会使用已导入的简历与岗位信息调用当前配置的 AI 服务。生成、复制和打开平台都不会发送消息；请你亲自检查并发送。
        </p>
        <label className="mt-4 block text-xs font-bold text-muted" htmlFor="yingjiesheng-draft-kind">草稿类型</label>
        <select id="yingjiesheng-draft-kind" value={kind} onChange={event => { setKind(event.target.value as 'greeting' | 'follow_up'); setText(''); setNotice('') }} className="mt-1 w-full rounded-xl border border-card-border bg-card p-2 text-sm">
          <option value="greeting">首次招呼语</option>
          <option value="follow_up">后续消息</option>
        </select>
        {kind === 'follow_up' && <>
          <label className="mt-4 block text-xs font-bold text-muted" htmlFor="yingjiesheng-draft-context">对方消息或沟通背景</label>
          <textarea id="yingjiesheng-draft-context" value={context} maxLength={1000} onChange={event => setContext(event.target.value)} rows={4} className="mt-1 w-full rounded-xl border border-card-border bg-card p-3 text-sm" placeholder="粘贴必要的沟通背景；这段内容会发送给当前配置的 AI 服务" />
        </>}
        <Button className="mt-4" disabled={busy || (kind === 'follow_up' && !context.trim())} onClick={() => void generate()}>{busy ? '生成中...' : '生成可复制草稿'}</Button>
        {text && <>
          <label className="mt-4 block text-xs font-bold text-muted" htmlFor="yingjiesheng-draft-text">请核对草稿</label>
          <textarea id="yingjiesheng-draft-text" value={text} onChange={event => setText(event.target.value)} rows={6} maxLength={500} className="mt-1 w-full rounded-xl border border-card-border bg-card p-3 text-sm" />
          <div className="mt-3 flex flex-wrap gap-2">
            <Button onClick={() => void copy()}>复制草稿</Button>
            {url && <a href={url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center rounded-md border border-card-border px-3 text-xs font-bold">打开原岗位</a>}
          </div>
        </>}
        {notice && <p role="status" className="mt-3 text-xs text-muted">{notice}</p>}
      </div>
    </div>
  )
}
