/**
 * Word / Excel / 文本的「原文」视图。
 *
 * 为什么不做成"下载下来自己开"：用户要的是**在上下文里核对**，为了看一段话去
 * 下载文件、找位置，等于把麻烦推回去。所以：
 * - **docx**：浏览器里按原版式排版（docx-preview，标题/表格/图片都在），
 *   再把切片那段话标出来并滚过去；
 * - **xlsx**：后端读成网格（openpyxl 本来就有），前端画可滚动表格，
 *   高亮命中的那一行；
 * - **md / txt**：直接读文本渲染。
 *
 * 这一层和 PDF 阅读器共用同一套工具条（翻页/缩放/跳到高亮/重新加载）。
 * Word 排版后确实有"页"，所以翻页是真功能；Excel 没有页，就不显示页码。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Spin, Tabs } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { errMsg } from '@/api/client'
import { sourceApi } from '@/api/modules'
import { markQuote } from '@/utils/findInDom'
import { findQuoteRange } from '@/utils/quoteHighlight'
import { findRowMatch } from '@/utils/sheetLocate'
import OriginToolbar from '@/components/origin/OriginToolbar'

export interface OfficeViewProps {
  docId: number
  /** 与 /file 同一份签名，表格数据接口复用它 */
  accessToken: string
  /** 签名 URL（拉字节用；不需要带 Authorization 头） */
  url: string
  filename: string
  fileType: string
  quote?: string | null
}

// 全局是深色主题（body 文字接近白色），而这几块是"白纸"——
// 不显式给深色文字，表格就是白字白底，用户什么也看不见。
const BOX: React.CSSProperties = {
  height: 'calc(76vh - 44px)',
  overflow: 'auto',
  background: '#fff',
  color: '#1f1f1f',
  borderRadius: 6,
  padding: 16,
}

const ZOOM_MIN = 0.6
const ZOOM_MAX = 2
const ZOOM_STEP = 0.15

/** 高亮结果的就实度：整段命中 / 只找到其中一段 / 没找到 */
type MatchLevel = 'exact' | 'partial' | 'none'

/**
 * 把元素滚到容器中间，**只动容器自己的 scrollTop**。
 *
 * 用 `el.scrollIntoView()` 会把整条祖先链都滚一遍——实测它把 antd 弹窗整体
 * 往上顶，工具栏和标题直接滚出可视区，用户只看到正文。
 */
function scrollIntoBox(box: HTMLElement | null, el: HTMLElement | null, align: 'center' | 'start' = 'center') {
  if (!box || !el) return
  const delta = el.getBoundingClientRect().top - box.getBoundingClientRect().top
  const offset = align === 'start' ? 0 : Math.max(0, box.clientHeight / 3)
  box.scrollTop += delta - offset
}

async function fetchBytes(url: string): Promise<ArrayBuffer> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`读取原文件失败（HTTP ${res.status}）`)
  return res.arrayBuffer()
}

function MatchHint({ level }: { level: MatchLevel }) {
  if (level === 'partial') {
    return (
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 8 }}
        message="切片和原文没有完全对上，已把切片里能找到的各段分别标出"
        description="切片开头的标题路径是程序合成的（原文里标题常带编号），整段拼不起来；下面显示的是全文。"
      />
    )
  }
  if (level === 'none') {
    return (
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 8 }}
        message="没在原文里找到这段话"
        description="下面显示的是全文，可以按内容自行查找。"
      />
    )
  }
  return null
}

// ---------------- Word ----------------

