import { useState } from 'react'
import { Button, Card, Input, Layout, Select, Space, Table, Tag, Typography } from 'antd'
import { ArrowLeftOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import UserMenu from '@/components/UserMenu'
import { auditApi } from '@/api/modules'

const { Header, Content } = Layout

// 操作类型 → 中文标签 + 颜色（供过滤下拉与表格列共用）
const ACTION_META: Record<string, { label: string; color: string }> = {
  'user.create': { label: '创建账号', color: 'cyan' },
  'user.update': { label: '修改账号', color: 'blue' },
  'user.delete': { label: '删除账号', color: 'red' },
  'user.password_reset': { label: '重置密码', color: 'orange' },
  'kb.create': { label: '创建知识库', color: 'green' },
  'kb.delete': { label: '删除知识库', color: 'red' },
  'document.upload': { label: '上传文档', color: 'cyan' },
  'document.delete': { label: '删除文档', color: 'red' },
  'document.reparse': { label: '重解析文档', color: 'gold' },
}

// 审计日志（单元 I）：管理员敏感操作留痕，只读、不可篡改
export default function AuditLogs() {
  const navigate = useNavigate()
  const [q, setQ] = useState('')
  const [action, setAction] = useState<string | undefined>(undefined)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)

  const listQuery = useQuery({
    queryKey: ['audit-logs', q, action, page, pageSize],
    queryFn: () => auditApi.list({ q: q || undefined, action, page, page_size: pageSize }),
  })

  const columns = [
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 160,
      render: (v: string) => dayjs(v).format('YYYY-MM-DD HH:mm:ss'),
    },
    {
      title: '操作人',
      dataIndex: 'actor_name',
      width: 120,
      render: (v: string) => v || '—',
    },
    {
      title: '操作',
      dataIndex: 'action',
      width: 130,
      render: (v: string) => {
        const meta = ACTION_META[v]
        return meta ? <Tag color={meta.color}>{meta.label}</Tag> : <Tag>{v}</Tag>
      },
    },
    {
      title: '摘要',
      dataIndex: 'detail',
      render: (v: string) => v || '—',
    },
    { title: '来源 IP', dataIndex: 'client_ip', width: 130, render: (v: string | null) => v || '—' },
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
          <SafetyCertificateOutlined style={{ color: '#00c6ff', fontSize: 18 }} />
          <Typography.Text strong>审计日志</Typography.Text>
          <Tag color="red">管理员</Tag>
        </Space>
        <UserMenu />
      </Header>

      <Content style={{ padding: 24, overflowY: 'auto' }}>
        <Card size="small" style={{ marginBottom: 16 }}>
          <Space wrap>
            <Input.Search
              placeholder="按操作人/摘要搜索"
              allowClear
              style={{ width: 220 }}
              onSearch={(v) => {
                setQ(v || '')
                setPage(1)
              }}
            />
            <Select
              allowClear
              placeholder="按操作类型筛选"
              style={{ width: 180 }}
              value={action}
              onChange={(v) => {
                setAction(v)
                setPage(1)
              }}
              options={Object.entries(ACTION_META).map(([value, meta]) => ({
                value,
                label: meta.label,
              }))}
            />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              共 {listQuery.data?.total ?? 0} 条记录（只读留痕，不可删除或篡改）
            </Typography.Text>
          </Space>
        </Card>

        <Card size="small">
          <Table
            rowKey="id"
            size="small"
            loading={listQuery.isLoading}
            dataSource={listQuery.data?.items ?? []}
            columns={columns}
            pagination={{
              current: page,
              pageSize,
              total: listQuery.data?.total ?? 0,
              showSizeChanger: true,
              pageSizeOptions: [10, 20, 50, 100],
              onChange: (p, ps) => {
                setPage(p)
                setPageSize(ps)
              },
            }}
          />
        </Card>
      </Content>
    </Layout>
  )
}
