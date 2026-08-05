import { api } from './client'
import { useAuthStore } from '@/stores/auth'
import type { ChunkItem, Citation, Conversation, KnowledgeBase, Message, TokenOut, User } from './types'

// ===== 认证 =====
export const authApi = {
  register: (data: { username: string; password: string; nickname?: string }) =>
    api.post<TokenOut>('/auth/register', data).then((r) => r.data),
  login: (data: { username: string; password: string }) =>
    api.post<TokenOut>('/auth/login', data).then((r) => r.data),
  me: () => api.get<User>('/auth/me').then((r) => r.data),
  changePassword: (data: { old_password: string; new_password: string }) =>
    api.put<User>('/auth/password', data).then((r) => r.data),
}

// ===== 知识库（登录用户，用于问答时选择）=====
export const kbPublicApi = {
  list: () => api.get('/knowledge-bases').then((r) => r.data as KnowledgeBase[]),
}

// ===== 会话 =====
export interface MessageOut extends Message {
  citations: Citation[]
}

export const convApi = {
  list: (params?: Record<string, unknown>) =>
    api.get('/conversations', { params }).then((r) => r.data as { items: Conversation[]; total: number }),
  create: (title?: string) => api.post('/conversations', { title }).then((r) => r.data as Conversation),
  detail: (id: number) => api.get(`/conversations/${id}`).then((r) => r.data as Conversation),
  rename: (id: number, title: string) => api.patch(`/conversations/${id}`, { title }).then((r) => r.data as Conversation),
  remove: (id: number) => api.delete(`/conversations/${id}`),
  messages: (id: number, params?: { cursor?: number; limit?: number }) =>
    api.get(`/conversations/${id}/messages`, { params }).then((r) => r.data as { items: MessageOut[]; has_more: boolean }),
}

// ===== 流式问答（SSE，fetch + ReadableStream）=====
export async function streamChat(opts: {
  conversationId: number
  content: string
  kbId?: number | null
  style?: string
  signal: AbortSignal
  onEvent: (ev: { event: string; data: unknown }) => void
}): Promise<void> {
  const token = useAuthStore.getState().token
  const res = await fetch(`/api/conversations/${opts.conversationId}/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ content: opts.content, kb_id: opts.kbId ?? null, style: opts.style ?? null }),
    signal: opts.signal,
  })
  if (!res.ok || !res.body) {
    throw new Error(`问答请求失败 (${res.status})`)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buf.indexOf('\n\n')) !== -1) {
      const raw = buf.slice(0, idx).trim()
      buf = buf.slice(idx + 2)
      if (!raw.startsWith('data:')) continue
      try {
        const msg = JSON.parse(raw.replace(/^data:\s?/, ''))
        opts.onEvent(msg)
      } catch {
        /* 忽略解析失败的分片 */
      }
    }
  }
}

// ===== 知识库 / 文档（管理员）=====
export const kbApi = {
  list: () => api.get('/admin/kbs').then((r) => r.data),
  create: (data: { name: string; description?: string; answer_style?: string }) =>
    api.post('/admin/kbs', data).then((r) => r.data),
  update: (id: number, data: { name?: string; description?: string; answer_style?: string }) =>
    api.patch(`/admin/kbs/${id}`, data).then((r) => r.data),
  remove: (id: number) => api.delete(`/admin/kbs/${id}`).then((r) => r.data),
  documents: (kbId: number, params?: Record<string, unknown>) =>
    api.get(`/admin/kbs/${kbId}/documents`, { params }).then((r) => r.data),
  upload: (kbId: number, file: File, onProgress?: (pct: number) => void) => {
    const form = new FormData()
    form.append('file', file)
    return api
      .post(`/admin/kbs/${kbId}/documents/upload`, form, {
        headers: { 'Content-Type': 'multipart/form-data' },
        timeout: 0,
        onUploadProgress: (e) => {
          if (onProgress && e.total) onProgress(Math.round((e.loaded / e.total) * 100))
        },
      })
      .then((r) => r.data)
  },
  documentDetail: (docId: number) => api.get(`/admin/documents/${docId}`).then((r) => r.data),
  documentRemove: (docId: number) => api.delete(`/admin/documents/${docId}`).then((r) => r.data),
  documentReparse: (docId: number) =>
    api.post(`/admin/documents/${docId}/reparse`).then((r) => r.data),
  search: (params: { q: string; kb_id?: number; top_k?: number }) =>
    api.get('/admin/search', { params }).then((r) => r.data),
  chunks: (kbId: number, params?: { page?: number; page_size?: number; doc_id?: number }) =>
    api
      .get(`/admin/kbs/${kbId}/chunks`, { params })
      .then((r) => r.data as { total: number; items: ChunkItem[] }),
}
