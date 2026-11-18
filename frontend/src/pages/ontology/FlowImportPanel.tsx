/**
 * 流程链条导入（O8-1）：一张 Excel 建「阶段 → 动作 → 工具 / 数据」整条流水线。
 *
 * 为什么用一张表而不是让人在界面上一个个点：一条流水线几十个节点、几十条边，
 * 点一遍容易错（NEXT_STAGE 连反、动作挂错阶段）。表里一行就是一个动作，
 * 程序按行推导四类边，人只判断「这张表对不对」。
 *
 * 流程长什么样是人定的：表里没写的类型不会偷偷造，缺类型/关系直接报错。
 */
import { useState } from 'react'
import { Alert, App, Button, Card, Space, Table, Tag, Typography, Upload } from 'antd'
import { UploadOutlined } from '@ant-design/icons'
import { errMsg } from '@/api/client'
import { ontologyApi } from '@/api/modules'
import type { FlowImportResult } from '@/api/types'

const { Text } = Typography

interface Props {
  profileId: string
}

export default function FlowImportPanel({ profileId }: Props) {
  const { message } = App.useApp()
  const [file, setFile] = useState<File | null>(null)
  const [result, setResult] = useState<FlowImportResult | null>(null)
  const [loading, setLoading] = useState(false)

  const run = async (dryRun: boolean) => {
    if (!file) {
      message.warning('先选一个 Excel 文件')
      return
    }
    setLoading(true)
    try {
      const data = await ontologyApi.importFlow(file, {
        profile: profileId || undefined,
        dryRun,
      })
      setResult(data)
      if (dryRun) {
        message.success('预演完成，确认无误再点「正式导入」')
      } else {
        message.success(
          `导入完成：新增 ${data.created ?? 0} 个实例、更新 ${data.updated ?? 0} 个`,
        )
        setFile(null)
      }
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }

  return (
    <Card size="small">
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="一行 = 一个动作，程序自动连好四类关系"
        description={
          <span>
            表头：<Text code>阶段</Text> <Text code>阶段序号</Text> <Text code>动作</Text>{' '}
            <Text code>工具</Text> <Text code>工具来源</Text> <Text code>需要的数据</Text>{' '}
            <Text code>数据格式</Text> <Text code>说明</Text>。
            阶段列留空表示沿用上一行；阶段序号不填就按出现顺序编号。
            导入会建出：阶段链（NEXT_STAGE）、阶段含动作（PERFORMED_BY）、动作用工具（USES_TOOL）、
            动作需要数据（REQUIRES_DATA）。
          </span>
        }
      />

      <Space wrap style={{ marginBottom: 12 }}>
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
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Space wrap>
            <Tag color="blue">阶段 {result.stages}</Tag>
            <Tag color="blue">动作 {result.actions}</Tag>
            <Tag color="blue">工具 {result.tools}</Tag>
            <Tag color="blue">数据 {result.data_assets}</Tag>
            <Tag color="green">关系 {result.relations}</Tag>
            {result.errors.length > 0 && (
              <Tag color="red">问题 {result.errors.length}</Tag>
            )}
            {!result.dry_run && (
              <Tag color="purple">已导入 {result.created ?? 0} 个</Tag>
            )}
          </Space>

          {result.skipped_headers.length > 0 && (
            <Text type="warning" style={{ fontSize: 12 }}>
              认不出的列（已跳过）：{result.skipped_headers.join('、')}
            </Text>
          )}
          {result.errors.length > 0 && (
            <Alert
              type="warning"
              showIcon
              message="有问题的行（不会写入）"
              description={
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {result.errors.slice(0, 8).map((e, i) => (
                    <li key={i}>
                      {e.row_no ? `第 ${e.row_no} 行：` : ''}
                      {e.message}
                    </li>
                  ))}
                </ul>
              }
            />
          )}

          <Table
            size="small"
            rowKey="row_no"
            pagination={{ pageSize: 10 }}
            dataSource={result.preview ?? []}
            columns={[
              { title: '行', dataIndex: 'row_no', width: 60 },
              { title: '阶段', dataIndex: 'stage', width: 140 },
              { title: '动作', dataIndex: 'action', width: 180 },
              { title: '工具', dataIndex: 'tool', width: 160 },
              { title: '需要的数据', dataIndex: 'data_asset' },
            ]}
          />
        </Space>
      )}
    </Card>
  )
}
