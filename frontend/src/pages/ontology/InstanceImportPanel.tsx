/**
 * 实例 Excel 批量导入（通用）：表头写属性名或属性中文说明，一行一个实例。
 *
 * 从原来的「实例」页签弹窗里搬出来，收进统一的「导入」页——
 * 三个导入入口合到一起，用户不用再猜"该去哪个页签"。
 *
 * 只建实例，**不建关系**；要对没错的列会被跳过并列出来，不猜。
 */
import { useState } from 'react'
import { Alert, App, Button, Empty, Select, Space, Table, Tag, Typography, Upload } from 'antd'
import { UploadOutlined } from '@ant-design/icons'
import { errMsg } from '@/api/client'
import { ontologyApi } from '@/api/modules'
import type { InstanceImportResult } from '@/api/types'

const { Text } = Typography

interface Props {
  profileId: string
  entityTypes: { name: string; display_name?: string }[]
  /** 导入成功后通知外层刷新实例/图缓存 */
  onImported?: () => void
}

export default function InstanceImportPanel({ profileId, entityTypes, onImported }: Props) {
  const { message } = App.useApp()
  const [file, setFile] = useState<File | null>(null)
  const [entityType, setEntityType] = useState('')
  const [result, setResult] = useState<InstanceImportResult | null>(null)
  const [loading, setLoading] = useState(false)

  const run = async (dryRun: boolean) => {
    if (!file) {
      message.warning('先选一个 Excel 文件')
      return
    }
    if (!entityType) {
      message.warning('先选这批数据属于哪个实体类型')
      return
    }
    setLoading(true)
    try {
      const data = await ontologyApi.importInstances(file, {
        entityType,
        profile: profileId || undefined,
        dryRun,
      })
      setResult(data)
      if (dryRun) {
        message.success('预演完成，确认无误再点「正式导入」')
      } else {
        message.success(`导入完成：新增 ${data.created ?? 0} / 更新 ${data.updated ?? 0}`)
        setFile(null)
        onImported?.()
      }
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }

  const typeOptions = entityTypes.map((t) => ({
    value: t.name,
    label: t.display_name || t.name,
  }))

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="表头写属性名或属性的中文说明，程序只认对得上的列"
        description="必须有名称列（名称 / display_zh）。对不上的列会被跳过并列出来，不猜。默认先预演，确认无误再正式导入。这里只建实例，不建实例之间的关系。"
      />

      <Space wrap>
        <Select
          value={entityType || undefined}
          onChange={(v) => {
            setEntityType(v)
            setResult(null)
          }}
          style={{ width: 220 }}
          placeholder="这批数据属于哪个类型"
          options={typeOptions}
        />
        <Upload
          accept=".xlsx,.xls"
          showUploadList={false}
          beforeUpload={(f) => {
            setFile(f)
            setResult(null)
            return false
          }}
        >
          <Button icon={<UploadOutlined />}>{file ? file.name : '选择 Excel'}</Button>
        </Upload>
        <Button loading={loading} onClick={() => run(true)}>
          先预演
        </Button>
        <Button type="primary" loading={loading} onClick={() => run(false)}>
          正式导入
        </Button>
      </Space>

      {result && (
        <Space direction="vertical" size={6} style={{ width: '100%' }}>
          <Space wrap>
            <Tag color="blue">共 {result.total} 行</Tag>
            <Tag color="green">可导入 {result.valid}</Tag>
            {result.rejected > 0 && <Tag color="red">有问题 {result.rejected}</Tag>}
            {!result.dry_run && <Tag color="purple">已写入 {result.created ?? 0} 个</Tag>}
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            对上的列：
            {Object.entries(result.mapped)
              .map(([h, a]) => `${h}→${a}`)
              .join('，') || '—'}
          </Text>
          {result.skipped_headers.length > 0 && (
            <Text type="warning" style={{ fontSize: 12 }}>
              跳过的列（没对上属性）：{result.skipped_headers.join('、')}
            </Text>
          )}
          {result.errors.length > 0 && (
            <div style={{ maxHeight: 160, overflow: 'auto' }}>
              {result.errors.slice(0, 20).map((e, i) => (
                <div key={i}>
                  <Text type="danger" style={{ fontSize: 12 }}>
                    第 {e.row_no} 行{e.name ? `（${e.name}）` : ''}：{e.message}
                  </Text>
                </div>
              ))}
            </div>
          )}
          {result.rows?.length ? (
            <Table
              size="small"
              rowKey="row_no"
              pagination={{ pageSize: 10 }}
              dataSource={result.rows}
              columns={[
                { title: '行', dataIndex: 'row_no', width: 60 },
                { title: '名称', dataIndex: 'name', width: 160 },
                {
                  title: '属性',
                  render: (_: unknown, row: NonNullable<InstanceImportResult['rows']>[0]) => (
                    <Text style={{ fontSize: 12 }}>
                      {Object.entries(row.props ?? {})
                        .map(([k, v]) => `${k}=${String(v)}`)
                        .join('，')}
                    </Text>
                  ),
                },
              ]}
            />
          ) : (
            !result.errors.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有可导入的行" />
          )}
        </Space>
      )}
    </Space>
  )
}
