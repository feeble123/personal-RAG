import { useEffect, useRef } from 'react'
import { Layout, Button, Empty, Space, Typography } from 'antd'
import { MessageOutlined } from '@ant-design/icons'
import SessionSidebar from '@/components/SessionSidebar'
import MessageInput from '@/components/MessageInput'
import { MessageBubble } from '@/components/MessageBubble'
import UserMenu from '@/components/UserMenu'
import { useChatStore } from '@/stores/chat'

const { Sider, Content } = Layout

// 知识库问答主界面：左侧会话栏 + 右侧聊天区
export default function Chat() {
  const { currentId, history, messages, loadHistory, historyHasMore, streaming } = useChatStore()
  const scrollRef = useRef<HTMLDivElement>(null)

  // 切换会话时加载历史
  useEffect(() => {
    if (currentId) loadHistory(currentId, true)
  }, [currentId, loadHistory])

  // 自动滚到底部（新消息/流式）
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [history.length, messages.length, messages[messages.length - 1]?.content])

  // 合并展示：历史（含引用）+ 本次流式消息
  const display = [
    ...history.map((m) => ({ id: String(m.id), role: m.role, content: m.content, is_complete: m.is_complete, citations: m.citations, error: m.error })),
    ...messages,
  ]

  return (
    <Layout style={{ height: '100vh' }}>
      <Sider width={240} theme="light" style={{ borderRight: '1px solid #f0f0f0' }}>
        <SessionSidebar />
      </Sider>
      <Content style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <div
          style={{
            height: 56,
            borderBottom: '1px solid #f0f0f0',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '0 16px',
          }}
        >
          <Space>
            <MessageOutlined style={{ fontSize: 18, color: '#1677ff' }} />
            <Typography.Text strong>知识库问答</Typography.Text>
          </Space>
          <UserMenu />
        </div>

        <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto', padding: '24px 16px' }}>
          {!currentId ? (
            <Empty style={{ marginTop: 80 }} description="新建会话，向水利知识库提问吧">
              <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
                例如：明渠均匀流的形成条件是什么？
              </Typography.Paragraph>
            </Empty>
          ) : display.length === 0 ? (
            <Empty style={{ marginTop: 80 }} image={Empty.PRESENTED_IMAGE_SIMPLE} description="开始你的第一轮问答" />
          ) : (
            <div style={{ maxWidth: 860, margin: '0 auto' }}>
              {historyHasMore && !streaming && (
                <div style={{ textAlign: 'center', marginBottom: 12 }}>
                  <Button size="small" type="link" onClick={() => loadHistory(currentId)}>
                    加载更早的消息
                  </Button>
                </div>
              )}
              {display.map((m) => (
                <MessageBubble key={m.id} msg={m} />
              ))}
            </div>
          )}
        </div>

        <MessageInput />
      </Content>
    </Layout>
  )
}
