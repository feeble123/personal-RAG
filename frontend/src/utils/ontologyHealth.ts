/**
 * 本体体检的展示转换（纯函数，便于单测）。
 *
 * 体检报告的原始字段是给机器看的（`missing_items: [1,3,6]`），
 * 界面上要变成「缺第 (1)(3)(6) 条」——**说少了 3 条没用，得说少的是哪 3 条**。
 */
import type { OntologyConstraint, OntologyViolation } from '@/api/types'

/** 把违规的 `detail` 翻成一句人话。 */
export function detailText(detail: OntologyViolation['detail']): string {
  if (!detail) return ''
  const parts: string[] = []

  const missing = detail.missing_items
  if (Array.isArray(missing) && missing.length) {
    parts.push(`缺第 ${missing.map((n) => `(${n})`).join('')} 条`)
  }

  const untagged = detail.untagged
  if (Array.isArray(untagged) && untagged.length) {
    const head = untagged.slice(0, 3).join('、')
    const tail = untagged.length > 3 ? '…' : ''
    parts.push(`${untagged.length} 条没标条目号（${head}${tail}）`)
  }

  if (typeof detail.actual === 'number' && typeof detail.expected === 'number') {
    parts.push(`实际 ${detail.actual}，要求 ${detail.expected}`)
  }

  return parts.join('；')
}

/** 一条约束的一句话说明：优先用 TBox 里写的人话，没有就把类型名翻出来。 */
export function constraintSummary(c: OntologyConstraint): string {
  if (c.message) return c.message
  const rel = (c.relation ?? []).join('/')
  switch (c.type) {
    case 'required_attribute':
      return `必须填 ${c.attribute ?? ''}`
    case 'min_count':
      return `${rel} 至少 ${c.min ?? c.min_attr ?? '?'} 条`
    case 'max_count':
      return `${rel} 最多 ${c.max ?? '?'} 条`
    case 'unique_attribute':
      return `${c.attribute ?? ''} 不能重复`
    case 'coverage':
      return `${rel} 要逐条覆盖原文条目`
    default:
      return c.type
  }
}
