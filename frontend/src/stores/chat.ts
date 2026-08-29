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
// 流式会话身份证：每次 send/optimize 递增，旧流的回调据此识别「自己已过期」。
// 切会话/新建/删除时会 abort 旧流，但 abort 生效是异步的——在生效前旧流可能仍触发
// 回调，靠这个号把过期回调挡掉，杜绝 A 会话答案污染 B 会话。
let streamSeq = 0

// 作废进行中的流：abort + 领号作废。切会话/新建/删除/登出时调用——
// abort 让 fetch 尽快停，streamSeq++ 让旧流尚未送达的 catch/finally 回调识别「已过期」
// 而直接丢弃（否则旧流收尾可能污染新会话的 messages / streaming 状态）。
const invalidateStream = (get: () => ChatState) => {
  get().abortCtrl?.abort()
  streamSeq++
}

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
  setCurrent: (id) => {
    invalidateStream(get) // 切会话先停掉旧流，避免 A 会话答案污染 B 会话
    set({ currentId: id, history: [], historyLoaded: false, historyHasMore: false, messages: [], streaming: false, abortCtrl: null })
  },

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
    invalidateStream(get) // 新建会话前停掉旧流
    const conv = await convApi.create()
    await get().refreshConversations()
    set({ currentId: conv.id, history: [], historyLoaded: true, historyHasMore: false, messages: [], streaming: false, abortCtrl: null })
    return conv.id
  },

  renameConversation: async (id, title) => {
    await convApi.rename(id, title)
    await get().refreshConversations()
  },

  deleteConversation: async (id) => {
    const { currentId } = get()
    if (currentId === id) invalidateStream(get) // 删除当前会话先停旧流
    await convApi.remove(id)
    await get().refreshConversations()
    if (currentId === id) set({ currentId: null, history: [], messages: [], streaming: false, abortCtrl: null })
  },

  send: async (content, kbId, style) => {
    const state = get()
    const convId = state.currentId
    if (!convId || state.streaming || !content.trim()) return

    const abortCtrl = new AbortController()
    const seq = ++streamSeq // 领号：作废任何还在跑的旧流回调
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
          if (seq !== streamSeq) return // 过期流回调，丢弃（切会话后新流已领号）
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
      if (seq !== streamSeq) return // 过期流：收尾也丢弃，不污染新会话
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
      // 过期流：不覆盖新流的 streaming/abortCtrl 状态（finally 里不用 return，避免吞异常）
      if (seq === streamSeq) {
        set({ streaming: false, abortCtrl: null })
        // 刷新会话侧栏（标题自动生成 + last_message_at 排序）
        await get().refreshConversations()
      }
    }
  },

  optimizeMessage: async (messageId) => {
    const state = get()
    const convId = state.currentId
    if (!convId || state.streaming || !messageId) return

    const abortCtrl = new AbortController()
    const seq = ++streamSeq // 领号：作废旧流回调
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
          if (seq !== streamSeq) return // 过期流回调，丢弃
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
      if (seq !== streamSeq) return // 过期流：收尾也丢弃
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
      // 过期流：不覆盖新流状态（finally 里不用 return，避免吞异常）
      if (seq === streamSeq) {
        set({ streaming: false, abortCtrl: null })
        await get().refreshConversations()
      }
    }
  },

  stop: () => {
    const { abortCtrl } = get()
    if (!abortCtrl) return
    abortCtrl.abort()
    // 同步复位：UI 立即切回「发送」按钮。旧流的 finally 会验号（seq 不变则仍等于
    // streamSeq），其 set(streaming:false) 为幂等；若用户已发新流，seq 已过期被丢弃。
    set({ streaming: false, abortCtrl: null })
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

  reset: () => {
    invalidateStream(get) // 登出/重置先停旧流
    set({ conversations: [], total: 0, currentId: null, history: [], messages: [], streaming: false, abortCtrl: null })
  },
}))

// 登录态变化时重置
useAuthStore.subscribe((s, prev) => {
  if (!s.token && prev.token) {
    useChatStore.getState().reset()
  }
})
