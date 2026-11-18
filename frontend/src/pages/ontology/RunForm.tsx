/**
 * 登记一次运行记录（O8-2）：率定跑完，把「用了哪些参数、达到什么指标」报进来。
 *
 * 为什么值得记：参数取了多少、指标到多少，是"这组参数下一次该不该沿用"的唯一依据。
 * 现在由人来补录；以后建模 agent 跑完会直接调 `/api/v1/ontology/runs` 上报。
 *
 * 规矩：
 * - 参数**必须已在库里**（参数是人梳理的，程序不替你造），对不上会明确列出来；
 * - 指标库里没有就自动建一个空节点（跑出来的指标名是事实）；
 * - `run_id` 是幂等键：同一个 id 再报一次只更新值，不会多出一个节点。
 */
import { useEffect } from 'react'
import {
  AutoComplete,
  Button,
  Col,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Typography,
} from 'antd'
import { MinusCircleOutlined, PlusOutlined } from '@ant-design/icons'

const { Text } = Typography

interface ParamOption {
  value: string
  label: string
}

interface Props {
  open: boolean
  parameters: ParamOption[]
  submitting: boolean
  onCancel: () => void
  onSubmit: (values: {
    run_id: string
    started_at?: string
    strategy?: string
    objective?: string
    note?: string
    parameters: Record<string, number | string>
    metrics: Record<string, number | string>
  }) => void
}

/** 默认 run_id：run-20261118-1530（人可改，它是幂等键） */
function defaultRunId(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `run-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`
}

export default function RunForm({ open, parameters, submitting, onCancel, onSubmit }: Props) {
  const [form] = Form.useForm()

  useEffect(() => {
    if (!open) return
    form.resetFields()
    form.setFieldsValue({
      run_id: defaultRunId(),
      started_at: new Date().toISOString().slice(0, 19),
      parameters: [{}],
      metrics: [{}],
    })
  }, [open, form])

  const handleOk = async () => {
    const values = await form.validateFields()
    const params: Record<string, number | string> = {}
    for (const row of values.parameters ?? []) {
      if (row?.name && row?.value !== undefined && row?.value !== null) {
        params[row.name] = row.value
      }
    }
    const metrics: Record<string, number | string> = {}
    for (const row of values.metrics ?? []) {
      const name = (row?.name ?? '').trim()
      if (name && row?.value !== undefined && row?.value !== null) {
        metrics[name] = row.value
      }
    }
    onSubmit({
      run_id: values.run_id,
      started_at: values.started_at,
      strategy: values.strategy,
      objective: values.objective,
      note: values.note,
      parameters: params,
      metrics,
    })
  }

  return (
    <Modal
      open={open}
      title="登记一次运行记录"
      width={760}
      okText="保存"
      cancelText="取消"
      confirmLoading={submitting}
      onCancel={onCancel}
      onOk={handleOk}
      destroyOnClose
    >
      <Form form={form} layout="vertical">
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item
              name="run_id"
              label="运行编号"
              tooltip="幂等键：同一个编号再报一次只更新值，不会多出一个节点"
              rules={[{ required: true, message: '需要一个运行编号' }]}
            >
              <Input placeholder="如 run-20261118-1530" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="started_at" label="开始时间">
              <Input placeholder="2026-11-18T10:00:00" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="strategy" label="率定策略">
              <AutoComplete
                options={['SCE-UA', 'PSO', 'GA', '手动'].map((v) => ({ value: v }))}
                placeholder="如 SCE-UA"
              />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="objective" label="目标 / 说明">
              <Input placeholder="如：NSE 最大化，洪峰误差放在次要位置" />
            </Form.Item>
          </Col>
        </Row>

        <Text type="secondary" style={{ fontSize: 12 }}>
          参数取了多少（只能选库里已有的参数）
        </Text>
        <Form.List name="parameters">
          {(fields, { add, remove }) => (
            <Space direction="vertical" size={6} style={{ width: '100%', marginTop: 6 }}>
              {fields.map((field) => (
                <Space key={field.key} align="baseline" style={{ width: '100%' }}>
                  <Form.Item name={[field.name, 'name']} style={{ marginBottom: 8, width: 320 }}>
                    <Select
                      showSearch
                      placeholder="选择参数"
                      options={parameters}
                      filterOption={(input, option) =>
                        String(option?.label ?? '').includes(input)
                      }
                    />
                  </Form.Item>
                  <Form.Item name={[field.name, 'value']} style={{ marginBottom: 8 }}>
                    <InputNumber placeholder="取值" style={{ width: 160 }} />
                  </Form.Item>
                  <MinusCircleOutlined onClick={() => remove(field.name)} />
                </Space>
              ))}
              <Button type="dashed" icon={<PlusOutlined />} onClick={() => add({})} block>
                再加一个参数
              </Button>
            </Space>
          )}
        </Form.List>

        <Text type="secondary" style={{ fontSize: 12 }}>
          达到的指标（库里没有的指标会自动建一个空节点）
        </Text>
        <Form.List name="metrics">
          {(fields, { add, remove }) => (
            <Space direction="vertical" size={6} style={{ width: '100%', marginTop: 6 }}>
              {fields.map((field) => (
                <Space key={field.key} align="baseline" style={{ width: '100%' }}>
                  <Form.Item name={[field.name, 'name']} style={{ marginBottom: 8 }}>
                    <Input placeholder="指标名，如 NSE" style={{ width: 320 }} />
                  </Form.Item>
                  <Form.Item name={[field.name, 'value']} style={{ marginBottom: 8 }}>
                    <InputNumber placeholder="数值" style={{ width: 160 }} />
                  </Form.Item>
                  <MinusCircleOutlined onClick={() => remove(field.name)} />
                </Space>
              ))}
              <Button type="dashed" icon={<PlusOutlined />} onClick={() => add({})} block>
                再加一个指标
              </Button>
            </Space>
          )}
        </Form.List>

        <Form.Item name="note" label="备注" style={{ marginTop: 12 }}>
          <Input.TextArea rows={2} placeholder="这次率定有什么特殊情况" />
        </Form.Item>
      </Form>
    </Modal>
  )
}
