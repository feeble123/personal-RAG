// ===== 全局共享类型 =====

// 三级角色（单元 I 补充）：superadmin 超管 / admin 库管 / user 普通
export type Role = 'superadmin' | 'admin' | 'user'

// 管理员 = 超管 + 库管（可进知识库/记忆库/审计/统计）；超管额外可进账号管理
export function isAdminRole(role: string | undefined): boolean {
  return role === 'admin' || role === 'superadmin'
}
export function isSuperadminRole(role: string | undefined): boolean {
  return role === 'superadmin'
}

export interface User {
  id: number
  username: string
  role: Role
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
  // 单元 2 业务域：general/flood/water/construction/operation/ecology（见 constants/taxonomy.ts）
  business_domain: string
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
  parse_mode: string       // 单元 S：解析模式 fast=快速 / high=高精度（旧数据默认 fast）
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
  block_type?: string | null
  parent_chunk_id?: number | null
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

// ===== 本体（Ontology RAG）=====

/** 图谱节点：来自 PG 本体实例（source=instance）或图库（source=graph）。 */
export interface OntologyGraphNode {
  uid: string
  name: string
  /** 中文展示名（有 display_zh 就用它） */
  display: string
  entity_type: string
  kb_id: number
  props: Record<string, unknown>
  evidence_count: number
  source: 'instance' | 'graph'
}

export interface OntologyGraphEdge {
  id: string
  source: string
  target: string
  predicate: string
  status: string
  confidence: number
  evidence_text: string | null
  chunk_ids: number[]
}

export interface OntologyGraph {
  profile_id: string
  nodes: OntologyGraphNode[]
  edges: OntologyGraphEdge[]
  truncated: boolean
  graph_available: boolean
  note?: string
  /** 实体类型 → 中文名（图例和分组标签用） */
  type_labels?: Record<string, string>
  /** 关系类型 → 中文名（PART_OF → 属于） */
  relation_labels?: Record<string, string>
}

/** TBox 里的实体类型（画图时用来分组着色，也是本体结构编辑的最小单位）。 */
export interface OntologyEntityType {
  name: string
  display_name: string
  uid_prefix: string
  /** local=按库隔离，global=跨库共享 */
  scope: 'local' | 'global'
  attributes: {
    name: string
    type: string
    values?: string[]
    description?: string
    required?: boolean
  }[]
}

/** 一条本体实例（ABox，真相源在 PG）。 */
export interface OntologyInstance {
  id: number
  uid: string
  profile_id: string
  entity_type: string
  name: string
  kb_id: number
  props: Record<string, unknown>
  ranges: unknown[] | null
  evidence: OntologyEvidence[] | null
  source: string
  status: string
  updated_at: string | null
}

/** 一条证据：能点回原文就带 doc_id/chunk_id，否则只是说明。 */
export interface OntologyEvidence {
  type?: string
  note?: string
  doc_id?: number | null
  chunk_id?: number | null
  doc_name?: string | null
  page?: number | null
  locator?: string | null
  snippet?: string | null
  traceable?: boolean
  resolved_by?: string
}

/** 参数推荐卡：range_level 表示范围强度，conflict 时 candidates 并列。 */
export interface OntologyParamCard {
  parameter: string
  name: string
  group: string
  unit: string
  range_level: 'none' | 'general' | 'conditional' | 'conflict'
  confidence: number
  evidence_level: 'none' | 'claim' | 'doc_level' | 'traceable'
  range: (number | null)[] | null
  physical_range: (number | null)[] | null
  initial: number | null
  sensitivity: string | null
  narrowed_by: string[]
  candidates: { when: Record<string, string>; range: (number | null)[] }[]
  evidence: OntologyEvidence[]
  notes: string[]
}

export interface OntologyTypes {
  profile_id: string
  /** 图谱库显示名（YAML display_name，缺省回落到 profile_id） */
  display_name?: string
  description?: string
  version?: number
  domain?: string
  entity_types: OntologyEntityType[]
  relation_types: {
    name: string
    source: string
    target: string
    /** 允许的宾语类型（多选）；空/缺省表示只能连 target */
    target_types?: string[]
    display_name?: string
    description?: string
  }[]
}

/** 一条本体关系（ABox 的边，真相源在 PG `ontology_relations`）。 */
export interface OntologyRelation {
  id: number
  profile_id: string
  subject_uid: string
  predicate: string
  object_uid: string
  kb_id: number
  props: Record<string, unknown>
  evidence: OntologyEvidence[] | null
  source: string
  status: string
  updated_at: string | null
}

/** 参数表导入结果（dry_run=true 时只预演）。 */
export interface OntologyImportResult {
  dry_run: boolean
  profile_id: string
  rows: number
  drafts: number
  created: number
  updated: number
  unchanged: number
  warnings: string[]
  errors: string[]
  preview: {
    name: string
    display_zh?: string
    group?: string
    unit?: string
    range?: (number | null)[]
    initial?: number | null
    conditions: number
    has_evidence: boolean
    evidence_level: string
  }[]
}


/** 从 Excel 表头推断出来的属性建议（F3-3，只预览不写库）。 */
export interface ExcelColumnSuggestion {
  header: string
  name: string
  type: string
  values: string[]
  samples: string[]
  description: string
}

/**
 * 图谱库：一个库 = 一份 TBox（YAML）+ 属于它的实例与关系。
 * profile_id 同时是文件名与所有本体接口的 `?profile=` 取值。
 */
export interface OntologyLibrary {
  profile_id: string
  display_name: string
  description: string
  file: string
  entity_type_count: number
  relation_type_count: number
  instance_count: number
  relation_count: number
  /** 图里真实节点数（按本库声明的实体类型收口） */
  node_count: number
  is_default: boolean
  error?: string | null
}

/** 本体模式问答时 SSE 的 `facts` 事件负载（先于回答正文到达）。 */
export interface OntologyFactsEvent {
  mode: 'ontology'
  facts: Record<string, unknown>[]
  fallback_used: boolean
  sources: string[]
  latency_ms: number
  notes: string[]
}

/** 本体问答返回的事实包。fact 的字段随 kind 不同，这里只约定公共部分。 */
export interface OntologyAskResult {
  question: string
  facts: Record<string, unknown>[]
  entities: Record<string, unknown>[]
  evidence: Record<string, unknown>[]
  fallback_used: boolean
  sources: string[]
  latency_ms: number
  notes: string[]
}
