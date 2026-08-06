import { memo } from 'react'
import { Avatar, Button, Space, Tag, Tooltip, Typography } from 'antd'
import { DislikeOutlined, LikeOutlined, RobotOutlined, StarOutlined, UserOutlined } from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
// KaTeX 公式样式（数学公式排版字体与布局）
import 'katex/dist/katex.min.css'
import CitationCard from './CitationCard'
import { useChatStore } from '@/stores/chat'
import type { Citation } from '@/api/types'
import type { StreamMessage } from '@/stores/chat'

interface Props {
  msg: StreamMessage
  showCitations?: boolean
}

// 消息气泡：Markdown 渲染 + 引用卡片 + 流式光标 + 问答记忆反馈
function MessageBubbleInner({ msg, showCitations = true }: Props) {
  const isUser = msg.role === 'user'
  const streaming = !msg.is_complete && msg.role === 'assistant'
  // 稳定引用（store 创建一次），不破坏 memo 的浅比较
  const giveFeedback = useChatStore((s) => s.giveFeedback)

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

        {/* 问答记忆：来源标签 + 👍/👎 反馈（有真实 message_id 才可反馈） */}
        {!isUser && msg.is_complete && (
          <div style={{ marginTop: 6, display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
            {msg.from_memory && (
              <Tag color="gold" icon={<StarOutlined />} style={{ marginInlineEnd: 0 }}>
                来自问答记忆
              </Tag>
            )}
            {msg.messageId && (
              <>
                <Tooltip title={msg.feedback === 'up' ? '取消点赞' : '点赞：沉淀为正向记忆，同题下次秒回'}>
                  <Button
                    size="small"
                    type={msg.feedback === 'up' ? 'primary' : 'text'}
                    icon={<LikeOutlined />}
                    aria-label="点赞"
                    onClick={() => giveFeedback(msg.messageId as number, msg.feedback === 'up' ? null : 'up')}
                  />
                </Tooltip>
                <Tooltip title={msg.feedback === 'down' ? '取消点踩' : '点踩：沉淀为负面记忆，同题下次强制重检'}>
                  <Button
                    size="small"
                    type={msg.feedback === 'down' ? 'primary' : 'text'}
                    danger={msg.feedback === 'down'}
                    icon={<DislikeOutlined />}
                    aria-label="点踩"
                    onClick={() => giveFeedback(msg.messageId as number, msg.feedback === 'down' ? null : 'down')}
                  />
                </Tooltip>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

export const MessageBubble = memo(MessageBubbleInner)
