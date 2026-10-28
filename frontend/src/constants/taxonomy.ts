/**
 * 知识分类字典（单元 2）—— 与后端 backend/app/core/taxonomy.py 保持**同名同值**。
 *
 * 业务域挂在知识库上（一个库一个域），文档类型挂在文档上（上传时选择）。
 * 改分类时必须两边一起改：后端 taxonomy.py + 本文件。
 * 运行时也可用 GET /api/knowledge-bases/taxonomy 取后端权威值。
 */

export interface CategoryOption {
  value: string
  label: string
  description?: string
}

// ---------------- 业务域（6 个） ----------------
export const BUSINESS_DOMAINS: CategoryOption[] = [
  { value: 'general', label: '综合与基础', description: '教材、通用规范、标准化、暂未归类资料' },
  { value: 'flood', label: '防洪减灾', description: '防汛抗旱、洪水风险、防洪标准、应急预案' },
  { value: 'water', label: '水资源与供水', description: '水资源规划配置、节约用水、供水与给排水工程' },
  { value: 'construction', label: '水利工程建设', description: '勘测设计、施工、监理、质量安全、造价与验收' },
  { value: 'operation', label: '工程运行管理', description: '水库、水闸、泵站、堤防的运行维护与安全监测' },
  { value: 'ecology', label: '河湖生态与农村水利', description: '河湖管理、水生态、水土保持、灌区与农村供水' },
]

export const DEFAULT_DOMAIN = 'general'

export const DOMAIN_LABELS: Record<string, string> = Object.fromEntries(
  BUSINESS_DOMAINS.map((d) => [d.value, d.label]),
)

// 业务域标签颜色（列表/卡片上区分）
export const DOMAIN_COLORS: Record<string, string> = {
  general: 'default',
  flood: 'red',
  water: 'blue',
  construction: 'geekblue',
  operation: 'orange',
  ecology: 'green',
}

export function domainLabel(value: string | null | undefined): string {
  return DOMAIN_LABELS[value || DEFAULT_DOMAIN] ?? '综合与基础'
}

export function domainColor(value: string | null | undefined): string {
  return DOMAIN_COLORS[value || DEFAULT_DOMAIN] ?? 'default'
}

// ---------------- 文档类型（10 类） ----------------
// 前 4 个是历史值（textbook/standard/manual/other），值保持不变，兼容旧数据与对外契约。
export const DOC_TYPES: CategoryOption[] = [
  { value: 'standard', label: '规范', description: '国家标准/行业标准（GB、SL、DL 等）' },
  { value: 'regulation', label: '规程', description: '操作/运行/检测/验收规程' },
  { value: 'textbook', label: '教材', description: '高校教材、专业书籍' },
  { value: 'manual', label: '手册', description: '设计手册、技术手册、产品手册' },
  { value: 'emergency_plan', label: '预案', description: '防汛抗旱、应急处置等预案' },
  { value: 'scheme', label: '方案', description: '施工方案、专项方案、实施方案' },
  { value: 'report', label: '报告', description: '设计报告、评估报告、检测报告、工作总结' },
  { value: 'case', label: '案例', description: '工程案例、事故案例、经验做法' },
  { value: 'ledger', label: '台账', description: '方案台账、设备清单、检查记录等数据表' },
  { value: 'other', label: '其他', description: '无法归类或待补充' },
]

export const DEFAULT_DOC_TYPE = 'other'

export const DOC_TYPE_LABELS: Record<string, string> = Object.fromEntries(
  DOC_TYPES.map((d) => [d.value, d.label]),
)

export const DOC_TYPE_COLORS: Record<string, string> = {
  standard: 'blue',
  regulation: 'cyan',
  textbook: 'purple',
  manual: 'geekblue',
  emergency_plan: 'red',
  scheme: 'orange',
  report: 'gold',
  case: 'green',
  ledger: 'magenta',
  other: 'default',
}

export function docTypeLabel(value: string | null | undefined): string {
  return DOC_TYPE_LABELS[value || DEFAULT_DOC_TYPE] ?? '其他'
}

export function docTypeColor(value: string | null | undefined): string {
  return DOC_TYPE_COLORS[value || DEFAULT_DOC_TYPE] ?? 'default'
}
