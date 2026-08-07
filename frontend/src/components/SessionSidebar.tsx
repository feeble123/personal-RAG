import { useEffect } from 'react'
import { App, Button, Dropdown, Empty, Input, List, Spin, Typography } from 'antd'
import { DeleteOutlined, EditOutlined, MessageOutlined, PlusOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import { useChatStore } from '@/stores/chat'

// 会话侧栏：会话列表 + 新建 + 重命名 + 删除
export default function SessionSidebar() {
  const {
    conversations,
    currentId,
    setCurrent,
    newConversation,
    renameConversation,
    deleteConversation,
    refreshConversations,
    loadingConversations,
  } = useChatStore()
  const { modal } = App.useApp()

  useEffect(() => {
    refreshConversations()
  }, [refreshConversations])

  // 重命名用 modal.confirm（与删除同一机制，已验证可用）：
  // 行内 Input 方案在 Dropdown 关闭时失焦立即提交（用原标题提交 → 看起来"没反应"）
  const startRename = (id: number, title: string) => {
    let next = title
    modal.confirm({
      title: '重命名会话',
      width: 360,
      content: (
        <Input
          defaultValue={title}
          onChange={(e) => {
            next = e.target.value
          }}
          placeholder="输入新的会话名称"
          maxLength={100}
        />
      ),
      okText: '保存',
      cancelText: '取消',
      onOk: async () => {
        const name = next.trim()
        if (name && name !== title) await renameConversation(id, name)
      },
    })
  }

  const confirmDelete = (id: number) => {
    modal.confirm({
      title: '删除该会话？',
      content: '删除后该会话的对话记录将不可恢复。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => deleteConversation(id),
    })
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ padding: '12px 12px 4px' }}>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          block
          onClick={async () => {
            const id = await newConversation()
            if (id) setCurrent(id)
          }}
        >
          新建会话
        </Button>
      </div>

      <div style={{ flex: 1, overflowY: 'auto', padding: '8px' }}>
        {loadingConversations && conversations.length === 0 ? (
          <Spin style={{ display: 'block', margin: '32px auto' }} />
        ) : conversations.length === 0 ? (
          <Empty description="暂无会话" style={{ marginTop: 32 }} image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <List
            dataSource={conversations}
            renderItem={(conv) => (
              <div
                key={conv.id}
                onClick={() => currentId !== conv.id && setCurrent(conv.id)}
                style={{
                  padding: '8px 10px',
                  borderRadius: 8,
                  cursor: 'pointer',
                  marginBottom: 4,
                  background: currentId === conv.id ? 'rgba(0, 198, 255, 0.16)' : 'transparent',
                  border: currentId === conv.id ? '1px solid rgba(0, 198, 255, 0.3)' : '1px solid transparent',
                }}
              >
                <Dropdown
                  trigger={['contextMenu']}
                  menu={{
                    onClick: ({ key }) => {
                      if (key === 'rename') startRename(conv.id, conv.title)
                      else if (key === 'delete') confirmDelete(conv.id)
                    },
                    items: [
                      { key: 'rename', icon: <EditOutlined />, label: '重命名' },
                      { key: 'delete', icon: <DeleteOutlined />, danger: true, label: '删除' },
                    ],
                  }}
                >
                  <div style={{ width: '100%' }}>
                    <Typography.Text
                      ellipsis
                      style={{ fontSize: 13, display: 'block', width: '100%' }}
                    >
                      <MessageOutlined style={{ marginRight: 6 }} />
                      {conv.title}
                    </Typography.Text>
                    <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                      {dayjs(conv.last_message_at).format('MM-DD HH:mm')}
                    </Typography.Text>
                  </div>
                </Dropdown>
              </div>
            )}
          />
        )}
      </div>
    </div>
  )
}
