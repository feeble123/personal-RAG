/**
 * 「看原文」按钮的接线测试：四个入口（问答引用/本体证据/分块浏览/检索预览）
 * 共用这个组件，所以要保证两件事——没文档 id 时按钮不可点；点了才去加载阅读器
 * （pdf.js 是 MB 级，不能在列表里每行都预加载）。
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('@/components/origin/PdfOriginViewer', () => ({
  default: ({ docId, page }: { docId: number; page?: number }) => (
    <div data-testid="viewer">
      viewer:{docId}:{page ?? '-'}
    </div>
  ),
}))

import OriginLink from './OriginLink'

describe('OriginLink', () => {
  it('没点之前不加载阅读器', async () => {
    render(<OriginLink docId={9} chunkId={1} />)
    expect(screen.getByRole('button')).toBeTruthy()
    expect(screen.queryByTestId('viewer')).toBeNull()
  })

  it('点了之后打开阅读器，并把文档/页码传下去', async () => {
    render(<OriginLink docId={9} page={4} chunkId={1} />)
    await userEvent.click(screen.getByRole('button'))
    await waitFor(() => expect(screen.getByTestId('viewer')).toBeTruthy())
    expect(screen.getByTestId('viewer').textContent).toBe('viewer:9:4')
  })

  it('没有关联文档时按钮不可点', () => {
    render(<OriginLink docId={null} />)
    expect(screen.getByRole('button')).toBeDisabled()
  })
})
