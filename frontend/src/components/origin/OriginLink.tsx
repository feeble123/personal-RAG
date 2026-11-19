/**
 * 「看原文」按钮 + 按需加载的阅读器。
 *
 * 为什么要抽出来：问答引用、本体证据弹窗、分块浏览、检索预览四处都要这个按钮，
 * 每处各写一遍懒加载 + Suspense + 错误边界，迟早漏掉一处（比如某处忘了包
 * ViewerBoundary，第三方渲染器一崩就把整页拖黑）。
 *
 * 只传 `chunkId` 也能用：阅读器会拿 chunk 去后端换页码与坐标。
 */
import { Suspense, lazy, useState } from 'react'
import { Button, Tooltip } from 'antd'
import { FileSearchOutlined } from '@ant-design/icons'
import ViewerBoundary from '@/components/origin/ViewerBoundary'

// pdf.js / docx-preview 都是 MB 级，只有真点了才加载
const PdfOriginViewer = lazy(() => import('@/components/origin/PdfOriginViewer'))

interface Props {
  /** 文档 id；没有它就无从打开原文 */
  docId?: number | null
  /** PDF 物理页序 */
  page?: number | null
  /** 要高亮的引用/切片正文 */
  quote?: string | null
  /** 切片 id：给了就由后端自己查页码与块坐标 */
  chunkId?: number | null
  /** 按钮文案 */
  text?: string
  size?: 'small' | 'middle'
  type?: 'link' | 'text' | 'default'
}

export default function OriginLink({
  docId,
  page,
  quote,
  chunkId,
  text = '看原文',
  size = 'small',
  type = 'link',
}: Props) {
  const [open, setOpen] = useState(false)
  const disabled = docId == null

  return (
    <>
      <Tooltip title={disabled ? '这条记录没有关联到具体文档' : '在原文件里查看上下文'}>
        <Button
          size={size}
          type={type}
          icon={<FileSearchOutlined />}
          disabled={disabled}
          onClick={() => setOpen(true)}
        >
          {text}
        </Button>
      </Tooltip>
      {open && docId != null && (
        <Suspense fallback={null}>
          <ViewerBoundary>
            <PdfOriginViewer
              open={open}
              docId={docId}
              page={page ?? undefined}
              quote={quote ?? null}
              chunkId={chunkId ?? null}
              onClose={() => setOpen(false)}
            />
          </ViewerBoundary>
        </Suspense>
      )}
    </>
  )
}
