/**
 * 知识库文档展示层的常量与纯函数（G6-1 从 KnowledgeBase.tsx 抽出）。
 *
 * 这里只放「不依赖 React 状态」的内容：状态映射、上传类型、解析质量格式化。
 * 组件拆分见同目录 DocumentTable / UploadProgress / SearchPreview / ChunkBrowser。
 */
import type { DocumentItem } from '@/api/types'

// 文档状态 → 展示文案/颜色
export const STATUS_META: Record<string, { text: string; color: string }> = {
  pending: { text: '排队中', color: 'default' },
  parsing: { text: '解析中', color: 'processing' },
  embedding: { text: '向量化中', color: 'processing' },
  ready: { text: '已就绪', color: 'success' },
  failed: { text: '失败', color: 'error' },
}

// 文档版本状态（P0-8 重灌审计）：building→validated→active，被新版替换→retired，失败→failed
export const VERSION_STATUS_META: Record<string, { text: string; color: string }> = {
  building: { text: '构建中', color: 'processing' },
  validated: { text: '已校验', color: 'default' },
  active: { text: '当前生效', color: 'success' },
  failed: { text: '失败', color: 'error' },
  retired: { text: '已退役', color: 'default' },
}

// 解析阶段 → 进度百分比（阶段标记，非精确进度）
export const STAGE_PCT: Record<string, number> = {
  pending: 5,
  parsing: 45,
  embedding: 80,
}

// 批量上传任务：每个文件一条独立进度（上传中/成功/失败），供进度面板展示
export interface UploadTask {
  key: string
  filename: string
  pct: number // 0~100
  status: 'uploading' | 'done' | 'error'
  error?: string
}

// P0-11 文档类型（未来 DSH 引用来源判断）：textbook 教材 / standard 规范 / manual 手册 / other 其他
export type DocType = 'textbook' | 'standard' | 'manual' | 'other'
export const DOC_TYPE_OPTIONS: { value: DocType; label: string }[] = [
  { value: 'textbook', label: '教材' },
  { value: 'standard', label: '规范' },
  { value: 'manual', label: '手册' },
  { value: 'other', label: '其他' },
]

// 单元 S：解析模式（上传时用户自选）。fast=快速（pipeline 老后端）/ high=高精度（hybrid-engine）
export type ParseMode = 'fast' | 'high'
export const PARSE_MODE_OPTIONS: { value: ParseMode; label: string }[] = [
  { value: 'fast', label: '快速' },
  { value: 'high', label: '高精度' },
]

export const ACCEPT = '.pdf,.docx,.md,.markdown,.txt,.xlsx,.csv'

export function formatBytes(v: number): string {
  if (!v) return '0 B'
  if (v < 1024) return `${v} B`
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`
  return `${(v / 1024 / 1024).toFixed(1)} MB`
}

// 解析质量 → 标签（文本层 / OCR / 文本+OCR / 各解析器名）
export function parseLabel(r: DocumentItem): { label: string; color: string } {
  const q = r.quality
  if (!q) return { label: '—', color: 'default' }
  if (q.parser === 'pdf') {
    const ocr = Number(q.ocr_pages) || 0
    const text = Number(q.text_pages) || 0
    if (ocr > 0 && text > 0) return { label: '文本+OCR', color: 'gold' }
    if (ocr > 0) return { label: 'OCR 识别', color: 'orange' }
    return { label: '文本层', color: 'green' }
  }
  return { label: String(q.parser ?? r.file_type).toUpperCase(), color: 'blue' }
}

// 解析质量 → 一行摘要（页数 / 段落 / 行数等）
export function parseSummary(r: DocumentItem): string[] {
  const q = r.quality
  if (!q) return []
  const n = (k: string) => Number(q[k]) || 0
  if (q.parser === 'pdf') {
    const lines = [`共 ${r.page_count ?? q.pages ?? 0} 页`]
    const text = n('text_pages')
    const ocr = n('ocr_pages')
    if (text > 0) lines.push(`文本层 ${text} 页`)
    if (ocr > 0) {
      lines.push(`OCR ${ocr} 页`)
      if (q.mean_ocr_confidence != null) lines.push(`OCR 置信度 ${q.mean_ocr_confidence}`)
    }
    return lines
  }
  if (q.parser === 'docx') return [`段落 ${n('paragraphs')}`, `标题 ${n('headings')}`, `表格 ${n('tables')}`]
  if (q.parser === 'excel') return [`工作表 ${n('sheets')}`, `${n('rows')} 行`]
  if (q.parser === 'csv') return [`${n('rows')} 行`]
  if (q.parser === 'markdown' || q.parser === 'text') return [`段落 ${n('paragraphs')}`, `片段 ${n('blocks')}`]
  if (q.parser === 'mineru') return [`共 ${n('pages')} 页`, `元素 ${n('elements')}`, `片段 ${n('blocks')}`]
  return [`片段 ${n('blocks')}`]
}

// 解析质量 → 详情键值对（文档详情弹窗 / 悬停明细）
export function qualityItems(q: Record<string, unknown>): { label: string; value: string }[] {
  const out: { label: string; value: string }[] = []
  const num = (k: string) => (q[k] != null ? String(q[k]) : '—')
  const parser = String(q.parser ?? '')
  if (parser === 'pdf') {
    out.push({ label: '总页数', value: num('pages') })
    out.push({ label: '文本层页数', value: num('text_pages') })
    out.push({ label: 'OCR页数', value: num('ocr_pages') })
    out.push({ label: '表格数', value: num('tables') })
    out.push({ label: '内容片段', value: num('blocks') })
    out.push({ label: '总字数', value: num('total_chars') })
    out.push({ label: '乱码比例', value: num('garble_ratio') })
    if (q.mean_ocr_confidence != null) out.push({ label: 'OCR平均置信度', value: num('mean_ocr_confidence') })
    return out
  }
  if (parser === 'mineru') {
    out.push({ label: '总页数', value: num('pages') })
    out.push({ label: '内容元素', value: num('elements') })
    out.push({ label: '内容片段', value: num('blocks') })
    if (q.mineru_version != null && q.mineru_version !== 'unknown') out.push({ label: 'MinerU 版本', value: num('mineru_version') })
    return out
  }
  if (parser === 'docx') {
    out.push({ label: '段落', value: num('paragraphs') })
    out.push({ label: '标题', value: num('headings') })
    out.push({ label: '表格', value: num('tables') })
    out.push({ label: '内容片段', value: num('blocks') })
    return out
  }
  if (parser === 'markdown' || parser === 'text') {
    out.push({ label: '段落', value: num('paragraphs') })
    out.push({ label: '内容片段', value: num('blocks') })
    return out
  }
  if (parser === 'excel') {
    out.push({ label: '工作表', value: num('sheets') })
    out.push({ label: '数据行', value: num('rows') })
    out.push({ label: '内容片段', value: num('blocks') })
    return out
  }
  if (parser === 'csv') {
    out.push({ label: '数据行', value: num('rows') })
    out.push({ label: '内容片段', value: num('blocks') })
    return out
  }
  return out
}
