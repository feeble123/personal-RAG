// 单元 E/G：chat store 的 SSE 流式竞态回归测试。
//
// 背景：RAG 问答是「流式」输出——答案一段段蹦出来。历史 bug 是切会话/新建/删除会话时
// 旧流没停，旧流的回调继续往新会话的消息里追加，导致 A 会话答案污染 B 会话。
// 修复方案：streamSeq「身份证」——每次发送领号，旧流回调验号不过即丢弃；切会话先 abort。
//
// 本测试锁定三件事：
//   1. 正常流式：delta 逐段追加到最后一条 assistant 消息
//   2. 切会话后旧流回调被丢弃（不污染新会话）
//   3. stop 同步复位 streaming（按钮即时切回「发送」）
import { beforeEach, describe, expect, it, vi } from 'vitest'

// 打桩 API 层：不碰真实后端。用 vi.mock 把 streamChat/convApi 换成可控替身。
vi.mock('@/api/modules', () => ({
  convApi: {
    list: vi.fn(async () => ({ items: [], total: 0 })),
    create: vi.fn(async () => ({ id: 99, title: '新会话', last_message_at: '', created_at: '' })),
    messages: vi.fn(async () => ({ items: [], has_more: false })),
    rename: vi.fn(async () => ({})),
    remove: vi.fn(async () => {}),
  },
  feedbackApi: { send: vi.fn(async () => ({ feedback: null })) },
  streamChat: vi.fn(),
  streamOptimize: vi.fn(),
}))

import { useChatStore } from './chat'
import { streamChat } from '@/api/modules'

// 每次测试前重置 store 状态，避免用例间串扰
beforeEach(() => {
  useChatStore.setState({
    conversations: [],
    total: 0,
    currentId: null,
    history: [],
    historyLoaded: false,
    historyHasMore: false,
    messages: [],
    streaming: false,
    abortCtrl: null,
  })
  vi.clearAllMocks()
})

describe('chat store 流式（SSE）', () => {
  it('正常流式：delta 逐段追加到最后一条 assistant 消息', async () => {
    useChatStore.setState({ currentId: 1 })

    // streamChat 不立即 resolve，而是捕获 onEvent 供手动驱动
    let capturedOnEvent: ((ev: { event: string; data: unknown }) => void) | undefined
    ;(streamChat as ReturnType<typeof vi.fn>).mockImplementation(
      async ({ onEvent }: { onEvent: (ev: { event: string; data: unknown }) => void }) => {
        capturedOnEvent = onEvent
        // 模拟流式生命周期：先发引用，再两段 delta，最后 done
        onEvent({ event: 'citations', data: [{ source: '规范', snippet: 'x' }] })
        onEvent({ event: 'delta', data: '明渠均匀流' })
        onEvent({ event: 'delta', data: '的形成条件' })
        onEvent({ event: 'done', data: { message_id: 7 } })
      },
    )

    const p = useChatStore.getState().send('明渠均匀流的形成条件是什么？', null, 'standard')
    // send 是 async，等它跑完（mock 立即 resolve）
    await p

    const { messages, streaming } = useChatStore.getState()
    expect(messages).toHaveLength(2) // 一条 user + 一条 assistant
    expect(messages[0].role).toBe('user')
    expect(messages[1].role).toBe('assistant')
    expect(messages[1].content).toBe('明渠均匀流的形成条件')
    expect(messages[1].is_complete).toBe(true)
    expect(messages[1].messageId).toBe(7)
    expect(streaming).toBe(false)
    expect(capturedOnEvent).toBeDefined()
  })

  it('切会话后旧流回调被丢弃，不污染新会话', async () => {
    useChatStore.setState({ currentId: 1 })

    // 捕获 onEvent，但不让 send 立即结束——旧流在「切会话」之后仍可能触发回调
    let capturedOnEvent: ((ev: { event: string; data: unknown }) => void) | undefined
    let resolveStream: (() => void) | undefined
    ;(streamChat as ReturnType<typeof vi.fn>).mockImplementation(
      async ({ onEvent }: { onEvent: (ev: { event: string; data: unknown }) => void }) => {
        capturedOnEvent = onEvent
        // 让 send 挂起，模拟真实网络流未结束
        await new Promise<void>((r) => {
          resolveStream = r
        })
      },
    )

    const p = useChatStore.getState().send('A 会话的问题', null, 'standard')
    // 等 send 进入 streamChat 并挂起
    await vi.waitFor(() => expect(capturedOnEvent).toBeDefined())

    // 此时切到会话 2（模拟用户在 A 流式中点侧栏切走）
    useChatStore.getState().setCurrent(2)

    // 旧流的 onEvent 在切会话后仍被触发（abort 生效前的竞态窗口）——应被 streamSeq 挡掉
    capturedOnEvent!({ event: 'delta', data: 'A 会话的答案片段' })

    // 新会话 messages 应为空，没有被 A 会话的旧流污染
    expect(useChatStore.getState().messages).toHaveLength(0)

    // 收尾：让旧流 resolve，确保测试不挂
    resolveStream!()
    await p
  })

  it('stop 同步复位 streaming（按钮即时切回「发送」）', async () => {
    useChatStore.setState({ currentId: 1 })

    // streamChat 挂起，模拟流式进行中
    let resolveStream: (() => void) | undefined
    ;(streamChat as ReturnType<typeof vi.fn>).mockImplementation(
      async () =>
        new Promise<void>((r) => {
          resolveStream = r
        }),
    )

    const p = useChatStore.getState().send('问题', null, 'standard')
    await vi.waitFor(() => expect(useChatStore.getState().streaming).toBe(true))

    // 点「停止」
    useChatStore.getState().stop()

    // streaming 应立即复位（不依赖旧流 finally 的异步收尾）
    expect(useChatStore.getState().streaming).toBe(false)
    expect(useChatStore.getState().abortCtrl).toBeNull()

    // 收尾
    resolveStream!()
    await p
  })
})
