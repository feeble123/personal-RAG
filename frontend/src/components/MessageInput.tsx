import { useMemo, useState } from 'react'
import { Segmented, Select, Space, Tooltip, Typography } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { kbPublicApi } from '@/api/modules'
import { ANSWER_STYLE_OPTIONS, type KnowledgeBase } from '@/api/types'
import { domainLabel } from '@/constants/taxonomy'
import { useChatStore, type AnswerMode } from '@/stores/chat'
import PromptInput from './PromptInput'

// 底部输入（单元2 装配层）：回答模式 + 知识库选择 + 回答风格 + 裁剪版 PromptInput。
// 业务逻辑（知识库/风格/store 对接）在这里，纯输入交互在 PromptInput。
export default function MessageInput() {
  const [kbId, setKbId] = useState<number | null>(null)
  // 回答风格（单元 F）：默认跟随所选知识库的 answer_style，也可手动切换
  const [style, setStyle] = useState<string>('standard')
  // 回答模式（O7）：知识库切片检索 / 本体优先（本体拿不到事实才回退检索）
  const [mode, setMode] = useState<AnswerMode>('kb')
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

  // 单元 2：知识库下拉按「业务域」分组，资料多时更好找
  const kbOptions = useMemo(() => {
    const groups = new Map<string, { value: number; label: string }[]>()
    for (const k of kbs) {
      const key = domainLabel(k.business_domain)
      if (!groups.has(key)) groups.set(key, [])
      groups.get(key)!.push({ value: k.id, label: `${k.name} (${k.doc_count} 文档)` })
    }
    return [...groups.entries()].map(([label, options]) => ({ label, options }))
  }, [kbs])

  const doSend = (text: string) => {
    send(text, kbId, style, mode)
  }

  return (
    <div className="glass-bar" style={{ borderTop: '1px solid rgba(122, 190, 255, 0.12)', padding: 12 }}>
      <Space wrap style={{ marginBottom: 8, width: '100%' }}>
        <Tooltip
          title={
            mode === 'ontology'
              ? '先查本体知识图谱（参数取值范围、实体关系）；本体一条事实都拿不到才回退知识库切片检索'
              : mode === 'hybrid'
                ? '本体事实 + 同时检索原文片段做补充：回答更全，但每次都要多花几秒检索'
                : '只查知识库切片（原文片段），不做本体推理'
          }
        >
          <Segmented
            size="small"
            value={mode}
            onChange={(v) => setMode(v as AnswerMode)}
            options={[
              { label: '知识库回答', value: 'kb' },
              { label: '本体回答', value: 'ontology' },
              { label: '本体+原文', value: 'hybrid' },
            ]}
          />
        </Tooltip>
        <Select
          value={kbId}
          onChange={onKbChange}
          style={{ minWidth: 'clamp(150px, 22vw, 260px)' }}
          allowClear
          placeholder="全部知识库"
          options={kbOptions}
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
