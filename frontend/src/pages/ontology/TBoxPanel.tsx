/**
 * 本体结构（TBox）在线编辑。
 *
 * 这里改的是「图纸」而不是「内容」：有哪些类、每类记哪些字段。
 * 保存即写回该图谱库的 YAML，并立刻热加载——不用重启服务。
 *
 * 名字/前缀为什么不让改：它们是实例 uid 的来源，改了老实例就全成孤儿。
 * 要换名字就新建一个类型、把实例挪过去，比偷偷改 id 安全。
 */
import { useState } from 'react'
import {
  Alert,
  App,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  Upload,
} from 'antd'
import {
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  PlusOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { errMsg } from '@/api/client'
import { ontologyApi } from '@/api/modules'
import type { ExcelColumnSuggestion, OntologyEntityType, OntologyTypes } from '@/api/types'

const { Text } = Typography

const ATTR_TYPES = [
  { value: 'string', label: '文本（短）' },
  { value: 'text', label: '文本（长）' },
  { value: 'number', label: '数值' },
  { value: 'range', label: '范围' },
  { value: 'enum', label: '枚举（单选）' },
  { value: 'list', label: '列表' },
  { value: 'boolean', label: '是/否' },
]

const SCOPES = [
  { value: 'local', label: '本库私有（同名不同库不串）' },
  { value: 'global', label: '跨库共享（如行政区、河流）' },
]

type RelationRow = OntologyTypes['relation_types'][number]

/** 一条属性的形状（含自动抽取规则）。 */
interface RowAttr {
  name: string
  type: string
  values?: string[]
  required?: boolean
  description?: string
  extract?: { pattern?: string; unit?: string; map?: Record<string, string> }
}

interface Props {
  types: OntologyTypes | undefined
  profileId: string
}

export default function TBoxPanel({ types, profileId }: Props) {
  const qc = useQueryClient()
  const { message } = App.useApp()
  const [entityModal, setEntityModal] = useState<{ open: boolean; row: OntologyEntityType | null }>({
    open: false,
    row: null,
  })
  const [attrModal, setAttrModal] = useState<{
    open: boolean
    entity: OntologyEntityType | null
    attr: OntologyEntityType['attributes'][number] | null
  }>({ open: false, entity: null, attr: null })
  const [relationModal, setRelationModal] = useState<{
    open: boolean
    row: OntologyTypes['relation_types'][number] | null
  }>({ open: false, row: null })
  const [entityForm] = Form.useForm()
  const [attrForm] = Form.useForm()
  const [relationForm] = Form.useForm()
  const attrType = Form.useWatch('type', attrForm)

  // Excel 表头 → 属性建议（先预览，用户确认后才写库）
  const [excelModal, setExcelModal] = useState<{
    open: boolean
    entity: OntologyEntityType | null
    columns: ExcelColumnSuggestion[]
  }>({ open: false, entity: null, columns: [] })
  const [importing, setImporting] = useState(false)

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['ontology-types'] })
    qc.invalidateQueries({ queryKey: ['ontology-libraries'] })
  }

  const saveEntity = useMutation({
    mutationFn: (values: { name: string; display_name: string; uid_prefix: string; scope: 'local' | 'global' }) =>
      ontologyApi.upsertEntityType(profileId || undefined, values),
    onSuccess: () => {
      message.success('已保存，TBox 已热加载')
      setEntityModal({ open: false, row: null })
      refresh()
    },
    onError: (e) => message.error(errMsg(e)),
  })

  const removeEntity = useMutation({
    mutationFn: (name: string) => ontologyApi.deleteEntityType(profileId || undefined, name),
    onSuccess: () => {
      message.success('实体类型已删除')
      refresh()
    },
    onError: (e) => message.error(errMsg(e)),
  })

  const saveAttr = useMutation({
    mutationFn: (values: {
      name: string
      type: string
      values?: string[]
      required?: boolean
      description?: string
      extract_pattern?: string
      extract_unit?: string
      extract_map_text?: string
    }) => {
      // 「敏感=高」按行写，转成 {敏感: '高'}
      const map: Record<string, string> = {}
      for (const line of (values.extract_map_text ?? '').split(/\r?\n/)) {
        const [k, v] = line.split('=')
        if (k?.trim() && v?.trim()) map[k.trim()] = v.trim()
      }
      return ontologyApi.upsertAttribute(profileId || undefined, attrModal.entity?.name ?? '', {
        ...values,
        values: values.values ?? [],
        extract_map: map,
      })
    },
    onSuccess: () => {
      message.success('属性已保存')
      setAttrModal({ open: false, entity: null, attr: null })
      refresh()
    },
    onError: (e) => message.error(errMsg(e)),
  })

  const removeAttr = useMutation({
    mutationFn: (v: { entity: string; attr: string }) =>
      ontologyApi.deleteAttribute(profileId || undefined, v.entity, v.attr),
    onSuccess: () => {
      message.success('属性已删除')
      refresh()
    },
    onError: (e) => message.error(errMsg(e)),
  })

  const saveRelation = useMutation({
    mutationFn: (values: {
      name: string
      display_name: string
      source: string
      target: string
      target_types?: string[]
      description?: string
    }) => ontologyApi.upsertRelationType(profileId || undefined, values),
    onSuccess: (data, values) => {
      setRelationModal({ open: false, row: null })
      refresh()
      // 改了主语/宾语方向：已有边还是老方向，图不会自己变——问一句要不要一起掉头
      if (data.reversed_edges > 0) {
        Modal.confirm({
          title: `有 ${data.reversed_edges} 条已有关系方向和新定义相反`,
          content: '改定义只影响以后新建的关系。要不要把这些已有关系也一起掉头？',
          okText: '一起掉头',
          cancelText: '先不动',
          onOk: async () => {
            const res = await ontologyApi.fixReversedEdges(profileId || undefined, values.name)
            message.success(`已调整 ${res.fixed} 条关系方向`)
            refresh()
          },
        })
      } else {
        message.success('关系类型已保存')
      }
    },
    onError: (e) => message.error(errMsg(e)),
  })

  const removeRelation = useMutation({
    mutationFn: (name: string) => ontologyApi.deleteRelationType(profileId || undefined, name),
    onSuccess: () => {
      message.success('关系类型已删除')
      refresh()
    },
    onError: (e) => message.error(errMsg(e)),
  })

  const onExcelPicked = async (file: File, entity: OntologyEntityType) => {
    try {
      const data = await ontologyApi.excelPreview(file)
      setExcelModal({ open: true, entity, columns: data.columns })
    } catch (e) {
      message.error(errMsg(e))
    }
    return false
  }

  const applyExcel = async () => {
    const entity = excelModal.entity
    if (!entity) return
    try {
      for (const col of excelModal.columns) {
        if (!col.name.trim()) continue
        await ontologyApi.upsertAttribute(profileId || undefined, entity.name, {
          name: col.name,
          type: col.type,
          values: col.values ?? [],
          description: col.description,
        })
      }
      message.success(`已给「${entity.display_name}」添加 ${excelModal.columns.length} 个属性`)
      setExcelModal({ open: false, entity: null, columns: [] })
      refresh()
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  const doImport = async (file: File) => {
    setImporting(true)
    try {
      const lib = await ontologyApi.importLibrary(file)
      message.success(`已导入图谱库「${lib.display_name}」`)
      qc.invalidateQueries({ queryKey: ['ontology-libraries'] })
      qc.invalidateQueries({ queryKey: ['ontology-types'] })
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setImporting(false)
    }
    return false
  }

  const openCreate = () => {
    entityForm.resetFields()
    entityForm.setFieldsValue({ scope: 'local' })
    setEntityModal({ open: true, row: null })
  }

  const openEdit = (row: OntologyEntityType) => {
    entityForm.setFieldsValue({
      name: row.name,
      display_name: row.display_name,
      uid_prefix: row.uid_prefix,
      scope: row.scope,
    })
    setEntityModal({ open: true, row })
  }

  const rows = types?.entity_types ?? []
  const typeLabel = (name: string) =>
    types?.entity_types.find((t) => t.name === name)?.display_name ?? name

  return (
    <Card size="small">
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="这里定义「能建什么、填什么」"
        description="实体类型 = 一类东西（水库、河段…），属性 = 这类东西要记的字段。改完保存就生效，不用重启。具体的水库叫什么、参数取多少，属于实例，去「实例」页填。"
      />
      <Space style={{ marginBottom: 12 }} wrap>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新建实体类型
        </Button>
        <Button
          icon={<DownloadOutlined />}
          disabled={!profileId}
          onClick={() => ontologyApi.exportLibrary(profileId)}
        >
          导出 YAML
        </Button>
        <Upload beforeUpload={doImport} showUploadList={false} accept=".yaml,.yml">
          <Button icon={<UploadOutlined />} loading={importing}>
            导入 YAML
          </Button>
        </Upload>
        <Text type="secondary" style={{ fontSize: 12 }}>
          共 {rows.length} 类实体 / {types?.relation_types?.length ?? 0} 种关系
        </Text>
      </Space>

      <Table
        size="small"
        rowKey="name"
        pagination={false}
        dataSource={rows}
        expandable={{
          expandedRowRender: (row) => (
            <Space direction="vertical" size={6} style={{ width: '100%' }}>
              <Space>
                <Button
                  size="small"
                  icon={<PlusOutlined />}
                  onClick={() => {
                    attrForm.resetFields()
                    attrForm.setFieldsValue({ type: 'string', required: false })
                    setAttrModal({ open: true, entity: row, attr: null })
                  }}
                >
                  添加属性
                </Button>
                <Upload
                  beforeUpload={(f) => onExcelPicked(f, row)}
                  showUploadList={false}
                  accept=".xlsx,.xls"
                >
                  <Button size="small" icon={<UploadOutlined />}>
                    从 Excel 表头生成
                  </Button>
                </Upload>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  属性决定实例表单里出现哪些字段
                </Text>
              </Space>
              {row.attributes.length === 0 ? (
                <Text type="secondary">这一类还没有属性，实例只能填名称。</Text>
              ) : (
                <Table
                  size="small"
                  rowKey="name"
                  pagination={false}
                  dataSource={row.attributes}
                  columns={[
                    { title: '字段', dataIndex: 'name', width: 200 },
                    {
                      title: '类型',
                      dataIndex: 'type',
                      width: 120,
                      render: (v: string) => ATTR_TYPES.find((t) => t.value === v)?.label ?? v,
                    },
                    {
                      title: '可选值',
                      dataIndex: 'values',
                      render: (v: string[] | undefined) =>
                        v?.length ? v.join(' / ') : <Text type="secondary">—</Text>,
                    },
                    {
                      title: '必填',
                      dataIndex: 'required',
                      width: 70,
                      render: (v: boolean) => (v ? <Tag color="red">必填</Tag> : '—'),
                    },
                    {
                      title: '说明',
                      dataIndex: 'description',
                      ellipsis: true,
                      render: (v: string | undefined) => v || <Text type="secondary">—</Text>,
                    },
                    {
                      title: '自动抽取',
                      width: 90,
                      render: (_: unknown, attr: RowAttr) =>
                        attr.extract?.pattern ? (
                          <Tag color="green">已配规则</Tag>
                        ) : (
                          <Text type="secondary">—</Text>
                        ),
                    },
                    {
                      title: '操作',
                      width: 90,
                      render: (_: unknown, attr: RowAttr) => (
                        <Space size={0}>
                          <Button
                            size="small"
                            type="link"
                            style={{ paddingInline: 4 }}
                            onClick={() => {
                              attrForm.setFieldsValue({
                                name: attr.name,
                                type: attr.type,
                                values: attr.values ?? [],
                                required: attr.required ?? false,
                                description: attr.description ?? '',
                                extract_pattern: attr.extract?.pattern ?? '',
                                extract_unit: (attr.extract?.unit as string) ?? '',
                                extract_map_text: Object.entries(
                                  (attr.extract?.map as Record<string, string>) ?? {},
                                )
                                  .map(([k, v]) => `${k}=${v}`)
                                  .join('\n'),
                              })
                              setAttrModal({ open: true, entity: row, attr })
                            }}
                          >
                            编辑
                          </Button>
                          <Popconfirm
                            title="删除这个属性？"
                            description="已有实例里这个字段的数据会保留，但表单不再显示。"
                            okText="删除"
                            cancelText="取消"
                            onConfirm={() => removeAttr.mutate({ entity: row.name, attr: attr.name })}
                          >
                            <Button size="small" danger type="text" icon={<DeleteOutlined />} />
                          </Popconfirm>
                        </Space>
                      ),
                    },
                  ]}
                />
              )}
            </Space>
          ),
        }}
        columns={[
          {
            title: '中文名',
            dataIndex: 'display_name',
            render: (v: string, row: OntologyEntityType) => (
              <Space>
                <Text strong>{v}</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {row.name}
                </Text>
              </Space>
            ),
          },
          { title: '编号前缀', dataIndex: 'uid_prefix', width: 100 },
          {
            title: '作用域',
            dataIndex: 'scope',
            width: 120,
            render: (v: string) => (v === 'global' ? <Tag color="blue">跨库共享</Tag> : <Tag>本库私有</Tag>),
          },
          {
            title: '属性数',
            width: 80,
            render: (_: unknown, row: OntologyEntityType) => row.attributes.length,
          },
          {
            title: '操作',
            width: 110,
            render: (_: unknown, row: OntologyEntityType) => (
              <Space size={0}>
                <Button size="small" type="text" icon={<EditOutlined />} onClick={() => openEdit(row)} />
                <Popconfirm
                  title="删除这个实体类型？"
                  description="被关系引用或已有实例时会被拒绝。"
                  okText="删除"
                  cancelText="取消"
                  onConfirm={() => removeEntity.mutate(row.name)}
                >
                  <Button size="small" danger type="text" icon={<DeleteOutlined />} />
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />

      <Alert
        type="info"
        showIcon
        style={{ margin: '16px 0 12px' }}
        message="关系类型：哪两类东西之间能连，叫什么"
        description="决定「连线」时能选出什么。主语/宾语只能在已声明的实体类型里选；已经声明好的关系类型不建议删除——有边在用时会被拒绝。"
      />
      <Space style={{ marginBottom: 12 }}>
        <Button
          icon={<PlusOutlined />}
          onClick={() => {
            relationForm.resetFields()
            setRelationModal({ open: true, row: null })
          }}
        >
          新建关系类型
        </Button>
      </Space>

      <Table<RelationRow>
        size="small"
        rowKey="name"
        pagination={false}
        dataSource={types?.relation_types ?? []}
        columns={[
          {
            title: '中文名',
            dataIndex: 'display_name',
            render: (v: string | undefined, row: { name: string }) => (
              <Space>
                <Text strong>{v || row.name}</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {row.name}
                </Text>
              </Space>
            ),
          },
          {
            title: '连法',
            render: (_: unknown, row: RelationRow) => (
              <Space size={4} wrap>
                <Tag>{typeLabel(row.source)}</Tag>
                <Text type="secondary">—{row.display_name || row.name}→</Text>
                <Tag>{typeLabel(row.target)}</Tag>
                {(row.target_types ?? []).filter((t: string) => t !== row.target).length > 0 && (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    或{' '}
                    {(row.target_types ?? [])
                      .filter((t: string) => t !== row.target)
                      .map(typeLabel)
                      .join(' / ')}
                  </Text>
                )}
              </Space>
            ),
          },
          {
            title: '说明',
            dataIndex: 'description',
            ellipsis: true,
            render: (v: string | undefined) => v || <Text type="secondary">—</Text>,
          },
          {
            title: '操作',
            width: 110,
            render: (_: unknown, row: RelationRow) => (
              <Space size={0}>
                <Button
                  size="small"
                  type="text"
                  icon={<EditOutlined />}
                  onClick={() => {
                    relationForm.setFieldsValue({
                      name: row.name,
                      display_name: row.display_name,
                      source: row.source,
                      target: row.target,
                      target_types: row.target_types ?? [],
                      description: row.description,
                    })
                    setRelationModal({ open: true, row })
                  }}
                />
                <Popconfirm
                  title="删除这个关系类型？"
                  description="已有关系边在用时会被拒绝。"
                  okText="删除"
                  cancelText="取消"
                  onConfirm={() => removeRelation.mutate(row.name)}
                >
                  <Button size="small" danger type="text" icon={<DeleteOutlined />} />
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />

      <Modal
        open={entityModal.open}
        title={entityModal.row ? '编辑实体类型' : '新建实体类型'}
        okText="保存"
        cancelText="取消"
        confirmLoading={saveEntity.isPending}
        onCancel={() => setEntityModal({ open: false, row: null })}
        onOk={async () => saveEntity.mutate(await entityForm.validateFields())}
        destroyOnClose
      >
        <Form form={entityForm} layout="vertical">
          <Form.Item
            name="display_name"
            label="中文名"
            rules={[{ required: true, message: '填一个看得懂的名字' }]}
          >
            <Input placeholder="如：预警区" maxLength={40} />
          </Form.Item>
          <Form.Item
            name="name"
            label="标识"
            tooltip="程序内部标识，也是实例类型的取值；创建后不可修改"
            rules={[
              { required: true, message: '填标识' },
              { pattern: /^[A-Za-z][A-Za-z0-9_]*$/, message: '字母开头，只能用字母、数字、下划线' },
            ]}
          >
            <Input placeholder="如：warning_zone" disabled={Boolean(entityModal.row)} maxLength={40} />
          </Form.Item>
          <Form.Item
            name="uid_prefix"
            label="编号前缀"
            tooltip="实例 uid 的前缀，2~4 位小写字母；创建后不可修改"
            rules={[
              { required: true, message: '填前缀' },
              { pattern: /^[a-z]{2,4}$/, message: '2~4 位小写字母' },
            ]}
          >
            <Input placeholder="如：wz" disabled={Boolean(entityModal.row)} maxLength={4} />
          </Form.Item>
          <Form.Item name="scope" label="作用域">
            <Select options={SCOPES} />
          </Form.Item>
          {saveEntity.isError && <Alert type="error" showIcon message={errMsg(saveEntity.error)} />}
        </Form>
      </Modal>

      <Modal
        open={attrModal.open}
        title={
          attrModal.attr
            ? `编辑属性「${attrModal.attr.name}」`
            : `给「${attrModal.entity?.display_name ?? ''}」加属性`
        }
        okText="保存"
        cancelText="取消"
        confirmLoading={saveAttr.isPending}
        onCancel={() => setAttrModal({ open: false, entity: null, attr: null })}
        onOk={async () => saveAttr.mutate(await attrForm.validateFields())}
        destroyOnClose
      >
        <Form form={attrForm} layout="vertical">
          <Form.Item
            name="name"
            label="字段名"
            tooltip="程序用；中文名请填到「说明」里"
            rules={[
              { required: true, message: '填字段名' },
              { pattern: /^[A-Za-z_][A-Za-z0-9_]*$/, message: '字母或下划线开头' },
            ]}
          >
            <Input placeholder="如：warning_level" maxLength={40} />
          </Form.Item>
          <Form.Item name="type" label="类型" rules={[{ required: true }]}>
            <Select options={ATTR_TYPES} />
          </Form.Item>
          {attrType === 'enum' && (
            <Form.Item
              name="values"
              label="可选值"
              rules={[{ required: true, message: '枚举必须给出可选值' }]}
              getValueProps={(v?: string[]) => ({ value: v ?? [] })}
            >
              <Select
                mode="tags"
                placeholder="一个选项输完按回车；也可以直接粘「平板闸/弧形闸/人字闸」"
                // 斜杠、顿号、分号都当分隔符——用户就是会这么写，吞成一整个选项是 bug
                tokenSeparators={[',', '，', '/', '、', ';', '；']}
              />
            </Form.Item>
          )}
          {attrType === 'enum' && (
            <Text type="secondary" style={{ fontSize: 12, marginBottom: 12, display: 'block' }}>
              将保存为 {(Form.useWatch('values', attrForm) as string[] | undefined)?.length ?? 0}{' '}
              个选项；实例表单里是**单选**下拉。
            </Text>
          )}
          <Form.Item name="required" label="必填" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item name="description" label="说明（会显示在实例表单里）">
            <Input placeholder="如：这个字段填什么、从哪来" maxLength={200} />
          </Form.Item>

          <Card size="small" title="自动抽取规则（可选）" style={{ marginBottom: 12 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              配了规则，程序才知道怎么去知识库里找这个字段的值。不配 = 不参与自动抽取（手填就够的话不用管）。
              抽到的结果进「自动抽取」tab 的待审区，你点接受才写进实例。
            </Text>
            <Form.Item label="不会写正则？选一个常用写法" style={{ marginTop: 8, marginBottom: 8 }}>
              <Select
                allowClear
                placeholder="选一个模板，下面会自动填好"
                options={[
                  { value: '{name}\\s*[:：=＝]\\s*(\\S{1,20})', label: '「名称：值」式（冒号/等号后面）' },
                  { value: '{name}\\s*\\|\\s*(\\S{1,20})', label: '表格行（竖线分隔，如 KC | 流域蒸散发折算系数 | 敏感）' },
                  { value: '{name}[^0-9]{0,10}([0-9]+(?:\\.[0-9]+)?)', label: '名称后面紧跟一个数字（取值/流量等）' },
                  { value: '{name}[^。]{0,12}?为\\s*(\\S{1,20})', label: '「…为 XXX」式（中文叙述）' },
                ]}
                onChange={(v) => v && attrForm.setFieldsValue({ extract_pattern: v })}
              />
            </Form.Item>
            <Form.Item
              name="extract_pattern"
              label="正则"
              tooltip="{name} 会替换成实例名，所以一条规则能复用给所有实例"
            >
              <Input placeholder="如：安全泄量[^0-9]{0,10}([0-9]+(?:\.[0-9]+)?)" maxLength={300} />
            </Form.Item>
            <Form.Item name="extract_unit" label="单位（可选）">
              <Input placeholder="如：m³/s" maxLength={20} />
            </Form.Item>
            <Form.Item
              name="extract_map_text"
              label="原文写法 → 枚举值（可选）"
              tooltip="原文写「敏感」，枚举值是「高」，就在这里翻译；一行一条"
            >
              <Input.TextArea rows={3} placeholder={'敏感=高\n不敏感=低'} />
            </Form.Item>
          </Card>
          {saveAttr.isError && <Alert type="error" showIcon message={errMsg(saveAttr.error)} />}
        </Form>
      </Modal>

      <Modal
        open={excelModal.open}
        title={`从 Excel 表头生成属性 →「${excelModal.entity?.display_name ?? ''}」`}
        okText="确认添加"
        cancelText="取消"
        width={760}
        onCancel={() => setExcelModal({ open: false, entity: null, columns: [] })}
        onOk={applyExcel}
        destroyOnClose
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="字段名可以改，类型是按样例值猜的"
          description="中文列名转不成程序标识，先给了 col_N 占位——请改成英文短名（如 safe_discharge）。"
        />
        <Table<ExcelColumnSuggestion>
          size="small"
          rowKey="header"
          pagination={false}
          dataSource={excelModal.columns}
          columns={[
            { title: 'Excel 列名', dataIndex: 'header', width: 160 },
            {
              title: '字段名（可改）',
              width: 200,
              render: (_: unknown, row: ExcelColumnSuggestion) => (
                <Input
                  size="small"
                  value={row.name}
                  onChange={(e) =>
                    setExcelModal((prev) => ({
                      ...prev,
                      columns: prev.columns.map((c) =>
                        c.header === row.header ? { ...c, name: e.target.value } : c,
                      ),
                    }))
                  }
                />
              ),
            },
            {
              title: '类型',
              width: 140,
              render: (_: unknown, row: ExcelColumnSuggestion) => (
                <Select
                  size="small"
                  value={row.type}
                  style={{ width: '100%' }}
                  options={ATTR_TYPES}
                  onChange={(v) =>
                    setExcelModal((prev) => ({
                      ...prev,
                      columns: prev.columns.map((c) =>
                        c.header === row.header ? { ...c, type: v } : c,
                      ),
                    }))
                  }
                />
              ),
            },
            {
              title: '样例 / 可选值',
              render: (_: unknown, row: ExcelColumnSuggestion) => (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {(row.values.length ? row.values : row.samples).slice(0, 5).join(' / ') || '—'}
                </Text>
              ),
            },
          ]}
        />
      </Modal>

      <Modal
        open={relationModal.open}
        title={relationModal.row ? '编辑关系类型' : '新建关系类型'}
        okText="保存"
        cancelText="取消"
        confirmLoading={saveRelation.isPending}
        onCancel={() => setRelationModal({ open: false, row: null })}
        onOk={async () => saveRelation.mutate(await relationForm.validateFields())}
        destroyOnClose
      >
        <Form form={relationForm} layout="vertical">
          <Form.Item
            name="display_name"
            label="中文名（界面上显示这个）"
            rules={[{ required: true, message: '如：下游 / 属于 / 责任' }]}
          >
            <Input placeholder="如：下游" maxLength={40} />
          </Form.Item>
          <Form.Item
            name="name"
            label="标识"
            tooltip="边上的 predicate，大写蛇形；创建后不可修改"
            rules={[
              { required: true, message: '填标识' },
              { pattern: /^[A-Z][A-Z0-9_]*$/, message: '大写字母开头，只能用大写、数字、下划线' },
            ]}
          >
            <Input placeholder="如：DOWNSTREAM_OF" disabled={Boolean(relationModal.row)} maxLength={60} />
          </Form.Item>
          <Form.Item
            name="source"
            label="主语类型"
            rules={[{ required: true, message: '选主语类型' }]}
          >
            <Select options={rows.map((t) => ({ value: t.name, label: t.display_name }))} />
          </Form.Item>
          <Form.Item
            name="target"
            label="宾语类型"
            rules={[{ required: true, message: '选宾语类型' }]}
          >
            <Select options={rows.map((t) => ({ value: t.name, label: t.display_name }))} />
          </Form.Item>
          <Form.Item
            name="target_types"
            label="还可以连到哪些类型（可选）"
            tooltip="留空表示只能连到上面的宾语类型；多选表示这些类型都可以"
          >
            <Select
              mode="multiple"
              allowClear
              options={rows.map((t) => ({ value: t.name, label: t.display_name }))}
              placeholder="不填就只允许上面那一种"
            />
          </Form.Item>
          <Form.Item name="description" label="说明（可选）">
            <Input placeholder="如：工程对象影响其下游的河段与行政区" maxLength={200} />
          </Form.Item>
          {saveRelation.isError && (
            <Alert type="error" showIcon message={errMsg(saveRelation.error)} />
          )}
        </Form>
      </Modal>
    </Card>
  )
}
