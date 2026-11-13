/**
 * 新建 / 编辑一个本体实例（ABox 节点）。
 *
 * 表单字段**由 TBox 自动生成**：`profiles/tuning.yaml` 里给某个实体类型加一个属性，
 * 这个弹窗下次打开就会自动多出一格——不用改前端代码。
 */
import { useEffect } from 'react'
import { Col, Form, Input, InputNumber, Modal, Row, Select, Switch, Typography } from 'antd'
import type { OntologyEntityType, OntologyInstance } from '@/api/types'

const { Text } = Typography

interface Props {
  open: boolean
  entityTypes: OntologyEntityType[]
  initial: OntologyInstance | null
  submitting: boolean
  onCancel: () => void
  onSubmit: (values: { entity_type: string; name: string; props: Record<string, unknown>; evidence: unknown[] }) => void
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

  useEffect(() => {
    if (!open) return
    if (initial) {
      form.setFieldsValue({
        entity_type: initial.entity_type,
        name: initial.name,
        ...(initial.props ?? {}),
        __evidence_type: initial.evidence?.[0]?.type,
        __evidence_note: initial.evidence?.[0]?.note,
        __evidence_doc: initial.evidence?.[0]?.doc_name,
        __evidence_page: initial.evidence?.[0]?.page,
      })
    } else {
      form.resetFields()
      form.setFieldsValue({ entity_type: entityTypes[0]?.name })
    }
  }, [open, initial, entityTypes, form])

  const selectedType = Form.useWatch('entity_type', form)
  const attrs =
    entityTypes.find((t) => t.name === selectedType)?.attributes ?? []

  const handleOk = async () => {
    const values = await form.validateFields()
    const props: Record<string, unknown> = {}
    for (const attr of attrs) {
      const value = values[attr.name]
      if (value !== undefined && value !== null && value !== '') props[attr.name] = value
    }
    // 只在「依据」三格被真正改动过时才生成新证据。
    // 之前这里每次都把表单里预填的旧证据再追加一遍，改两次实例证据就变成 3 条（用户实测）。
    const draft =
      values.__evidence_note || values.__evidence_doc
        ? {
            type: values.__evidence_type || '人工录入',
            note: values.__evidence_note || '',
            doc_name: values.__evidence_doc || undefined,
            page: values.__evidence_page ?? undefined,
            traceable: false,
            resolved_by: 'none',
          }
        : null
    const existing = initial?.evidence ?? []
    const dup = draft
      ? existing.some(
          (e) =>
            (e.type ?? '') === draft.type &&
            (e.note ?? '') === draft.note &&
            (e.doc_name ?? '') === (draft.doc_name ?? '') &&
            (e.page ?? null) === (draft.page ?? null),
        )
      : true
    onSubmit({
      entity_type: values.entity_type,
      name: values.name,
      props,
      evidence: draft && !dup ? [...existing, draft] : existing,
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
      width={720}
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

        <Text type="secondary" style={{ fontSize: 12 }}>
          依据（强烈建议填：没有依据的数值不会出现在推荐卡里）
        </Text>
        <Row gutter={16} style={{ marginTop: 8 }}>
          <Col span={8}>
            <Form.Item name="__evidence_type" label="依据类型">
              <Select
                allowClear
                options={['教材', '手册', '规范', '文献', '人工录入'].map((v) => ({
                  value: v,
                  label: v,
                }))}
              />
            </Form.Item>
          </Col>
          <Col span={16}>
            <Form.Item name="__evidence_note" label="依据说明">
              <Input placeholder="如：《新安江模型手册》参数表 取值范围 [5,50]，默认 10" />
            </Form.Item>
          </Col>
          <Col span={16}>
            <Form.Item name="__evidence_doc" label="文档名（可选）">
              <Input placeholder="如 新安江模型手册.pdf" />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="__evidence_page" label="页码（可选）">
              <InputNumber style={{ width: '100%' }} min={1} />
            </Form.Item>
          </Col>
        </Row>
      </Form>
    </Modal>
  )
}
