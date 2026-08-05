import { useState } from 'react'
import { App, Avatar, Dropdown, Form, Input, Modal, Space, Typography } from 'antd'
import { DatabaseOutlined, KeyOutlined, LogoutOutlined, UserOutlined } from '@ant-design/icons'
import { useNavigate, Link } from 'react-router-dom'
import { authApi } from '@/api/modules'
import { useAuthStore } from '@/stores/auth'
import { errMsg } from '@/api/client'

// 用户菜单：修改密码 / 知识库管理(admin) / 退出登录
export default function UserMenu() {
  const user = useAuthStore((s) => s.user)
  const setUser = useAuthStore((s) => s.setUser)
  const logout = useAuthStore((s) => s.logout)
  const navigate = useNavigate()
  const { message } = App.useApp()
  const [pwdOpen, setPwdOpen] = useState(false)
  const [form] = Form.useForm()

  const changePassword = async (values: { old_password: string; new_password: string }) => {
    try {
      const updated = await authApi.changePassword(values)
      setUser(updated)
      message.success('密码修改成功')
      setPwdOpen(false)
      form.resetFields()
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  const items = [
    user?.role === 'admin'
      ? {
          key: 'kb',
          icon: <DatabaseOutlined />,
          label: <Link to="/knowledge">知识库管理</Link>,
        }
      : null,
    { key: 'pwd', icon: <KeyOutlined />, label: '修改密码', onClick: () => setPwdOpen(true) },
    { type: 'divider' as const },
    {
      key: 'logout',
      icon: <LogoutOutlined />,
      danger: true,
      label: '退出登录',
      onClick: () => {
        logout()
        navigate('/login', { replace: true })
      },
    },
  ].filter(Boolean)

  return (
    <>
      <Dropdown menu={{ items }} placement="bottomRight">
        <Space style={{ cursor: 'pointer' }}>
          <Avatar style={{ backgroundColor: '#1677ff' }} icon={<UserOutlined />} />
          <Typography.Text>{user?.nickname || user?.username}</Typography.Text>
        </Space>
      </Dropdown>

      <Modal title="修改密码" open={pwdOpen} onCancel={() => setPwdOpen(false)} onOk={() => form.submit()} okText="确认修改">
        <Form form={form} layout="vertical" onFinish={changePassword} style={{ marginTop: 16 }}>
          <Form.Item name="old_password" label="原密码" rules={[{ required: true, message: '请输入原密码' }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item
            name="new_password"
            label="新密码"
            rules={[
              { required: true, message: '请输入新密码' },
              { min: 6, message: '至少 6 位' },
            ]}
          >
            <Input.Password />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
