/**
 * 本体管理（F2）：在前端维护 ABox。
 *
 * 三个页签：
 *   实例  —— 增 / 改 / 删本体实例（表单字段由 TBox 自动生成）
 *   关系  —— 增 / 改 / 删主语-关系-宾语，下拉只给 TBox 允许的组合
 *   参数表 —— 上传 Excel 预演 / 导入
 *
 * 保存即生效（写 PG 真相源 + 同步图库），所有写操作落审计。
 */
import { useMemo, useState } from 'react'
import {
  Alert,
  App,
  Button,
  Card,
  Input,
  Layout,
  Popconfirm,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  Upload,
} from 'antd'
import {
  ApartmentOutlined,
  ArrowLeftOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import UserMenu from '@/components/UserMenu'
import OntologyLibrarySelect from '@/components/OntologyLibrarySelect'
import { errMsg } from '@/api/client'
import { ontologyApi } from '@/api/modules'
import { useOntologyLibrary } from '@/stores/ontologyLibrary'
import TBoxPanel from './ontology/TBoxPanel'
import ExtractionPanel from './ontology/ExtractionPanel'
import type {
  OntologyImportResult,
  OntologyInstance,
  OntologyRelation,
} from '@/api/types'
import InstanceForm from './ontology/InstanceForm'
import RelationForm from './ontology/RelationForm'

const { Header, Content } = Layout
const { Text } = Typography

export default function OntologyManager() {
  const navigate = useNavigate()
  const { message } = App.useApp()
  const qc = useQueryClient()

  const [typeFilter, setTypeFilter] = useState<string | undefined>()
  const [keyword, setKeyword] = useState('')
  const [instanceModal, setInstanceModal] = useState<{ open: boolean; row: OntologyInstance | null }>(
    { open: false, row: null },
  )
  const [relationModal, setRelationModal] = useState<{ open: boolean; row: OntologyRelation | null }>(
    { open: false, row: null },
  )
  const [importFile, setImportFile] = useState<File | null>(null)
  const [importResult, setImportResult] = useState<OntologyImportResult | null>(null)

  // 当前图谱库：换库时三类查询全部重取，实体/关系/参数表互不串库
  const { profileId } = useOntologyLibrary()
  const typesQuery = useQuery({
    queryKey: ['ontology-types', profileId],
    queryFn: () => ontologyApi.types(profileId || undefined),
  })
  const instancesQuery = useQuery({
    queryKey: ['ontology-instances', profileId],
    queryFn: () => ontologyApi.instances({ profile: profileId || undefined, limit: 1000, status: 'all' }),
  })
  const relationsQuery = useQuery({
    queryKey: ['ontology-relations', profileId],
    queryFn: () =>
      ontologyApi.relations({ profile: profileId || undefined, status: 'all', limit: 5000 }),
  })

  const entityTypes = typesQuery.data?.entity_types ?? []
  // 类型 → 中文名：界面上不出现 Parameter / PART_OF 这种原始标识
  const typeLabel = (name: string) =>
    entityTypes.find((t) => t.name === name)?.display_name || name
  const relationLabel = (name: string) =>
    (typesQuery.data?.relation_types ?? []).find((r) => r.name === name)?.display_name || name
  const instances = instancesQuery.data ?? []
  const relations = relationsQuery.data ?? []
  const uidToName = useMemo(() => {
    const map = new Map<string, string>()
    for (const item of instances) {
      map.set(item.uid, String(item.props?.display_zh || item.name))
    }
    return map
  }, [instances])

  const refreshAll = () => {
    qc.invalidateQueries({ queryKey: ['ontology-instances'] })
    qc.invalidateQueries({ queryKey: ['ontology-relations'] })
    qc.invalidateQueries({ queryKey: ['ontology-graph'] })
  }

  const saveInstance = useMutation({
    mutationFn: (values: Record<string, unknown>) => {
      const id = instanceModal.row?.id
      return id
        ? ontologyApi.updateInstance(id, { props: values.props, evidence: values.evidence })
        : ontologyApi.createInstance({ ...values, profile_id: profileId || undefined })
    },
    onSuccess: () => {
      message.success('已保存')
      setInstanceModal({ open: false, row: null })
      refreshAll()
    },
    onError: (e) => message.error(errMsg(e)),
  })

  const removeInstance = useMutation({
    mutationFn: (id: number) => ontologyApi.deleteInstance(id),
    onSuccess: () => {
      message.success('已删除（连同挂在它身上的关系）')
      refreshAll()
    },
    onError: (e) => message.error(errMsg(e)),
  })

  const saveRelation = useMutation({
    mutationFn: (values: Record<string, unknown>) => {
      const id = relationModal.row?.id
      return id
        ? ontologyApi.updateRelation(id, values)
        : ontologyApi.createRelation({ ...values, profile_id: profileId || undefined })
    },
    onSuccess: () => {
      message.success('已保存')
      setRelationModal({ open: false, row: null })
      refreshAll()
    },
    onError: (e) => message.error(errMsg(e)),
  })

  const removeRelation = useMutation({
    mutationFn: (id: number) => ontologyApi.deleteRelation(id),
    onSuccess: () => {
      message.success('已删除')
      refreshAll()
    },
    onError: (e) => message.error(errMsg(e)),
  })

  const importMutation = useMutation({
    mutationFn: (dryRun: boolean) =>
      ontologyApi.importParams(importFile as File, { dryRun, profile: profileId || undefined }),
    onSuccess: (data) => {
      setImportResult(data)
      message.success(data.dry_run ? '预演完成，确认无误后再点「正式导入」' : '导入完成')
      if (!data.dry_run) refreshAll()
    },
    onError: (e) => message.error(errMsg(e)),
  })

  const filteredInstances = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    return instances.filter((row) => {
      if (typeFilter && row.entity_type !== typeFilter) return false
      if (!kw) return true
      const haystack = [
        row.name,
        String(row.props?.display_zh ?? ''),
        String(row.props?.group ?? ''),
      ]
        .join(' ')
        .toLowerCase()
      return haystack.includes(kw)
    })
  }, [instances, typeFilter, keyword])

  const instanceColumns = [
    { title: '标识', dataIndex: 'name', width: 130 },
    {
      title: '中文名',
      width: 160,
      render: (_: unknown, row: OntologyInstance) => String(row.props?.display_zh ?? '—'),
    },
    {
      title: '类型',
      dataIndex: 'entity_type',
      width: 140,
      render: (v: string) => <Tag>{typeLabel(v)}</Tag>,
    },
    {
      title: '范围',
      width: 150,
      render: (_: unknown, row: OntologyInstance) => {
        const low = row.props?.range_low
        const high = row.props?.range_high
        return low === undefined && high === undefined ? '—' : `[${low ?? '?'}, ${high ?? '?'}]`
      },
    },
    {
      title: '证据',
      width: 90,
      render: (_: unknown, row: OntologyInstance) => {
        const count = row.evidence?.length ?? 0
        return count ? <Tag color="green">{count} 条</Tag> : <Tag color="orange">无</Tag>
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (v: string) => (v === 'active' ? <Tag color="cyan">生效</Tag> : <Tag>归档</Tag>),
    },
    {
      title: '操作',
      width: 130,
      render: (_: unknown, row: OntologyInstance) => (
        <Space>
          <Button
            size="small"
            type="text"
            icon={<EditOutlined />}
            onClick={() => setInstanceModal({ open: true, row })}
          />
          <Popconfirm
            title="删除这个实例？"
            description="挂在它身上的关系也会一起删掉"
            onConfirm={() => removeInstance.mutate(row.id)}
          >
            <Button size="small" type="text" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const relationColumns = [
    {
      title: '主语',
      width: 200,
      render: (_: unknown, row: OntologyRelation) => uidToName.get(row.subject_uid) ?? row.subject_uid,
    },
    {
      title: '关系',
      dataIndex: 'predicate',
      width: 160,
      render: (v: string) => <Tag color="blue">{relationLabel(v)}</Tag>,
    },
    {
      title: '宾语',
      width: 200,
      render: (_: unknown, row: OntologyRelation) => uidToName.get(row.object_uid) ?? row.object_uid,
    },
    {
      title: '依据',
      render: (_: unknown, row: OntologyRelation) =>
        row.evidence?.length ? (
          <Text type="secondary">{row.evidence[0]?.note || '—'}</Text>
        ) : (
          <Tag color="orange">无</Tag>
        ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (v: string) => (v === 'active' ? <Tag color="cyan">生效</Tag> : <Tag>归档</Tag>),
    },
    {
      title: '操作',
      width: 130,
      render: (_: unknown, row: OntologyRelation) => (
        <Space>
          <Button
            size="small"
            type="text"
            icon={<EditOutlined />}
            onClick={() => setRelationModal({ open: true, row })}
          />
          <Popconfirm title="删除这条关系？" onConfirm={() => removeRelation.mutate(row.id)}>
            <Button size="small" type="text" danger icon={<DeleteOutlined />} />
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
          <ApartmentOutlined style={{ color: '#00c6ff', fontSize: 18 }} />
          <Text strong>本体管理</Text>
          <Tag color="cyan">{typesQuery.data?.display_name ?? typesQuery.data?.profile_id ?? '—'}</Tag>
        </Space>
        <Space>
          <OntologyLibrarySelect allowCreate />
          <Button onClick={() => navigate('/graph')}>看图谱</Button>
          <UserMenu />
        </Space>
      </Header>

      <Content style={{ padding: 24, overflowY: 'auto' }}>
        <Tabs
          defaultActiveKey="instances"
          items={[
            {
              key: 'instances',
              label: `实例（${instances.length}）`,
              children: (
                <Card size="small">
                  <Space wrap style={{ marginBottom: 12 }}>
                    <Input.Search
                      allowClear
                      placeholder="搜标识/中文名/分组"
                      style={{ width: 240 }}
                      onSearch={setKeyword}
                      onChange={(e) => !e.target.value && setKeyword('')}
                    />
                    <Select
                      allowClear
                      placeholder="按实体类型筛选"
                      style={{ width: 260 }}
                      value={typeFilter}
                      onChange={setTypeFilter}
                      options={entityTypes.map((t) => {
                        const count = instances.filter((i) => i.entity_type === t.name).length
                        return {
                          value: t.name,
                          label: `${t.display_name || t.name}（${count}）`,
                          disabled: count === 0,
                        }
                      })}
                    />
                    <Button
                      type="primary"
                      icon={<PlusOutlined />}
                      onClick={() => setInstanceModal({ open: true, row: null })}
                    >
                      新建实例
                    </Button>
                  </Space>
                  <Table
                    rowKey="id"
                    size="small"
                    loading={instancesQuery.isLoading}
                    dataSource={filteredInstances}
                    columns={instanceColumns}
                    pagination={{ pageSize: 20, showSizeChanger: true }}
                    locale={{
                      emptyText: (
                        <Space direction="vertical" size={4}>
                          <Text type="secondary">这个类型下还没有实例</Text>
                          <Button
                            size="small"
                            type="primary"
                            icon={<PlusOutlined />}
                            onClick={() =>
                              setInstanceModal({
                                open: true,
                                row: typeFilter
                                  ? ({ entity_type: typeFilter } as unknown as OntologyInstance)
                                  : null,
                              })
                            }
                          >
                            新建一个
                          </Button>
                        </Space>
                      ),
                    }}
                  />
                </Card>
              ),
            },
            {
              key: 'schema',
              label: `本体结构（${entityTypes.length}）`,
              children: <TBoxPanel types={typesQuery.data} profileId={profileId} />,
            },
            {
              key: 'extract',
              label: '自动抽取',
              children: <ExtractionPanel profileId={profileId} />,
            },
            {
              key: 'relations',
              label: `关系（${relations.length}）`,
              children: (
                <Card size="small">
                  <Space wrap style={{ marginBottom: 12 }}>
                    <Button
                      type="primary"
                      icon={<PlusOutlined />}
                      onClick={() => setRelationModal({ open: true, row: null })}
                    >
                      新建关系
                    </Button>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      关系的两端都必须是已存在的实例；能选哪些类型由 TBox 决定
                    </Text>
                  </Space>
                  <Table
                    rowKey="id"
                    size="small"
                    loading={relationsQuery.isLoading}
                    dataSource={relations}
                    columns={relationColumns}
                    pagination={{ pageSize: 20, showSizeChanger: true }}
                  />
                </Card>
              ),
            },
            {
              key: 'import',
              label: '参数表导入',
              children: (
                <Card size="small">
                  <Space direction="vertical" size={16} style={{ width: '100%' }}>
                    <Alert
                      type="info"
                      showIcon
                      message="先预演，确认无误再正式导入"
                      description="预演只解析不写库；正式导入会按名称覆盖更新（证据只增不减）。"
                    />
                    <Upload
                      accept=".xlsx,.xlsm"
                      maxCount={1}
                      beforeUpload={(file) => {
                        setImportFile(file)
                        setImportResult(null)
                        return false
                      }}
                      onRemove={() => setImportFile(null)}
                    >
                      <Button icon={<UploadOutlined />}>选择参数表（.xlsx）</Button>
                    </Upload>
                    <Space>
                      <Button
                        disabled={!importFile}
                        loading={importMutation.isPending}
                        onClick={() => importMutation.mutate(true)}
                      >
                        预演
                      </Button>
                      <Button
                        type="primary"
                        disabled={!importFile}
                        loading={importMutation.isPending}
                        onClick={() => importMutation.mutate(false)}
                      >
                        正式导入
                      </Button>
                    </Space>
                    {importResult && (
                      <Space direction="vertical" size={8} style={{ width: '100%' }}>
                        <Space wrap>
                          <Tag>行数 {importResult.rows}</Tag>
                          <Tag color="green">新增 {importResult.created}</Tag>
                          <Tag color="blue">更新 {importResult.updated}</Tag>
                          <Tag>无变化 {importResult.unchanged}</Tag>
                          {importResult.dry_run && <Tag color="orange">预演</Tag>}
                        </Space>
                        {importResult.warnings.length > 0 && (
                          <Alert
                            type="warning"
                            message={`${importResult.warnings.length} 条告警`}
                            description={
                              <ul style={{ margin: 0, paddingLeft: 18 }}>
                                {importResult.warnings.slice(0, 8).map((w) => (
                                  <li key={w}>{w}</li>
                                ))}
                              </ul>
                            }
                          />
                        )}
                        {importResult.errors.length > 0 && (
                          <Alert
                            type="error"
                            message={`${importResult.errors.length} 行被跳过`}
                            description={
                              <ul style={{ margin: 0, paddingLeft: 18 }}>
                                {importResult.errors.slice(0, 8).map((w) => (
                                  <li key={w}>{w}</li>
                                ))}
                              </ul>
                            }
                          />
                        )}
                        <Table
                          rowKey="name"
                          size="small"
                          dataSource={importResult.preview}
                          pagination={{ pageSize: 10 }}
                          columns={[
                            { title: '参数', dataIndex: 'name', width: 100 },
                            { title: '中文名', dataIndex: 'display_zh', width: 140 },
                            {
                              title: '范围',
                              render: (_: unknown, row: OntologyImportResult['preview'][0]) =>
                                row.range ? `[${row.range[0]}, ${row.range[1]}]` : '—',
                            },
                            { title: '初值', dataIndex: 'initial', width: 80 },
                            {
                              title: '证据等级',
                              dataIndex: 'evidence_level',
                              width: 120,
                              render: (v: string) => <Tag>{v}</Tag>,
                            },
                          ]}
                        />
                      </Space>
                    )}
                  </Space>
                </Card>
              ),
            },
          ]}
        />
      </Content>

      <InstanceForm
        open={instanceModal.open}
        entityTypes={entityTypes}
        initial={instanceModal.row}
        submitting={saveInstance.isPending}
        onCancel={() => setInstanceModal({ open: false, row: null })}
        onSubmit={(values) => saveInstance.mutate(values as unknown as Record<string, unknown>)}
      />
      <RelationForm
        open={relationModal.open}
        types={typesQuery.data}
        instances={instances}
        initial={relationModal.row}
        submitting={saveRelation.isPending}
        onCancel={() => setRelationModal({ open: false, row: null })}
        onSubmit={(values) => saveRelation.mutate(values)}
      />
    </Layout>
  )
}
