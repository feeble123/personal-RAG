import { api } from './client'
import { useAuthStore } from '@/stores/auth'
import type { AuditLog, ChunkItem, Citation, Conversation, ExcelColumnSuggestion, KnowledgeBase, MemoryItem, MemoryStats, Message, OntologyAskResult, OntologyGraph, OntologyImportResult, OntologyInstance, OntologyLibrary, OntologyParamCard, OntologyRelation, OntologyTypes, SystemStats, TokenOut, User } from './types'

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

// ===== 消息反馈（问答记忆库：👍 沉淀正向记忆 / 👎 沉淀负面记忆 / null 取消）=====
export const feedbackApi = {
  send: (convId: number, messageId: number, feedback: 'up' | 'down' | null) =>
    api
      .post<{ feedback: 'up' | 'down' | null }>(`/conversations/${convId}/messages/${messageId}/feedback`, {
        feedback,
      })
      .then((r) => r.data),
}

// ===== 问答记忆库管理（仅管理员）=====
export const memoryApi = {
  list: (params?: Record<string, unknown>) =>
    api.get('/admin/memories', { params }).then((r) => r.data as { items: MemoryItem[]; total: number }),
  stats: (params?: Record<string, unknown>) =>
    api.get('/admin/memories/stats', { params }).then((r) => r.data as MemoryStats),
  create: (data: { question: string; answer: string; kb_id?: number | null; style?: string }) =>
    api.post('/admin/memories', data).then((r) => r.data as MemoryItem),
  setStatus: (id: number, status: 'good' | 'bad') =>
    api.patch(`/admin/memories/${id}`, { status }).then((r) => r.data as MemoryItem),
  remove: (id: number) => api.delete(`/admin/memories/${id}`),
  batchRemove: (ids: number[]) =>
    api.request({ method: 'delete', url: '/admin/memories', data: { ids } }),
  clearKb: (kbId: number) => api.delete(`/admin/kbs/${kbId}/memories`),
  exportFile: async (fmt: 'csv' | 'json', params?: Record<string, unknown>) => {
    const res = await api.get('/admin/memories/export', {
      params: { ...params, fmt },
      responseType: 'blob',
    })
    const url = URL.createObjectURL(res.data as Blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `qa_memory.${fmt}`
    a.click()
    URL.revokeObjectURL(url)
  },
  // 记忆页用户筛选下拉（管理接口）
  listUsers: () => api.get('/admin/users').then((r) => r.data as { items: { id: number; username: string }[] }),
}

// ===== 账号管理（仅管理员）=====
export const usersApi = {
  list: (params?: Record<string, unknown>) =>
    api.get('/admin/users', { params }).then((r) => r.data as { items: User[]; total: number }),
  create: (data: { username: string; password: string; nickname?: string; role?: string }) =>
    api.post('/admin/users', data).then((r) => r.data as User),
  patch: (id: number, data: { role?: string; is_active?: boolean }) =>
    api.patch(`/admin/users/${id}`, data).then((r) => r.data as User),
  resetPassword: (id: number, newPassword: string) =>
    api.put(`/admin/users/${id}/password`, { new_password: newPassword }).then((r) => r.data as User),
  remove: (id: number) => api.delete(`/admin/users/${id}`),
}

// ===== 系统统计（管理员：检索证据质量分布等答辩数据）=====
export const statsApi = {
  system: () => api.get<SystemStats>('/admin/stats').then((r) => r.data),
}

// ===== 审计日志（单元 I：管理员敏感操作留痕）=====
export const auditApi = {
  list: (params?: Record<string, unknown>) =>
    api.get('/admin/audit-logs', { params }).then((r) => r.data as { items: AuditLog[]; total: number }),
}

// ===== 本体（Ontology RAG）=====
export const ontologyApi = {
  /** TBox：当前本体有哪些实体类型 / 关系类型 / 属性 */
  types: (profile?: string) =>
    api.get('/admin/ontology/types', { params: { profile } }).then((r) => r.data as OntologyTypes),
  /** 图谱全览：节点 + 边（节点来自 PG 实例，边来自图库） */
  graph: (params?: Record<string, unknown>) =>
    api.get('/admin/ontology/graph', { params }).then((r) => r.data as OntologyGraph),
  /** ABox 实例列表 */
  instances: (params?: Record<string, unknown>) =>
    api.get('/admin/ontology/instances', { params }).then((r) => r.data as OntologyInstance[]),
  /** 参数推荐卡 */
  params: (params?: Record<string, unknown>) =>
    api.get('/admin/ontology/params', { params }).then((r) => r.data as OntologyParamCard[]),

  // —— 实例（ABox 节点）——
  createInstance: (data: Record<string, unknown>) =>
    api.post('/admin/ontology/instances', data).then((r) => r.data as OntologyInstance),
  updateInstance: (id: number, data: Record<string, unknown>) =>
    api.patch(`/admin/ontology/instances/${id}`, data).then((r) => r.data as OntologyInstance),
  deleteInstance: (id: number) => api.delete(`/admin/ontology/instances/${id}`),

  // —— 关系（ABox 的边）——
  relations: (params?: Record<string, unknown>) =>
    api.get('/admin/ontology/relations', { params }).then((r) => r.data as OntologyRelation[]),
  createRelation: (data: Record<string, unknown>) =>
    api.post('/admin/ontology/relations', data).then((r) => r.data as OntologyRelation),
  updateRelation: (id: number, data: Record<string, unknown>) =>
    api.patch(`/admin/ontology/relations/${id}`, data).then((r) => r.data as OntologyRelation),
  deleteRelation: (id: number) => api.delete(`/admin/ontology/relations/${id}`),

  // —— TBox 在线编辑（F3）：改的是图谱库的「图纸」，写回 YAML ——
  upsertEntityType: (
    profile: string | undefined,
    data: { name: string; display_name: string; uid_prefix: string; scope: 'local' | 'global' },
  ) =>
    api
      .post('/admin/ontology/schema/entity', data, { params: { profile } })
      .then((r) => r.data as OntologyTypes),
  deleteEntityType: (profile: string | undefined, name: string) =>
    api
      .delete(`/admin/ontology/schema/entity/${name}`, { params: { profile } })
      .then((r) => r.data as OntologyTypes),
  upsertRelationType: (
    profile: string | undefined,
    data: {
      name: string
      display_name: string
      source: string
      target: string
      target_types?: string[]
      description?: string
    },
  ) =>
    api
      .post('/admin/ontology/schema/relation', data, { params: { profile } })
      .then((r) => r.data as OntologyTypes),
  deleteRelationType: (profile: string | undefined, name: string) =>
    api
      .delete(`/admin/ontology/schema/relation/${name}`, { params: { profile } })
      .then((r) => r.data as OntologyTypes),
  upsertAttribute: (
    profile: string | undefined,
    entity: string,
    data: { name: string; type: string; values?: string[]; required?: boolean; description?: string },
  ) =>
    api
      .post(`/admin/ontology/schema/entity/${entity}/attribute`, data, { params: { profile } })
      .then((r) => r.data as OntologyTypes),
  deleteAttribute: (profile: string | undefined, entity: string, attr: string) =>
    api
      .delete(`/admin/ontology/schema/entity/${entity}/attribute/${attr}`, { params: { profile } })
      .then((r) => r.data as OntologyTypes),

  // —— 图谱库（一个库 = 一份 TBox + 属于它的实例/关系）——
  libraries: () => api.get('/admin/ontology/libraries').then((r) => r.data as OntologyLibrary[]),
  createLibrary: (data: {
    profile_id: string
    display_name: string
    description?: string
    template?: string
  }) =>
    api.post('/admin/ontology/libraries', data).then((r) => r.data as OntologyLibrary),
  deleteLibrary: (profileId: string) => api.delete(`/admin/ontology/libraries/${profileId}`),
  /** 导出该库的 TBox YAML（换机器带走 / 交给同事） */
  exportLibrary: async (profileId: string) => {
    const res = await api.get(`/admin/ontology/libraries/${profileId}/export`, {
      responseType: 'blob',
    })
    const url = URL.createObjectURL(res.data as Blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${profileId}.yaml`
    a.click()
    URL.revokeObjectURL(url)
  },
  /** 导入一份 TBox YAML（不覆盖同名库） */
  importLibrary: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return api
      .post('/admin/ontology/libraries/import', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      .then((r) => r.data as OntologyLibrary)
  },
  /** 读 Excel 表头 → 属性建议（只预览，不写库） */
  excelPreview: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return api
      .post('/admin/ontology/schema/excel-preview', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      .then((r) => r.data as { columns: ExcelColumnSuggestion[] })
  },

  /** 本体问答试验台（管理员） */
  ask: (question: string, opts?: { kbId?: number; minFacts?: number }) =>
    api
      .post('/admin/ontology/ask', {
        question,
        kb_id: opts?.kbId ?? null,
        min_facts: opts?.minFacts ?? null,
      })
      .then((r) => r.data as OntologyAskResult),

  // —— 参数表导入（默认只预演）——
  importParams: (file: File, opts: { dryRun: boolean; kbId?: number; profile?: string }) => {
    const form = new FormData()
    form.append('file', file)
    return api
      .post('/admin/ontology/params/import', form, {
        params: { dry_run: opts.dryRun, kb_id: opts.kbId ?? 0, profile: opts.profile },
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      .then((r) => r.data as OntologyImportResult)
  },
}

// ===== 流式 SSE（fetch + ReadableStream）：解析 `data: {json}\n\n` 事件 =====
async function readSSE(
  res: Response,
  onEvent: (ev: { event: string; data: unknown }) => void,
): Promise<void> {
  if (!res.ok || !res.body) {
    throw new Error(`流式请求失败 (${res.status})`)
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
        onEvent(msg)
      } catch {
        /* 忽略解析失败的分片 */
      }
    }
  }
}

// 普通问答流
export async function streamChat(opts: {
  conversationId: number
  content: string
  kbId?: number | null
  style?: string
  /** kb = 只查知识库切片（默认）；ontology = 先查本体，拿不到事实才回退检索 */
  mode?: 'kb' | 'ontology'
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
    body: JSON.stringify({
      content: opts.content,
      kb_id: opts.kbId ?? null,
      style: opts.style ?? null,
      mode: opts.mode ?? 'kb',
    }),
    signal: opts.signal,
  })
  await readSSE(res, opts.onEvent)
}

// LLM 优化流（opt-in）：用户对某条回答不满意 → 触发 /optimize 重生成
export async function streamOptimize(opts: {
  conversationId: number
  messageId: number
  signal: AbortSignal
  onEvent: (ev: { event: string; data: unknown }) => void
}): Promise<void> {
  const token = useAuthStore.getState().token
  const res = await fetch(
    `/api/conversations/${opts.conversationId}/messages/${opts.messageId}/optimize`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: '{}',
      signal: opts.signal,
    },
  )
  await readSSE(res, opts.onEvent)
}

// ===== 知识库 / 文档（管理员）=====
export const kbApi = {
  list: () => api.get('/admin/kbs').then((r) => r.data),
  create: (data: {
    name: string
    description?: string
    answer_style?: string
    business_domain?: string
  }) => api.post('/admin/kbs', data).then((r) => r.data),
  update: (
    id: number,
    data: { name?: string; description?: string; answer_style?: string; business_domain?: string },
  ) => api.patch(`/admin/kbs/${id}`, data).then((r) => r.data),
  remove: (id: number) => api.delete(`/admin/kbs/${id}`).then((r) => r.data),
  documents: (kbId: number, params?: Record<string, unknown>) =>
    api.get(`/admin/kbs/${kbId}/documents`, { params }).then((r) => r.data),
  upload: (
    kbId: number,
    file: File,
    docType?: string,
    onProgress?: (pct: number) => void,
    parseMode?: string,
  ) => {
    const form = new FormData()
    form.append('file', file)
    form.append('doc_type', docType ?? 'other')
    form.append('parse_mode', parseMode ?? 'fast')
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
  chunks: (
    kbId: number,
    params?: { page?: number; page_size?: number; doc_id?: number; kind?: 'child' | 'parent' | 'all' },
  ) =>
    api
      .get(`/admin/kbs/${kbId}/chunks`, { params })
      .then(
        (r) =>
          r.data as {
            total: number
            parent_total: number
            child_total: number
            items: ChunkItem[]
          },
      ),
}
