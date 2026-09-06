import { FileTextOutlined } from '@ant-design/icons'
import type { Citation } from '@/api/types'

// 底部来源清单（借鉴 Agent 组件库 InlineCitations 的 citeFooter 视觉，改透明青蓝）。
// 每条 = 角标编号 + 来源文件名 + 页码 + 相关度，点击弹出引用详情。
interface Props {
  citations: Citation[]
  onOpen: (index: number) => void
}

export default function InlineCitations({ citations, onOpen }: Props) {
  if (citations.length === 0) return null
  return (
    <div className="cite-list" style={{ marginTop: 8 }}>
      {citations.map((c, i) => (
        <button
          key={c.chunk_id ?? `cite-${i}`}
          className="cite-ref"
          onClick={() => onOpen(i)}
          aria-label={`查看引用来源 ${i + 1}`}
        >
          <span className="cite-mark cite-mark-static">{i + 1}</span>
          <FileTextOutlined style={{ color: '#00c6ff', fontSize: 12, flexShrink: 0 }} />
          <span className="cite-ref-label">{c.source}</span>
          <span className="cite-sep">·</span>
          <span className="cite-ref-meta">
            {c.page ? `第 ${c.page} 页` : '未标页码'}
            {c.score != null ? ` · ${(c.score * 100).toFixed(0)}%` : ''}
          </span>
          <span className="cite-arrow" aria-hidden>
            ↗
          </span>
        </button>
      ))}
    </div>
  )
}
