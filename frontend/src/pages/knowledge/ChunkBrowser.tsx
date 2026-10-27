/**
 * 文档切片浏览 Tab（G6-1 从 KnowledgeBase.tsx 抽出）。
 *
 * 支持按文档筛选 + 子块/父块/全部切换；父块与子块分开展示，
 * 避免平铺造成「重复切」假象。
 */
import { useState } from 'react'
import { Empty, List, Select, Space, Tag, Typography } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { kbApi } from '@/api/modules'
import type { ChunkItem, DocumentItem } from '@/api/types'

interface Props {
  kbId: number | null
  allDocs: DocumentItem[]
}

export default function ChunkBrowser({ kbId, allDocs }: Props) {
  const [chunkDocId, setChunkDocId] = useState<number | undefined>(undefined)
  const [chunkPage, setChunkPage] = useState(1)
  // 父子块分家显示：默认「子块」；父块（粗粒度、覆盖全书）单独一栏
  const [chunkView, setChunkView] = useState<'child' | 'parent' | 'all'>('child')
  const chunkQuery = useQuery<{
    total: number
    parent_total: number
    child_total: number
    items: ChunkItem[]
  }>({
    queryKey: ['kb-chunks', kbId, chunkDocId, chunkPage, chunkView],
    queryFn: () =>
      kbApi.chunks(kbId!, {
        page: chunkPage,
        page_size: 20,
        doc_id: chunkDocId,
        kind: chunkView,
      }),
    enabled: !!kbId,
  })

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Select
          allowClear
          placeholder="按文档筛选"
          style={{ width: 320 }}
          value={chunkDocId}
          onChange={(v) => {
            setChunkDocId(v)
            setChunkPage(1)
          }}
          options={allDocs.map((d) => ({ value: d.id, label: d.filename }))}
        />
        <Select
          value={chunkView}
          style={{ width: 140 }}
          onChange={(v) => {
            setChunkView(v)
            setChunkPage(1)
          }}
          options={[
            { value: 'child', label: '子块（精确检索）' },
            { value: 'parent', label: '父块（长上下文）' },
            { value: 'all', label: '全部（不分家）' },
          ]}
        />
        <Typography.Text type="secondary">
          父块 {chunkQuery.data?.parent_total ?? 0} · 子块 {chunkQuery.data?.child_total ?? 0}
          （当前显示 {chunkQuery.data?.total ?? 0}）
        </Typography.Text>
      </Space>
      {chunkQuery.data?.items?.length ? (
        <List
          dataSource={chunkQuery.data.items}
          pagination={{
            current: chunkPage,
            pageSize: 20,
            total: chunkQuery.data.total,
            showSizeChanger: false,
            onChange: setChunkPage,
          }}
          renderItem={(c) => (
            <List.Item>
              <div style={{ width: '100%' }}>
                <Space wrap style={{ marginBottom: 4 }}>
                  <Tag>#{c.chunk_index}</Tag>
                  {c.block_type === 'parent' && <Tag color="gold">父块</Tag>}
                  {c.block_type && c.block_type !== 'parent' && <Tag>{c.block_type}</Tag>}
                  {c.page != null && <Tag>第{c.page}页</Tag>}
                  {c.section && <Tag color="blue">{c.section}</Tag>}
                </Space>
                <div className="citation-snippet" style={{ fontSize: 13 }}>
                  {c.content}
                </div>
              </div>
            </List.Item>
          )}
        />
      ) : chunkQuery.isFetched ? (
        <Empty description="无切片（文档可能仍在解析中）" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : null}
    </div>
  )
}
