import { useState } from 'react'
import {
  App,
  Button,
  Card,
  Col,
  Descriptions,
  Dropdown,
  Form,
  Input,
  Layout,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
} from 'antd'
import {
  ArrowLeftOutlined,
  BulbOutlined,
  DeleteOutlined,
  DownloadOutlined,
  PlusOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import dayjs from 'dayjs'
import UserMenu from '@/components/UserMenu'
import { kbApi, memoryApi } from '@/api/modules'
import { ANSWER_STYLE_OPTIONS, type KnowledgeBase, type MemoryItem } from '@/api/types'

const { Header, Content } = Layout

const STYLE_LABEL: Record<string, string> = Object.fromEntries(
  ANSWER_STYLE_OPTIONS.map((o) => [o.value, o.label]),
)

// 问答记忆库管理系统（仅管理员）：统计 + 筛选 + 列表 + 详情 + 录入 + 纠正 + 删除 + 导出
export default function MemoryManager() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { message } = App.useApp()

  const [filters, setFilters] = useState<{ status?: string; kbId?: number; userId?: number; q?: string }>({})
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [selectedKeys, setSelectedKeys] = useState<number[]>([])
  const [detail, setDetail] = useState<MemoryItem | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [form] = Form.useForm()

  const filterParams = () => ({
    status: filters.status || undefined,
    kb_id: filters.kbId ?? undefined,
    user_id: filters.userId ?? undefined,
    q: filters.q || undefined,
  })

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['memories'] })
    qc.invalidateQueries({ queryKey: ['memory-stats'] })
  }

  // 数据
  const { data: kbs = [] } = useQuery<KnowledgeBase[]>({
    queryKey: ['admin-kbs'],
    queryFn: kbApi.list,
    staleTime: 60_000,
  })
  const { data: users = [] } = useQuery({
    queryKey: ['admin-users'],
    queryFn: async () => (await memoryApi.listUsers()).items,
    staleTime: 60_000,
  })
  const statsQuery = useQuery({
    queryKey: ['memory-stats', filters],
    queryFn: () => memoryApi.stats(filterParams()),
  })
  const listQuery = useQuery({
    queryKey: ['memories', page, pageSize, filters],
    queryFn: () => memoryApi.list({ ...filterParams(), page, page_size: pageSize }),
  })

  // 写操作
  const removeMut = useMutation({ mutationFn: (id: number) => memoryApi.remove(id), onSuccess: invalidate })
  const batchRemoveMut = useMutation({
    mutationFn: (ids: number[]) => memoryApi.batchRemove(ids),
    onSuccess: () => {
      invalidate()
      setSelectedKeys([])
    },
  })
  const setStatusMut = useMutation({
    mutationFn: (p: { id: number; status: 'good' | 'bad' }) => memoryApi.setStatus(p.id, p.status),
    onSuccess: invalidate,
  })
  const createMut = useMutation({
    mutationFn: (d: { question: string; answer: string; kb_id?: number | null; style?: string }) =>
      memoryApi.create(d),
    onSuccess: () => {
      invalidate()
      setCreateOpen(false)
      form.resetFields()
      message.success('记忆已录入')
    },
  })
  const clearKbMut = useMutation({
    mutationFn: (kbId: number) => memoryApi.clearKb(kbId),
    onSuccess: invalidate,
  })

  const stats = statsQuery.data

  const columns = [
    {
      title: '问题',
      dataIndex: 'question',
      ellipsis: true,
      render: (q: string, r: MemoryItem) => (
        <a onClick={() => setDetail(r)} style={{ color: '#00c6ff' }}>
          {q}
        </a>
      ),
    },
    { title: '用户', dataIndex: 'username', width: 90, render: (v: string | null) => v ?? '—' },
    { title: '知识库', dataIndex: 'kb_name', width: 150, render: (v: string | null) => v ?? '全部' },
    {
      title: '风格',
      dataIndex: 'style',
      width: 105,
      render: (v: string | null) => (v ? STYLE_LABEL[v] ?? v : '—'),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 80,
      render: (s: string) => (
        <Tag color={s === 'good' ? 'success' : 'error'}>{s === 'good' ? '好评' : '差评'}</Tag>
      ),
    },
    {
      title: '命中',
      dataIndex: 'hit_count',
      width: 60,
      align: 'center' as const,
      render: (v: number) => <span className="font-mono">{v}</span>,
    },
    {
      title: '相似度',
      dataIndex: 'score',
      width: 85,
      render: (v: number | null) =>
        v == null ? '—' : <span className="font-mono">{v.toFixed(3)}</span>,
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 140,
      render: (v: string) => <span className="font-mono">{dayjs(v).format('MM-DD HH:mm')}</span>,
    },
    {
      title: '操作',
      width: 170,
      render: (_: unknown, r: MemoryItem) => (
        <Space size={0}>
          <Button size="small" type="link" onClick={() => setDetail(r)}>
            详情
          </Button>
          <Popconfirm
            title={r.status === 'good' ? '标记为差评？（同题下次将强制重检）' : '恢复为好评？'}
            onConfirm={() => setStatusMut.mutate({ id: r.id, status: r.status === 'good' ? 'bad' : 'good' })}
          >
            <Button size="small" type="link">
              {r.status === 'good' ? '标记差评' : '恢复好评'}
            </Button>
          </Popconfirm>
          <Popconfirm title="删除该记忆？" onConfirm={() => removeMut.mutate(r.id)}>
            <Button size="small" type="link" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Layout style={{ height: '100vh' }}>
      <Header
        className="tech-line"
        style={{
          background: 'rgba(10, 17, 34, 0.5)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          borderBottom: '1px solid rgba(122, 190, 255, 0.1)',
          paddingInline: 24,
        }}
      >
        <Space>
          <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate('/chat')}>
            返回问答
          </Button>
          <BulbOutlined style={{ color: '#00c6ff', fontSize: 18 }} />
          <Typography.Text strong>记忆库管理</Typography.Text>
          <Tag color="red">管理员</Tag>
        </Space>
        <UserMenu />
      </Header>

      <Content style={{ padding: 24, overflowY: 'auto' }}>
        {/* 统计卡片：等宽数字 + 数据感 */}
        <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
          <Col xs={12} md={6}>
            <Card size="small">
              <Statistic
                title="记忆总数"
                value={stats?.total ?? 0}
                valueStyle={{ fontFamily: 'var(--font-mono)', fontVariantNumeric: 'tabular-nums' }}
              />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card size="small">
              <Statistic
                title="好评记忆"
                value={stats?.good ?? 0}
                valueStyle={{ color: '#2ee6b8', fontFamily: 'var(--font-mono)', fontVariantNumeric: 'tabular-nums' }}
              />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card size="small">
              <Statistic
                title="差评记忆"
                value={stats?.bad ?? 0}
                valueStyle={{ color: '#ff5c7a', fontFamily: 'var(--font-mono)', fontVariantNumeric: 'tabular-nums' }}
              />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card size="small">
              <Statistic
                title="总命中次数"
                value={stats?.total_hits ?? 0}
                valueStyle={{
                  color: '#00c6ff',
                  textShadow: '0 0 12px rgba(0, 198, 255, 0.4)',
                  fontFamily: 'var(--font-mono)',
                  fontVariantNumeric: 'tabular-nums',
                }}
              />
            </Card>
          </Col>
        </Row>

        {/* 筛选 + 操作栏 */}
        <Card size="small" style={{ marginBottom: 16 }}>
          <Space wrap>
            <Select
              placeholder="状态"
              allowClear
              style={{ width: 100 }}
              value={filters.status}
              onChange={(v) => {
                setFilters((f) => ({ ...f, status: v }))
                setPage(1)
              }}
              options={[
                { value: 'good', label: '好评' },
                { value: 'bad', label: '差评' },
              ]}
            />
            <Select
              placeholder="知识库"
              allowClear
              style={{ width: 180 }}
              value={filters.kbId}
              onChange={(v) => {
                setFilters((f) => ({ ...f, kbId: v }))
                setPage(1)
              }}
              options={kbs.map((k) => ({ value: k.id, label: k.name }))}
            />
            <Select
              placeholder="用户"
              allowClear
              style={{ width: 140 }}
              value={filters.userId}
              onChange={(v) => {
                setFilters((f) => ({ ...f, userId: v }))
                setPage(1)
              }}
              options={users.map((u) => ({ value: u.id, label: u.username }))}
            />
            <Input.Search
              placeholder="按问题搜索"
              allowClear
              style={{ width: 220 }}
              onSearch={(v) => {
                setFilters((f) => ({ ...f, q: v || undefined }))
                setPage(1)
              }}
            />
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
              录入记忆
            </Button>
            {selectedKeys.length > 0 && (
              <Popconfirm
                title={`删除选中的 ${selectedKeys.length} 条记忆？`}
                onConfirm={() => batchRemoveMut.mutate(selectedKeys)}
              >
                <Button danger icon={<DeleteOutlined />}>
                  批量删除 ({selectedKeys.length})
                </Button>
              </Popconfirm>
            )}
            {filters.kbId && (
              <Popconfirm
                title="清空该知识库沉淀的全部记忆？此操作不可恢复。"
                onConfirm={() => clearKbMut.mutate(filters.kbId!)}
              >
                <Button danger>清空该库记忆</Button>
              </Popconfirm>
            )}
            <Dropdown
              menu={{
                items: [
                  { key: 'csv', label: '导出 CSV（Excel）', onClick: () => memoryApi.exportFile('csv', filterParams()) },
                  { key: 'json', label: '导出 JSON', onClick: () => memoryApi.exportFile('json', filterParams()) },
                ],
              }}
            >
              <Button icon={<DownloadOutlined />}>导出</Button>
            </Dropdown>
          </Space>
        </Card>

        {/* 列表 */}
        <Card size="small">
          <Table
            rowKey="id"
            size="small"
            loading={listQuery.isLoading}
            dataSource={listQuery.data?.items ?? []}
            columns={columns}
            pagination={{
              current: page,
              pageSize,
              total: listQuery.data?.total ?? 0,
              showSizeChanger: true,
              pageSizeOptions: [10, 20, 50],
              onChange: (p, ps) => {
                setPage(p)
                setPageSize(ps)
              },
            }}
            rowSelection={{ selectedRowKeys: selectedKeys, onChange: (keys) => setSelectedKeys(keys as number[]) }}
          />
        </Card>
      </Content>

      {/* 详情弹窗 */}
      <Modal open={!!detail} onCancel={() => setDetail(null)} footer={null} width={720} title="记忆详情">
        {detail && (
          <>
            <Descriptions column={2} size="small" style={{ marginBottom: 12 }}>
              <Descriptions.Item label="用户">{detail.username ?? '—'}</Descriptions.Item>
              <Descriptions.Item label="知识库">{detail.kb_name ?? '全部'}</Descriptions.Item>
              <Descriptions.Item label="状态">
                <Tag color={detail.status === 'good' ? 'success' : 'error'}>
                  {detail.status === 'good' ? '好评' : '差评'}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="风格">
                {detail.style ? STYLE_LABEL[detail.style] ?? detail.style : '—'}
              </Descriptions.Item>
              <Descriptions.Item label="命中次数">
                <span className="font-mono">{detail.hit_count}</span>
              </Descriptions.Item>
              <Descriptions.Item label="相似度">
                <span className="font-mono">{detail.score?.toFixed(3) ?? '—'}</span>
              </Descriptions.Item>
              <Descriptions.Item label="主题词">{detail.subject ?? '—'}</Descriptions.Item>
              <Descriptions.Item label="更新时间">
                <span className="font-mono">{dayjs(detail.updated_at).format('YYYY-MM-DD HH:mm')}</span>
              </Descriptions.Item>
              <Descriptions.Item label="问题" span={2}>
                <Typography.Text>{detail.question}</Typography.Text>
              </Descriptions.Item>
            </Descriptions>
            <Typography.Text type="secondary">回答</Typography.Text>
            <div className="citation-snippet" style={{ margin: '6px 0 12px', whiteSpace: 'pre-wrap' }}>
              {detail.answer}
            </div>
            {detail.citations.length > 0 && (
              <>
                <Typography.Text type="secondary">引用（{detail.citations.length}）</Typography.Text>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 6 }}>
                  {detail.citations.map((c, i) => (
                    <Tag key={i} color="blue">
                      [{i + 1}] {c.source}
                      {c.page ? ` · 第${c.page}页` : ''}
                    </Tag>
                  ))}
                </div>
              </>
            )}
          </>
        )}
      </Modal>

      {/* 录入弹窗 */}
      <Modal
        title="录入记忆"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => form.submit()}
        okText="录入"
        confirmLoading={createMut.isPending}
      >
        <Form
          form={form}
          layout="vertical"
          style={{ marginTop: 16 }}
          onFinish={(v) =>
            createMut.mutate({
              question: v.question,
              answer: v.answer,
              kb_id: v.kb_id ?? null,
              style: v.style,
            })
          }
        >
          <Form.Item name="question" label="问题" rules={[{ required: true, message: '请输入问题' }]}>
            <Input.TextArea rows={2} maxLength={2000} placeholder="用户可能会问的问题" />
          </Form.Item>
          <Form.Item name="answer" label="答案" rules={[{ required: true, message: '请输入答案' }]}>
            <Input.TextArea rows={4} maxLength={8000} placeholder="该问题的标准答案" />
          </Form.Item>
          <Form.Item name="kb_id" label="所属知识库">
            <Select allowClear placeholder="全部库（跨库）" options={kbs.map((k) => ({ value: k.id, label: k.name }))} />
          </Form.Item>
          <Form.Item name="style" label="回答风格">
            <Select allowClear placeholder="standard" options={ANSWER_STYLE_OPTIONS} />
          </Form.Item>
        </Form>
      </Modal>
    </Layout>
  )
}
