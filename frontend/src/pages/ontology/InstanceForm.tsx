/**
 * 新建 / 编辑一个本体实例（ABox 节点）。
 *
 * 表单字段**由 TBox 自动生成**：`profiles/tuning.yaml` 里给某个实体类型加一个属性，
 * 这个弹窗下次打开就会自动多出一格——不用改前端代码。
 *
 * 证据是**列表**（P1）：一个实例往往有多条依据（手册 + 教材 + 台账），
 * 每条还要标清楚支持哪个属性。以前这里只有固定三格、只编辑 evidence[0]，
 * 第二条依据既看不见也改不了。现在每条依据独立一行，可增可删、可点回原文。
 */
import { useEffect, useState } from 'react'
import {
  AutoComplete,
  Button,
  Card,
  Col,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { DeleteOutlined, FileSearchOutlined, PlusOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { kbApi } from '@/api/modules'
import type { OntologyEntityType, OntologyEvidence, OntologyInstance } from '@/api/types'
import EvidenceChunkModal from '@/components/EvidenceChunkModal'

const { Text } = Typography

const EVIDENCE_TYPES = ['手册', '教材', '规范', '文献', '台账', '人工录入']

interface Props {
  open: boolean
  entityTypes: OntologyEntityType[]
  initial: OntologyInstance | null
  submitting: boolean
  onCancel: () => void
  onSubmit: (values: {
    entity_type: string
    name: string
    props: Record<string, unknown>
    evidence: OntologyEvidence[]
  }) => void
}

/** 按 TBox 的属性类型选控件 */
function fieldOf(attr: { name: string; type: string; values?: string[] }) {
  if (attr.type === 'enum' && attr.values?.length) {
    return (
      <Select
        allowClear
        options={attr.values.map((v) => ({ value: v, label: v }))}
        placeholder="请选择"
      />
    )
  }
  if (attr.type === 'number') return <InputNumber style={{ width: '100%' }} />
  if (attr.type === 'boolean') return <Switch />
  if (attr.type === 'text') return <Input.TextArea rows={2} />
  return <Input placeholder="可留空" />
}

/** 一条依据的溯源状态：能点回原文 / 只到文档 / 只有一句话 */
function TraceBadge({ ev }: { ev: OntologyEvidence }) {
  if (ev.chunk_id) return <Tag color="success">可回溯原文</Tag>
  if (ev.doc_id) {
    return (
      <Tooltip title="文档找到了，但没定位到具体切片：补页码或 ≥6 字的原文摘录就能定位">
        <Tag color="warning">仅到文档</Tag>
      </Tooltip>
    )
  }
  return (
    <Tooltip title="没有指定知识库里的文档，按「人工来源」处理（如现场台账、专家经验）">
      <Tag>未溯源</Tag>
    </Tooltip>
  )
}

export default function InstanceForm({
  open,
  entityTypes,
  initial,
  submitting,
  onCancel,
  onSubmit,
}: Props) {
  const [form] = Form.useForm()
  const isEdit = Boolean(initial)
  const [evidence, setEvidence] = useState<OntologyEvidence[]>([])
  const [preview, setPreview] = useState<{ chunkId: number; quote?: string | null } | null>(null)

  // 知识库里的真实文档名，供下拉选择（也允许手填：台账类依据本来就不在任何文档里）
  const { data: docOptions = [] } = useQuery<string[]>({
    queryKey: ['ontology-evidence-doc-options'],
    queryFn: async () => {
      const kbs = (await kbApi.list()) as { id: number; name: string }[]
      const pages = await Promise.all(
        kbs.map((kb) => kbApi.documents(kb.id, { page: 1, page_size: 100 })),
      )
      const names = new Set<string>()
      for (const page of pages) {
        for (const doc of (page?.items ?? []) as { filename?: string }[]) {
          if (doc?.filename) names.add(doc.filename)
        }
      }
      return [...names].sort()
    },
    enabled: open,
    staleTime: 5 * 60_000,
    retry: false,
  })

  useEffect(() => {
    if (!open) return
    if (initial) {
      form.setFieldsValue({
        entity_type: initial.entity_type,
        name: initial.name,
        ...(initial.props ?? {}),
      })
      setEvidence(initial.evidence ?? [])
    } else {
      form.resetFields()
      form.setFieldsValue({ entity_type: entityTypes[0]?.name })
      setEvidence([])
    }
  }, [open, initial, entityTypes, form])

  const selectedType = Form.useWatch('entity_type', form)
  const attrs = entityTypes.find((t) => t.name === selectedType)?.attributes ?? []

  /** 改「文档 / 页码 / 摘录」必须清掉旧的定位结果，否则服务端会认为已经锚定好了 */
  const updateEvidence = (index: number, patch: Partial<OntologyEvidence>) => {
    setEvidence((prev) =>
      prev.map((ev, i) => (i === index ? { ...ev, ...patch } : ev)),
    )
  }
  const updateLocator = (index: number, patch: Partial<OntologyEvidence>) => {
    updateEvidence(index, {
      ...patch,
      doc_id: null,
      chunk_id: null,
      traceable: false,
      resolved_by: undefined,
    })
  }

  const handleOk = async () => {
    const values = await form.validateFields()
    const props: Record<string, unknown> = {}
    for (const attr of attrs) {
      const value = values[attr.name]
      if (value !== undefined && value !== null && value !== '') props[attr.name] = value
    }
    // 空的「依据」行不该入库：说明和文档都没填就等于没写
    const cleaned = evidence.filter((ev) => (ev.note || '').trim() || (ev.doc_name || '').trim())
    onSubmit({
      entity_type: values.entity_type,
      name: values.name,
      props,
      evidence: cleaned,
    })
  }

  return (
    <Modal
      open={open}
      title={isEdit ? `编辑实例：${initial?.name}` : '新建本体实例'}
      onCancel={onCancel}
      onOk={handleOk}
      confirmLoading={submitting}
      okText="保存"
      width={860}
      destroyOnClose
    >
      <Form form={form} layout="vertical">
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item
              name="entity_type"
              label="实体类型"
              rules={[{ required: true, message: '请选择实体类型' }]}
            >
              <Select
                disabled={isEdit}
                options={entityTypes.map((t) => ({
                  value: t.name,
                  label: `${t.name}${t.display_name ? `（${t.display_name}）` : ''}`,
                }))}
              />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item
              name="name"
              label="标识（英文/代号）"
              rules={[{ required: true, message: '请填标识' }]}
              tooltip={isEdit ? '标识是主键，要改请删了重建' : '如参数代号 sm、水库名等'}
            >
              <Input disabled={isEdit} placeholder="如 sm" />
            </Form.Item>
          </Col>
        </Row>

        <Text type="secondary" style={{ fontSize: 12 }}>
          下面的字段来自 TBox 定义，改本体 YAML 就会自动增减
        </Text>
        <Row gutter={16} style={{ marginTop: 8 }}>
          {attrs.map((attr) => (
            <Col span={12} key={attr.name}>
              <Form.Item
                name={attr.name}
                label={`${attr.name}${attr.description ? ` · ${attr.description}` : ''}`}
                rules={
                  attr.required ? [{ required: true, message: `${attr.name} 是必填项` }] : []
                }
              >
                {fieldOf(attr)}
              </Form.Item>
            </Col>
          ))}
        </Row>

        <Divider orientation="left" style={{ margin: '8px 0 12px' }}>
          依据
        </Divider>
        <Text type="secondary" style={{ fontSize: 12 }}>
          没有依据的数值不会出现在推荐卡里。文档名选知识库里的真实文档，保存后程序会自动定位到具体切片，点「原文」可以对照。
        </Text>

        <div style={{ marginTop: 10 }}>
          {evidence.length === 0 && (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="还没有依据"
              style={{ margin: '8px 0' }}
            />
          )}
          {evidence.map((ev, index) => (
            <Card
              key={index}
              size="small"
              style={{ marginBottom: 8 }}
              title={
                <Space size={6}>
                  <span style={{ fontSize: 13 }}>依据 {index + 1}</span>
                  <TraceBadge ev={ev} />
                  {ev.printed_page ? <Tag>书内第 {ev.printed_page} 页</Tag> : null}
                  {ev.chunk_id ? (
                    <Button
                      size="small"
                      type="link"
                      icon={<FileSearchOutlined />}
                      onClick={() => setPreview({ chunkId: ev.chunk_id as number, quote: ev.quote })}
                    >
                      原文
                    </Button>
                  ) : null}
                </Space>
              }
              extra={
                <Button
                  size="small"
                  type="text"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => setEvidence((prev) => prev.filter((_, i) => i !== index))}
                />
              }
            >
              <Row gutter={12}>
                <Col span={6}>
                  <Form.Item label="类型" style={{ marginBottom: 10 }}>
                    <AutoComplete
                      options={EVIDENCE_TYPES.map((v) => ({ value: v }))}
                      value={ev.type}
                      placeholder="手册 / 台账"
                      onChange={(value) => updateEvidence(index, { type: value })}
                    />
                  </Form.Item>
                </Col>
                <Col span={18}>
                  <Form.Item label="依据说明" style={{ marginBottom: 10 }}>
                    <Input
                      value={ev.note}
                      placeholder="如：《新安江模型手册》参数表 取值范围 [5,50]，默认 10"
                      onChange={(e) => updateEvidence(index, { note: e.target.value })}
                    />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item
                    label="支持属性"
                    style={{ marginBottom: 10 }}
                    tooltip="这条依据证明的是哪个属性的值；不选表示支持整条实例"
                  >
                    <Select
                      mode="multiple"
                      allowClear
                      value={ev.attributes ?? []}
                      placeholder="可多选，如 取值范围 / 初值"
                      options={attrs.map((a) => ({
                        value: a.name,
                        label: `${a.name}${a.description ? ` · ${a.description}` : ''}`,
                      }))}
                      onChange={(value) => updateEvidence(index, { attributes: value })}
                    />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item label="文档名（可选）" style={{ marginBottom: 10 }}>
                    <AutoComplete
                      value={ev.doc_name ?? undefined}
                      options={docOptions.map((name) => ({ value: name }))}
                      placeholder="选知识库里的文档，或手填"
                      filterOption={(input, option) =>
                        String(option?.value ?? '').includes(input)
                      }
                      onChange={(value) => updateLocator(index, { doc_name: value })}
                    />
                  </Form.Item>
                </Col>
                <Col span={6}>
                  <Form.Item
                    label="PDF 页码（可选）"
                    style={{ marginBottom: 10 }}
                    tooltip="PDF 阅读器里显示的页序；书里印的页码由程序自动识别，不用填"
                  >
                    <InputNumber
                      style={{ width: '100%' }}
                      min={1}
                      value={ev.page ?? undefined}
                      onChange={(value) => updateLocator(index, { page: value as number })}
                    />
                  </Form.Item>
                </Col>
                <Col span={18}>
                  <Form.Item
                    label="原文摘录（可选，≥6 字）"
                    style={{ marginBottom: 0 }}
                    tooltip="从原文里抄一小段。有页码就按页码找，抄得越准越容易定位到具体切片"
                  >
                    <Input
                      value={ev.snippet ?? undefined}
                      placeholder="如：自由水蓄水容量 5～50"
                      onChange={(e) => updateLocator(index, { snippet: e.target.value })}
                    />
                  </Form.Item>
                </Col>
              </Row>
            </Card>
          ))}
          <Button
            block
            type="dashed"
            icon={<PlusOutlined />}
            onClick={() => setEvidence((prev) => [...prev, { type: '手册' }])}
          >
            添加依据
          </Button>
        </div>
      </Form>

      <EvidenceChunkModal
        open={Boolean(preview)}
        chunkId={preview?.chunkId ?? null}
        quote={preview?.quote}
        onClose={() => setPreview(null)}
      />
    </Modal>
  )
}
