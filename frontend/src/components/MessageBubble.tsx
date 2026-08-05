import { memo } from 'react'
import { Avatar, Space, Typography } from 'antd'
import { RobotOutlined, UserOutlined } from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
// KaTeX 公式样式（数学公式排版字体与布局）
import 'katex/dist/katex.min.css'
import CitationCard from './CitationCard'
import type { Citation } from '@/api/types'
import type { StreamMessage } from '@/stores/chat'

interface Props {
  msg: StreamMessage
  showCitations?: boolean
}

// 消息气泡：Markdown 渲染 + 引用卡片 + 流式光标
function MessageBubbleInner({ msg, showCitations = true }: Props) {
  const isUser = msg.role === 'user'
  const streaming = !msg.is_complete && msg.role === 'assistant'

  return (
    <div style={{ display: 'flex', gap: 10, marginBottom: 20, flexDirection: isUser ? 'row-reverse' : 'row' }}>
      <Avatar
        style={{ backgroundColor: isUser ? '#1677ff' : '#52c41a', flexShrink: 0 }}
        icon={isUser ? <UserOutlined /> : <RobotOutlined />}
      />
      <div style={{ maxWidth: '82%' }}>
        <div
          className="msg-bubble"
          style={{
            background: isUser ? '#1677ff' : '#ffffff',
            color: isUser ? '#fff' : 'inherit',
            border: isUser ? 'none' : '1px solid #f0f0f0',
          }}
        >
          {isUser ? (
            <Typography.Text style={{ color: '#fff', whiteSpace: 'pre-wrap' }}>{msg.content}</Typography.Text>
          ) : msg.content ? (
            <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}>
              {msg.content}
            </ReactMarkdown>
          ) : streaming ? (
            <Typography.Text type="secondary">正在思考…</Typography.Text>
          ) : (
            <Typography.Text type="secondary">（空回复）</Typography.Text>
          )}
          {streaming && (
            <span className="stream-cursor" aria-hidden>
              ▍
            </span>
          )}
        </div>

        {msg.error && (
          <Typography.Text type="danger" style={{ fontSize: 12 }}>
            {msg.error}
          </Typography.Text>
        )}

        {!isUser && showCitations && msg.citations.length > 0 && (
          <Space wrap size={[6, 6]} style={{ marginTop: 6 }}>
            {msg.citations.map((c, i) => (
              <CitationCard key={c.chunk_id} citation={c} index={i + 1} />
            ))}
          </Space>
        )}
      </div>
    </div>
  )
}

export const MessageBubble = memo(MessageBubbleInner)
