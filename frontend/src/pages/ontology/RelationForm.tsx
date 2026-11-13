/**
 * 新建 / 编辑一条本体关系。
 *
 * 交互顺序刻意设计成「关系类型 → 主语 → 宾语」：
 * 先说清楚"要建哪种关系"，两端的选择范围就自动收窄到合法类型，
 * 界面上不可能拼出非法组合，也不用先理解主语/宾语是什么意思。
 */
import { useEffect, useMemo } from 'react'
import { Alert, Col, Form, Input, Modal, Row, Select, Typography } from 'antd'
import type { OntologyInstance, OntologyRelation, OntologyTypes } from '@/api/types'

const { Text } = Typography

interface Props {
  open: boolean
  types: OntologyTypes | undefined
  instances: OntologyInstance[]
  initial: OntologyRelation | null
  submitting: boolean
  onCancel: () => void
  onSubmit: (values: Record<string, unknown>) => void
}

export default function RelationForm({
  open,
  types,
  instances,
  initial,
  submitting,
  onCancel,
  onSubmit,
}: Props) {
  const [form] = Form.useForm()
  const isEdit = Boolean(initial)
  const predicate = Form.useWatch('predicate', form)
  const subjectUid = Form.useWatch('subject_uid', form)
  const objectUid = Form.useWatch('object_uid', form)

  const nameOf = (uid?: string) => {
    const item = instances.find((i) => i.uid === uid)
    return item ? String(item.props?.display_zh || item.name) : ''
  }

  const relationDef = (types?.relation_types ?? []).find((r) => r.name === predicate)
  // 界面上一律中文：类型显示中文名，关系显示「属于 / 影响 / 适用于」
  const tLabel = (name: string) =>
    (types?.entity_types ?? []).find((t) => t.name === name)?.display_name || name
  const rLabel = (name: string) =>
    (types?.relation_types ?? []).find((r) => r.name === name)?.display_name || name

  const relationOptions = (types?.relation_types ?? []).map((r) => ({
    value: r.name,
    label: `${r.display_name || r.name}（${tLabel(r.source)} → ${tLabel(r.target)}）`,
  }))

  // 主语只列「关系定义里声明的来源类型」的实例
  const subjectOptions = useMemo(
    () =>
      instances
        .filter((i) => !relationDef || i.entity_type === relationDef.source)
        .map((i) => ({ value: i.uid, label: `${String(i.props?.display_zh || i.name)}（${i.name}）` })),
    [instances, relationDef],
  )

  const objectOptions = useMemo(
    () =>
      instances
        .filter((i) => (!relationDef || i.entity_type === relationDef.target) && i.uid !== subjectUid)
        .map((i) => ({ value: i.uid, label: `${String(i.props?.display_zh || i.name)}（${i.name}）` })),
    [instances, relationDef, subjectUid],
  )

  useEffect(() => {
    if (!open) return
    if (initial) {
      form.setFieldsValue({
        predicate: initial.predicate,
        subject_uid: initial.subject_uid,
        object_uid: initial.object_uid,
        __evidence_note: initial.evidence?.[0]?.note,
      })
    } else {
      form.resetFields()
    }
  }, [open, initial, form])

  const handleOk = async () => {
    const values = await form.validateFields()
    const evidence = values.__evidence_note
      ? [{ type: '人工录入', note: values.__evidence_note, traceable: false, resolved_by: 'none' }]
      : []
    onSubmit(
      isEdit
        ? { predicate: values.predicate, evidence }
        : {
            subject_uid: values.subject_uid,
            predicate: values.predicate,
            object_uid: values.object_uid,
            evidence,
          },
    )
  }

  // 用一句话复述，别用 A —关系→ B 这种箭头写法
  const preview =
    subjectUid && predicate && objectUid
      ? `${nameOf(subjectUid)}　${rLabel(predicate)}　${nameOf(objectUid)}`
      : ''

  return (
    <Modal
      open={open}
      title={isEdit ? '编辑关系' : '新建关系'}
      onCancel={onCancel}
      onOk={handleOk}
      confirmLoading={submitting}
      okText="保存"
      width={680}
      destroyOnClose
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="一条关系 = 两个实体之间的连接"
        description="例如「自由水蓄水能力 属于 蒸散发折算系数」。先选要建哪种关系，两侧能选的范围会自动对应该关系允许的类型。"
      />
      <Form form={form} layout="vertical">
        <Form.Item
          name="predicate"
          label="① 要建哪种关系"
          rules={[{ required: true, message: '请选择关系类型' }]}
        >
          <Select options={relationOptions} placeholder="先选要建哪种关系" />
        </Form.Item>

        <Row gutter={16}>
          <Col span={12}>
            <Form.Item
              name="subject_uid"
              label={relationDef ? `② 左边是谁（${tLabel(relationDef.source)}）` : '② 左边是谁'}
              rules={[{ required: true, message: '请选择主语' }]}
            >
              <Select
                showSearch
                disabled={isEdit || !relationDef}
                optionFilterProp="label"
                options={subjectOptions}
                placeholder={relationDef ? '选一个实体' : '先选关系类型'}
                notFoundContent="这个类型下还没有实例"
              />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item
              name="object_uid"
              label={relationDef ? `③ 右边是谁（${tLabel(relationDef.target)}）` : '③ 右边是谁'}
              rules={[{ required: true, message: '请选择宾语' }]}
            >
              <Select
                showSearch
                disabled={isEdit || !relationDef}
                optionFilterProp="label"
                options={objectOptions}
                placeholder={relationDef ? '选一个实体' : '先选关系类型'}
                notFoundContent="这个类型下还没有实例"
              />
            </Form.Item>
          </Col>
        </Row>

        {preview && (
          <Alert type="success" showIcon style={{ marginBottom: 16 }} message={`将要建立：${preview}`} />
        )}

        <Form.Item name="__evidence_note" label="依据说明（可选）">
          <Input placeholder="这条关系从哪来？如：防洪台账 / 某规范第 x 条 / 手册第 4 页" />
        </Form.Item>
        {isEdit && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            主语和宾语不可修改；要换端点请删掉这条关系重新建。
          </Text>
        )}
      </Form>
    </Modal>
  )
}
