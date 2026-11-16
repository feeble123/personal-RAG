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
import {
  CheckOutlined,
  CloseOutlined,
  FileSearchOutlined,
  PlayCircleOutlined,
  SwapOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { errMsg } from '@/api/client'
import { kbPublicApi, ontologyApi } from '@/api/modules'
import type { AnchorRunResult, ExtractRunResult, OntologyProposalItem } from '@/api/types'

const { Text } = Typography

interface Props {
  profileId: string
}

/** 提案里的值是不是和实例当前值不一致（锚定提案才会"发现冲突"）。 */
function isConflict(row: OntologyProposalItem): boolean {
  if (row.method !== 'anchor') return false
  if (row.value === '' || row.value === null || row.value === undefined) return false
  if (row.current_value === null || row.current_value === undefined) return false
  return String(row.current_value) !== String(row.value)
}

export default function ExtractionPanel({ profileId }: Props) {
  const qc = useQueryClient()
  const { message } = App.useApp()
  const [kbId, setKbId] = useState<number | null>(null)
  const [useLlm, setUseLlm] = useState(false)
  const [lastRun, setLastRun] = useState<ExtractRunResult | null>(null)
  const [lastAnchor, setLastAnchor] = useState<AnchorRunResult | null>(null)
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

  // P2：给已有实例找原文出处。不改值，只把「哪段原文支持它」摆上来。
  const anchor = useMutation({
    mutationFn: () =>
      ontologyApi.anchorRun({
        profile_id: profileId || undefined,
        kb_id: kbId,
        dry_run: false,
      }),
    onSuccess: (data) => {
      setLastAnchor(data)
      message.success(
        `扫描 ${data.scanned} 个实例，通过校验 ${data.proposed} 条，新增待审 ${data.saved} 条`,
      )
      refresh()
    },
    onError: (e) => message.error(errMsg(e)),
  })

  const review = useMutation({
    mutationFn: (v: { ids: number[]; accept: boolean; adoptValue?: boolean }) =>
      v.accept
        ? v.ids.length > 1
          ? ontologyApi.acceptProposals(v.ids)
          : ontologyApi.acceptProposal(v.ids[0], v.adoptValue)
        : ontologyApi.rejectProposal(v.ids[0]),
    onSuccess: (_d, v) => {
      message.success(
        v.accept
          ? `已接受 ${v.ids.length} 条${v.adoptValue ? '，并把原文值写进实例' : ''}`
          : '已驳回',
      )
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
        description="两种活分开干：「抽值」按属性上配的规则/大模型从原文读出一个值；「找依据」只负责指出哪段原文支持这个实例，一个字都不改值。两种结果都先进待审表，你点「接受」才生效。"
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
          抽属性值
        </Button>
        <Tooltip title="给已有实例找原文出处：检索候选切片 → 模型指认 → 逐字校验。只挂证据，不改值">
          <Button
            icon={<FileSearchOutlined />}
            loading={anchor.isPending}
            onClick={() => anchor.mutate()}
          >
            找原文依据
          </Button>
        </Tooltip>
        {lastRun && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            上次抽值：规则 {lastRun.rule_hits} / 模型 {lastRun.llm_hits} / 新增 {lastRun.saved}
          </Text>
        )}
        {lastAnchor && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            上次找依据：扫描 {lastAnchor.scanned} / 通过 {lastAnchor.proposed} / 拦截{' '}
            {lastAnchor.rejected} / 新增 {lastAnchor.saved}
          </Text>
        )}
      </Space>

      {lastAnchor && lastAnchor.rejected > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={`有 ${lastAnchor.rejected} 条被校验挡下了`}
          description={
            Object.entries(lastAnchor.reject_reasons)
              .map(([k, v]) => `${k} ${v} 条`)
              .join('；') || '模型给的引用在原文里找不到，已丢弃（假出处比没出处更坏）'
          }
        />
      )}

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
          { title: '实例', dataIndex: 'instance_name', width: 150 },
          {
            title: '字段',
            width: 130,
            render: (_: unknown, r: OntologyProposalItem) => (r.attribute || '整条实例'),
          },
          {
            title: '原文里的值',
            width: 190,
            render: (_: unknown, r: OntologyProposalItem) => {
              const suggested = r.value === '' || r.value === null ? '' : String(r.value)
              if (!suggested) return <Text type="secondary">—</Text>
              if (!isConflict(r)) return <Text strong>{suggested}</Text>
              return (
                <Space size={4} wrap>
                  <Text delete type="secondary">{String(r.current_value)}</Text>
                  <Text strong>{suggested}</Text>
                  <Tooltip title="原文写的和你填的不一样——要不要改成原文值由你定">
                    <Tag color="gold">不一致</Tag>
                  </Tooltip>
                </Space>
              )
            },
          },
          {
            title: '来源',
            width: 80,
            render: (_: unknown, r: OntologyProposalItem) => (
              <Tooltip title={`置信度 ${(r.confidence * 100).toFixed(0)}%`}>
                <Tag color={r.method === 'rule' ? 'green' : r.method === 'anchor' ? 'blue' : 'orange'}>
                  {r.method === 'rule' ? '规则' : r.method === 'anchor' ? '证据' : '模型'}
                </Tag>
              </Tooltip>
            ),
          },
          {
            title: '出处',
            render: (_: unknown, r: OntologyProposalItem) => {
              const ev = (r.evidence ?? {}) as Record<string, unknown>
              const quote = String(ev.quote ?? ev.snippet ?? '')
              return (
                <Space direction="vertical" size={0}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {String(ev.doc_name ?? '')}
                    {ev.page ? ` · 第${ev.page}页` : ''}
                  </Text>
                  <Text type="secondary" style={{ fontSize: 11 }} ellipsis={{ tooltip: quote }}>
                    {quote}
                  </Text>
                </Space>
              )
            },
          },
          {
            title: '操作',
            width: 150,
            render: (_: unknown, r: OntologyProposalItem) => {
              const suggested = r.value === '' || r.value === null ? '' : String(r.value)
              return (
                <Space size={0}>
                  <Tooltip title={r.method === 'anchor' ? '只挂这条依据，不改值' : '写入实例'}>
                    <Button
                      size="small"
                      type="text"
                      icon={<CheckOutlined />}
                      onClick={() => review.mutate({ ids: [r.id], accept: true })}
                    />
                  </Tooltip>
                  {r.method === 'anchor' && suggested ? (
                    <Tooltip title={`接受，并把这个字段改成原文里的值（${suggested}）`}>
                      <Button
                        size="small"
                        type="text"
                        icon={<SwapOutlined />}
                        onClick={() =>
                          review.mutate({ ids: [r.id], accept: true, adoptValue: true })
                        }
                      />
                    </Tooltip>
                  ) : null}
                  <Button
                    size="small"
                    type="text"
                    danger
                    icon={<CloseOutlined />}
                    onClick={() => review.mutate({ ids: [r.id], accept: false })}
                  />
                </Space>
              )
            },
          },
        ]}
      />
    </Card>
  )
}
