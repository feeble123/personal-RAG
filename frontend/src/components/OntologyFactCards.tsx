/**
 * 本体事实卡：把「参数先验 / 图谱关系 / 图谱节点」renders 成一小块结构化摘要。
 *
 * 为什么单独展示而不是混进回答正文：本体给的是**确定性事实**（范围、初值、实体关系），
 * 和 LLM 生成的散文是两种东西。分开摆，用户能一眼判断"这句话是有依据的还是模型自己说的"。
 */
import { Collapse, Space, Tag, Typography } from 'antd'
import type { OntologyEvidence, OntologyFactsEvent } from '@/api/types'

const { Text } = Typography

const KIND_LABEL: Record<string, string> = {
  parameter_prior: '参数先验',
  graph_node: '图谱节点',
  graph_edge: '图谱关系',
}

const SOURCE_LABEL: Record<string, string> = {
  ontology: '本体',
  graph: '图谱',
  rag: '知识库',
}

type Fact = Record<string, unknown>

const str = (v: unknown) => (v == null ? '' : String(v))

function EvidenceLines({ items }: { items?: OntologyEvidence[] }) {
  if (!items?.length) return null
  return (
    <Space direction="vertical" size={0} style={{ marginTop: 2 }}>
      {items.map((ev, i) => (
        <Text key={i} type="secondary" style={{ fontSize: 11 }}>
          依据：{ev.type || '来源'}
          {ev.note ? ` · ${ev.note}` : ''}
          {ev.doc_id ? ` · doc ${ev.doc_id}` : ''}
          {ev.page ? ` · 第${ev.page}页` : ''}
        </Text>
      ))}
    </Space>
  )
}

function FactBody({ fact }: { fact: Fact }) {
  const kind = str(fact.kind)
  if (kind === 'parameter_prior') {
    const range = fact.range as number[] | null | undefined
    const unit = fact.unit ? ` ${str(fact.unit)}` : ''
    return (
      <Space direction="vertical" size={0}>
        <Text>
          {str(fact.name || fact.subject)}
          <Text type="secondary" style={{ fontSize: 12 }}>
            {fact.subject ? `（${str(fact.subject)}）` : ''}
          </Text>
        </Text>
        <Text style={{ fontSize: 12 }}>
          取值 {range?.[0] ?? '—'} ~ {range?.[1] ?? '—'}
          {unit}　初值 {str(fact.initial) || '—'}
        </Text>
        <Text type="secondary" style={{ fontSize: 11 }}>
          {[fact.range_level && `范围强度 ${str(fact.range_level)}`,
            fact.evidence_level && `证据等级 ${str(fact.evidence_level)}`]
            .filter(Boolean)
            .join(' · ')}
        </Text>
        <EvidenceLines items={fact.evidence as OntologyEvidence[]} />
      </Space>
    )
  }
  if (kind === 'graph_edge') {
    return (
      <Space direction="vertical" size={0}>
        <Text>
          {str(fact.subject)}
          <Text type="secondary"> {str(fact.predicate_label || fact.predicate)} </Text>
          {str(fact.object)}
        </Text>
        {fact.evidence_text ? (
          <Text type="secondary" style={{ fontSize: 11 }}>
            依据：{str(fact.evidence_text)}
          </Text>
        ) : null}
      </Space>
    )
  }
  return (
    <Space direction="vertical" size={0}>
      <Text>{str(fact.name || fact.subject)}</Text>
      <Text type="secondary" style={{ fontSize: 12 }}>
        {str(fact.entity_type_label || fact.entity_type)}
      </Text>
    </Space>
  )
}

interface Props {
  data: OntologyFactsEvent
  /** 展开显示（用于调试/首次展示），默认折叠 */
  defaultOpen?: boolean
}

export default function OntologyFactCards({ data, defaultOpen = false }: Props) {
  const facts = data.facts ?? []
  // 本体一条事实都没拿到 → 已经回退知识库检索。要让人看见"这次没走本体"，
  // 否则用户会以为本体模式没生效。
  if (facts.length === 0) {
    return (
      <div className="ontology-facts">
        <Tag color="orange" style={{ marginInlineEnd: 0 }}>
          本体无结果，已回退知识库检索
        </Tag>
      </div>
    )
  }

  const sourceText = (data.sources ?? []).map((s) => SOURCE_LABEL[s] ?? s).join(' / ')

  return (
    <div className="ontology-facts">
      <Collapse
        size="small"
        ghost
        defaultActiveKey={defaultOpen ? ['f'] : undefined}
        items={[
          {
            key: 'f',
            label: (
              <Space size={6} wrap>
                <Tag color={data.fallback_used ? 'orange' : 'green'} style={{ marginInlineEnd: 0 }}>
                  {data.fallback_used ? '本体无结果，已回退知识库' : '全部来自本体'}
                </Tag>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {sourceText || '本体'} · {facts.length} 条事实 · {data.latency_ms} ms
                </Text>
              </Space>
            ),
            children: (
              <Space direction="vertical" size={10} style={{ width: '100%' }}>
                {facts.map((fact, i) => (
                  <div key={i} className="ontology-fact-item">
                    <Tag color="blue" style={{ marginInlineEnd: 6 }}>
                      {KIND_LABEL[str(fact.kind)] ?? str(fact.kind)}
                    </Tag>
                    <FactBody fact={fact} />
                  </div>
                ))}
              </Space>
            ),
          },
        ]}
      />
    </div>
  )
}
