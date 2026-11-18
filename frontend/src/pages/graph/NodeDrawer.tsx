/**
 * 点开一个节点后的右侧详情面板：属性、证据、邻居。
 *
 * 证据要点开时再取：`/graph` 返回的节点只带 `evidence_count`（列表型数据不进节点标量），
 * 直接读 `props.evidence` 会永远显示「依据 0」——用户实测踩过这个坑。
 */
import { useState } from 'react'
import { Drawer, Descriptions, Empty, List, Space, Spin, Tag, Tooltip, Typography } from 'antd'
import { useQuery } from '@tanstack/react-query'
import EvidenceChunkModal from '@/components/EvidenceChunkModal'
import { ontologyApi } from '@/api/modules'
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

/** 证据状态标签：有 chunk_id 的能点开看原文（见下面的 onClick）。 */
function evidenceTag(item: OntologyEvidence) {
  if (item.chunk_id) {
    return (
      <Tooltip title="点这一条查看原文片段">
        <Tag color="success" style={{ cursor: 'pointer' }}>
          查看原文
        </Tag>
      </Tooltip>
    )
  }
  if (item.doc_id) return <Tag color="warning">仅到文档</Tag>
  return <Tag>仅说明</Tag>
}

export default function NodeDrawer({ node, color, relationLabels, typeLabels, neighbors, onClose, onPick }: Props) {
  // 只有「本体实例」来源的节点才有证据可取；图库节点（graph）没有实例行
  const canFetch = Boolean(node?.uid) && node?.source !== 'graph'
  const detail = useQuery({
    queryKey: ['ontology-instance-uid', node?.uid],
    queryFn: () => ontologyApi.instanceByUid(node!.uid),
    enabled: canFetch,
    staleTime: 30_000,
    retry: false,
  })

  const evidence: OntologyEvidence[] =
    detail.data?.evidence ?? (node?.props?.evidence as OntologyEvidence[] | undefined) ?? []
  const displayProps = Object.entries(node?.props ?? {}).filter(([key]) => key !== 'evidence')

  // 点依据开原文：之前这里只画了一个「可点回原文」的标签，压根没接点击事件
  const [preview, setPreview] = useState<{ chunkId: number; quote?: string | null } | null>(null)

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
            <Text strong>依据（{detail.isFetching ? '…' : evidence.length}）</Text>
            {detail.isFetching ? (
              <div style={{ marginTop: 8 }}>
                <Spin size="small" />
              </div>
            ) : evidence.length === 0 ? (
              <Paragraph type="secondary" style={{ marginTop: 8 }}>
                这条实例还没有依据。到「本体管理 → 实例」里编辑它就能补上。
              </Paragraph>
            ) : (
              <List
                size="small"
                style={{ marginTop: 8 }}
                dataSource={evidence}
                renderItem={(item) => (
                  <List.Item
                    style={item.chunk_id ? { cursor: 'pointer' } : undefined}
                    onClick={() => {
                      if (item.chunk_id) {
                        setPreview({ chunkId: Number(item.chunk_id), quote: item.quote })
                      }
                    }}
                  >
                    <Space direction="vertical" size={2} style={{ width: '100%' }}>
                      <Space>
                        {evidenceTag(item)}
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {item.doc_name || item.type || '—'}
                          {/* 书内印刷页码优先（用户拿书翻的是它） */}
                          {item.printed_page ?? item.page
                            ? ` · 第${item.printed_page ?? item.page}页`
                            : ''}
                        </Text>
                      </Space>
                      {/* 锚定证据的说明是固定一句，真正有信息量的是逐字引用 */}
                      <Text style={{ fontSize: 13 }}>{item.quote || item.note || item.snippet || '—'}</Text>
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
                还没有和它相连的关系。到「本体管理 → 关系」里可以连。
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

      <EvidenceChunkModal
        open={Boolean(preview)}
        chunkId={preview?.chunkId ?? null}
        quote={preview?.quote}
        onClose={() => setPreview(null)}
      />
    </Drawer>
  )
}
