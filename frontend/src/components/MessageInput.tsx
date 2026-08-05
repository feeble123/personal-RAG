import { useState } from 'react'
import { Button, Input, Select, Space, Typography } from 'antd'
import { SendOutlined, StopOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { kbPublicApi } from '@/api/modules'
import type { KnowledgeBase } from '@/api/types'
import { useChatStore } from '@/stores/chat'

// 底部输入：知识库选择 + 发送/停止
export default function MessageInput() {
  const [text, setText] = useState('')
  const [kbId, setKbId] = useState<number | null>(null)
  const { send, stop, streaming, currentId } = useChatStore()

  const { data: kbs = [] } = useQuery<KnowledgeBase[]>({
    queryKey: ['public-kbs'],
    queryFn: kbPublicApi.list,
    staleTime: 60_000,
  })

  const doSend = () => {
    if (!text.trim()) return
    send(text.trim(), kbId)
    setText('')
  }

  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      doSend()
    }
  }

  return (
    <div style={{ borderTop: '1px solid #f0f0f0', padding: 12 }}>
      <Space.Compact style={{ width: '100%' }} size="large">
        <Select
          value={kbId}
          onChange={setKbId}
          style={{ minWidth: 180, maxWidth: 260 }}
          allowClear
          placeholder="全部知识库"
          options={kbs.map((k) => ({ value: k.id, label: `${k.name} (${k.doc_count} 文档)` }))}
        />
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
