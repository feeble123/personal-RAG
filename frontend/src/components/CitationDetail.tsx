import { Modal, Space, Tag, Typography } from 'antd'
import { FileTextOutlined } from '@ant-design/icons'
import type { Citation } from '@/api/types'

// 引用详情弹窗：来源文件 / 页码 / 章节 / 相关度 / 排名 + 原文片段。
// 从原 CitationCard 提取，供「行内角标」与「底部来源清单」两处复用。
export default function CitationDetail({
  citation,
  open,
  onClose,
}: {
  citation: Citation | null
  open: boolean
  onClose: () => void
}) {
  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={640}
      title={
        <Space>
          <FileTextOutlined />
          引用详情
        </Space>
      }
    >
      {citation && (
        <>
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
        </>
      )}
    </Modal>
  )
}
