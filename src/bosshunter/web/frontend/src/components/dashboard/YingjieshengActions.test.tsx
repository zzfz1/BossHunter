import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { YingjieshengDraftDialog } from './YingjieshengDraftDialog'
import { YingjieshengApplyDialog } from './YingjieshengApplyDialog'
import { YingjieshengProgressDialog } from './YingjieshengProgressDialog'
import { JobsTable } from './JobsTable'

const job = {
  id: 'yingjiesheng:1001', source_platform: 'yingjiesheng', source_job_id: '1001',
  company: '示例公司', title: 'AI 工程师', city: '上海', salary: '', score: 0,
  status: 'pending', hr_active: '', created_at: '',
  url: 'https://q.yingjiesheng.com/jobdetail/1001.html', jd: '', experience: '',
} as any

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('shows draft action and only offers application when enabled by its parent', () => {
  const onDraft = vi.fn()
  const props = {
    jobs: [job], page: 0, pageSize: 10, total: 1, onPageChange: () => {},
    selectedIds: [], onToggleSelected: () => {}, sortBy: 'created_at' as const,
    sortOrder: 'desc' as const, onSortChange: () => {}, onYingjieshengDraft: onDraft,
  }
  const view = render(<JobsTable {...props} />)
  expect(screen.queryByRole('button', { name: '准备申请' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '沟通草稿' }))
  expect(onDraft).toHaveBeenCalledWith(job)
  expect(screen.getByRole('link', { name: '管理简历' }).getAttribute('href')).toBe('https://q.yingjiesheng.com/pc/myresume')
  view.rerender(<JobsTable {...props} onYingjieshengApply={() => {}} />)
  expect(screen.getByRole('button', { name: '准备申请' })).toBeTruthy()
})

it('generates a copyable draft without calling any application endpoint', async () => {
  const fetcher = vi.fn(async (_path: string) => ({ ok: true, json: async () => ({ text: '您好，想了解这个岗位。', sent: false }) }))
  vi.stubGlobal('fetch', fetcher)
  render(<YingjieshengDraftDialog job={job} onClose={() => {}} />)
  expect(fetcher).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '生成可复制草稿' }))
  await waitFor(() => expect(screen.getByLabelText('请核对草稿')).toBeTruthy())
  expect(screen.getByRole('link', { name: '打开原岗位' }).getAttribute('href')).toBe(job.url)
  expect(fetcher.mock.calls[0][0]).toBe('/api/yingjiesheng/drafts')
})

it('checks job before a separate per-job confirmation submits once', async () => {
  const paths: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    paths.push(path)
    return { ok: true, json: async () => path.endsWith('/prepare')
      ? { confirmation_token: 'one-use-token', job_id: job.id, title: job.title, company: job.company, url: job.url }
      : { status: 'applied' } }
  }))
  const confirm = vi.fn(() => true)
  vi.stubGlobal('confirm', confirm)
  const onApplied = vi.fn()
  render(<YingjieshengApplyDialog job={job} onClose={() => {}} onApplied={onApplied} />)
  expect(paths).toEqual([])
  fireEvent.click(screen.getByRole('button', { name: '检查申请条件' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '确认并立即申请' })).toBeTruthy())
  expect(paths).toEqual(['/api/yingjiesheng/applications/prepare'])
  fireEvent.click(screen.getByRole('button', { name: '确认并立即申请' }))
  await waitFor(() => expect(onApplied).toHaveBeenCalledOnce())
  expect(confirm).toHaveBeenCalledOnce()
  expect(paths).toEqual(['/api/yingjiesheng/applications/prepare', '/api/yingjiesheng/applications/confirm'])
})

it('records progress only after the user confirms a manual platform check', async () => {
  const paths: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    paths.push(path)
    return { ok: true, json: async () => ({ source: 'manual', latest: null, events: [], check_due: false }) }
  }))
  const confirm = vi.fn(() => true)
  vi.stubGlobal('confirm', confirm)
  render(<YingjieshengProgressDialog job={{ ...job, status: 'sent' }} onClose={() => {}} />)
  await waitFor(() => expect(paths.length).toBe(1))
  expect(paths[0]).toContain('/api/yingjiesheng/progress/')
  expect(screen.getByRole('link', { name: '打开应届生个人中心' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '记录人工核对结果' }))
  await waitFor(() => expect(paths.length).toBe(2))
  expect(confirm).toHaveBeenCalledOnce()
  expect(paths[1]).toBe('/api/yingjiesheng/progress')
})
