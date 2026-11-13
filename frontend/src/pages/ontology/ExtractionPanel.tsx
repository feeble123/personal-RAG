/**
 * 自动抽取（F4）：从知识库切片里找属性值，结果进「待审区」，人工确认才落实例。
 *
 * 为什么一定要人工把关：机器抽出来的值和人工查证过的值混在一起后，
 * 事后分不清哪条是查过的、哪条是猜的。所以这里默认什么都不会写进实例，
 * 你点「接受」才写，并且会把原文页码一起作为证据附上。
 */
import { useState } from 'react'
import {
  Alert,
  App,
  Button,
  Card,
  InputNumber,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { CheckOutlined, CloseOutlined, PlayCircleOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { errMsg } from '@/api/client'
import { kbPublicApi, ontologyApi } from '@/api/modules'
import type { ExtractRunResult, OntologyProposalItem } from '@/api/types'

const { Text } = Typography

interface Props {
  profileId: string
}

export default function ExtractionPanel({ profileId }: Props) {
  const qc = useQueryClient()
  const { message } = App.useApp()
  const [kbId, setKbId] = useState<number | null>(null)
  const [useLlm, setUseLlm] = useState(false)
  const [lastRun, setLastRun] = useState<ExtractRunResult | null>(null)
  const [selected, setSelected] = useState<number[]>([])

  const { data: kbs = [] } = useQuery({ queryKey: ['public-kbs'], queryFn: kbPublicApi.list })
  const proposalsQuery = useQuery({
    queryKey: ['ontology-proposals', profileId],
    queryFn: () => ontologyApi.proposals({ profile: profileId || undefined, status: 'pending' }),
  })

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['ontology-proposals'] })
    qc.invalidateQueries({ queryKey: ['ontology-instances'] })
    qc.invalidateQueries({ queryKey: ['ontology-graph'] })
  }

  const run = useMutation({
    mutationFn: () =>
      ontologyApi.extractRun({
        profile_id: profileId || undefined,
        kb_id: kbId,
        use_llm: useLlm,
        dry_run: false,
      }),
    onSuccess: (data) => {
      setLastRun(data)
      message.success(
        `扫描完成：规则 ${data.rule_hits} 条${useLlm ? ` / 模型 ${data.llm_hits} 条` : ''}，新增待审 ${data.saved} 条`,
      )
      refresh()
    },
    onError: (e) => message.error(errMsg(e)),
  })

  const review = useMutation({
    mutationFn: (v: { ids: number[]; accept: boolean }) =>
      v.accept
        ? v.ids.length > 1
          ? ontologyApi.acceptProposals(v.ids)
          : ontologyApi.acceptProposal(v.ids[0])
        : ontologyApi.rejectProposal(v.ids[0]),
    onSuccess: (_d, v) => {
      message.success(v.accept ? `已接受 ${v.ids.length} 条并写入实例` : '已驳回')
      setSelected([])
      refresh()
    },
    onError: (e) => message.error(errMsg(e)),
  })

  const rows = proposalsQuery.data ?? []

  return (
    <Card size="small">
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="程序先找，人来定稿"
        description="按「本体结构」里给属性配的抽取规则扫知识库切片；开了大模型就让它补规则漏掉的。抽出来的都进下面这张待审表，你点「接受」才会写进实例（连同原文页码作为证据）。"
      />

      <Space wrap style={{ marginBottom: 12 }}>
        <Select
          allowClear
          placeholder="全部知识库"
          style={{ width: 240 }}
          value={kbId}
          onChange={setKbId}
          options={kbs.map((k) => ({ value: k.id, label: k.name }))}
        />
        <Tooltip title="规则抽不到的实例交给大模型读原文；慢、费 token，结果置信度低">
          <Space size={6}>
            <Switch size="small" checked={useLlm} onChange={setUseLlm} />
            <Text style={{ fontSize: 12 }}>用大模型补漏</Text>
          </Space>
        </Tooltip>
        <Button
          type="primary"
          icon={<PlayCircleOutlined />}
          loading={run.isPending}
          onClick={() => run.mutate()}
        >
          开始扫描
        </Button>
        {lastRun && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            上次：规则 {lastRun.rule_hits} / 模型 {lastRun.llm_hits} / 新增 {lastRun.saved}
          </Text>
        )}
      </Space>

      <Space style={{ marginBottom: 8 }}>
        <Text strong>待审 {rows.length} 条</Text>
        <Button
          size="small"
          disabled={!selected.length}
          loading={review.isPending}
          onClick={() => review.mutate({ ids: selected, accept: true })}
        >
          接受选中（{selected.length}）
        </Button>
      </Space>

      <Table<OntologyProposalItem>
        size="small"
        rowKey="id"
        loading={proposalsQuery.isLoading}
        dataSource={rows}
        pagination={{ pageSize: 20 }}
        rowSelection={{
          selectedRowKeys: selected,
          onChange: (keys) => setSelected(keys as number[]),
        }}
        columns={[
          { title: '实例', dataIndex: 'instance_name', width: 160 },
          { title: '字段', dataIndex: 'attribute', width: 160 },
          {
            title: '抽到的值',
            width: 120,
            render: (_: unknown, r: OntologyProposalItem) => <Text strong>{String(r.value)}</Text>,
          },
          {
            title: '来源',
            width: 90,
            render: (_: unknown, r: OntologyProposalItem) => (
              <Tooltip title={`置信度 ${(r.confidence * 100).toFixed(0)}%`}>
                <Tag color={r.method === 'rule' ? 'green' : 'orange'}>
                  {r.method === 'rule' ? '规则' : '模型'}
                </Tag>
              </Tooltip>
            ),
          },
          {
            title: '出处',
            render: (_: unknown, r: OntologyProposalItem) => {
              const ev = (r.evidence ?? {}) as Record<string, unknown>
              return (
                <Space direction="vertical" size={0}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {String(ev.doc_name ?? '')}
                    {ev.doc_id ? ` · doc ${ev.doc_id}` : ''}
                    {ev.page ? ` · 第${ev.page}页` : ''}
                  </Text>
                  <Text type="secondary" style={{ fontSize: 11 }} ellipsis>
                    {String(ev.snippet ?? '')}
                  </Text>
                </Space>
              )
            },
          },
          {
            title: '操作',
            width: 100,
            render: (_: unknown, r: OntologyProposalItem) => (
              <Space size={0}>
                <Button
                  size="small"
                  type="text"
                  icon={<CheckOutlined />}
                  onClick={() => review.mutate({ ids: [r.id], accept: true })}
                />
                <Button
                  size="small"
                  type="text"
                  danger
                  icon={<CloseOutlined />}
                  onClick={() => review.mutate({ ids: [r.id], accept: false })}
                />
              </Space>
            ),
          },
        ]}
      />
    </Card>
  )
}
