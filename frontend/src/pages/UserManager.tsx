import { useState } from 'react'
import {
  App,
  Button,
  Card,
  Form,
  Input,
  Layout,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import {
  ArrowLeftOutlined,
  DeleteOutlined,
  KeyOutlined,
  PlusOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import dayjs from 'dayjs'
import UserMenu from '@/components/UserMenu'
import { usersApi } from '@/api/modules'
import { useAuthStore } from '@/stores/auth'
import type { User } from '@/api/types'

const { Header, Content } = Layout

// 账号管理系统（仅管理员）：列表/搜索 + 创建 + 改角色 + 启停 + 重置密码 + 删除
export default function UserManager() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { message } = App.useApp()
  const currentUser = useAuthStore((s) => s.user)

  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [createOpen, setCreateOpen] = useState(false)
  const [createForm] = Form.useForm()
  const [resetTarget, setResetTarget] = useState<User | null>(null)
  const [resetForm] = Form.useForm()

  const invalidate = () => qc.invalidateQueries({ queryKey: ['users'] })

  const listQuery = useQuery({
    queryKey: ['users', q, page, pageSize],
    queryFn: () => usersApi.list({ q: q || undefined, page, page_size: pageSize }),
  })

  const createMut = useMutation({
    mutationFn: (d: { username: string; password: string; nickname?: string; role: string }) =>
      usersApi.create(d),
    onSuccess: () => {
      invalidate()
      setCreateOpen(false)
      createForm.resetFields()
      message.success('账号已创建')
    },
  })
  const patchMut = useMutation({
    mutationFn: (p: { id: number; data: { role?: string; is_active?: boolean } }) => usersApi.patch(p.id, p.data),
    onSuccess: invalidate,
  })
  const resetMut = useMutation({
    mutationFn: (p: { id: number; password: string }) => usersApi.resetPassword(p.id, p.password),
    onSuccess: () => {
      invalidate()
      setResetTarget(null)
      resetForm.resetFields()
      message.success('密码已重置')
    },
  })
  const removeMut = useMutation({
    mutationFn: (id: number) => usersApi.remove(id),
    onSuccess: () => {
      invalidate()
      message.success('账号已删除')
    },
  })

  const columns = [
    {
      title: '用户名',
      dataIndex: 'username',
      width: 160,
      render: (v: string, r: User) => (
        <Space size={6}>
          <UserOutlined style={{ color: '#00c6ff' }} />
          {v}
          {r.id === currentUser?.id && <Tag color="gold">当前账号</Tag>}
        </Space>
      ),
    },
    { title: '昵称', dataIndex: 'nickname', width: 140, render: (v: string | null) => v || '—' },
    {
      title: '角色',
      dataIndex: 'role',
      width: 100,
      render: (v: string) => (
        <Tag color={v === 'admin' ? 'gold' : 'blue'}>{v === 'admin' ? '管理员' : '普通用户'}</Tag>
      ),
    },
    {
      title: '状态',
      dataIndex: 'is_active',
      width: 80,
      render: (v: boolean) => <Tag color={v ? 'success' : 'error'}>{v ? '启用' : '禁用'}</Tag>,
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 150,
      render: (v?: string) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm') : '—'),
    },
    {
      title: '操作',
      width: 240,
      render: (_: unknown, r: User) => {
        const isSelf = r.id === currentUser?.id
        return (
          <Space size={0}>
            <Button
              size="small"
              type="link"
              icon={<KeyOutlined />}
              onClick={() => {
                setResetTarget(r)
                resetForm.resetFields()
              }}
            >
              重置密码
            </Button>
            {!isSelf && (
              <>
                <Popconfirm
                  title={r.role === 'admin' ? '设为普通用户？' : '设为管理员？'}
                  onConfirm={() => patchMut.mutate({ id: r.id, data: { role: r.role === 'admin' ? 'user' : 'admin' } })}
                >
                  <Button size="small" type="link">
                    {r.role === 'admin' ? '设为普通' : '设为管理员'}
                  </Button>
                </Popconfirm>
                <Popconfirm
                  title={r.is_active ? '禁用该账号？' : '启用该账号？'}
                  onConfirm={() => patchMut.mutate({ id: r.id, data: { is_active: !r.is_active } })}
                >
                  <Button size="small" type="link">
                    {r.is_active ? '禁用' : '启用'}
                  </Button>
                </Popconfirm>
                <Popconfirm
                  title="删除该账号？其会话/消息/问答记忆将一并删除，不可恢复。"
                  onConfirm={() => removeMut.mutate(r.id)}
                >
                  <Button size="small" type="link" danger icon={<DeleteOutlined />}>
                    删除
                  </Button>
                </Popconfirm>
              </>
            )}
          </Space>
        )
      },
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
          <TeamOutlined style={{ color: '#00c6ff', fontSize: 18 }} />
          <Typography.Text strong>账号管理</Typography.Text>
          <Tag color="red">管理员</Tag>
        </Space>
        <UserMenu />
      </Header>

      <Content style={{ padding: 24, overflowY: 'auto' }}>
        <Card size="small" style={{ marginBottom: 16 }}>
          <Space wrap>
            <Input.Search
              placeholder="按用户名搜索"
              allowClear
              style={{ width: 220 }}
              onSearch={(v) => {
                setQ(v || '')
                setPage(1)
              }}
            />
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
              创建账号
            </Button>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              共 {listQuery.data?.total ?? 0} 个账号
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
              pageSizeOptions: [10, 20, 50],
              onChange: (p, ps) => {
                setPage(p)
                setPageSize(ps)
              },
            }}
          />
        </Card>
      </Content>

      {/* 创建账号 */}
      <Modal
        title="创建账号"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => createForm.submit()}
        okText="创建"
        confirmLoading={createMut.isPending}
      >
        <Form
          form={createForm}
          layout="vertical"
          style={{ marginTop: 16 }}
          onFinish={(v) =>
            createMut.mutate({
              username: v.username,
              password: v.password,
              nickname: v.nickname,
              role: v.role ?? 'user',
            })
          }
        >
          <Form.Item
            name="username"
            label="用户名"
            rules={[
              { required: true, message: '请输入用户名' },
              { pattern: /^[a-zA-Z0-9_]{3,50}$/, message: '3-50 位字母/数字/下划线' },
            ]}
          >
            <Input placeholder="登录用，如 zhangsan" />
          </Form.Item>
          <Form.Item name="password" label="初始密码" rules={[{ required: true, message: '请输入密码' }, { min: 6, message: '至少 6 位' }]}>
            <Input.Password placeholder="至少 6 位" />
          </Form.Item>
          <Form.Item name="nickname" label="昵称">
            <Input placeholder="可选" />
          </Form.Item>
          <Form.Item name="role" label="角色" initialValue="user">
            <Select
              options={[
                { value: 'user', label: '普通用户' },
                { value: 'admin', label: '管理员' },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* 重置密码 */}
      <Modal
        title={`重置密码 - ${resetTarget?.username ?? ''}`}
        open={!!resetTarget}
        onCancel={() => setResetTarget(null)}
        onOk={() => resetForm.submit()}
        okText="确认重置"
        confirmLoading={resetMut.isPending}
      >
        <Form
          form={resetForm}
          layout="vertical"
          style={{ marginTop: 16 }}
          onFinish={(v) => resetTarget && resetMut.mutate({ id: resetTarget.id, password: v.new_password })}
        >
          <Form.Item
            name="new_password"
            label="新密码"
            rules={[{ required: true, message: '请输入新密码' }, { min: 6, message: '至少 6 位' }]}
          >
            <Input.Password placeholder="至少 6 位" />
          </Form.Item>
          <Form.Item
            name="confirm"
            label="确认新密码"
            dependencies={['new_password']}
            rules={[
              { required: true, message: '请再次输入新密码' },
              ({ getFieldValue }) => ({
                validator: (_, value) =>
                  !value || getFieldValue('new_password') === value
                    ? Promise.resolve()
                    : Promise.reject(new Error('两次输入的密码不一致')),
              }),
            ]}
          >
            <Input.Password placeholder="再次输入" />
          </Form.Item>
        </Form>
      </Modal>
    </Layout>
  )
}
