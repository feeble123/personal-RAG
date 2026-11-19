/**
 * 原 PDF 阅读器：在问答引用 / 本体证据里点「看原文」时打开。
 *
 * 为什么不用「下载下来自己开」：切片只能看到一段，没有上下文就没法判断
 * 这段到底是不是答案。能看到整页、能上下滑、能跳到那句话，才谈得上核对。
 *
 * 三个实现要点：
 * - **虚拟化**：384 页的书不能一次全渲染，用 react-window 只画可见的几页；
 * - **按需拉取**：后端支持 Range（206），大书不必先下完 63MB；
 * - **高亮浮层**：矩形来自后端 `/locate`（PDF 点坐标），按渲染宽度等比缩放。
 *   不用 pdf.js 自己的文本层着色——跨行引用在逐 item 回调里维护游标太脆，
 *   而且扫描件根本没有文本层，浮层方案两种文档共用一套。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Modal, Space, Spin, Tag, Tooltip } from 'antd'
import { DownloadOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { Document, Page, pdfjs } from 'react-pdf'
import { VariableSizeList } from 'react-window'
import workerSrc from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import 'react-pdf/dist/Page/TextLayer.css'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import { errMsg } from '@/api/client'
import OfficeView from '@/components/origin/OfficeView'
import OriginToolbar from '@/components/origin/OriginToolbar'
import { sourceApi } from '@/api/modules'
import { isWholePageBox, scaleRects, type OverlayBox } from '@/utils/pageRects'

pdfjs.GlobalWorkerOptions.workerSrc = workerSrc

//: 默认渲染宽度（CSS 像素）。A4 正文在 760~860 之间最舒服。
const DEFAULT_WIDTH = 820
const MIN_WIDTH = 560
const MAX_WIDTH = 1400
const STEP = 120
const PAGE_GAP = 16
//: 页面长宽比的兜底值（A4）。真实比例在第一页渲染出来后会被替换。
const FALLBACK_RATIO = 1.4142

interface Props {
  open: boolean
  docId: number | null
  /** 目标页码（PDF 物理页，1 起） */
  page?: number | null
  /** 要高亮的引用；有就调 /locate 精确定位 */
  quote?: string | null
  /** 给切片 id 就由后端自己取页码与块坐标 */
  chunkId?: number | null
  onClose: () => void
}

/** 观测容器尺寸：react-window 要的是像素数，不能吃 vh。 */
function useElementSize<T extends HTMLElement>() {
  const ref = useRef<T | null>(null)
  const [size, setSize] = useState({ width: 0, height: 0 })
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const update = () => setSize({ width: el.clientWidth, height: el.clientHeight })
    update()
    const observer = new ResizeObserver(update)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])
  return [ref, size] as const
}

interface RowProps {
  index: number
  style: React.CSSProperties
  pageNumber: number
  width: number
  boxes: OverlayBox[]
  height: number
  /** 页面真正渲染出来后回报实际宽高比，用于修正行高 */
  onMeasure?: (pageNumber: number, ratio: number) => void
}

/** 单页：Page 本身渲染 canvas + 可选文本层，高亮框用绝对定位浮在上面。 */
function PageRow({ style, pageNumber, width, boxes, height, onMeasure }: RowProps) {
  // 加载占位用**整页大小的白纸**而不是一小块透明/白条：
  // 原来 Page 加载时只占 200px，下面露出灰色容器，翻页时就会"闪一下灰"。
  const pageHeight = Math.max(120, height)
  return (
    <div style={style} data-page={pageNumber}>
      <div
        style={{
          position: 'relative',
          width,
          margin: '0 auto',
          boxShadow: '0 2px 12px rgba(0,0,0,0.16)',
          background: '#fff',
        }}
      >
        <Page
          pageNumber={pageNumber}
          width={width}
          renderTextLayer
          renderAnnotationLayer={false}
          loading={<div style={{ width, height: pageHeight, background: '#fff' }} />}
          onLoadSuccess={(proxy: { view?: number[] }) => {
            // 各页高度并不完全一致（插图页、折页），一律用估算值会让几百页的
            // 累计误差越滚越大 —— 实测第 348 页会停在 342 页。
            const r = proxy.view?.[3] && proxy.view?.[2] ? proxy.view[3] / proxy.view[2] : 0
            if (r > 0) onMeasure?.(pageNumber, r)
          }}
        />
        {boxes.map((box, i) => (
          <div
            key={i}
            style={{
              position: 'absolute',
              left: box.left,
              top: box.top,
              width: box.width,
              height: box.height,
              background: 'rgba(255, 205, 60, 0.35)',
              outline: '1.5px solid rgba(230, 140, 0, 0.9)',
              borderRadius: 2,
              pointerEvents: 'none',
              mixBlendMode: 'multiply',
            }}
          />
        ))}
      </div>
    </div>
  )
}

