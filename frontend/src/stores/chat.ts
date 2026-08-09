import { create } from 'zustand'
import { convApi, feedbackApi, streamChat, streamOptimize, type MessageOut } from '@/api/modules'
import type { Citation, Conversation } from '@/api/types'
import { useAuthStore } from './auth'

// 本地流式消息：assistant 流式生成期间的临时状态
export interface StreamMessage {
  id: string // 本地临时 id
  role: 'user' | 'assistant'
  content: string
  is_complete: boolean
  citations: Citation[]
  error?: string | null
  // 问答记忆库：服务端落库后回填的真实 message_id / 反馈态 / 记忆来源标记
  messageId?: number
  feedback?: 'up' | 'down' | null
  from_memory?: boolean
  // 证据等级（U3）：检索质量判级（充足/部分/较弱/不足）
  evidence_level?: 'sufficient' | 'partial' | 'weak' | 'none' | null
  evidence_top_score?: number | null
  // 层2 完备性：True=校验完整 / False=不完整（生成被截断或优化后仍不全，前端提示） / null=未校验
  answer_complete?: boolean | null
  // LLM 优化（opt-in）：true = 用户点「🤖 LLM优化」产生的结果
  optimized?: boolean
}

interface ChatState {
  // 会话侧栏
  conversations: Conversation[]
  total: number
  loadingConversations: boolean
  refreshConversations: () => Promise<void>

  // 当前会话
  currentId: number | null
  setCurrent: (id: number | null) => void

  // 历史消息（当前会话，时间正序）
  history: MessageOut[]
  historyLoaded: boolean
  historyHasMore: boolean
  loadHistory: (convId: number, reset?: boolean) => Promise<void>

  // 流式消息缓冲
  messages: StreamMessage[]
  streaming: boolean
  abortCtrl: AbortController | null

  // 操作
  newConversation: () => Promise<number | null>
  renameConversation: (id: number, title: string) => Promise<void>
  deleteConversation: (id: number) => Promise<void>
  send: (content: string, kbId?: number | null, style?: string) => Promise<void>
  optimizeMessage: (messageId: number) => Promise<void>
  stop: () => void
  giveFeedback: (messageId: number, feedback: 'up' | 'down' | null) => Promise<void>
  reset: () => void
}

let msgSeq = 0

