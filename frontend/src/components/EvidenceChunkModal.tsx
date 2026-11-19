/**
 * 点开一条依据：显示它在知识库里锚定的**那段原文**。
 *
 * 为什么要有这一层：证据里存的 `chunk_id` 只是个数字，用户没法判断
 * 「第 4 页那段」到底写着什么。点开看到原文，才能判断这条依据靠不靠谱。
 *
 * 文档重新解析后切片 id 会变，这时接口返回 404，界面明说"原文已失效"，
 * 而不是悄悄给一段不相干的文字。
 */
import type { ReactNode } from 'react'
import { Alert, Modal, Space, Spin, Tag, Typography } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { errMsg } from '@/api/client'
import { ontologyApi } from '@/api/modules'
import OriginLink from '@/components/origin/OriginLink'
import { findQuoteRange } from '@/utils/quoteHighlight'

const { Text, Paragraph } = Typography

interface Props {
  open: boolean
  chunkId: number | null
  /** 逐字引用：有就在正文里高亮出来 */
  quote?: string | null
  onClose: () => void
}

/** 把引用片段在正文里标出来；找不到就原样返回（不假装高亮成功）。 */
function highlight(content: string, quote?: string | null): ReactNode {
  const range = findQuoteRange(content, quote)
  if (!range) return content
  const [start, end] = range
  return (
    <>
      {content.slice(0, start)}
      <mark style={{ background: 'rgba(255, 214, 102, 0.45)', color: 'inherit' }}>
        {content.slice(start, end)}
      </mark>
      {content.slice(end)}
    </>
  )
}

export default function EvidenceChunkModal({ open, chunkId, quote, onClose }: Props) {
  const { data, isFetching, error } = useQuery({
    queryKey: ['ontology-evidence-chunk', chunkId],
    queryFn: () => ontologyApi.evidenceChunk(chunkId as number),
    enabled: open && Boolean(chunkId),
    staleTime: 60_000,
    retry: false,
  })

  return (
    <Modal open={open} title="依据原文" footer={null} width={760} onCancel={onClose} destroyOnClose>
      {isFetching && <Spin />}
      {error != null && (
        <Alert
          type="warning"
          showIcon
          message="看不到原文"
          description={errMsg(error)}
        />
      )}
      {data && (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Space size={6} wrap>
            <Tag color="blue">{data.doc_name || '未知文档'}</Tag>
            {/* 书里印的页码优先：能按书上页码翻到你手里那本书 */}
            {(data.printed_page ?? data.page) != null && (
              <Tag>第 {data.printed_page ?? data.page} 页</Tag>
            )}
            {data.printed_page != null && data.page != null && data.printed_page !== data.page && (
              <Tag color="default">PDF 第 {data.page} 页</Tag>
            )}
            {data.section && <Tag>{data.section}</Tag>}
            {data.block_type && <Tag color="default">{data.block_type}</Tag>}
            <Text type="secondary" style={{ fontSize: 11 }}>
              切片 #{data.chunk_id}
            </Text>
            {/* 切片只是片段，要看上下文就跳到原文件对应位置 */}
            <OriginLink
              docId={data.doc_id}
              page={data.page}
              chunkId={data.chunk_id}
              quote={quote ?? null}
              text="在原文件里看"
            />
          </Space>
          {(quote ?? '').trim() && !findQuoteRange(data.content, quote) && (
            <Alert
              type="info"
              showIcon
              message="这段引用在切片里没找到原句"
              description="下面显示的是整段原文。可能是切片被重新解析过，或引用是模型概括的（不是逐字抄的）。"
            />
          )}
          <Paragraph
            style={{
              maxHeight: 420,
              overflow: 'auto',
              whiteSpace: 'pre-wrap',
              background: 'rgba(122,190,255,0.06)',
              padding: 12,
              borderRadius: 6,
              marginBottom: 0,
            }}
          >
            {highlight(data.content, quote)}
          </Paragraph>
        </Space>
      )}
    </Modal>
  )
}
