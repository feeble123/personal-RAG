import { create } from 'zustand'
import { convApi, streamChat, type MessageOut } from '@/api/modules'
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
  stop: () => void
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
          } else if (ev.event === 'done') {
            set((s) => {
              const msgs = [...s.messages]
              const last = msgs[msgs.length - 1]
              if (last && last.role === 'assistant') {
                msgs[msgs.length - 1] = { ...last, is_complete: true }
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

  stop: () => {
    get().abortCtrl?.abort()
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