function DocxView({ url, quote }: OfficeViewProps) {
  const boxRef = useRef<HTMLDivElement | null>(null)
  const hostRef = useRef<HTMLDivElement | null>(null)
  const markRef = useRef<HTMLElement | null>(null)
  const [screens, setScreens] = useState(1)
  const [current, setCurrent] = useState(1)
  const [zoom, setZoom] = useState(1)
  const [nonce, setNonce] = useState(0)
  const [level, setLevel] = useState<MatchLevel>('none')
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setStatus('loading')
    ;(async () => {
      try {
        const [buffer, docx] = await Promise.all([fetchBytes(url), import('docx-preview')])
        const host = hostRef.current
        if (cancelled || !host) return
        host.innerHTML = ''
        await docx.renderAsync(new Uint8Array(buffer), host, undefined, {
          inWrapper: true,
          ignoreWidth: false,
          breakPages: true,
          experimental: true,
        })
        if (cancelled) return
        setStatus('ready')
        if (quote) {
          const hit = markQuote(host, quote)
          markRef.current = hit?.mark ?? null
          setLevel(hit ? (hit.exact ? 'exact' : 'partial') : 'none')
          scrollIntoBox(boxRef.current, hit?.mark ?? null)
        } else {
          setLevel('none')
        }
      } catch (err) {
        if (cancelled) return
        setError(err instanceof Error ? err.message : String(err))
        setStatus('error')
      }
    })()
    return () => {
      cancelled = true
    }
  }, [url, quote, nonce])

  // 缩放：docx-preview 的尺寸是 pt，整体 CSS zoom 最省事
  useEffect(() => {
    if (hostRef.current) hostRef.current.style.zoom = String(zoom)
  }, [zoom, status])

  /**
   * 按**屏幕高度**算"翻一页"。
   *
   * docx-preview 不做真实分页：它只在文档里有显式分页符的地方断页，所以
   * 一本 170 页的方案只能数出 15 个 `<section>`（每个一万多像素高）——
   * 原来拿它当页数，用户点"下一页"直接翻过一整章，还写着"共 15 页"，
   * 是**谎报**。Word 原文件本来就没有页信息，这里如实按屏翻。
   */
  const refreshScreens = useCallback(() => {
    const box = boxRef.current
    if (!box) return
    setScreens(Math.max(1, Math.ceil(box.scrollHeight / Math.max(1, box.clientHeight))))
  }, [])

  useEffect(() => {
    if (status === 'ready') refreshScreens()
  }, [status, zoom, refreshScreens])

  const gotoScreen = useCallback((screen: number) => {
    const box = boxRef.current
    if (!box) return
    const total = Math.max(1, Math.ceil(box.scrollHeight / Math.max(1, box.clientHeight)))
    const next = Math.min(Math.max(Math.round(screen), 1), total)
    box.scrollTop = (next - 1) * box.clientHeight
    setCurrent(next)
  }, [])

  // 滚动时把"当前屏"跟着更新，工具条上的数字才不是死的
  const onScroll = useCallback(() => {
    const box = boxRef.current
    if (!box) return
    setCurrent(Math.floor(box.scrollTop / Math.max(1, box.clientHeight)) + 1)
  }, [])

  return (
    <>
      <OriginToolbar
        pages={screens}
        unit="屏"
        current={current}
        onGoto={gotoScreen}
        onZoom={(d) => setZoom((z) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z + d * ZOOM_STEP)))}
        onJump={() => scrollIntoBox(boxRef.current, markRef.current)}
        canJump={Boolean(markRef.current)}
        onReload={() => setNonce((n) => n + 1)}
      />
      <div ref={boxRef} style={BOX} onScroll={onScroll}>
        {status === 'loading' && (
          <div style={{ textAlign: 'center', paddingTop: 60 }}>
            <Spin />
            <div style={{ marginTop: 10, color: '#888' }}>正在排版 Word 文档…</div>
          </div>
        )}
        {status === 'error' && (
          <Alert
            type="error"
            showIcon
            message="这份 Word 打不开"
            description={`${error}（可以点「重新加载」；仍失败就用右上角的「下载原件」）`}
          />
        )}
        {status === 'ready' && <MatchHint level={level} />}
        {/* color 兜底：Word 文档里没显式设颜色的文本会继承这里 */}
        <div ref={hostRef} className="docx-origin-host" style={{ color: '#1f1f1f' }} />
      </div>
    </>
  )
}

// ---------------- Excel ----------------

