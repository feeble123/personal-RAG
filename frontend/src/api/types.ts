// ===== 全局共享类型 =====

export interface User {
  id: number
  username: string
  role: 'admin' | 'user'
  nickname: string | null
  is_active: boolean
  created_at?: string // 账号管理列表补出
}

export interface TokenOut {
  access_token: string
  token_type: string
  user: User
}

export interface KnowledgeBase {
  id: number
  name: string
  description: string | null
  doc_count: number
  chunk_count: number
  status: string
  answer_style: string
  created_at: string
}

// 回答风格选项（单元 F）：值与后端 chat.ANSWER_STYLES 一致
export const ANSWER_STYLE_OPTIONS: { value: string; label: string }[] = [
  { value: 'standard', label: '规范条文式' },
  { value: 'logical', label: '专业论证式' },
  { value: 'summary', label: '要点摘要式' },
  { value: 'expanded', label: '拓展延伸式' },
  { value: 'tutorial', label: '通俗讲解式' },
]

export interface DocumentItem {
  id: number
  kb_id: number
  filename: string
  file_type: string
  file_size: number
  status: string // pending/parsing/embedding/ready/failed
  error_message: string | null
  page_count: number | null
  chunk_count: number
  quality: Record<string, unknown> | null
  chunk_strategy: string   // 后端保留字段，兼容旧数据
  // P0-11 文档类型（未来 DSH 引用来源判断）：textbook/standard/manual/other
  doc_type: string
  created_at: string
  parsed_at: string | null
  // 入库进度（解析中实时填充，如 OCR 页数）：{stage, done, total, percent}
  progress?: { stage?: string; done?: number; total?: number; percent?: number } | null
  // P0-8 版本历史（重灌审计）：最近若干版本
  versions?: DocumentVersionItem[]
}

// 文档版本（重灌审计）：展示版本状态/切片数/时间线
export interface DocumentVersionItem {
  id: number
  status: string // building / validated / active / failed / retired
  chunk_count: number
  source_hash: string | null
  error_message: string | null
  created_at: string
  activated_at: string | null
  retired_at: string | null
}

export interface ChunkItem {
  id: number
  doc_id: number
  chunk_index: number
  page: number | null
  section: string | null
  content: string
}

export interface Conversation {
  id: number
  title: string
  last_message_at: string
  created_at: string
}

export interface Message {
  id: number
  conversation_id: number
  role: 'user' | 'assistant'
  content: string
  is_complete: boolean
  error: string | null
  // 问答记忆库：用户反馈 + 是否来自记忆复用（历史刷新后可还原标签与按钮态）
  feedback: 'up' | 'down' | null
  from_memory: boolean
  kb_id: number | null
  doc_scope: string | null
  style: string | null
  // 证据等级（U3）：检索质量判级
  evidence_level: 'sufficient' | 'partial' | 'weak' | 'none' | null
  evidence_top_score: number | null
  // 层2 完备性校验：True=完整 / False=触发补全 / null=未校验
  answer_complete: boolean | null
  // LLM优化（opt-in）：True = 用户点「🤖 LLM优化」产生的结果
  optimized: boolean
  created_at: string
}

export interface Citation {
  chunk_id: number | null  // P0-5：重灌/删文档后历史引用的 chunk 已删 → null（快照字段仍可显示）
  kb_id: number | null
  doc_id: number | null
  source: string
  page: number | null
  section: string | null
  snippet: string
  score: number | null
  rank: number | null
}

// 问答记忆条目（记忆库管理系统，镜像后端 MemoryOut）
export interface MemoryItem {
  id: number
  user_id: number
  username: string | null
  kb_id: number | null
  kb_name: string | null
  doc_scope: string | null
  style: string | null
  status: 'good' | 'bad'
  question: string
  subject: string | null
  answer: string
  citations: Citation[]
  hit_count: number
  score: number | null
  created_at: string
  updated_at: string
}

// 记忆统计（前端统计卡片）
export interface MemoryStats {
  total: number
  good: number
  bad: number
  total_hits: number
}

// 检索证据质量分布（U3：系统统计中的 evidence 字段）
export interface EvidenceStats {
  total: number
  sufficient: number
  partial: number
  weak: number
  none: number
}

// 系统统计（/admin/stats，含证据质量分布）
export interface SystemStats {
  users: number
  conversations: number
  messages: number
  knowledge_bases: number
  documents: number
  chunks: number
  qa_memory: number
  evidence: EvidenceStats
  vectors_in_chroma: number
  bm25_indexed_kbs: number
  per_kb: { name: string; chunk_count: number }[]
}

export interface PageResult<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

// 审计日志（单元 I）：管理员敏感操作留痕
export interface AuditLog {
  id: number
  actor_name: string
  action: string
  target_type: string
  target_id: string | null
  detail: string
  client_ip: string | null
  created_at: string
}
