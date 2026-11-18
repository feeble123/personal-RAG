/**
 * 参数先验表导入（调参本体专用）：带物理/通用范围、条件范围、敏感度和依据。
 *
 * 为什么不能并进「实例 Excel 导入」：那张泛用表只有"属性名 → 值"，
 * 表达不了「湿润 + 单峰时取 0.15~0.30」这种条件范围，也带不了证据溯源。
 * 所以这里是**独立的一套列**，只是入口收在同一个「导入」页里。
 */
import { useState } from 'react'
import { Alert, App, Button, Space, Table, Tag, Typography, Upload } from 'antd'
import { UploadOutlined } from '@ant-design/icons'
import { useMutation } from '@tanstack/react-query'
import { errMsg } from '@/api/client'
import { ontologyApi } from '@/api/modules'
import type { OntologyImportResult } from '@/api/types'

const { Text } = Typography

interface Props {
  profileId: string
  /** 当前库有没有 Parameter 类型：没有就提示切库（而不是让人点了才看到 13 行报错） */
  hasParamType: boolean
  libraryName: string
  onImported?: () => void
}

export default function ParamImportPanel({
  profileId,
  hasParamType,
  libraryName,
  onImported,
}: Props) {
  const { message } = App.useApp()
  const [file, setFile] = useState<File | null>(null)
  const [result, setResult] = useState<OntologyImportResult | null>(null)

  const mutation = useMutation({
    mutationFn: (dryRun: boolean) =>
      ontologyApi.importParams(file as File, { dryRun, profile: profileId || undefined }),
    onSuccess: (data) => {
      setResult(data)
      message.success(data.dry_run ? '预演完成，确认无误后再点「正式导入」' : '导入完成')
      if (!data.dry_run) onImported?.()
    },
    onError: (e) => message.error(errMsg(e)),
  })

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="先预演，确认无误再正式导入"
        description="预演只解析不写库；正式导入会按名称覆盖更新（证据只增不减）。这张表支持物理/通用范围、条件范围和依据溯源，普通实例导入表达不了这些。"
      />

      {!hasParamType && (
        <Alert
          type="warning"
          showIcon
          message={`当前图谱库「${libraryName}」没有「参数」类型`}
          description="参数表只认 Parameter 类型。请先在上方切换到「调参本体」这类含参数类型的库，再导入。"
        />
      )}

      <Upload
        accept=".xlsx,.xlsm"
        maxCount={1}
        beforeUpload={(f) => {
          setFile(f)
          setResult(null)
          return false
        }}
        onRemove={() => setFile(null)}
      >
        <Button icon={<UploadOutlined />}>选择参数表（.xlsx）</Button>
      </Upload>

      <Space>
        <Button
          disabled={!file}
          loading={mutation.isPending}
          onClick={() => mutation.mutate(true)}
        >
          预演
        </Button>
        <Button
          type="primary"
          disabled={!file}
          loading={mutation.isPending}
          onClick={() => mutation.mutate(false)}
        >
          正式导入
        </Button>
      </Space>

      {result && (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Space wrap>
            <Tag>行数 {result.rows}</Tag>
            <Tag color="green">新增 {result.created}</Tag>
            <Tag color="blue">更新 {result.updated}</Tag>
            <Tag>无变化 {result.unchanged}</Tag>
            {result.dry_run && <Tag color="orange">预演</Tag>}
          </Space>
          {result.errors.length === 0 && result.created === 0 && result.updated === 0 && (
            <Alert
              type="success"
              showIcon
              message="表里的参数都已在库中，且内容完全一致"
              description="所以「新增/更新」都是 0——说明之前已经导入过，不是导入失败。"
            />
          )}
          {result.warnings.length > 0 && (
            <Alert
              type="warning"
              message={`${result.warnings.length} 条告警`}
              description={
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {result.warnings.slice(0, 8).map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              }
            />
          )}
          {result.errors.length > 0 && (
            <Alert
              type="error"
              message={`${result.errors.length} 行被跳过`}
              description={
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {result.errors.slice(0, 8).map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              }
            />
          )}
          <Table
            rowKey="name"
            size="small"
            dataSource={result.preview}
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
          <Text type="secondary" style={{ fontSize: 12 }}>
            预演和正式导入走的是同一套写入逻辑：预演会真跑一遍再回滚，所以数字和正式导入一致。
          </Text>
        </Space>
      )}
    </Space>
  )
}
