// ===== 全局共享类型 =====

export interface User {
  id: number
  username: string
  role: 'admin' | 'user'
  nickname: string | null
  is_active: boolean
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
  created_at: string
}

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
  created_at: string
  parsed_at: string | null
  // 入库进度（解析中实时填充，如 OCR 页数）：{stage, done, total, percent}
  progress?: { stage?: string; done?: number; total?: number; percent?: number } | null
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
  created_at: string
}

export interface Citation {
  chunk_id: number
  kb_id: number | null
  doc_id: number | null
  source: string
  page: number | null
  section: string | null
  snippet: string
  score: number | null
  rank: number | null
}

export interface PageResult<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}
