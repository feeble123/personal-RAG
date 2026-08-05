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
          icon={<FileTextOutlined />}
          onClick={() => setOpen(true)}
          style={{ fontSize: 12 }}
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
            {citation.page && <Tag>第 {citation.page} 页</Tag>}
            {citation.score != null && <Tag color="green">相关度 {citation.score.toFixed(3)}</Tag>}
            <Tag>排名 #{citation.rank}</Tag>
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
