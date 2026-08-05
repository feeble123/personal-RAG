import { useState } from 'react'
import { Button, Card, Form, Input, Typography, App, Space } from 'antd'
import { UserOutlined, LockOutlined } from '@ant-design/icons'
import { Link, useNavigate } from 'react-router-dom'
import { authApi } from '@/api/modules'
import { useAuthStore } from '@/stores/auth'

export default function Register() {
  const [loading, setLoading] = useState(false)
  const setAuth = useAuthStore((s) => s.setAuth)
  const navigate = useNavigate()
  const { message } = App.useApp()

  const onFinish = async (values: { username: string; password: string; nickname?: string }) => {
    setLoading(true)
    try {
      const res = await authApi.register(values)
      setAuth(res.access_token, res.user)
      message.success('注册成功，已自动登录')
      navigate('/chat', { replace: true })
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="auth-bg">
      <Card style={{ width: 380 }}>
        <Space direction="vertical" style={{ width: '100%', marginBottom: 8 }} align="center">
          <Typography.Title level={3} style={{ margin: 0 }}>
            注册账号
          </Typography.Title>
          <Typography.Text type="secondary">注册后可进行知识库问答</Typography.Text>
        </Space>
        <Form onFinish={onFinish} size="large">
          <Form.Item
            name="username"
            rules={[
              { required: true, message: '请输入用户名' },
              { pattern: /^[a-zA-Z0-9_]{3,50}$/, message: '3-50 位字母/数字/下划线' },
            ]}
          >
            <Input prefix={<UserOutlined />} placeholder="用户名（字母/数字/下划线）" />
          </Form.Item>
          <Form.Item name="nickname">
            <Input placeholder="昵称（可选）" />
          </Form.Item>
          <Form.Item
            name="password"
            rules={[
              { required: true, message: '请输入密码' },
              { min: 6, message: '密码至少 6 位' },
            ]}
          >
            <Input.Password prefix={<LockOutlined />} placeholder="密码（至少 6 位）" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" block loading={loading}>
              注 册
            </Button>
          </Form.Item>
        </Form>
        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          <Typography.Text type="secondary">已有账号？</Typography.Text>
          <Link to="/login">去登录</Link>
        </Space>
      </Card>
    </div>
  )
}
