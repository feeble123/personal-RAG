/**
 * 检索预览 Tab（G6-1 从 KnowledgeBase.tsx 抽出）。
 *
 * 输入关键词 → 调 /api/admin/search 验证检索质量；自己的查询状态自持，
 * 父组件只传当前知识库 id。
 */
import { useState } from 'react'
import { Button, Empty, Input, List, Space, Tag, Typography } from 'antd'
import { SearchOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { kbApi } from '@/api/modules'
import type { Citation } from '@/api/types'

export default function SearchPreview({ kbId }: { kbId: number | null }) {
  const [searchQ, setSearchQ] = useState('')
  const searchQuery = useQuery<{ hits: Citation[] }>({
    queryKey: ['kb-search', kbId, searchQ],
    queryFn: () => kbApi.search({ q: searchQ, kb_id: kbId ?? undefined, top_k: 5 }),
    enabled: searchQ.trim().length > 0 && !!kbId,
  })

  return (
    <div style={{ maxWidth: 720 }}>
      <Space.Compact style={{ width: '100%', marginBottom: 16 }}>
        <Input
          value={searchQ}
          onChange={(e) => setSearchQ(e.target.value)}
          onPressEnter={() => searchQuery.refetch()}
          placeholder="输入关键词验证检索质量（如：明渠均匀流）"
        />
        <Button type="primary" icon={<SearchOutlined />} onClick={() => searchQuery.refetch()}>
          检索
        </Button>
      </Space.Compact>
      {searchQuery.data?.hits?.length ? (
        <List
          dataSource={searchQuery.data.hits}
          renderItem={(h) => (
            <List.Item>
              <div style={{ width: '100%' }}>
                <Space wrap style={{ marginBottom: 4 }}>
                  <Tag color="blue">#{h.rank}</Tag>
                  <Typography.Text strong>{h.source}</Typography.Text>
                  {h.page && <Tag>第{h.page}页</Tag>}
                  <Tag>相关度 {h.score?.toFixed(3)}</Tag>
                </Space>
                {h.section && (
                  <Typography.Paragraph type="secondary" style={{ margin: 0, fontSize: 12 }}>
                    章节：{h.section}
                  </Typography.Paragraph>
                )}
                <div className="citation-snippet" style={{ marginTop: 4 }}>
                  {h.snippet}
                </div>
              </div>
            </List.Item>
          )}
        />
      ) : searchQ ? (
        <Empty description="无检索结果" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <Typography.Text type="secondary">输入关键词进行检索验证</Typography.Text>
      )}
    </div>
  )
}
