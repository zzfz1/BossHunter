import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { CollectJobsDialog } from './CollectJobsDialog'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('starts YingJieSheng read-only collection without another platform city code', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const body = url === '/api/config'
      ? { platforms: { boss: { enabled: false }, yingjiesheng: { enabled: false } }, collection: { default_order: ['boss'] } }
      : url === '/api/collection/runs?limit=100' ? [] : { ok: true, cities: [] }
    return new Response(JSON.stringify(body), { status: 200 })
  }))
  const onStart = vi.fn(async () => ({ ok: true }))
  render(<CollectJobsDialog open activeTask={null} onStart={onStart} onClose={() => {}} />)
  const section = (await screen.findByText('应届生求职')).closest('section')!
  fireEvent.click(within(section).getByRole('checkbox'))
  fireEvent.change(within(section).getByLabelText('关键词（逗号或换行分隔）'), { target: { value: 'AI' } })
  fireEvent.change(within(section).getByLabelText('城市（逗号或换行分隔）'), { target: { value: '上海' } })
  fireEvent.click(screen.getByRole('button', { name: '重新采集' }))
  await waitFor(() => expect(onStart).toHaveBeenCalledWith({
    platform_order: ['yingjiesheng'], auto_score: false,
    platforms: { yingjiesheng: { keywords: ['AI'], cities: ['上海'], city_codes: {}, max_pages: 1, sort: 'default' } },
  }))
})
