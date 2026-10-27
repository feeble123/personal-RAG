/**
 * 左侧知识库列表（G6-1 从 KnowledgeBase.tsx 抽出）。
 *
 * 只负责展示与触发回调；选中/新建/编辑/删除的真实逻辑在父组件。
 */
import { Button, Card, List, Popconfirm, Space, Tooltip, Typography } from 'antd'
import { DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons'
import type { KnowledgeBase } from '@/api/types'

interface Props {
  kbs: KnowledgeBase[]
  activeKb: number | null
  onSelect: (id: number) => void
  onCreate: () => void
  onEdit: (kb: KnowledgeBase) => void
  onDelete: (id: number) => void
}

export default function KbList({ kbs, activeKb, onSelect, onCreate, onEdit, onDelete }: Props) {
  return (
    <Card size="small" title="知识库" style={{ width: 280, flexShrink: 0 }} styles={{ body: { padding: 8 } }}>
      <Space direction="vertical" style={{ width: '100%' }}>
        <Button type="primary" block icon={<PlusOutlined />} onClick={onCreate}>
          新建知识库
        </Button>
        <List
          dataSource={kbs}
          style={{ maxHeight: 'calc(100vh - 260px)', overflowY: 'auto' }}
          renderItem={(kb) => (
            <div
              onClick={() => onSelect(kb.id)}
              style={{
                padding: '8px 10px',
                borderRadius: 8,
                cursor: 'pointer',
                marginBottom: 4,
                background: activeKb === kb.id ? 'rgba(0, 198, 255, 0.16)' : 'transparent',
                border: activeKb === kb.id ? '1px solid rgba(0, 198, 255, 0.3)' : '1px solid transparent',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
              }}
            >
              <div style={{ minWidth: 0 }}>
                <Typography.Text ellipsis style={{ display: 'block', fontSize: 13 }}>{kb.name}</Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                  {kb.doc_count} 文档 · {kb.chunk_count} chunks
                </Typography.Text>
              </div>
              <Space size={0}>
                <Tooltip title="编辑">
                  <Button
                    size="small"
                    type="text"
                    icon={<EditOutlined />}
                    onClick={(e) => {
                      e.stopPropagation()
                      onEdit(kb)
                    }}
                  />
                </Tooltip>
                <Tooltip title="删除">
                  <Popconfirm title="删除该知识库及其全部文档？" okText="删除" cancelText="取消" onConfirm={() => onDelete(kb.id)}>
                    <Button size="small" type="text" danger icon={<DeleteOutlined />} onClick={(e) => e.stopPropagation()} />
                  </Popconfirm>
                </Tooltip>
              </Space>
            </div>
          )}
        />
      </Space>
    </Card>
  )
}
