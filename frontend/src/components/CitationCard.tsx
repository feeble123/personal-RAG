import { useState } from 'react'
import { Button, Modal, Space, Tag, Tooltip, Typography } from 'antd'
import { FileTextOutlined } from '@ant-design/icons'
import type { Citation } from '@/api/types'

// 引用卡片：来源文件 / 页码 / 章节 + 展开原文
export default function CitationCard({ citation, index }: { citation: Citation; index: number }) {
  const [open, setOpen] = useState(false)

  return (
    <>
      <Tooltip title={`来源：${citation.source}${citation.page ? ` · 第${citation.page}页` : ''}`}>
        <Button
          size="small"
          className="citation-pill"
          icon={<FileTextOutlined style={{ color: '#00c6ff' }} />}
          onClick={() => setOpen(true)}
        >
          [{index}] {citation.source}
          {citation.page ? ` · 第${citation.page}页` : ''}
        </Button>
      </Tooltip>
      <Modal
        open={open}
        onCancel={() => setOpen(false)}
        footer={null}
        width={640}
        title={
          <Space>
            <FileTextOutlined />
            引用详情
          </Space>
        }
      >
        <div style={{ marginBottom: 8 }}>
          <Space wrap>
            <Tag color="blue">{citation.source}</Tag>
            {citation.page && <Tag className="font-mono">第 {citation.page} 页</Tag>}
            {citation.score != null && (
              <Tag color="green" className="font-mono">
                相关度 {citation.score.toFixed(3)}
              </Tag>
            )}
            <Tag className="font-mono">排名 #{citation.rank}</Tag>
          </Space>
        </div>
        {citation.section && (
          <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
            章节：{citation.section}
          </Typography.Paragraph>
        )}
        <div className="citation-snippet">{citation.snippet}</div>
      </Modal>
    </>
  )
}
