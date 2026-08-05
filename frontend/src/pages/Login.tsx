import { useState } from 'react'
import { Button, Card, Form, Input, Typography, App, Space } from 'antd'
import { UserOutlined, LockOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { authApi } from '@/api/modules'
import { useAuthStore } from '@/stores/auth'

export default function Login() {
  const [loading, setLoading] = useState(false)
  const setAuth = useAuthStore((s) => s.setAuth)
  const navigate = useNavigate()
  const location = useLocation()
  const { message } = App.useApp()

  const from = (location.state as { from?: { pathname: string } } | null)?.from?.pathname

  const onFinish = async (values: { username: string; password: string }) => {
    setLoading(true)
    try {
      const res = await authApi.login(values)
      setAuth(res.access_token, res.user)
      message.success('登录成功')
      navigate(res.user.role === 'admin' && from ? from : res.user.role === 'admin' ? '/knowledge' : '/chat', {
        replace: true,
      })
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="auth-bg">
      <Card style={{ width: 380 }}>
        <Space direction="vertical" style={{ width: '100%', marginBottom: 8 }} align="center">
          <ThunderboltOutlined style={{ fontSize: 40, color: '#1677ff' }} />
          <Typography.Title level={3} style={{ margin: 0 }}>
            水利知识库问答系统
          </Typography.Title>
          <Typography.Text type="secondary">基于 LangChain 的 RAG 智能问答</Typography.Text>
        </Space>
        <Form onFinish={onFinish} size="large">
          <Form.Item name="username" rules={[{ required: true, message: '请输入用户名' }]}>
            <Input prefix={<UserOutlined />} placeholder="用户名" />
          </Form.Item>
          <Form.Item name="password" rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password prefix={<LockOutlined />} placeholder="密码" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" block loading={loading}>
              登 录
            </Button>
          </Form.Item>
        </Form>
        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          <Typography.Text type="secondary">还没有账号？</Typography.Text>
          <Link to="/register">立即注册</Link>
        </Space>
      </Card>
    </div>
  )
}
