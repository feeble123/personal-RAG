import { useState } from 'react'
import { Button, Input, Select, Space, Typography } from 'antd'
import { SendOutlined, StopOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { kbPublicApi } from '@/api/modules'
import { ANSWER_STYLE_OPTIONS, type KnowledgeBase } from '@/api/types'
import { useChatStore } from '@/stores/chat'

// 底部输入：知识库选择 + 回答风格 + 发送/停止
export default function MessageInput() {
  const [text, setText] = useState('')
  const [kbId, setKbId] = useState<number | null>(null)
  // 回答风格（单元 F）：默认跟随所选知识库的 answer_style，也可手动切换
  const [style, setStyle] = useState<string>('standard')
  const { send, stop, streaming, currentId } = useChatStore()

  const { data: kbs = [] } = useQuery<KnowledgeBase[]>({
    queryKey: ['public-kbs'],
    queryFn: kbPublicApi.list,
    staleTime: 60_000,
  })

  const onKbChange = (id: number | null) => {
    setKbId(id)
    if (id) {
      const kb = kbs.find((k) => k.id === id)
      if (kb?.answer_style) setStyle(kb.answer_style)
    }
  }

  const doSend = () => {
    if (!text.trim()) return
    send(text.trim(), kbId, style)
    setText('')
  }

  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      doSend()
    }
  }

  return (
    <div style={{ borderTop: '1px solid rgba(122, 190, 255, 0.12)', padding: 12 }}>
      <Space wrap style={{ marginBottom: 8, width: '100%' }}>
        <Select
          value={kbId}
          onChange={onKbChange}
          style={{ minWidth: 180, maxWidth: 260 }}
          allowClear
          placeholder="全部知识库"
          options={kbs.map((k) => ({ value: k.id, label: `${k.name} (${k.doc_count} 文档)` }))}
        />
        <Space size={4}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            风格
          </Typography.Text>
          <Select
            value={style}
            onChange={setStyle}
            style={{ width: 160 }}
            options={ANSWER_STYLE_OPTIONS}
            placeholder="回答风格"
          />
        </Space>
      </Space>
      <Space.Compact style={{ width: '100%' }} size="large">
        <Input.TextArea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKey}
          autoSize={{ minRows: 1, maxRows: 6 }}
          placeholder="输入你的水利工程问题，Enter 发送，Shift+Enter 换行"
          style={{ flex: 1 }}
        />
        {streaming ? (
          <Button danger icon={<StopOutlined />} onClick={stop} size="large">
            停止
          </Button>
        ) : (
          <Button type="primary" icon={<SendOutlined />} onClick={doSend} size="large" disabled={!text.trim()}>
            发送
          </Button>
        )}
      </Space.Compact>
      {!currentId && <Typography.Text type="secondary" style={{ fontSize: 12, marginTop: 6, display: 'block' }}>请先新建或选择一个会话</Typography.Text>}
    </div>
  )
}
