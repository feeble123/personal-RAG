/**
 * 自动抽取（F4）+ 图提案（7-C）：机器先找，人来定稿。
 *
 * 这里的四种活分开干，但**结果都进同一个待审区**：
 * - 抽值（rule/llm）：给已有实例填字段；
 * - 找依据（anchor）：给已有实例指出"哪段原文支持它"，一个字都不改值；
 * - 提新实例（graph/C-2）：读一份文档，提出这里有哪些实例和关系；
 * - 补关系（graph/C-3）：围着一个已有实例，问它还跟谁有关系。
 *
 * 为什么一定要人工把关：机器抽出来的值和人工查证过的值混在一起后，事后分不清
 * 哪条是查过的、哪条是猜的。所以这里默认什么都不会写进实例，你点「接受」才写。
 *
 * 图提案还多一层：接受时**先实例后关系**（关系两端得先存在），落不下去的留在
 * 待审区并说明原因，落地后当场体检告诉你"这条还缺什么"。
 */
import { useMemo, useState } from 'react'
import {
  Alert,
  App,
  Button,
  Card,
  InputNumber,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  ApartmentOutlined,
  CheckOutlined,
  CloseOutlined,
  FileAddOutlined,
  FileSearchOutlined,
  PlayCircleOutlined,
  SwapOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { errMsg } from '@/api/client'
import { kbPublicApi, ontologyApi } from '@/api/modules'
import type {
  AnchorRunResult,
  ExtractRunResult,
  OntologyProposalItem,
  ProposeRunResult,
} from '@/api/types'

const { Text } = Typography

interface Props {
  profileId: string
}

/** 提案类型的中文名与颜色。 */
const KIND_LABEL: Record<string, string> = {
  attr: '字段值',
  instance: '新实例',
  relation: '新关系',
}
const KIND_COLOR: Record<string, string> = {
  attr: 'default',
  instance: 'blue',
  relation: 'purple',
}

/** 一条提案「提的是什么」——三种提案的正文长得完全不一样。 */
function proposalSubject(row: OntologyProposalItem): { main: string; sub: string } {
  const payload = (row.payload ?? {}) as Record<string, unknown>
  if (row.kind === 'instance') {
    const name = String(payload.name ?? row.instance_name ?? '')
    return { main: name, sub: String(payload.entity_type ?? row.entity_type ?? '') }
  }
  if (row.kind === 'relation') {
    const pred = String(payload.predicate ?? '')
    return {
      main: `${row.subject_name || row.instance_name} —${pred}→ ${row.object_name}`,
      sub: pred,
    }
  }
  return { main: row.instance_name, sub: '' }
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
  const [lastPropose, setLastPropose] = useState<ProposeRunResult | null>(null)
  const [lastReview, setLastReview] = useState<{
    failures: Array<{ subject: string; reason: string }>
    warnings: Array<{ subject: string; message: string }>
  } | null>(null)
  const [selected, setSelected] = useState<number[]>([])
  const [kindFilter, setKindFilter] = useState<'all' | 'attr' | 'instance' | 'relation'>('all')

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

  // 7-C C-2：按文档整体扫——模型读原文提"这里有哪些新实例和新关系"。
  const propose = useMutation({
    mutationFn: () =>
      ontologyApi.proposeRun({
        profile_id: profileId || undefined,
        kb_id: kbId,
        dry_run: false,
      }),
    onSuccess: (data) => {
      setLastPropose(data)
      message.success(
        `扫了 ${data.scanned}/${data.chunks} 段，提新实例 ${data.instances} 个 / 新关系 ${data.relations} 条，新增待审 ${data.saved} 条`,
      )
      refresh()
    },
    onError: (e) => message.error(errMsg(e)),
  })

  // 7-C C-3：不点名实例 → 程序自己挑最孤立的那几个来补边。
  const link = useMutation({
    mutationFn: () =>
      ontologyApi.linkRun({ profile_id: profileId || undefined, kb_id: kbId, dry_run: false }),
    onSuccess: (data) => {
      setLastPropose(data)
      message.success(
        `补出新关系 ${data.relations} 条 / 新实例 ${data.instances} 个，新增待审 ${data.saved} 条`,
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
    onSuccess: (data, v) => {
      setSelected([])
      refresh()
      if (!v.accept) {
        message.success('已驳回')
        return
      }
      // 批量接受会带回落不下去的条目和落地后的体检结论——不能只报"已接受 N 条"
      const result = data as {
        reviewed?: number
        failed?: number
        failures?: Array<{ subject: string; reason: string }>
        warnings?: Array<{ subject: string; message: string }>
      }
      if (result?.failures?.length || result?.warnings?.length) {
        setLastReview({
          failures: result.failures ?? [],
          warnings: result.warnings ?? [],
        })
      } else {
        setLastReview(null)
      }
      if (result?.failures?.length) {
        message.warning(
          `接受 ${result.reviewed ?? 0} 条，${result.failed ?? result.failures.length} 条没落地（仍留在待审区）`,
        )
      } else {
        message.success(`已接受 ${v.ids.length} 条${v.adoptValue ? '，并把原文值写进实例' : ''}`)
      }
      if (result?.warnings?.length) {
        message.info(`新建的实例还缺 ${result.warnings.length} 处东西，见下方提示`)
      }
    },
    onError: (e) => message.error(errMsg(e)),
  })

  const rows = proposalsQuery.data ?? []
  const counts = useMemo(() => {
    const acc = { attr: 0, instance: 0, relation: 0 }
    for (const r of rows) {
      if (r.kind in acc) acc[r.kind as keyof typeof acc] += 1
    }
    return acc
  }, [rows])
  const shown = kindFilter === 'all' ? rows : rows.filter((r) => r.kind === kindFilter)

  return (
    <Card size="small">
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="程序先找，人来定稿"
        description="四种活分开干：「抽值」按属性规则/大模型从原文读出一个值；「找依据」只指出哪段原文支持这个实例，不改值；「提新实例」读一份文档，提出这里有哪些实例和关系；「补关系」围着一个已有实例问它还跟谁有关系。结果都先进待审表，你点「接受」才生效——接受时先建实例再建关系。"
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
        <Tooltip title="选一个知识库，模型读其中的原文，提出「这里有哪些实例、它们之间什么关系」。人只审不建">
          <Button
            icon={<FileAddOutlined />}
            loading={propose.isPending}
            onClick={() => propose.mutate()}
          >
            提新实例
          </Button>
        </Tooltip>
        <Tooltip title="程序自己挑最孤立的几个实例（一条边都没有的先补），问模型它们还跟谁有关系">
          <Button
            icon={<ApartmentOutlined />}
            loading={link.isPending}
            onClick={() => link.mutate()}
          >
            补关系
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

      {lastPropose && (
        <Alert
          type={lastPropose.reject_reasons && Object.keys(lastPropose.reject_reasons).length ? 'warning' : 'info'}
          showIcon
          style={{ marginBottom: 12 }}
          message={`扫了 ${lastPropose.scanned}/${lastPropose.chunks} 段，提出新实例 ${lastPropose.instances} 个 / 新关系 ${lastPropose.relations} 条，被挡下 ${lastPropose.rejected} 条`}
          description={
            <Space direction="vertical" size={2} style={{ fontSize: 12 }}>
              {lastPropose.scanned < lastPropose.chunks && (
                <Text type="warning">
                  范围内还有 {lastPropose.chunks - lastPropose.scanned} 段这一轮**没扫**——
                  没扫的部分不会出现在提案里，别把这次当成"整份文档都提过了"。
                </Text>
              )}
              {Object.entries(lastPropose.reject_reasons ?? {}).map(([k, v]) => (
                <Text key={k} type="secondary">
                  挡下：{k} {v} 条
                </Text>
              ))}
              {lastPropose.notes.slice(0, 5).map((n, i) => (
                <Text key={i} type="secondary">
                  · {n}
                </Text>
              ))}
            </Space>
          }
        />
      )}

      {lastReview && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={[
            lastReview.failures.length ? `${lastReview.failures.length} 条没落地（仍留在待审区）` : '',
            lastReview.warnings.length ? `新建的实例还有 ${lastReview.warnings.length} 处缺口` : '',
          ]
            .filter(Boolean)
            .join('；')}
          description={
            <Space direction="vertical" size={2} style={{ fontSize: 12 }}>
              {lastReview.failures.map((f, i) => (
                <Text key={`f${i}`} type="danger">
                  · {f.subject}：{f.reason}
                </Text>
              ))}
              {lastReview.warnings.map((w, i) => (
                <Text key={`w${i}`} type="secondary">
                  · {w.subject}：{w.message}
                </Text>
              ))}
            </Space>
          }
        />
      )}

      <Space style={{ marginBottom: 8 }}>
        <Text strong>待审 {rows.length} 条</Text>
        <Segmented
          size="small"
          value={kindFilter}
          onChange={(v) => setKindFilter(v as typeof kindFilter)}
          options={[
            { value: 'all', label: `全部 ${rows.length}` },
            { value: 'instance', label: `新实例 ${counts.instance}` },
            { value: 'relation', label: `新关系 ${counts.relation}` },
            { value: 'attr', label: `字段值 ${counts.attr}` },
          ]}
        />
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
        dataSource={shown}
        pagination={{ pageSize: 20 }}
        rowSelection={{
          selectedRowKeys: selected,
          onChange: (keys) => setSelected(keys as number[]),
        }}
        columns={[
          {
            title: '类型',
            dataIndex: 'kind',
            width: 84,
            render: (value: string) => (
              <Tag color={KIND_COLOR[value] ?? 'default'}>{KIND_LABEL[value] ?? value}</Tag>
            ),
          },
          {
            title: '提的是什么',
            width: 260,
            render: (_: unknown, r: OntologyProposalItem) => {
              const { main, sub } = proposalSubject(r)
              return (
                <Space direction="vertical" size={0}>
                  <Text strong>{main}</Text>
                  {sub && r.kind !== 'relation' && (
                    <Text type="secondary" style={{ fontSize: 11 }}>
                      {sub}
                    </Text>
                  )}
                </Space>
              )
            },
          },
          {
            title: '字段',
            width: 110,
            render: (_: unknown, r: OntologyProposalItem) =>
              r.kind === 'attr' ? r.attribute || '整条实例' : <Text type="secondary">—</Text>,
          },
          {
            title: '原文里的值',
            width: 190,
            render: (_: unknown, r: OntologyProposalItem) => {
              // 图提案的 value 是整份结构化内容，直接 String() 会变成 [object Object]
              if (r.kind === 'instance') {
                const props = ((r.payload ?? {}) as Record<string, unknown>).props as
                  | Record<string, unknown>
                  | undefined
                const entries = Object.entries(props ?? {})
                if (!entries.length) return <Text type="secondary">（没填字段）</Text>
                return (
                  <Space size={4} wrap>
                    {entries.map(([k, v]) => (
                      <Tag key={k}>
                        {k}={String(v)}
                      </Tag>
                    ))}
                  </Space>
                )
              }
              if (r.kind === 'relation') return <Text type="secondary">建一条新边</Text>
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
                <Tag
                  color={
                    r.method === 'rule'
                      ? 'green'
                      : r.method === 'anchor'
                        ? 'blue'
                        : r.method === 'graph'
                          ? 'purple'
                          : 'orange'
                  }
                >
                  {r.method === 'rule'
                    ? '规则'
                    : r.method === 'anchor'
                      ? '证据'
                      : r.method === 'graph'
                        ? '图提案'
                        : '模型'}
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
