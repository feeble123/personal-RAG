import { useState } from 'react'
import {
  App,
  Button,
  Card,
  Descriptions,
  Empty,
  Input,
  Layout,
  List,
  Modal,
  Popconfirm,
  Progress,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from 'antd'
import {
  DeleteOutlined,
  DatabaseOutlined,
  EditOutlined,
  PlusOutlined,
  RedoOutlined,
  SearchOutlined,
  UploadOutlined,
  FileOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import dayjs from 'dayjs'
import { kbApi } from '@/api/modules'
import type { ChunkItem, Citation, DocumentItem, KnowledgeBase } from '@/api/types'
import { ANSWER_STYLE_OPTIONS } from '@/api/types'
import { errMsg } from '@/api/client'
import UserMenu from '@/components/UserMenu'
import { useNavigate } from 'react-router-dom'
import { ArrowLeftOutlined } from '@ant-design/icons'

const { Header, Content } = Layout

const STATUS_META: Record<string, { text: string; color: string }> = {
  pending: { text: '排队中', color: 'default' },
  parsing: { text: '解析中', color: 'processing' },
  embedding: { text: '向量化中', color: 'processing' },
  ready: { text: '已就绪', color: 'success' },
  failed: { text: '失败', color: 'error' },
}

// 解析阶段 → 进度百分比（阶段标记，非精确进度）
const STAGE_PCT: Record<string, number> = {
  pending: 5,
  parsing: 45,
  embedding: 80,
}

function formatBytes(v: number): string {
  return v > 1024 * 1024 ? `${(v / 1024 / 1024).toFixed(1)} MB` : `${(v / 1024).toFixed(0)} KB`
}

// 解析类型标签（PDF 区分 文本层 / OCR / 混合）
function parseLabel(r: DocumentItem): { label: string; color: string } {
  const q = r.quality
  if (!q) return { label: '—', color: 'default' }
  if (q.parser === 'pdf') {
    const ocr = Number(q.ocr_pages) || 0
    const text = Number(q.text_pages) || 0
    if (ocr > 0 && text > 0) return { label: '文本+OCR', color: 'gold' }
    if (ocr > 0) return { label: 'OCR 识别', color: 'orange' }
    return { label: '文本层', color: 'green' }
  }
  return { label: String(q.parser ?? r.file_type).toUpperCase(), color: 'blue' }
}

// 解析质量 → 一行摘要（页数 / 段落 / 行数等）
function parseSummary(r: DocumentItem): string[] {
  const q = r.quality
  if (!q) return []
  const n = (k: string) => Number(q[k]) || 0
  if (q.parser === 'pdf') {
    const lines = [`共 ${r.page_count ?? q.pages ?? 0} 页`]
    const text = n('text_pages')
    const ocr = n('ocr_pages')
    if (text > 0) lines.push(`文本层 ${text} 页`)
    if (ocr > 0) {
      lines.push(`OCR ${ocr} 页`)
      if (q.mean_ocr_confidence != null) lines.push(`OCR 置信度 ${q.mean_ocr_confidence}`)
    }
    return lines
  }
  if (q.parser === 'docx') return [`段落 ${n('paragraphs')}`, `标题 ${n('headings')}`, `表格 ${n('tables')}`]
  if (q.parser === 'excel') return [`工作表 ${n('sheets')}`, `${n('rows')} 行`]
  if (q.parser === 'csv') return [`${n('rows')} 行`]
  if (q.parser === 'markdown' || q.parser === 'text') return [`段落 ${n('paragraphs')}`, `片段 ${n('blocks')}`]
  return [`片段 ${n('blocks')}`]
}

// 解析质量 → 详情键值对（文档详情弹窗 / 悬停明细）
function qualityItems(q: Record<string, unknown>): { label: string; value: string }[] {
  const out: { label: string; value: string }[] = []
  const num = (k: string) => (q[k] != null ? String(q[k]) : '—')
  const parser = String(q.parser ?? '')
  if (parser === 'pdf') {
    out.push({ label: '总页数', value: num('pages') })
    out.push({ label: '文本层页数', value: num('text_pages') })
    out.push({ label: 'OCR页数', value: num('ocr_pages') })
    out.push({ label: '表格数', value: num('tables') })
    out.push({ label: '内容片段', value: num('blocks') })
    out.push({ label: '总字数', value: num('total_chars') })
    out.push({ label: '乱码比例', value: num('garble_ratio') })
    if (q.mean_ocr_confidence != null) out.push({ label: 'OCR平均置信度', value: num('mean_ocr_confidence') })
    return out
  }
  if (parser === 'docx') {
    out.push({ label: '段落', value: num('paragraphs') })
    out.push({ label: '标题', value: num('headings') })
    out.push({ label: '表格', value: num('tables') })
    out.push({ label: '内容片段', value: num('blocks') })
    return out
  }
  if (parser === 'markdown' || parser === 'text') {
    out.push({ label: '段落', value: num('paragraphs') })
    out.push({ label: '内容片段', value: num('blocks') })
    return out
  }
  if (parser === 'excel') {
    out.push({ label: '工作表', value: num('sheets') })
    out.push({ label: '数据行', value: num('rows') })
    out.push({ label: '内容片段', value: num('blocks') })
    return out
  }
  if (parser === 'csv') {
    out.push({ label: '数据行', value: num('rows') })
    out.push({ label: '内容片段', value: num('blocks') })
    return out
  }
  return out
}

function DocDetail({ doc }: { doc: DocumentItem }) {
  const quality = doc.quality
  return (
    <Descriptions column={1} size="small" bordered>
      <Descriptions.Item label="文件名">{doc.filename}</Descriptions.Item>
      <Descriptions.Item label="类型">{doc.file_type}</Descriptions.Item>
      <Descriptions.Item label="大小">{formatBytes(doc.file_size)}</Descriptions.Item>
      <Descriptions.Item label="状态">{STATUS_META[doc.status]?.text ?? doc.status}</Descriptions.Item>
      <Descriptions.Item label="总页数">{doc.page_count ?? '—'}</Descriptions.Item>
      <Descriptions.Item label="内容片段">{doc.chunk_count}</Descriptions.Item>
      <Descriptions.Item label="入库时间">{dayjs(doc.created_at).format('YYYY-MM-DD HH:mm:ss')}</Descriptions.Item>
      {doc.parsed_at && (
        <Descriptions.Item label="解析完成">{dayjs(doc.parsed_at).format('YYYY-MM-DD HH:mm:ss')}</Descriptions.Item>
      )}
      {quality &&
        qualityItems(quality).map((it) => (
          <Descriptions.Item key={it.label} label={it.label}>
            {it.value}
          </Descriptions.Item>
        ))}
      {doc.error_message && <Descriptions.Item label="错误信息">{doc.error_message}</Descriptions.Item>}
    </Descriptions>
  )
}

const ACCEPT = '.pdf,.docx,.md,.markdown,.txt,.xlsx,.csv'

export default function KnowledgeBase() {
  const navigate = useNavigate()
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [activeKb, setActiveKb] = useState<number | null>(null)
  const [kbModal, setKbModal] = useState<{ open: boolean; editing?: KnowledgeBase }>({ open: false })
  const [kbForm, setKbForm] = useState({ name: '', description: '', answer_style: 'standard' })
  const [detailDoc, setDetailDoc] = useState<DocumentItem | null>(null)

  // ---- 文档切片浏览 ----
  const [chunkDocId, setChunkDocId] = useState<number | undefined>(undefined)
  const [chunkPage, setChunkPage] = useState(1)
  const chunkQuery = useQuery<{ total: number; items: ChunkItem[] }>({
    queryKey: ['kb-chunks', activeKb, chunkDocId, chunkPage],
    queryFn: () => kbApi.chunks(activeKb!, { page: chunkPage, page_size: 20, doc_id: chunkDocId }),
    enabled: !!activeKb,
  })

  // ---- 知识库列表 ----
  const { data: kbs = [] } = useQuery<KnowledgeBase[]>({ queryKey: ['admin-kbs'], queryFn: kbApi.list })

  // ---- 文档列表（active kb），有任务未完成时轮询 ----
  const hasActive = (docs: DocumentItem[]) =>
    docs.some((d) => ['pending', 'parsing', 'embedding'].includes(d.status))
  const docsQuery = useQuery<{ items: DocumentItem[]; total: number }>({
    queryKey: ['kb-docs', activeKb],
    queryFn: () => kbApi.documents(activeKb!, { page: 1, page_size: 100 }),
    enabled: !!activeKb,
    refetchInterval: (query) => (hasActive(query.state.data?.items ?? []) ? 2500 : false),
  })
  const docs = docsQuery.data?.items ?? []

  // ---- 上传 ----
  const uploadMutation = useMutation({
    mutationFn: ({ file, kbId }: { file: File; kbId: number }) =>
      kbApi.upload(kbId, file, () => {}),
    onSuccess: () => {
      message.success('上传成功，正在后台入库')
      queryClient.invalidateQueries({ queryKey: ['kb-docs'] })
      queryClient.invalidateQueries({ queryKey: ['admin-kbs'] })
    },
    onError: (e) => message.error(errMsg(e)),
  })

  const deleteDoc = async (id: number) => {
    try {
      await kbApi.documentRemove(id)
      message.success('已删除')
      queryClient.invalidateQueries({ queryKey: ['kb-docs'] })
      queryClient.invalidateQueries({ queryKey: ['admin-kbs'] })
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  const reparse = async (id: number) => {
    try {
      await kbApi.documentReparse(id)
      message.success('已重新排队入库')
      queryClient.invalidateQueries({ queryKey: ['kb-docs'] })
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  const createKb = async () => {
    try {
      await kbApi.create(kbForm)
      message.success('知识库已创建')
      setKbModal({ open: false })
      queryClient.invalidateQueries({ queryKey: ['admin-kbs'] })
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  const renameKb = async () => {
    if (!kbModal.editing) return
    try {
      await kbApi.update(kbModal.editing.id, {
        name: kbForm.name,
        description: kbForm.description,
        answer_style: kbForm.answer_style,
      })
      message.success('已更新')
      setKbModal({ open: false })
      queryClient.invalidateQueries({ queryKey: ['admin-kbs'] })
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  const deleteKb = async (id: number) => {
    try {
      await kbApi.remove(id)
      message.success('知识库已删除')
      if (activeKb === id) setActiveKb(null)
      queryClient.invalidateQueries({ queryKey: ['admin-kbs'] })
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  // ---- 检索预览 ----
  const [searchQ, setSearchQ] = useState('')
  const searchQuery = useQuery<{ hits: Citation[] }>({
    queryKey: ['kb-search', activeKb, searchQ],
    queryFn: () => kbApi.search({ q: searchQ, kb_id: activeKb ?? undefined, top_k: 5 }),
    enabled: searchQ.trim().length > 0 && !!activeKb,
  })

  const columns = [
    {
      title: '文件',
      dataIndex: 'filename',
      key: 'filename',
      ellipsis: true,
      render: (v: string, r: DocumentItem) => (
        <Space>
          <FileOutlined style={{ color: '#1677ff' }} />
          <a onClick={() => setDetailDoc(r)} title="查看解析详情">
            {v}
          </a>
        </Space>
      ),
    },
    { title: '类型', dataIndex: 'file_type', key: 'file_type', width: 80 },
    {
      title: '大小',
      dataIndex: 'file_size',
      key: 'file_size',
      width: 90,
      render: (v: number) => formatBytes(v),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 140,
      render: (s: string, r: DocumentItem) => {
        const meta = STATUS_META[s] || { text: s, color: 'default' }
        const pct = STAGE_PCT[s]
        const ocr = r.progress
        const ocrPct = ocr && ocr.total ? (ocr.percent ?? 0) : null
        return (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'flex-start' }}>
            <Tag color={meta.color}>{meta.text}</Tag>
            {ocrPct != null ? (
              <>
                <Progress percent={ocrPct} size="small" strokeWidth={5} style={{ width: 90 }} />
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                  OCR {ocr?.done ?? 0}/{ocr?.total ?? 0} 页
                </Typography.Text>
              </>
            ) : pct != null ? (
              <Progress percent={pct} showInfo={false} status="active" strokeWidth={4} style={{ width: 72 }} />
            ) : null}
          </div>
        )
      },
    },
    {
      title: 'Chunks',
      dataIndex: 'chunk_count',
      key: 'chunk_count',
      width: 80,
      render: (v: number, r: DocumentItem) =>
        r.status === 'failed' ? (
          <Tooltip title={r.error_message}>
            <Typography.Text type="danger" style={{ cursor: 'help' }}>失败</Typography.Text>
          </Tooltip>
        ) : (
          v
        ),
    },
    {
      title: '解析质量',
      dataIndex: 'quality',
      key: 'quality',
      width: 210,
      render: (_: unknown, r: DocumentItem) => {
        if (r.status === 'failed') return <Tag color="error">解析失败</Tag>
        if (!r.quality) return <Typography.Text type="secondary">—</Typography.Text>
        const { label, color } = parseLabel(r)
        const lines = parseSummary(r)
        return (
          <Tooltip
            title={
              <div style={{ maxWidth: 260 }}>
                {qualityItems(r.quality).map((it) => (
                  <div key={it.label} style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
                    <span>{it.label}</span>
                    <span style={{ color: '#d9d9d9' }}>{it.value}</span>
                  </div>
                ))}
              </div>
            }
          >
            <div>
              <Tag color={color}>{label}</Tag>
              <Typography.Text style={{ fontSize: 12, color: '#666', display: 'block' }}>{lines.join(' · ')}</Typography.Text>
            </div>
          </Tooltip>
        )
      },
    },
    { title: '上传时间', dataIndex: 'created_at', key: 'created_at', width: 110, render: (v: string) => dayjs(v).format('MM-DD HH:mm') },
    {
      title: '操作',
      key: 'action',
      width: 110,
      render: (_: unknown, r: DocumentItem) => (
        <Space>
          <Tooltip title="重新入库">
            <Button size="small" icon={<RedoOutlined />} onClick={() => reparse(r.id)} />
          </Tooltip>
          <Popconfirm title="删除该文档？" okText="删除" cancelText="取消" onConfirm={() => deleteDoc(r.id)}>
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ background: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderBottom: '1px solid #f0f0f0', paddingInline: 24 }}>
        <Space>
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/chat')}>
            返回问答
          </Button>
          <DatabaseOutlined style={{ color: '#1677ff', fontSize: 18 }} />
          <Typography.Text strong>知识库管理</Typography.Text>
          <Tag color="red">管理员</Tag>
        </Space>
        <UserMenu />
      </Header>
      <Content style={{ padding: 24 }}>
        <div style={{ display: 'flex', gap: 16 }}>
          {/* 左：知识库列表 */}
          <Card size="small" title="知识库" style={{ width: 280, flexShrink: 0 }} styles={{ body: { padding: 8 } }}>
            <Space direction="vertical" style={{ width: '100%' }}>
              <Button
                type="primary"
                block
                icon={<PlusOutlined />}
                onClick={() => {
                  setKbForm({ name: '', description: '', answer_style: 'standard' })
                  setKbModal({ open: true })
                }}
              >
                新建知识库
              </Button>
              <List
                dataSource={kbs}
                style={{ maxHeight: 'calc(100vh - 260px)', overflowY: 'auto' }}
                renderItem={(kb) => (
                  <div
                    onClick={() => setActiveKb(kb.id)}
                    style={{
                      padding: '8px 10px',
                      borderRadius: 8,
                      cursor: 'pointer',
                      marginBottom: 4,
                      background: activeKb === kb.id ? '#e6f4ff' : 'transparent',
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
                            setKbForm({
                              name: kb.name,
                              description: kb.description || '',
                              answer_style: kb.answer_style || 'standard',
                            })
                            setKbModal({ open: true, editing: kb })
                          }}
                        />
                      </Tooltip>
                      <Tooltip title="删除">
                        <Popconfirm title="删除该知识库及其全部文档？" okText="删除" cancelText="取消" onConfirm={() => deleteKb(kb.id)}>
                          <Button size="small" type="text" danger icon={<DeleteOutlined />} onClick={(e) => e.stopPropagation()} />
                        </Popconfirm>
                      </Tooltip>
                    </Space>
                  </div>
                )}
              />
            </Space>
          </Card>

          {/* 右：文档 + 检索预览 */}
          <Card
            size="small"
            style={{ flex: 1, minWidth: 0 }}
            title={activeKb ? kbs.find((k) => k.id === activeKb)?.name : '请选择知识库'}
            extra={
              activeKb ? (
                <Upload
                  accept={ACCEPT}
                  showUploadList={false}
                  multiple
                  beforeUpload={(file) => {
                    if (file.size > 200 * 1024 * 1024) {
                      message.error('文件超过 200MB 限制')
                      return Upload.LIST_IGNORE
                    }
                    uploadMutation.mutate({ file, kbId: activeKb })
                    return false
                  }}
                >
                  <Button type="primary" icon={<UploadOutlined />} loading={uploadMutation.isPending}>
                    上传文档
                  </Button>
                </Upload>
              ) : undefined
            }
          >
            {!activeKb ? (
              <Empty description="请先选择或创建知识库" />
            ) : (
              <Tabs
                items={[
                  {
                    key: 'docs',
                    label: `文档 (${docs.length})`,
                    children: (
                      <Table
                        rowKey="id"
                        columns={columns}
                        dataSource={docs}
                        size="small"
                        pagination={false}
                        loading={docsQuery.isFetching && docs.length === 0}
                      />
                    ),
                  },
                  {
                    key: 'search',
                    label: '检索预览',
                    children: (
                      <div style={{ maxWidth: 720 }}>
                        <Space.Compact style={{ width: '100%', marginBottom: 16 }}>
                          <Input
                            value={searchQ}
                            onChange={(e) => setSearchQ(e.target.value)}
                            onPressEnter={() => searchQuery.refetch()}
                            placeholder="输入关键词验证检索质量（如：明渠均匀流）"
                          />
                          <Button type="primary" icon={<SearchOutlined />} onClick={() => searchQuery.refetch()}>
                            检索
                          </Button>
                        </Space.Compact>
                        {searchQuery.data?.hits?.length ? (
                          <List
                            dataSource={searchQuery.data.hits}
                            renderItem={(h, i) => (
                              <List.Item>
                                <div style={{ width: '100%' }}>
                                  <Space wrap style={{ marginBottom: 4 }}>
                                    <Tag color="blue">#{h.rank}</Tag>
                                    <Typography.Text strong>{h.source}</Typography.Text>
                                    {h.page && <Tag>第{h.page}页</Tag>}
                                    <Tag>相关度 {h.score?.toFixed(3)}</Tag>
                                  </Space>
                                  {h.section && (
                                    <Typography.Paragraph type="secondary" style={{ margin: 0, fontSize: 12 }}>
                                      章节：{h.section}
                                    </Typography.Paragraph>
                                  )}
                                  <div className="citation-snippet" style={{ marginTop: 4 }}>
                                    {h.snippet}
                                  </div>
                                </div>
                              </List.Item>
                            )}
                          />
                        ) : searchQ ? (
                          <Empty description="无检索结果" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                        ) : (
                          <Typography.Text type="secondary">输入关键词进行检索验证</Typography.Text>
                        )}
                      </div>
                    ),
                  },
                  {
                    key: 'chunks',
                    label: '文档切片',
                    children: (
                      <div>
                        <Space style={{ marginBottom: 12 }} wrap>
                          <Select
                            allowClear
                            placeholder="按文档筛选"
                            style={{ width: 320 }}
                            value={chunkDocId}
                            onChange={(v) => {
                              setChunkDocId(v)
                              setChunkPage(1)
                            }}
                            options={docs.map((d) => ({ value: d.id, label: d.filename }))}
                          />
                          <Typography.Text type="secondary">
                            共 {chunkQuery.data?.total ?? 0} 个切片
                          </Typography.Text>
                        </Space>
                        {chunkQuery.data?.items?.length ? (
                          <List
                            dataSource={chunkQuery.data.items}
                            pagination={{
                              current: chunkPage,
                              pageSize: 20,
                              total: chunkQuery.data.total,
                              showSizeChanger: false,
                              onChange: setChunkPage,
                            }}
                            renderItem={(c) => (
                              <List.Item>
                                <div style={{ width: '100%' }}>
                                  <Space wrap style={{ marginBottom: 4 }}>
                                    <Tag>#{c.chunk_index}</Tag>
                                    {c.page != null && <Tag>第{c.page}页</Tag>}
                                    {c.section && <Tag color="blue">{c.section}</Tag>}
                                  </Space>
                                  <div className="citation-snippet" style={{ fontSize: 13 }}>
                                    {c.content}
                                  </div>
                                </div>
                              </List.Item>
                            )}
                          />
                        ) : chunkQuery.isFetched ? (
                          <Empty
                            description="无切片（文档可能仍在解析中）"
                            image={Empty.PRESENTED_IMAGE_SIMPLE}
                          />
                        ) : null}
                      </div>
                    ),
                  },
                ]}
              />
            )}
          </Card>
        </div>
      </Content>

      {/* 新建/编辑知识库 */}
      <Modal
        title={kbModal.editing ? '编辑知识库' : '新建知识库'}
        open={kbModal.open}
        onCancel={() => setKbModal({ open: false })}
        onOk={kbModal.editing ? renameKb : createKb}
        okText={kbModal.editing ? '保存' : '创建'}
      >
        <Space direction="vertical" style={{ width: '100%', marginTop: 16 }}>
          <Input placeholder="知识库名称（如：水力学）" value={kbForm.name} onChange={(e) => setKbForm({ ...kbForm, name: e.target.value })} />
          <Input.TextArea
            placeholder="描述（可选）"
            rows={3}
            value={kbForm.description}
            onChange={(e) => setKbForm({ ...kbForm, description: e.target.value })}
          />
          <div>
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              回答风格
            </Typography.Text>
            <Select
              value={kbForm.answer_style}
              onChange={(v) => setKbForm({ ...kbForm, answer_style: v })}
              style={{ width: '100%', marginTop: 4 }}
              options={ANSWER_STYLE_OPTIONS}
            />
          </div>
        </Space>
      </Modal>

      {/* 文档解析详情 */}
      <Modal title="文档解析详情" open={!!detailDoc} onCancel={() => setDetailDoc(null)} footer={null} width={560}>
        {detailDoc && <DocDetail doc={detailDoc} />}
      </Modal>
    </Layout>
  )
}
