/** 点开一个节点后的右侧详情面板：属性、证据、邻居。 */
import { Drawer, Descriptions, Empty, List, Space, Tag, Typography } from 'antd'
import type { OntologyEvidence } from '@/api/types'
import type { GraphLink, GraphNode } from './canvasTheme'

const { Text, Paragraph } = Typography

interface Props {
  node: GraphNode | null
  color: string
  /** 关系类型 → 中文名 */
  relationLabels: Record<string, string>
  /** 实体类型 → 中文名 */
  typeLabels: Record<string, string>
  neighbors: { node: GraphNode; link: GraphLink; direction: 'out' | 'in' }[]
  onClose: () => void
  onPick: (uid: string) => void
}

function evidenceTag(item: OntologyEvidence) {
  if (item.chunk_id) return <Tag color="success">可点回原文</Tag>
  if (item.doc_id) return <Tag color="warning">仅到文档</Tag>
  return <Tag>仅说明</Tag>
}

export default function NodeDrawer({ node, color, relationLabels, typeLabels, neighbors, onClose, onPick }: Props) {
  const evidence = (node?.props?.evidence as OntologyEvidence[] | undefined) ?? []
  const displayProps = Object.entries(node?.props ?? {}).filter(([key]) => key !== 'evidence')

  return (
    <Drawer
      open={Boolean(node)}
      onClose={onClose}
      width={420}
      title={
        node ? (
          <Space>
            <span
              style={{
                display: 'inline-block',
                width: 10,
                height: 10,
                borderRadius: '50%',
                background: color,
                boxShadow: `0 0 12px ${color}`,
              }}
            />
            <span>{node.display || node.name}</span>
          </Space>
        ) : null
      }
    >
      {node && (
        <Space direction="vertical" size={18} style={{ width: '100%' }}>
          <Descriptions column={1} size="small" bordered>
            <Descriptions.Item label="标识">{node.name}</Descriptions.Item>
            <Descriptions.Item label="类型">
              {typeLabels[node.entity_type] ?? node.entity_type}
            </Descriptions.Item>
            <Descriptions.Item label="uid">
              <Text code copyable style={{ fontSize: 12 }}>
                {node.uid}
              </Text>
            </Descriptions.Item>
            <Descriptions.Item label="来源">
              {node.source === 'instance' ? '本体实例（PG 真相源）' : '图库节点'}
            </Descriptions.Item>
            <Descriptions.Item label="知识库">
              {node.kb_id === 0 ? '全局共享' : node.kb_id}
            </Descriptions.Item>
          </Descriptions>

          <div>
            <Text strong>属性</Text>
            {displayProps.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有属性" />
            ) : (
              <Descriptions column={1} size="small" style={{ marginTop: 8 }}>
                {displayProps.map(([key, value]) => (
                  <Descriptions.Item key={key} label={key}>
                    {typeof value === 'object' ? JSON.stringify(value) : String(value)}
                  </Descriptions.Item>
                ))}
              </Descriptions>
            )}
          </div>

          <div>
            <Text strong>依据（{evidence.length}）</Text>
            {evidence.length === 0 ? (
              <Paragraph type="secondary" style={{ marginTop: 8 }}>
                这条实例没有证据，推荐卡里不会输出它的数值。
              </Paragraph>
            ) : (
              <List
                size="small"
                style={{ marginTop: 8 }}
                dataSource={evidence}
                renderItem={(item) => (
                  <List.Item>
                    <Space direction="vertical" size={2} style={{ width: '100%' }}>
                      <Space>
                        {evidenceTag(item)}
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {item.doc_name || item.type || '—'}
                          {item.page ? ` · 第${item.page}页` : ''}
                        </Text>
                      </Space>
                      <Text style={{ fontSize: 13 }}>{item.note || item.snippet || '—'}</Text>
                    </Space>
                  </List.Item>
                )}
              />
            )}
          </div>

          <div>
            <Text strong>关系（{neighbors.length}）</Text>
            {neighbors.length === 0 ? (
              <Paragraph type="secondary" style={{ marginTop: 8 }}>
                图库里没有与它相连的边。关系维护会在「本体管理」里做。
              </Paragraph>
            ) : (
              <List
                size="small"
                style={{ marginTop: 8 }}
                dataSource={neighbors}
                renderItem={({ node: other, link, direction }) => (
                  <List.Item>
                    <Space>
                      <Tag>{relationLabels[link.predicate] ?? link.predicate}</Tag>
                      <Text type="secondary">{direction === 'out' ? '→' : '←'}</Text>
                      <a onClick={() => onPick(other.uid)}>{other.display || other.name}</a>
                    </Space>
                  </List.Item>
                )}
              />
            )}
          </div>
        </Space>
      )}
    </Drawer>
  )
}
