/**
 * 阅读器的**接线与稳定性**测试：pdf.js 与 react-window 全打桩。
 *
 * 为什么打桩而不是渲染真实 PDF：jsdom 没有 canvas，真渲染必然失败；
 * 而真正要防的回归是接线与状态——签名 URL 会不会被莫名换掉、
 * locate 后到会不会把页列表清空（用户实测"黑屏"就是这个）、
 * 加载失败后有没有给出可恢复的路。
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const fileToken = vi.fn()
const locate = vi.fn()

vi.mock('@/api/modules', () => ({
  sourceApi: {
    fileToken: (...args: unknown[]) => fileToken(...args),
    locate: (...args: unknown[]) => locate(...args),
  },
}))

/** 让每个用例决定"这次挂载是成功还是失败" */
const docBehavior = { result: 'success' as 'success' | 'error' }

vi.mock('react-pdf', () => ({
  pdfjs: { GlobalWorkerOptions: {} },
  Document: ({ children, onLoadSuccess, onLoadError, file }: Record<string, any>) => {
    if (file) {
      setTimeout(() => {
        if (docBehavior.result === 'error') {
          onLoadError?.(new Error('UnexpectedResponseException: 403'))
        } else {
          onLoadSuccess?.({
            numPages: 3,
            getPage: () => Promise.resolve({ view: [0, 0, 600, 800] }),
          })
        }
      }, 0)
    }
    return <div data-testid="pdf-doc">{children}</div>
  },
  Page: ({ pageNumber }: { pageNumber: number }) => <div data-testid={`page-${pageNumber}`} />,
}))

// Office 文档的真实渲染在 OfficeView.test.tsx 里测；这里只验证"分流对不对"
vi.mock('@/components/origin/OfficeView', () => ({
  default: ({ fileType }: { fileType: string }) => (
    <div data-testid="office-view">office:{fileType}</div>
  ),
}))

vi.mock('react-window', () => ({
  VariableSizeList: ({ children }: { children: (p: { index: number; style: object }) => any }) => (
    <div>{[0, 1, 2].map((index) => children({ index, style: {} }))}</div>
  ),
}))

import PdfOriginViewer from './PdfOriginViewer'

// jsdom 没有 ResizeObserver。补空壳并把容器尺寸做成 800x600，
// 否则 `size.height > 0` 不成立，Document 根本不会挂载。
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', ResizeObserverStub)
Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, value: 600 })
Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, value: 800 })

const TOKEN = {
  doc_id: 23,
  filename: '水文预报 第5版.pdf',
  file_type: 'pdf',
  file_size: 10,
  page_count: 3,
  url: '/api/documents/23/file?t=abc',
  expires_in: 1800,
}

const BBOX_LOCATE = {
  doc_id: 23,
  filename: '水文预报 第5版.pdf',
  page: 2,
  printed_page: 157,
  page_count: 3,
  page_size: { width: 600, height: 800 },
  has_text_layer: false,
  source: 'bbox',
  rects: [[10, 20, 100, 40]],
  note: '这页是扫描件（没有文本层），框出的是整块区域',
}

function renderViewer(props: Partial<Parameters<typeof PdfOriginViewer>[0]> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <PdfOriginViewer open docId={23} onClose={() => {}} {...props} />
    </QueryClientProvider>,
  )
}

describe('PdfOriginViewer', () => {
  beforeEach(() => {
    docBehavior.result = 'success'
    fileToken.mockReset().mockResolvedValue(TOKEN)
    locate.mockReset().mockResolvedValue(BBOX_LOCATE)
  })

  it('打开就取签名 URL，并把它交给 pdf.js', async () => {
    renderViewer({ chunkId: 28197 })
    await waitFor(() => expect(fileToken).toHaveBeenCalledWith(23))
    // 给 chunk_id 时只传 chunk_id，不再传 page/quote（避免两套定位打架）
    await waitFor(() =>
      expect(locate).toHaveBeenCalledWith(23, {
        t: 'abc', // 签名从 file-token 的 url 里取，locate/sheets 复用它
        chunk_id: 28197,
        quote: undefined,
        page: undefined,
      }),
    )
    expect(await screen.findByText('水文预报 第5版.pdf')).toBeTruthy()
    expect(screen.getByText('书内第 157 页')).toBeTruthy()
  })

  it('locate 后到不会把已经渲染的页清空（用户看到的"黑屏"）', async () => {
    // locate 故意慢一步回来
    let release: (v: unknown) => void = () => {}
    locate.mockReturnValue(new Promise((resolve) => (release = resolve)))

    renderViewer({ chunkId: 1 })
    await waitFor(() => expect(screen.getByTestId('page-2')).toBeTruthy())

    release(BBOX_LOCATE)
    await waitFor(() => expect(screen.getByText('书内第 157 页')).toBeTruthy())
    // 关键断言：页还在，没有被清零
    expect(screen.getByTestId('page-2')).toBeTruthy()
    expect(screen.getByTestId('page-1')).toBeTruthy()
  })

  it('签名 URL 不因重渲染/Locate 返回而重取（重取会让 pdf.js 整份重载）', async () => {
    renderViewer({ chunkId: 1 })
    await waitFor(() => expect(fileToken).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByText('书内第 157 页')).toBeTruthy())
    await new Promise((r) => setTimeout(r, 50))
    expect(fileToken).toHaveBeenCalledTimes(1)
  })

  it('扫描件要明说"框出的是整块"，不能让用户以为框错了', async () => {
    renderViewer({ chunkId: 1 })
    expect(await screen.findByText('扫描件：框出的是整块区域')).toBeTruthy()
  })

  it('定位不到时说明原因，而不是假装成功', async () => {
    locate.mockResolvedValue({
      ...BBOX_LOCATE,
      has_text_layer: true,
      source: 'none',
      rects: [],
      note: '没在原文里找到这句话（可能被改写、跨页，或切片来自表格重排）',
    })
    renderViewer({ page: 1, quote: '某句不存在的话' })
    expect(await screen.findByText('没有在原文里框出位置')).toBeTruthy()
    expect(await screen.findByText('未能定位到具体位置')).toBeTruthy()
  })

  it('加载失败：给出提示并自动换一份链接重试一次，不是整片空白', async () => {
    docBehavior.result = 'error'
    renderViewer({ chunkId: 1 })

    await waitFor(() => expect(screen.getByTestId('pdf-doc')).toBeTruthy())
    // 自动重试：会再取一次 token（800ms 后）
    await waitFor(() => expect(fileToken).toHaveBeenCalledTimes(2), { timeout: 3000 })
  })

  it('docx/xlsx 走 Office 视图，不进 pdf.js（否则只会 "Invalid PDF structure."）', async () => {
    fileToken.mockResolvedValue({ ...TOKEN, file_type: 'docx', filename: '高支模专项施工方案.docx' })
    renderViewer({ page: 3 })

    expect(await screen.findByTestId('office-view')).toBeTruthy()
    expect(screen.getByTestId('office-view').textContent).toBe('office:docx')
    // 关键：不挂 pdf.js，也不发 locate（后端对非 PDF 返回 415）
    expect(screen.queryByTestId('pdf-doc')).toBeNull()
    expect(locate).not.toHaveBeenCalled()
    // 仍然可以下载原件，但不是唯一出路
    expect(screen.getByRole('link', { name: /下载原件/ })).toBeTruthy()
  })

  it('没有 locator 时不发 locate 请求', async () => {
    renderViewer({})
    await waitFor(() => expect(fileToken).toHaveBeenCalled())
    expect(locate).not.toHaveBeenCalled()
  })
})
