/**
 * 批量上传进度面板（G6-1 从 KnowledgeBase.tsx 抽出）。
 *
 * 每个文件一条：文件名 + 进度条/错误 + 成败标签；全部完成后可清空。
 */
import { Button, Card, Progress, Space, Tag, Typography } from 'antd'
import type { UploadTask } from './documentMeta'

interface Props {
  tasks: UploadTask[]
  uploading: boolean
  onClear: () => void
}

export default function UploadProgress({ tasks, uploading, onClear }: Props) {
  if (tasks.length === 0) return null
  return (
    <Card
      size="small"
      title={
        <Space>
          <span>上传进度</span>
          {uploading ? <Tag color="processing">上传中</Tag> : <Tag>已完成</Tag>}
        </Space>
      }
      extra={
        !uploading && (
          <Button size="small" type="text" onClick={onClear}>
            清空
          </Button>
        )
      }
      style={{ marginBottom: 12, background: 'rgba(10, 17, 34, 0.4)' }}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {tasks.map((t) => (
          <div key={t.key} style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <div style={{ width: 220, minWidth: 0 }}>
              <Typography.Text ellipsis style={{ display: 'block', fontSize: 13 }} title={t.filename}>
                {t.filename}
              </Typography.Text>
            </div>
            <div style={{ flex: 1 }}>
              {t.status === 'error' ? (
                <Typography.Text type="danger" style={{ fontSize: 12 }}>
                  {t.error || '上传失败'}
                </Typography.Text>
              ) : (
                <Progress percent={t.pct} size="small" status={t.status === 'done' ? 'success' : 'active'} />
              )}
            </div>
            {t.status === 'error' ? (
              <Tag color="error">失败</Tag>
            ) : t.status === 'done' ? (
              <Tag color="success">完成</Tag>
            ) : null}
          </div>
        ))}
      </div>
    </Card>
  )
}