export default function PdfOriginViewer({ open, docId, page, quote, chunkId, onClose }: Props) {
  const [width, setWidth] = useState(DEFAULT_WIDTH)
  const [current, setCurrent] = useState(page ?? 1)
  const [numPages, setNumPages] = useState(0)
  const [ratio, setRatio] = useState(FALLBACK_RATIO)
  const [tokenNonce, setTokenNonce] = useState(0)
  const [docError, setDocError] = useState<string | null>(null)
  const listRef = useRef<VariableSizeList | null>(null)
  //: pdf.js 的文档代理，用来预热相邻页
  const pdfRef = useRef<{
    numPages: number
    getPage: (n: number) => Promise<{ view: number[]; getOperatorList: () => Promise<unknown> }>
  } | null>(null)
  //: 每页真实宽高比。**加载时一次性量全**（547 页只要 32ms），
  //: 用它算行高，远距离跳页才不会因为估算误差滚歪。
  const ratios = useRef<Map<number, number>>(new Map())
  //: react-window 的外层滚动容器，滚动校正要用它的 scrollTop
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  //: 加载失败后只自动重试一次，避免和"文件真的坏了"互相拉扯
  const autoRetried = useRef(false)
  const [boxRef, size] = useElementSize<HTMLDivElement>()

  // 签名 URL **整个会话内不许变**。这是踩出来的：原来 staleTime:0 + React Query
  // 默认「窗口聚焦就重取」，用户切窗口再回来 → 拿到新 token → url 变了 →
  // pdf.js 认为换了文档，销毁重载；65MB 扫描件重载期间整片空白，重载失败就
  // 只剩一句"PDF 加载失败"。只有点「重新加载」（nonce 变）才该换链接。
  const token = useQuery({
    queryKey: ['source-token', docId, tokenNonce],
    queryFn: () => sourceApi.fileToken(docId as number),
    enabled: open && Boolean(docId),
    staleTime: Infinity,
    // 关掉阅读器就丢缓存：下次打开换一份新链接（30 分钟前签发的链接可能已过期），
    // 但**打开的这段时间里**绝不再换——换一次就等于让 pdf.js 整本重载。
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  })

  // 再把 url 钉死一层：即便 query 因为别的原因重取了，也不让 pdf.js 看到新值
  const [fileUrl, setFileUrl] = useState<string | null>(null)
  const urlNonce = useRef(-1)
  useEffect(() => {
    if (!token.data) return
    if (urlNonce.current === tokenNonce) return
    urlNonce.current = tokenNonce
    setFileUrl(token.data.url)
  }, [token.data, tokenNonce])

  // 只有 PDF 能"翻到第 N 页"。docx/xlsx/md 没有页的概念，硬塞给 pdf.js
  // 只会得到一句 "Invalid PDF structure."（用户实测踩过）。
  const fileType = token.data?.file_type ?? ''
  const isPdf = fileType === 'pdf'

  // 签名藏在 file-token 给的 url 里；locate / sheets 复用它，前端不必另持凭证
  const accessToken = useMemo(() => {
    if (!token.data) return null
    try {
      return new URL(token.data.url, window.location.origin).searchParams.get('t')
    } catch {
      return null
    }
  }, [token.data])

  // 只有 quote 没有页码时定位不了（后端要 page 或 chunk_id），别白发一次 400
  const hasLocator = Boolean(chunkId) || page != null
  const locate = useQuery({
    queryKey: ['source-locate', docId, chunkId, page, quote],
    queryFn: () =>
      sourceApi.locate(docId as number, {
        t: accessToken as string,
        chunk_id: chunkId ?? undefined,
        quote: chunkId ? undefined : quote ?? undefined,
        page: chunkId ? undefined : page ?? undefined,
      }),
    enabled: open && Boolean(docId) && hasLocator && isPdf && Boolean(accessToken),
    staleTime: 60_000,
    retry: false,
  })

  const targetPage = locate.data?.page ?? page ?? 1
  const pageWidthPt = locate.data?.page_size.width ?? 0

  // 只在**换文档**时清空页数。原来这里依赖了 targetPage，而 targetPage 要等
  // /locate 返回才定下来——于是"文档已经加载好了、locate 随后返回"会把
  // numPages 清零，整片页列表瞬间消失，看起来就是黑屏。
  const initialPage = useRef(page ?? 1)
  initialPage.current = page ?? 1
  useEffect(() => {
    if (!open) return
    setNumPages(0)
    setCurrent(initialPage.current)
  }, [open, docId])

  // options **必须 memo**：react-pdf 会比对它的引用，每次渲染新建一个对象
  // 就等于告诉它"文档要换"，它会重新加载整本书（实测 73MB 被反复拉了 3 次）。
  const pdfOptions = useMemo(
    () => ({
      withCredentials: false,
      // 65MB+ 的扫描件如果开着自动预取，会把整本在后台拉完，
      // 单页渲染被挤在后面，表现就是"翻着翻着不动了"。
      disableAutoFetch: true,
      rangeChunkSize: 262_144,
    }),
    [],
  )

  const boxes = useMemo(
    () => scaleRects(locate.data?.rects, pageWidthPt, width),
    [locate.data?.rects, pageWidthPt, width],
  )
  const pageHeightPx = width * ratio
  const hideBoxes = boxes.some((b) => isWholePageBox(b, pageHeightPx))

  const rowHeight = useCallback(
    (index: number) =>
      Math.round(width * (ratios.current.get(index + 1) ?? ratio)) +
      (index === numPages - 1 ? 0 : PAGE_GAP),
    [width, ratio, numPages],
  )

  /** 页面渲染出来后回报真实比例（极少数页与整本不同，见 books 里的折页/插图） */
  const onMeasure = useCallback((pageNumber: number, r: number) => {
    const prev = ratios.current.get(pageNumber)
    if (prev != null && Math.abs(prev - r) < 0.002) return
    ratios.current.set(pageNumber, r)
    listRef.current?.resetAfterIndex(pageNumber - 1)
  }, [])

  /**
   * 滚到指定页并**校正落点**。
   *
   * `scrollToItem` 只能按当前已知的高度估算偏移；远距离跳转时前面几百页的
   * 误差会累计（实测点 348 页停在 342 页）。目标行渲染出来后用它的
   * offsetTop 再对一次，最多来回 4 次。
   */
  const settleTo = useCallback((pageNumber: number) => {
    let tries = 0
    const tick = () => {
      const scroller = scrollerRef.current
      const row = scroller?.querySelector(`[data-page="${pageNumber}"]`) as HTMLElement | null
      if (scroller && row) {
        const target = row.offsetTop
        if (Math.abs(target - scroller.scrollTop) <= 4 || tries >= 4) return
        scroller.scrollTop = target
        tries += 1
      } else if (tries >= 4) {
        return
      }
      tries += 1
      window.setTimeout(tick, 80)
    }
    window.setTimeout(tick, 40)
  }, [])


  // 宽度变了：目录项尺寸跟着变，必须让 react-window 重算
  useEffect(() => {
    listRef.current?.resetAfterIndex(0)
  }, [width, ratio])

  // 预热当前页前后各两页：只预取页面数据与绘图指令，不画 canvas。
  // 不预热的话，点"下一页"要等 pdf.js 现取再画，中间那一下空白就是用户看到的"闪"。
  useEffect(() => {
    const pdf = pdfRef.current
    if (!pdf || !numPages) return
    for (const n of [current - 2, current - 1, current + 1, current + 2]) {
      if (n < 1 || n > numPages) continue
      pdf
        .getPage(n)
        .then((p: any) => p.getOperatorList())
        .catch(() => undefined)
    }
  }, [current, numPages])

  /** 滚到某页：先按当前行高跳，再由 settleTo 用真实 DOM 位置校正 */
  const scrollToPage = useCallback(
    (target: number, knownPages?: number) => {
      const total = knownPages ?? numPages
      if (!total) return
      const next = Math.min(Math.max(Math.round(target), 1), total)
      setCurrent(next)
      listRef.current?.scrollToItem(next - 1, 'start')
      settleTo(next)
    },
    [numPages, settleTo],
  )

  const jumpTo = (target: number) => scrollToPage(target)

  // 页数定了、或目标页后到，滚动过去（不碰 numPages）
  useEffect(() => {
    if (!numPages) return
    scrollToPage(targetPage)
  }, [numPages, targetPage, scrollToPage])

  const zoom = (delta: number) =>
    setWidth((w) => Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, w + delta)))

  const title = token.data?.filename ?? '原文'
  const fileLabel = fileType ? fileType.toUpperCase() : ''

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width="92vw"
      style={{ top: 16, maxWidth: 1400 }}
      destroyOnHidden
      title={
        <Space size={8} wrap>
          <span>{title}</span>
          {fileType && !isPdf && <Tag color="blue">{fileLabel}</Tag>}
          {!isPdf && token.data && (
            <Button
              size="small"
              icon={<DownloadOutlined />}
              href={token.data.url}
              download={token.data.filename}
            >
              下载原件
            </Button>
          )}
          {/*
        locator 三态必须让用户看见：扫描件的框只到"块"，跟精确到"行"不是一回事，
        不说清楚用户会以为框错了。
      */}
          {locate.data?.source === 'text' && <Tag color="success">已定位到原文那一行</Tag>}
          {locate.data?.source === 'bbox' && (
            <Tooltip title={locate.data.note ?? ''}>
              <Tag color="warning">扫描件：框出的是整块区域</Tag>
            </Tooltip>
          )}
          {locate.data?.source === 'none' && <Tag>未能定位到具体位置</Tag>}
          {locate.data?.printed_page != null && <Tag>书内第 {locate.data.printed_page} 页</Tag>}
        </Space>
      }
    >
      {isPdf && (
        <OriginToolbar
          pages={numPages}
          current={current}
          onGoto={jumpTo}
          onZoom={(d) => zoom(d > 0 ? STEP : -STEP)}
          onJump={() => jumpTo(targetPage)}
          canJump={Boolean(numPages) && hasLocator}
          onReload={() => {
            autoRetried.current = true // 手动重试不算自动重试，别再触发一次
            setDocError(null)
            setTokenNonce((n) => n + 1)
          }}
        />
      )}

      {token.error != null && (
        <Alert type="error" showIcon message="打不开原文件" description={errMsg(token.error)} />
      )}
      {docError && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 8 }}
          message={autoRetried.current ? '这一份链接加载失败，已自动换链接重试' : '加载失败'}
          description={docError}
        />
      )}
      {locate.data?.source === 'none' && locate.data.note && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 8 }}
          message="没有在原文里框出位置"
          description={locate.data.note}
        />
      )}
      {hideBoxes && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 8 }}
          message="这段话在原文件里占据了接近整页，已隐藏色块以免遮住正文"
          description="按上面的「跳到高亮」可以滚到那一页；扫描件只能定位到块，做不到精确到行。"
        />
      )}

      <div
        ref={boxRef}
        style={{
          height: '76vh',
          overflow: 'hidden',
          background: 'rgba(0,0,0,0.04)',
          borderRadius: 6,
        }}
      >
        {token.data && !isPdf && (
          <OfficeView
            docId={token.data.doc_id}
            accessToken={accessToken ?? ''}
            url={token.data.url}
            filename={token.data.filename}
            fileType={token.data.file_type}
            quote={quote ?? null}
          />
        )}
        {token.isFetching && (
          <div style={{ paddingTop: 80, textAlign: 'center' }}>
            <Spin />
            <div style={{ marginTop: 10, color: '#888' }}>正在获取原文件…</div>
          </div>
        )}
        {isPdf && fileUrl && size.height > 0 && (
          <Document
            key={`${docId}-${tokenNonce}`}
            file={fileUrl}
            onLoadSuccess={(pdf) => {
              pdfRef.current = pdf
              setNumPages(pdf.numPages)
              setDocError(null)
              autoRetried.current = false
              // 一次性量出**所有页**的宽高比。实测 547 页只要 32ms（pdf.js 内部
              // 已缓存页面树），有了真值，远距离跳页就不会被估算误差带偏。
              // 之前只用目标页的比例当全书比例，恰好那页是特殊尺寸，整本被估短，
              // 点 348 页会停在 342 页。
              void (async () => {
                try {
                  const all = new Map<number, number>()
                  let common = 0
                  let commonCount = 0
                  const tally = new Map<number, number>()
                  for (let n = 1; n <= pdf.numPages; n++) {
                    const p = await pdf.getPage(n)
                    const r = p.view[3] / p.view[2]
                    if (!Number.isFinite(r) || r <= 0) continue
                    all.set(n, r)
                    const key = Math.round(r * 100)
                    const c = (tally.get(key) ?? 0) + 1
                    tally.set(key, c)
                    if (c > commonCount) {
                      commonCount = c
                      common = key / 100
                    }
                  }
                  ratios.current = all
                  if (common > 0) setRatio(common)
                  listRef.current?.resetAfterIndex(0)
                  // 行高变了但 scrollTop 没变，落点会整体前移（实测偏 4~6 页）。
                  // 这里按量好的行高重新定位一次。
                  window.setTimeout(() => scrollToPage(targetPage, pdf.numPages), 0)
                } catch {
                  /* 量不出来就继续用估算值，不影响能看 */
                }
              })()
            }}
            onLoadError={(error) => {
              // 加载失败**不销毁页列表**：清空 numPages 会立刻变成整片空白，
              // 用户以为"什么都没有了"。保留现场 + 顶部说明 + 自动换一份链接重试一次。
              setDocError(error?.message || '未知错误')
              if (!autoRetried.current) {
                autoRetried.current = true
                window.setTimeout(() => setTokenNonce((n) => n + 1), 800)
              }
            }}
            loading={
              <div style={{ paddingTop: 80, textAlign: 'center' }}>
                <Spin />
                <div style={{ marginTop: 10, color: '#888' }}>正在读取 PDF…</div>
              </div>
            }
            error={
              <Alert
                type="error"
                showIcon
                style={{ margin: 16 }}
                message="PDF 加载失败"
                description="已经自动换过一份链接；仍失败就点上面的「重新加载」。大文件首次打开需要几秒。"
              />
            }
            options={pdfOptions}
          >
            {numPages > 0 && (
              <VariableSizeList
                ref={listRef}
                outerRef={scrollerRef}
                height={size.height}
                width={size.width}
                itemCount={numPages}
                itemSize={rowHeight}
                overscanCount={4}
                onItemsRendered={({ visibleStartIndex }) => setCurrent(visibleStartIndex + 1)}
              >
                {(props) => (
                  <PageRow
                    {...props}
                    pageNumber={props.index + 1}
                    width={width}
                    height={Math.round(width * ratio)}
                    onMeasure={onMeasure}
                    boxes={props.index + 1 === targetPage && !hideBoxes ? boxes : []}
                  />
                )}
              </VariableSizeList>
            )}
          </Document>
        )}
      </div>
    </Modal>
  )
}
