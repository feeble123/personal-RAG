import { memo } from 'react'
import { App, Avatar, Button, Space, Tag, Tooltip, Typography } from 'antd'
import { CopyOutlined, DislikeOutlined, LikeOutlined, RobotOutlined, StarOutlined, UserOutlined } from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'

// 证据等级（U3）：标签文案 + 颜色映射
const EVIDENCE_META: Record<string, { label: string; color: string }> = {
  sufficient: { label: '证据充足', color: 'green' },
  partial: { label: '证据部分', color: 'blue' },
  weak: { label: '证据较弱', color: 'orange' },
  none: { label: '证据不足', color: 'red' },
}
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
  const { message: appMsg } = App.useApp()

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(msg.content)
      appMsg.success('已复制')
    } catch {
      appMsg.error('复制失败')
    }
  }

  return (
    <div
      className="msg-in"
      style={{ display: 'flex', gap: 10, marginBottom: 20, flexDirection: isUser ? 'row-reverse' : 'row' }}
    >
      <Avatar
        style={{
          background: isUser
            ? 'linear-gradient(135deg, #00c6ff, #0a7bff)'
            : 'linear-gradient(135deg, #1fd6c0, #0a9cff)',
          flexShrink: 0,
        }}
        icon={isUser ? <UserOutlined /> : <RobotOutlined />}
      />
      <div style={{ maxWidth: 'min(82%, 760px)' }}>
        <div
          className="msg-bubble"
          style={{
            background: isUser
              ? 'linear-gradient(135deg, #00c6ff, #0a7bff)'
              : 'rgba(28, 43, 74, 0.55)',
            color: isUser ? '#fff' : 'rgba(228, 241, 255, 0.92)',
            border: isUser ? 'none' : '1px solid rgba(122, 190, 255, 0.16)',
            boxShadow: isUser
              ? '0 4px 18px rgba(0, 198, 255, 0.28)'
              : 'inset 0 1px 0 rgba(255, 255, 255, 0.07), 0 2px 12px rgba(0, 0, 0, 0.25)',
            backdropFilter: 'blur(8px)',
          }}
        >
          {isUser ? (
            <Typography.Text style={{ color: '#fff', whiteSpace: 'pre-wrap' }}>{msg.content}</Typography.Text>
          ) : msg.content ? (
            <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}>
              {msg.content}
            </ReactMarkdown>
          ) : streaming ? (
            <span className="think-dots" aria-label="正在思考">
              <i />
              <i />
              <i />
            </span>
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

        {/* 用户提问：复制按钮 */}
        {isUser && msg.content && (
          <div style={{ marginTop: 4, display: 'flex', justifyContent: 'flex-end' }}>
            <Tooltip title="复制提问">
              <Button size="small" type="text" icon={<CopyOutlined />} onClick={onCopy} />
            </Tooltip>
          </div>
        )}

        {!isUser && showCitations && msg.citations.length > 0 && (
          <Space wrap size={[6, 6]} style={{ marginTop: 6 }}>
            {msg.citations.map((c, i) => (
              <CitationCard key={c.chunk_id} citation={c} index={i + 1} />
            ))}
          </Space>
        )}

        {/* 问答记忆：来源标签 + 复制 + 👍/👎 反馈（有真实 message_id 才可反馈） */}
        {!isUser && msg.is_complete && (
          <div style={{ marginTop: 6, display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
            {msg.content && (
              <Tooltip title="复制回答">
                <Button size="small" type="text" icon={<CopyOutlined />} onClick={onCopy} />
              </Tooltip>
            )}
            {msg.from_memory && (
              <Tag color="gold" icon={<StarOutlined />} style={{ marginInlineEnd: 0 }}>
                来自问答记忆
              </Tag>
            )}
            {msg.evidence_level && EVIDENCE_META[msg.evidence_level] && (
              <Tag color={EVIDENCE_META[msg.evidence_level].color} style={{ marginInlineEnd: 0 }}>
                {EVIDENCE_META[msg.evidence_level].label}
                {msg.evidence_top_score != null && (
                  <span className="font-mono" style={{ marginLeft: 4 }}>
                    {msg.evidence_top_score.toFixed(2)}
                  </span>
                )}
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
