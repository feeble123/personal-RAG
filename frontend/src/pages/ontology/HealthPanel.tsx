/**
 * 本体体检（7-B 缺口驱动）。
 *
 * **这里回答的是「这套本体还缺什么」**——不是"建了多少实例"。
 *
 * 为什么需要它：以前判断本体建完没有，只能靠人读答案觉得"差点意思"。
 * 现在 TBox 里声明了规矩（一个等级该有几条措施、一个工况只能触发一个等级…），
 * 违反哪条会指名道姓地说出来，还能直接跳到那个实例去补。
 *
 * 三条设计原则：
 *   1. **只报不改**：违规是数据问题，补数据还是改规矩要人来定。
 *   2. **每条违规都能追溯**：`cq` 指向 `docs/本体能力问题清单.md` 的编号。
 *   3. **能点进去**：违规挂在这个库里已有的实例上时，直接打开它的编辑框。
 */
import { useMemo, useState } from 'react'
import {
  Button,
  Card,
  Collapse,
  Empty,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { ReloadOutlined, RightOutlined } from '@ant-design/icons'
import type {
  OntologyConstraint,
  OntologyConstraintReport,
  OntologyEntityType,
  OntologyInstance,
  OntologyViolation,
} from '@/api/types'
import { constraintSummary, detailText } from '@/utils/ontologyHealth'

const { Text } = Typography

const SEVERITY_LABEL: Record<string, string> = { error: '错误', warning: '提醒' }
const TYPE_LABEL: Record<string, string> = {
  required_attribute: '必填属性',
  min_count: '数量下限',
  max_count: '数量上限',
  unique_attribute: '取值唯一',
  coverage: '逐条核对',
}

export default function HealthPanel({
  report,
  loading,
  entityTypes,
  instances,
  onRefetch,
  onLocate,
}: {
  report: OntologyConstraintReport | undefined
  loading: boolean
  entityTypes: OntologyEntityType[]
  instances: OntologyInstance[]
  onRefetch: () => void
  onLocate: (row: OntologyInstance) => void
}) {
  const [severity, setSeverity] = useState<'all' | 'error' | 'warning'>('all')
  const [constraintId, setConstraintId] = useState<string | undefined>()

  const typeLabel = (name: string) =>
    entityTypes.find((t) => t.name === name)?.display_name || name

  // 违规里的 subject 是实例名；同名实例可能不止一个类型，所以按「类型 + 名字」找
  const locate = (v: OntologyViolation) =>
    instances.find((i) => i.name === v.subject && i.entity_type === v.entity)

  const violations = report?.violations ?? []
  const shown = useMemo(
    () =>
      violations.filter(
        (v) =>
          (severity === 'all' || v.severity === severity) &&
          (!constraintId || v.constraint_id === constraintId),
      ),
    [violations, severity, constraintId],
  )

  const summary = report?.summary
  const constraints = report?.constraints ?? []
  const constraintById = useMemo(() => {
    const map = new Map<string, OntologyConstraint>()
    for (const c of constraints) map.set(c.id, c)
    return map
  }, [constraints])

  const columns = [
    {
      title: '级别',
      dataIndex: 'severity',
      width: 72,
      filters: undefined,
      render: (value: string) => (
        <Tag color={value === 'error' ? 'red' : 'orange'}>{SEVERITY_LABEL[value] ?? value}</Tag>
      ),
    },
    {
      title: '规矩',
      dataIndex: 'constraint_id',
      width: 240,
      render: (value: string) => {
        const c = constraintById.get(value)
        return (
          <Tooltip title={<span style={{ fontSize: 12 }}>{value}</span>}>
            <Text>{c ? constraintSummary(c) : value}</Text>
          </Tooltip>
        )
      },
    },
    {
      title: '对象',
      dataIndex: 'subject',
      width: 200,
      render: (value: string, row: OntologyViolation) => (
        <Space direction="vertical" size={0}>
          <Text strong>{value}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {typeLabel(row.entity)}
          </Text>
        </Space>
      ),
    },
    {
      title: '缺什么',
      dataIndex: 'message',
      render: (_: unknown, row: OntologyViolation) => {
        const detail = detailText(row.detail)
        return (
          <Space direction="vertical" size={0}>
            <Text style={{ fontSize: 13 }}>{row.message}</Text>
            {detail && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {detail}
              </Text>
            )}
          </Space>
        )
      },
    },
    {
      title: '能力问题',
      dataIndex: 'cq',
      width: 96,
      render: (value?: string) =>
        value ? (
          <Tooltip title="对应 docs/本体能力问题清单.md 里的编号">
            <Tag>{value}</Tag>
          </Tooltip>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: '操作',
      width: 96,
      render: (_: unknown, row: OntologyViolation) => {
        const hit = locate(row)
        return (
          <Tooltip title={hit ? '打开这个实例去补' : '这个实例不在当前库里，无法直接跳转'}>
            <Button
              size="small"
              type="link"
              icon={<RightOutlined />}
              disabled={!hit}
              onClick={() => hit && onLocate(hit)}
            >
              去处理
            </Button>
          </Tooltip>
        )
      },
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Card size="small">
        <Space wrap align="center">
          <Button icon={<ReloadOutlined />} onClick={onRefetch} loading={loading}>
            重新体检
          </Button>
          <Tag color={summary?.errors ? 'red' : 'green'}>错误 {summary?.errors ?? 0}</Tag>
          <Tag color="orange">提醒 {summary?.warnings ?? 0}</Tag>
          <Text type="secondary" style={{ fontSize: 12 }}>
            共 {constraints.length} 条规矩在管这个库；违规是**数据**问题，要不要补由你定
          </Text>
        </Space>
        {!summary?.total && !loading && (
          <div style={{ marginTop: 12 }}>
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="全部通过——当前没有违反任何已声明的约束"
            />
          </div>
        )}
      </Card>

      {!!violations.length && (
        <Card size="small" title={`违规 ${violations.length} 条`}>
          <Space wrap style={{ marginBottom: 12 }}>
            <Select
              size="small"
              value={severity}
              style={{ width: 120 }}
              onChange={setSeverity}
              options={[
                { value: 'all', label: '全部级别' },
                { value: 'error', label: '只看错误' },
                { value: 'warning', label: '只看提醒' },
              ]}
            />
            <Select
              size="small"
              allowClear
              placeholder="按规矩筛选"
              style={{ minWidth: 240 }}
              value={constraintId}
              onChange={setConstraintId}
              options={(summary?.by_constraint
                ? Object.entries(summary.by_constraint)
                : []
              )
                .sort((a, b) => b[1] - a[1])
                .map(([id, count]) => {
                  const c = constraintById.get(id)
                  return {
                    value: id,
                    label: `${c ? constraintSummary(c) : id}（${count}）`,
                  }
                })}
            />
          </Space>
          <Table
            rowKey={(row) => `${row.constraint_id}-${row.subject}`}
            size="small"
            loading={loading}
            dataSource={shown}
            columns={columns}
            pagination={{ pageSize: 20, showSizeChanger: true }}
          />
        </Card>
      )}

      <Card size="small" title={`这个库在管哪些规矩（${constraints.length}）`}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          规矩写在 TBox 的 <Text code>constraints:</Text> 段里，每条都带{' '}
          <Text code>cq</Text> 追溯到能力问题清单——没有需求支撑的规矩不该存在。
        </Text>
        <Collapse
          size="small"
          style={{ marginTop: 12 }}
          items={constraints.map((c) => ({
            key: c.id,
            label: (
              <Space wrap size={6}>
                <Tag color={c.severity === 'error' ? 'red' : 'orange'}>
                  {SEVERITY_LABEL[c.severity] ?? c.severity}
                </Tag>
                <Text strong>{constraintSummary(c)}</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {typeLabel(c.entity)} · {TYPE_LABEL[c.type] ?? c.type}
                </Text>
                {c.cq && <Tag>{c.cq}</Tag>}
              </Space>
            ),
            children: (
              <Space direction="vertical" size={2} style={{ fontSize: 12 }}>
                <Text type="secondary">
                  标识：<Text code>{c.id}</Text>
                </Text>
                {(c.relation ?? []).length > 0 && (
                  <Text type="secondary">
                    关系：{c.relation?.join(' / ')}
                    {c.target_type ? ` → ${typeLabel(c.target_type)}` : ''}
                    {c.direction === 'in' ? '（数指向它的边）' : ''}
                  </Text>
                )}
                {c.attribute && <Text type="secondary">属性：{c.attribute}</Text>}
                {c.min !== undefined && <Text type="secondary">下限：{c.min}</Text>}
                {c.max !== undefined && <Text type="secondary">上限：{c.max}</Text>}
                {c.min_attr && <Text type="secondary">下限读自属性：{c.min_attr}</Text>}
                {c.total_attr && <Text type="secondary">总条数读自属性：{c.total_attr}</Text>}
                {c.item_attr && <Text type="secondary">条目号读自边属性：{c.item_attr}</Text>}
              </Space>
            ),
          }))}
        />
      </Card>
    </Space>
  )
}