function SheetView({ docId, accessToken, quote }: OfficeViewProps) {
  const boxRef = useRef<HTMLDivElement | null>(null)
  const sheets = useQuery({
    queryKey: ['source-sheets', docId, accessToken],
    queryFn: () => sourceApi.sheets(docId, accessToken),
    staleTime: 5 * 60_000,
    retry: false,
  })

  const [active, setActive] = useState<string | null>(null)
  const [zoom, setZoom] = useState(1)
  const rowRef = useRef<HTMLTableRowElement | null>(null)

  const hitSheet = useMemo(() => {
    for (const sheet of sheets.data?.sheets ?? []) {
      if (findRowMatch(sheet.rows, quote)) return sheet.name
    }
    return null
  }, [sheets.data, quote])

  useEffect(() => {
    const first = hitSheet ?? sheets.data?.sheets?.[0]?.name ?? null
    if (first && !active) setActive(first)
  }, [hitSheet, sheets.data, active])

  const current = sheets.data?.sheets.find((s) => s.name === active) ?? sheets.data?.sheets[0]
  const match = useMemo(() => (current ? findRowMatch(current.rows, quote) : null), [current, quote])

  useEffect(() => {
    scrollIntoBox(boxRef.current, rowRef.current)
  }, [match, active])

  if (sheets.error != null) {
    return <Alert type="error" showIcon message="这份 Excel 打不开" description={errMsg(sheets.error)} />
  }
  if (sheets.isFetching || !current) {
    return (
      <div style={{ ...BOX, textAlign: 'center', paddingTop: 60 }}>
        <Spin />
        <div style={{ marginTop: 10, color: '#888' }}>正在读取表格…</div>
      </div>
    )
  }

  const colCount = Math.max(...current.rows.map((r) => r.length), 1)

  return (
    <>
      <OriginToolbar
        onZoom={(d) => setZoom((z) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z + d * ZOOM_STEP)))}
        onJump={() => scrollIntoBox(boxRef.current, rowRef.current)}
        canJump={Boolean(match)}
        onReload={() => sheets.refetch()}
      />
      <div ref={boxRef} style={BOX}>
        {(sheets.data?.sheets.length ?? 0) > 1 && (
          <Tabs
            size="small"
            activeKey={active ?? undefined}
            onChange={setActive}
            items={(sheets.data?.sheets ?? []).map((s) => ({ key: s.name, label: s.name }))}
          />
        )}
        {current.truncated && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 8 }}
            message="表格太大，这里只显示前一部分"
            description="需要完整内容可以点右上角的「下载原件」。"
          />
        )}
        {current.rows.length === 0 && (
          <Alert type="info" showIcon message="这个工作表里没有内容" description="可以按右上角的「下载原件」用 Excel 打开确认。" />
        )}
        <table style={{ borderCollapse: 'collapse', fontSize: 13 * zoom, width: '100%' }}>
          <tbody>
            {current.rows.map((row, r) => {
              const isHit = match?.row === r
              return (
                <tr
                  key={r}
                  ref={isHit ? rowRef : undefined}
                  style={isHit ? { backgroundColor: 'rgba(255,205,60,0.25)' } : undefined}
                >
                  <td
                    style={{
                      color: '#999',
                      textAlign: 'right',
                      padding: '4px 8px',
                      borderBottom: '1px solid #f0f0f0',
                      position: 'sticky',
                      left: 0,
                      backgroundColor: isHit ? '#fff6d8' : '#fff',
                      fontVariantNumeric: 'tabular-nums',
                    }}
                  >
                    {r + 1}
                  </td>
                  {Array.from({ length: colCount }, (_, c) => (
                    <td
                      key={c}
                      style={{
                        padding: '4px 10px',
                        borderBottom: '1px solid #f0f0f0',
                        borderLeft: '1px solid #f0f0f0',
                        whiteSpace: 'pre-wrap',
                        outline:
                          isHit && match.cols.includes(c) ? '2px solid rgba(230,140,0,0.8)' : undefined,
                      }}
                    >
                      {row[c] ?? ''}
                    </td>
                  ))}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </>
  )
}

// ---------------- Markdown / 纯文本 ----------------

function TextView({ url, quote }: OfficeViewProps) {
  const boxRef = useRef<HTMLDivElement | null>(null)
  const text = useQuery({
    queryKey: ['source-text', url],
    queryFn: async () => new TextDecoder('utf-8').decode(await fetchBytes(url)),
    staleTime: 5 * 60_000,
    retry: false,
  })
  const [zoom, setZoom] = useState(1)
  const markRef = useRef<HTMLElement | null>(null)
  const range = useMemo(() => findQuoteRange(text.data ?? '', quote), [text.data, quote])

  useEffect(() => {
    scrollIntoBox(boxRef.current, markRef.current)
  }, [range])

  if (text.error != null) {
    return <Alert type="error" showIcon message="打不开原文件" description={errMsg(text.error)} />
  }
  if (text.isFetching) {
    return (
      <div style={{ ...BOX, textAlign: 'center', paddingTop: 60 }}>
        <Spin />
      </div>
    )
  }
  const content = text.data ?? ''
  return (
    <>
      <OriginToolbar
        onZoom={(d) => setZoom((z) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z + d * ZOOM_STEP)))}
        onJump={() => scrollIntoBox(boxRef.current, markRef.current)}
        canJump={Boolean(range)}
        onReload={() => text.refetch()}
      />
      <div ref={boxRef} style={BOX}>
        {!range && (quote ?? '').trim() && <MatchHint level="none" />}
        <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontSize: 13 * zoom, lineHeight: 1.7 }}>
          {range ? (
            <>
              {content.slice(0, range[0])}
              <mark
                ref={(el) => {
                  markRef.current = el
                }}
                style={{ background: 'rgba(255,205,60,0.45)' }}
              >
                {content.slice(range[0], range[1])}
              </mark>
              {content.slice(range[1])}
            </>
          ) : (
            content
          )}
        </pre>
      </div>
    </>
  )
}

export default function OfficeView(props: OfficeViewProps) {
  const kind = (props.fileType || '').toLowerCase()
  if (kind === 'xlsx' || kind === 'xlsm') return <SheetView {...props} />
  if (kind === 'docx') return <DocxView {...props} />
  return <TextView {...props} />
}
