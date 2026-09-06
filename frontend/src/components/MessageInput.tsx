import { useState } from 'react'
import { Select, Space, Typography } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { kbPublicApi } from '@/api/modules'
import { ANSWER_STYLE_OPTIONS, type KnowledgeBase } from '@/api/types'
import { useChatStore } from '@/stores/chat'
import PromptInput from './PromptInput'

// 底部输入（单元2 装配层）：知识库选择 + 回答风格 + 裁剪版 PromptInput（透明青蓝）。
// 业务逻辑（知识库/风格/store 对接）在这里，纯输入交互在 PromptInput。
export default function MessageInput() {
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

  const doSend = (text: string) => {
    send(text, kbId, style)
  }

  return (
    <div className="glass-bar" style={{ borderTop: '1px solid rgba(122, 190, 255, 0.12)', padding: 12 }}>
      <Space wrap style={{ marginBottom: 8, width: '100%' }}>
        <Select
          value={kbId}
          onChange={onKbChange}
          style={{ minWidth: 'clamp(150px, 22vw, 260px)' }}
          allowClear
          placeholder="全部知识库"
          options={kbs.map((k) => ({ value: k.id, label: `${k.name} (${k.doc_count} 文档)` }))}
        />
        <Space size={4}>
          <Typography.Text type="secondary" style={{ fontSize: 'var(--font-xs)' }}>
            风格
          </Typography.Text>
          <Select
            value={style}
            onChange={setStyle}
            style={{ width: 'clamp(130px, 18vw, 180px)' }}
            options={ANSWER_STYLE_OPTIONS}
            placeholder="回答风格"
          />
        </Space>
      </Space>
      <PromptInput onSend={doSend} onStop={stop} streaming={streaming} />
      {!currentId && (
        <Typography.Text type="secondary" style={{ fontSize: 'var(--font-xs)', marginTop: 6, display: 'block' }}>
          请先新建或选择一个会话
        </Typography.Text>
      )}
    </div>
  )
}
