/**
 * 知识库管理页（G6-1 拆分后的主文件）。
 *
 * 本文件只保留：页面状态、数据查询、操作回调、布局编排。
 * 展示细节已拆到同目录 knowledge/ 与 components/DocDetail：
 *   - documentMeta.ts  常量 + 纯函数
 *   - DocumentTable    文档表格列
 *   - UploadProgress   批量上传进度
 *   - SearchPreview    检索预览
 *   - ChunkBrowser     文档切片
 *   - KbList           左侧知识库列表
 */
import { useRef, useState } from 'react'
import {
  App,
  Button,
  Card,
  Empty,
  Input,
  Layout,
  Modal,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { ArrowLeftOutlined, DatabaseOutlined, UploadOutlined } from '@ant-design/icons'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { kbApi } from '@/api/modules'
import type { DocumentItem, KnowledgeBase } from '@/api/types'
import { ANSWER_STYLE_OPTIONS } from '@/api/types'
import { errMsg } from '@/api/client'
import UserMenu from '@/components/UserMenu'
import DocDetail from '@/components/DocDetail'
import { useNavigate } from 'react-router-dom'
import {
  ACCEPT,
  DOC_TYPE_OPTIONS,
  PARSE_MODE_OPTIONS,
  type DocType,
  type ParseMode,
  type UploadTask,
} from './knowledge/documentMeta'
import { buildDocumentColumns } from './knowledge/DocumentTable'
import UploadProgress from './knowledge/UploadProgress'
import SearchPreview from './knowledge/SearchPreview'
import ChunkBrowser from './knowledge/ChunkBrowser'
import KbList from './knowledge/KbList'

const { Header, Content } = Layout

const DOCS_PAGE_SIZE = 20

export default function KnowledgeBase() {
  const navigate = useNavigate()
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [activeKb, setActiveKb] = useState<number | null>(null)
  // P0-11 文档类型（未来 DSH 引用来源判断）：textbook 教材 / standard 规范 / manual 手册 / other 其他
  const [docType, setDocType] = useState<DocType>('other')
  const [parseMode, setParseMode] = useState<ParseMode>('fast')
  const [kbModal, setKbModal] = useState<{ open: boolean; editing?: KnowledgeBase }>({ open: false })
  const [kbForm, setKbForm] = useState({ name: '', description: '', answer_style: 'standard' })
  const [detailDoc, setDetailDoc] = useState<DocumentItem | null>(null)
  // 批量上传任务列表（per-file 进度）
  const [uploadTasks, setUploadTasks] = useState<UploadTask[]>([])
  const [uploading, setUploading] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // ---- 知识库列表 ----
  const { data: kbs = [] } = useQuery<KnowledgeBase[]>({ queryKey: ['admin-kbs'], queryFn: kbApi.list })

  // ---- 文档列表（active kb），有任务未完成时轮询 ----
  const [docsPage, setDocsPage] = useState(1)
  const hasActive = (docs: DocumentItem[]) =>
    docs.some((d) => ['pending', 'parsing', 'embedding'].includes(d.status))
  const docsQuery = useQuery<{ items: DocumentItem[]; total: number }>({
    queryKey: ['kb-docs', activeKb, docsPage],
    queryFn: () => kbApi.documents(activeKb!, { page: docsPage, page_size: DOCS_PAGE_SIZE }),
    enabled: !!activeKb,
    refetchInterval: (query) => (hasActive(query.state.data?.items ?? []) ? 2500 : false),
  })
  const docs = docsQuery.data?.items ?? []
  const docsTotal = docsQuery.data?.total ?? 0

  // 切片筛选下拉需要「该库全部文档名」而非当前页——独立拉一份（100 上限，与分页前一致）
  const allDocsQuery = useQuery<{ items: DocumentItem[] }>({
    queryKey: ['kb-all-docs', activeKb],
    queryFn: () => kbApi.documents(activeKb!, { page: 1, page_size: 100 }),
    enabled: !!activeKb,
  })
  const allDocs = allDocsQuery.data?.items ?? []

  // ---- 上传（批量，per-file 进度）----
  const uploadFiles = async (files: File[]) => {
    if (!activeKb || files.length === 0) return
    const tasks: UploadTask[] = files.map((f) => ({
      key: `${Date.now()}-${f.name}-${Math.random().toString(36).slice(2, 6)}`,
      filename: f.name,
      pct: 0,
      status: 'uploading',
    }))
    setUploadTasks((prev) => [...prev, ...tasks])
    setUploading(true)

    let ok = 0
    let fail = 0
    // 逐个串行上传（大文件不并发打满带宽/内存），每个文件独立更新进度
    for (let i = 0; i < files.length; i++) {
      const task = tasks[i]
      try {
        await kbApi.upload(activeKb, files[i], docType, (pct) => {
          setUploadTasks((prev) => prev.map((t) => (t.key === task.key ? { ...t, pct } : t)))
        }, parseMode)
        ok++
        setUploadTasks((prev) =>
          prev.map((t) => (t.key === task.key ? { ...t, pct: 100, status: 'done' } : t)),
        )
      } catch (e) {
        fail++
        setUploadTasks((prev) =>
          prev.map((t) => (t.key === task.key ? { ...t, status: 'error', error: errMsg(e) } : t)),
        )
      }
    }
    setUploading(false)
    queryClient.invalidateQueries({ queryKey: ['kb-docs'] })
    queryClient.invalidateQueries({ queryKey: ['admin-kbs'] })
    if (fail === 0) {
      message.success(`${ok} 个文件上传成功，正在后台入库`)
    } else if (ok > 0) {
      message.warning(`${ok} 个成功，${fail} 个失败（详见上传进度）`)
    } else {
      message.error(`${fail} 个文件全部上传失败`)
    }
  }

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

  const columns = buildDocumentColumns({
    onOpenDetail: setDetailDoc,
    onReparse: reparse,
    onDelete: deleteDoc,
  })

  const openCreateKb = () => {
    setKbForm({ name: '', description: '', answer_style: 'standard' })
    setKbModal({ open: true })
  }

  const openEditKb = (kb: KnowledgeBase) => {
    setKbForm({
      name: kb.name,
      description: kb.description || '',
      answer_style: kb.answer_style || 'standard',
    })
    setKbModal({ open: true, editing: kb })
  }

  return (
    <Layout style={{ minHeight: '100vh' }}>
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
          <KbList
            kbs={kbs}
            activeKb={activeKb}
            onSelect={setActiveKb}
            onCreate={openCreateKb}
            onEdit={openEditKb}
            onDelete={deleteKb}
          />

          {/* 右：文档 + 检索预览 + 切片 */}
          <Card
            size="small"
            style={{ flex: 1, minWidth: 0 }}
            title={activeKb ? kbs.find((k) => k.id === activeKb)?.name : '请选择知识库'}
            extra={
              activeKb ? (
                <Space>
                  <Select
                    size="small"
                    value={docType}
                    onChange={setDocType}
                    style={{ width: 110 }}
                    options={DOC_TYPE_OPTIONS}
                    placeholder="文档类型"
                  />
                  <Tooltip
                    title={
                      parseMode === 'fast'
                        ? '快速：解析快，适合普通文档（纯文字、少量公式表格）。'
                        : '高精度：公式、数字、符号、标题识别更准，适合公式/表格密集的文档；解析慢约 40%。'
                    }
                  >
                    <Select
                      size="small"
                      value={parseMode}
                      onChange={setParseMode}
                      style={{ width: 100 }}
                      options={PARSE_MODE_OPTIONS}
                      placeholder="解析模式"
                    />
                  </Tooltip>
                  <Button type="primary" icon={<UploadOutlined />} loading={uploading} onClick={() => fileInputRef.current?.click()}>
                    上传文档
                  </Button>
                  {/* 原生文件选择：一次拿到整批文件做串行上传（AntD Upload 的 multiple
                      逐个 beforeUpload 拿不到完整批次，无法聚合整体进度与成败统计） */}
                  <input
                    ref={fileInputRef}
                    type="file"
                    multiple
                    accept={ACCEPT}
                    style={{ display: 'none' }}
                    onChange={(e) => {
                      const files = Array.from(e.target.files ?? [])
                      const valid = files.filter((f) => {
                        if (f.size > 200 * 1024 * 1024) {
                          message.error(`文件「${f.name}」超过 200MB 限制，已跳过`)
                          return false
                        }
                        return true
                      })
                      if (valid.length) uploadFiles(valid)
                      e.target.value = '' // 清空，允许再次选择同一文件
                    }}
                  />
                </Space>
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
                    label: `文档 (${docsTotal})`,
                    children: (
                      <div>
                        <UploadProgress
                          tasks={uploadTasks}
                          uploading={uploading}
                          onClear={() => setUploadTasks([])}
                        />
                        <Table
                          rowKey="id"
                          columns={columns}
                          dataSource={docs}
                          size="small"
                          pagination={{
                            current: docsPage,
                            pageSize: DOCS_PAGE_SIZE,
                            total: docsTotal,
                            showSizeChanger: false,
                            showTotal: (t) => `共 ${t} 个文档`,
                            onChange: (p) => setDocsPage(p),
                          }}
                          loading={docsQuery.isFetching && docs.length === 0}
                        />
                      </div>
                    ),
                  },
                  {
                    key: 'search',
                    label: '检索预览',
                    children: <SearchPreview kbId={activeKb} />,
                  },
                  {
                    key: 'chunks',
                    label: '文档切片',
                    children: <ChunkBrowser kbId={activeKb} allDocs={allDocs} />,
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
