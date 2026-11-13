/**
 * 图上拖线后弹出的「建关系」小窗。
 *
 * 只列**同时满足**两端类型的关系：主语类型 == source 且宾语类型 == target，
 * 所以界面上选不出非法组合。
 */
import { useEffect, useMemo } from 'react'
import { Form, Input, Modal, Select, Typography } from 'antd'
import type { OntologyTypes } from '@/api/types'
import type { GraphNode } from './canvasTheme'

const { Text } = Typography

interface Props {
  pair: { subject: GraphNode; object: GraphNode } | null
  types: OntologyTypes | undefined
  submitting: boolean
  onCancel: () => void
  onSubmit: (values: { predicate: string; note?: string }) => void
}

export default function LinkModal({ pair, types, submitting, onCancel, onSubmit }: Props) {
  const [form] = Form.useForm()

  const options = useMemo(() => {
    if (!pair) return []
    return (types?.relation_types ?? [])
      .filter((r) => r.source === pair.subject.entity_type && r.target === pair.object.entity_type)
      .map((r) => ({ value: r.name, label: `${r.name}（${r.source} → ${r.target}）` }))
  }, [types, pair])

  useEffect(() => {
    if (pair) form.resetFields()
  }, [pair, form])

  const handleOk = async () => {
    const values = await form.validateFields()
    onSubmit({ predicate: values.predicate, note: values.note })
  }

  return (
    <Modal
      open={Boolean(pair)}
      title="建立关系"
      onCancel={onCancel}
      onOk={handleOk}
      confirmLoading={submitting}
      okText="创建"
      width={560}
      destroyOnClose
    >
      {pair && (
        <Form form={form} layout="vertical">
          <Text type="secondary" style={{ fontSize: 12 }}>
            {pair.subject.display || pair.subject.name}（{pair.subject.entity_type}）
            {' → '}
            {pair.object.display || pair.object.name}（{pair.object.entity_type}）
          </Text>
          <Form.Item
            name="predicate"
            label="关系类型"
            rules={[{ required: true, message: '请选择关系类型' }]}
            style={{ marginTop: 12 }}
          >
            <Select
              options={options}
              placeholder={options.length ? '选一个关系类型' : '这两种类型之间没有可用关系'}
              notFoundContent="TBox 里没有定义这两种类型之间的关系"
            />
          </Form.Item>
          <Form.Item name="note" label="依据说明（可选）">
            <Input placeholder="这条关系从哪来？" />
          </Form.Item>
        </Form>
      )}
    </Modal>
  )
}
