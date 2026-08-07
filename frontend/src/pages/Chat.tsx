import { useCallback, useEffect, useRef, useState } from 'react'
import { Layout, Button, Empty, Space, Typography } from 'antd'
import { MessageOutlined } from '@ant-design/icons'
import SessionSidebar from '@/components/SessionSidebar'
import MessageInput from '@/components/MessageInput'
import { MessageBubble } from '@/components/MessageBubble'
import UserMenu from '@/components/UserMenu'
import { useChatStore } from '@/stores/chat'

const { Sider, Content } = Layout

const SIDEBAR_MIN = 160
const SIDEBAR_MAX = 480
const SIDEBAR_KEY = 'chat-sidebar-width'

// 知识库问答主界面：左侧会话栏（可拖拽调宽）+ 右侧聊天区
export default function Chat() {
  const { currentId, history, messages, loadHistory, historyHasMore, streaming } = useChatStore()
  const scrollRef = useRef<HTMLDivElement>(null)
  const resizingRef = useRef(false)

  // 会话栏宽度：默认 240，可拖拽调整并记忆到 localStorage
  const [sidebarWidth, setSidebarWidth] = useState<number>(() => {
    try {
      const saved = Number(localStorage.getItem(SIDEBAR_KEY))
      if (saved >= SIDEBAR_MIN && saved <= SIDEBAR_MAX) return saved
    } catch {
      /* localStorage 不可用时用默认值 */
    }
    return 240
  })

  const startResize = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    resizingRef.current = true
    document.body.style.userSelect = 'none'
    document.body.style.cursor = 'col-resize'
    const onMove = (ev: MouseEvent) => {
      if (!resizingRef.current) return
      setSidebarWidth(Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, ev.clientX)))
    }
    const onUp = () => {
      resizingRef.current = false
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
      setSidebarWidth((w) => {
        try {
          localStorage.setItem(SIDEBAR_KEY, String(w))
        } catch {
          /* 忽略持久化失败 */
        }
        return w
      })
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [])

  // 切换会话时加载历史
  useEffect(() => {
    if (currentId) loadHistory(currentId, true)
  }, [currentId, loadHistory])

  // 自动滚到底部（新消息/流式）
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [history.length, messages.length, messages[messages.length - 1]?.content])

  // 合并展示：历史（含引用/反馈/记忆来源）+ 本次流式消息
  const display = [
    ...history.map((m) => ({
      id: String(m.id),
      role: m.role,
      content: m.content,
      is_complete: m.is_complete,
      citations: m.citations,
      error: m.error,
      messageId: m.id,
      feedback: m.feedback,
      from_memory: m.from_memory,
    })),
    ...messages,
  ]

  return (
    <Layout style={{ height: '100vh' }}>
      <Sider
        width={sidebarWidth}
        theme="dark"
        style={{ borderRight: '1px solid rgba(122, 190, 255, 0.12)', position: 'relative' }}
      >
        <SessionSidebar />
        {/* 拖拽手柄：调整会话栏宽度 */}
        <div
          onMouseDown={startResize}
          title="拖动调整宽度"
          style={{
            position: 'absolute',
            top: 0,
            bottom: 0,
            right: 0,
            width: 6,
            cursor: 'col-resize',
            zIndex: 10,
          }}
        />
      </Sider>
      <Content style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <div
          className="tech-line"
          style={{
            height: 56,
            borderBottom: '1px solid rgba(122, 190, 255, 0.1)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '0 16px',
            background: 'rgba(10, 17, 34, 0.4)',
          }}
        >
          <Space>
            <MessageOutlined
              style={{ fontSize: 18, color: '#00c6ff', textShadow: '0 0 12px rgba(0, 198, 255, 0.7)' }}
            />
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
