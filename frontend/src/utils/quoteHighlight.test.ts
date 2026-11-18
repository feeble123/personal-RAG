import { describe, expect, it } from 'vitest'
import { findQuoteRange } from './quoteHighlight'

describe('findQuoteRange', () => {
  it('精确匹配直接返回区间', () => {
    const content = '第四层次 | 汇流计算 | CI | 壤中流消退系数 | 敏感 |'
    const range = findQuoteRange(content, 'CI | 壤中流消退系数')
    expect(range).not.toBeNull()
    expect(content.slice(range![0], range![1])).toBe('CI | 壤中流消退系数')
  })

  it('空白不同也能定位（PDF 换行位置不稳定）', () => {
    const content = '第四层次\n汇流计算  CI\n壤中流消退系数   敏感'
    const range = findQuoteRange(content, 'CI | 壤中流消退系数')
    // 引用里的竖线原文没有 → 找不到，返回 null（不硬凑）
    expect(range).toBeNull()

    const range2 = findQuoteRange(content, 'CI 壤中流消退系数')
    expect(range2).not.toBeNull()
    expect(content.slice(range2![0], range2![1])).toContain('壤中流消退系数')
  })

  it('跨行引用能匹配到原文的真实起止位置', () => {
    const content = '...前文\n第四层次 | 汇流计算 |\nCI | 壤中流消退系数 | 敏感\n后文...'
    const range = findQuoteRange(content, '第四层次 | 汇流计算 | CI | 壤中流消退系数 | 敏感')
    expect(range).not.toBeNull()
    const hit = content.slice(range![0], range![1])
    expect(hit.startsWith('第四层次')).toBe(true)
    expect(hit.endsWith('敏感')).toBe(true)
  })

  it('引用为空或不存在时返回 null', () => {
    expect(findQuoteRange('原文', '')).toBeNull()
    expect(findQuoteRange('原文', null)).toBeNull()
    expect(findQuoteRange('原文', '完全不存在的句子')).toBeNull()
  })
})
