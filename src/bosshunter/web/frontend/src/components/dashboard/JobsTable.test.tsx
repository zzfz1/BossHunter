import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { JobsTable } from './JobsTable'

afterEach(cleanup)

it('shows the original external link and only marks an application after a user click', () => {
  const onMark = vi.fn()
  const job = {
    id: 'yingjiesheng:ext-42', source_platform: 'yingjiesheng',
    source_job_id: 'ext-42', company: '示例公司', title: 'AI 工程师', city: '上海',
    salary: '', score: 0, status: 'pending', hr_active: '', created_at: '',
    url: 'https://careers.example.org/jobs/42', jd: '', experience: '',
  } as any
  render(<JobsTable
    jobs={[job]} page={0} pageSize={10} total={1} onPageChange={() => {}}
    selectedIds={[]} onToggleSelected={() => {}} onMarkManuallySent={onMark}
    sortBy="created_at" sortOrder="desc" onSortChange={() => {}}
  />)
  expect(screen.getByText('应届生')).toBeTruthy()
  expect(screen.getByRole('link', { name: '打开平台' }).getAttribute('href')).toBe(job.url)
  expect(onMark).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '我已投递' }))
  expect(onMark).toHaveBeenCalledWith(job)
})