export const useChatStore = create<ChatState>()((set, get) => ({
  conversations: [],
  total: 0,
  loadingConversations: false,

  refreshConversations: async () => {
    set({ loadingConversations: true })
    try {
      const data = await convApi.list({ page: 1, page_size: 100 })
      set({ conversations: data.items, total: data.total })
    } finally {
      set({ loadingConversations: false })
    }
  },

  currentId: null,
  setCurrent: (id) => set({ currentId: id, history: [], historyLoaded: false, historyHasMore: false, messages: [] }),

  history: [],
  historyLoaded: false,
  historyHasMore: false,
  loadHistory: async (convId, reset = false) => {
    const { history, historyHasMore } = get()
    if (!reset && (history.length > 0 || !historyHasMore)) return
    const cursor = reset ? undefined : history[0]?.id
    const data = await convApi.messages(convId, { cursor, limit: 30 })
    set((s) => ({
      history: reset ? data.items : [...data.items, ...s.history],
      historyHasMore: data.has_more,
      historyLoaded: true,
    }))
  },

  messages: [],
  streaming: false,
  abortCtrl: null,

  newConversation: async () => {
    const conv = await convApi.create()
    await get().refreshConversations()
    set({ currentId: conv.id, history: [], historyLoaded: true, historyHasMore: false, messages: [] })
    return conv.id
  },

  renameConversation: async (id, title) => {
    await convApi.rename(id, title)
    await get().refreshConversations()
  },

  deleteConversation: async (id) => {
    await convApi.remove(id)
    const { currentId } = get()
    await get().refreshConversations()
    if (currentId === id) set({ currentId: null, history: [], messages: [] })
  },

  send: async (content, kbId, style) => {
    const state = get()
    const convId = state.currentId
    if (!convId || state.streaming || !content.trim()) return

    const abortCtrl = new AbortController()
    set((s) => ({
      streaming: true,
      abortCtrl,
      messages: [
        ...s.messages,
        { id: `u${++msgSeq}`, role: 'user', content, is_complete: true, citations: [] },
        { id: `a${++msgSeq}`, role: 'assistant', content: '', is_complete: false, citations: [], error: null },
      ],
    }))

    try {
      let citations: Citation[] = []
      await streamChat({
        conversationId: convId,
        content,
        kbId,
        style,
        signal: abortCtrl.signal,
        onEvent: (ev) => {
          if (ev.event === 'citations') {
            citations = ev.data as Citation[]
            set((s) => {
              const msgs = [...s.messages]
              const last = msgs[msgs.length - 1]
              if (last && last.role === 'assistant') {
                // 不可变更新：新对象引用，触发 MessageBubble(React.memo 浅比较) 重渲染
                msgs[msgs.length - 1] = { ...last, citations }
              }
              return { messages: msgs }
            })
          } else if (ev.event === 'delta') {
            const text = ev.data as string
            set((s) => {
              const msgs = [...s.messages]
              const last = msgs[msgs.length - 1]
              if (last && last.role === 'assistant') {
                msgs[msgs.length - 1] = { ...last, content: last.content + text }
              }
              return { messages: msgs }
            })
          } else if (ev.event === 'reset') {
            // 层2 完备性校验触发补全重生成：清空本回答内容与引用，等待重新流式输出
            set((s) => {
              const msgs = [...s.messages]
              const last = msgs[msgs.length - 1]
              if (last && last.role === 'assistant') {
                msgs[msgs.length - 1] = { ...last, content: '', citations: [] }
              }
              return { messages: msgs }
            })
          } else if (ev.event === 'done') {
            // 服务端已落库：回填真实 message_id（供👍/👎）与 from_memory（记忆复用标记）+ 证据判级（U3）
            const done = ev.data as
              | {
                  message_id?: number
                  from_memory?: boolean
                  evidence_level?: 'sufficient' | 'partial' | 'weak' | 'none'
                  evidence_top_score?: number
                  answer_complete?: boolean
                }
              | null
            set((s) => {
              const msgs = [...s.messages]
              const last = msgs[msgs.length - 1]
              if (last && last.role === 'assistant') {
                msgs[msgs.length - 1] = {
                  ...last,
                  is_complete: true,
                  messageId: done?.message_id,
                  from_memory: done?.from_memory ?? false,
                  evidence_level: done?.evidence_level ?? null,
                  evidence_top_score: done?.evidence_top_score ?? null,
                  answer_complete: done?.answer_complete ?? null,
                }
              }
              return { messages: msgs }
            })
          } else if (ev.event === 'error') {
            set((s) => {
              const msgs = [...s.messages]
              const last = msgs[msgs.length - 1]
              if (last && last.role === 'assistant') {
                msgs[msgs.length - 1] = {
                  ...last,
                  error: (ev.data as string) || '生成失败',
                  is_complete: true,
                }
              }
              return { messages: msgs }
            })
          }
        },
      })
    } catch (e) {
      const aborted = abortCtrl.signal.aborted
      set((s) => {
        const msgs = [...s.messages]
        const last = msgs[msgs.length - 1]
        if (last && last.role === 'assistant') {
          msgs[msgs.length - 1] = {
            ...last,
            error: aborted ? '已停止生成' : String((e as Error).message || e),
            is_complete: true,
          }
        }
        return { messages: msgs }
      })
    } finally {
      set({ streaming: false, abortCtrl: null })
      // 刷新会话侧栏（标题自动生成 + last_message_at 排序）
      await get().refreshConversations()
    }
  },

  optimizeMessage: async (messageId) => {
    const state = get()
    const convId = state.currentId
    if (!convId || state.streaming || !messageId) return

    const abortCtrl = new AbortController()
    set((s) => ({
      streaming: true,
      abortCtrl,
      // 追加一条「LLM优化」气泡（原回答保留可对比），流式填充
      messages: [
        ...s.messages,
        {
          id: `a${++msgSeq}`,
          role: 'assistant',
          content: '',
          is_complete: false,
          citations: [],
          error: null,
          optimized: true,
        },
      ],
    }))

    try {
      await streamOptimize({
        conversationId: convId,
        messageId,
        signal: abortCtrl.signal,
        onEvent: (ev) => {
          if (ev.event === 'citations') {
            const citations = ev.data as Citation[]
            set((s) => {
              const msgs = [...s.messages]
              const last = msgs[msgs.length - 1]
              if (last && last.role === 'assistant') {
                msgs[msgs.length - 1] = { ...last, citations }
              }
              return { messages: msgs }
            })
          } else if (ev.event === 'delta') {
            const text = ev.data as string
            set((s) => {
              const msgs = [...s.messages]
              const last = msgs[msgs.length - 1]
              if (last && last.role === 'assistant') {
                msgs[msgs.length - 1] = { ...last, content: last.content + text }
              }
              return { messages: msgs }
            })
          } else if (ev.event === 'reset') {
            // 优化重试：清空本次优化气泡，重新流式
            set((s) => {
              const msgs = [...s.messages]
              const last = msgs[msgs.length - 1]
              if (last && last.role === 'assistant') {
                msgs[msgs.length - 1] = { ...last, content: '', citations: [] }
              }
              return { messages: msgs }
            })
          } else if (ev.event === 'done') {
            const done = ev.data as
              | {
                  message_id?: number
                  evidence_level?: 'sufficient' | 'partial' | 'weak' | 'none'
                  evidence_top_score?: number
                  answer_complete?: boolean
                }
              | null
            set((s) => {
              const msgs = [...s.messages]
              const last = msgs[msgs.length - 1]
              if (last && last.role === 'assistant') {
                msgs[msgs.length - 1] = {
                  ...last,
                  is_complete: true,
                  messageId: done?.message_id,
                  evidence_level: done?.evidence_level ?? null,
                  evidence_top_score: done?.evidence_top_score ?? null,
                  answer_complete: done?.answer_complete ?? null,
                  optimized: true,
                }
              }
              return { messages: msgs }
            })
          } else if (ev.event === 'error') {
            set((s) => {
              const msgs = [...s.messages]
              const last = msgs[msgs.length - 1]
              if (last && last.role === 'assistant') {
                msgs[msgs.length - 1] = {
                  ...last,
                  error: (ev.data as string) || '优化失败',
                  is_complete: true,
                }
              }
              return { messages: msgs }
            })
          }
        },
      })
    } catch (e) {
      const aborted = abortCtrl.signal.aborted
      set((s) => {
        const msgs = [...s.messages]
        const last = msgs[msgs.length - 1]
        if (last && last.role === 'assistant') {
          msgs[msgs.length - 1] = {
            ...last,
            error: aborted ? '已停止优化' : String((e as Error).message || e),
            is_complete: true,
          }
        }
        return { messages: msgs }
      })
    } finally {
      set({ streaming: false, abortCtrl: null })
      await get().refreshConversations()
    }
  },

  stop: () => {
    get().abortCtrl?.abort()
  },

  giveFeedback: async (messageId, feedback) => {
    const { currentId } = get()
    if (!currentId || !messageId) return
    try {
      // 以服务端确认后的反馈态为准（同值再点 → 后端置 null 取消）
      const { feedback: confirmed } = await feedbackApi.send(currentId, messageId, feedback)
      set((s) => ({
        messages: s.messages.map((m) => (m.messageId === messageId ? { ...m, feedback: confirmed } : m)),
        history: s.history.map((m) => (m.id === messageId ? { ...m, feedback: confirmed } : m)),
      }))
    } catch {
      /* 反馈失败由 client.ts 统一弹错 */
    }
  },

  reset: () =>
    set({ conversations: [], total: 0, currentId: null, history: [], messages: [], streaming: false, abortCtrl: null }),
}))

// 登录态变化时重置
useAuthStore.subscribe((s, prev) => {
  if (!s.token && prev.token) {
    useChatStore.getState().reset()
  }
})
