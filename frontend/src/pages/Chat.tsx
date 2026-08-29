import { useCallback, useEffect, useRef, useState } from 'react'
import { Layout, Button, Space, Tag, Typography } from 'antd'
import { MessageOutlined } from '@ant-design/icons'
import SessionSidebar from '@/components/SessionSidebar'
import MessageInput from '@/components/MessageInput'
import { MessageBubble } from '@/components/MessageBubble'
import ChatBackground from '@/components/ChatBackground'
import UserMenu from '@/components/UserMenu'
import { useChatStore } from '@/stores/chat'

const { Sider, Content } = Layout

const SIDEBAR_MIN = 160
const SIDEBAR_MAX = 480
const SIDEBAR_KEY = 'chat-sidebar-width'

// 欢迎卡示例问题：点击即建会话并发送
const HERO_QUESTIONS = [
  '明渠均匀流的形成条件是什么？',
  '水库汛期调度运用计划包含哪些内容？',
  '混凝土坝的渗流问题如何防治？',
  '什么是管涌？有哪些治理措施？',
]

// 知识库问答主界面：左侧会话栏（可拖拽调宽）+ 右侧聊天区
export default function Chat() {
  const { currentId, history, messages, loadHistory, historyHasMore, streaming, newConversation, send } =
    useChatStore()
  const scrollRef = useRef<HTMLDivElement>(null)
  const resizingRef = useRef(false)

  // 会话栏宽度：默认随视口自适应（约 16%，200~300px），可拖拽调整并记忆到 localStorage
  const [sidebarWidth, setSidebarWidth] = useState<number>(() => {
    try {
      const saved = Number(localStorage.getItem(SIDEBAR_KEY))
      if (saved >= SIDEBAR_MIN && saved <= SIDEBAR_MAX) return saved
    } catch {
      /* localStorage 不可用时用默认值 */
    }
    const vwDefault = Math.round(window.innerWidth * 0.16)
    return Math.min(300, Math.max(200, vwDefault))
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

  // 自动滚到底部（新消息/流式）。最后一条消息内容提成变量，供 hooks 依赖静态检查。
  const lastMsgContent = messages[messages.length - 1]?.content
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [history.length, messages.length, lastMsgContent])

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
      evidence_level: m.evidence_level ?? null,
      evidence_top_score: m.evidence_top_score ?? null,
      answer_complete: m.answer_complete ?? null,
      optimized: m.optimized ?? false,
    })),
    ...messages,
  ]

  // 欢迎卡示例问题：无会话先新建，再发送
  const onPick = async (q: string) => {
    if (!currentId) await newConversation()
    send(q, null, 'standard')
  }

  return (
    <Layout style={{ height: '100vh', position: 'relative' }}>
      {/* 动态科技背景：粒子 + 极光，铺在内容底层 */}
      <ChatBackground />
      <Sider
        width={sidebarWidth}
        theme="dark"
        style={{
          borderRight: '1px solid rgba(122, 190, 255, 0.12)',
          position: 'relative',
          zIndex: 1,
        }}
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
      <Content style={{ display: 'flex', flexDirection: 'column', minWidth: 0, position: 'relative', zIndex: 1 }}>
        <div
          className="tech-line"
          style={{
            height: 56,
            borderBottom: '1px solid rgba(122, 190, 255, 0.1)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '0 clamp(12px, 2vw, 24px)',
            background: 'rgba(10, 17, 34, 0.4)',
          }}
        >
          <Space size={10}>
            <span className="brand-logo">
              <MessageOutlined />
            </span>
            <Typography.Text strong style={{ fontSize: 'var(--font-md)', letterSpacing: '0.5px' }}>
              智慧水利知识库
            </Typography.Text>
            <Tag
              className="font-mono"
              style={{
                fontSize: 'var(--font-xs)',
                color: '#00c6ff',
                borderColor: 'rgba(0, 198, 255, 0.4)',
                background: 'rgba(0, 198, 255, 0.08)',
                marginInlineEnd: 0,
              }}
            >
              RAG · AI
            </Tag>
          </Space>
          <UserMenu />
        </div>

        <div
          ref={scrollRef}
          style={{ flex: 1, overflowY: 'auto', padding: 'clamp(16px, 2.2vw, 32px) clamp(12px, 2vw, 28px)' }}
        >
          {display.length === 0 ? (
            <div className="hero-card">
              <div className="hero-logo">
                <MessageOutlined />
              </div>
              <Typography.Title level={3} style={{ marginTop: 0, marginBottom: 8 }}>
                欢迎使用智慧水利知识库
              </Typography.Title>
              <Typography.Text type="secondary" style={{ fontSize: 'var(--font-sm)' }}>
                基于 RAG 的 AI 原生问答系统 · 越用越聪明。点击下方示例，或直接输入你的问题。
              </Typography.Text>
              <div className="hero-chips">
                {HERO_QUESTIONS.map((q) => (
                  <span key={q} className="hero-chip" onClick={() => onPick(q)}>
                    {q}
                  </span>
                ))}
              </div>
            </div>
          ) : (
            <div style={{ maxWidth: 'min(70vw, 1080px)', margin: '0 auto' }}>
              {historyHasMore && !streaming && (
                <div style={{ textAlign: 'center', marginBottom: 12 }}>
                  <Button size="small" type="link" onClick={() => currentId && loadHistory(currentId)}>
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
