import { describe, expect, it } from 'vitest'
import { constraintSummary, detailText } from './ontologyHealth'
import type { OntologyConstraint } from '@/api/types'

describe('detailText', () => {
  it('逐条核对要说出缺的是哪几条，不能只说少了几条', () => {
    // 「少了 3 条」补数据的人无从下手；「缺第 (1)(3)(6) 条」才可操作
    expect(detailText({ missing_items: [1, 3, 6], covered_items: [2, 4, 5] })).toBe(
      '缺第 (1)(3)(6) 条',
    )
  })

  it('数量类违规给出实际与要求', () => {
    expect(detailText({ actual: 3, expected: 6 })).toBe('实际 3，要求 6')
  })

  it('没标条目号的边也要点名', () => {
    expect(detailText({ untagged: ['甲', '乙', '丙', '丁'] })).toBe(
      '4 条没标条目号（甲、乙、丙…）',
    )
  })

  it('多种信息同时存在时按顺序拼起来', () => {
    expect(detailText({ missing_items: [2], actual: 3, expected: 6 })).toBe(
      '缺第 (2) 条；实际 3，要求 6',
    )
  })

  it('没有 detail 或字段为空时返回空串', () => {
    expect(detailText(undefined)).toBe('')
    expect(detailText({})).toBe('')
    expect(detailText({ missing_items: [] })).toBe('')
  })
})

describe('constraintSummary', () => {
  const base = { id: 'x', type: 'min_count', entity: 'measure', severity: 'error' } as OntologyConstraint

  it('TBox 里写了人话就用它', () => {
    expect(constraintSummary({ ...base, message: '措施没有挂执行单位' })).toBe(
      '措施没有挂执行单位',
    )
  })

  it('没写人话时按类型拼一句', () => {
    expect(
      constraintSummary({ ...base, relation: ['RESPONDS_WITH'], min: 6 }),
    ).toBe('RESPONDS_WITH 至少 6 条')
    expect(
      constraintSummary({ ...base, type: 'max_count', relation: ['TRIGGERS'], max: 1 }),
    ).toBe('TRIGGERS 最多 1 条')
    expect(
      constraintSummary({ ...base, type: 'required_attribute', attribute: 'timing' }),
    ).toBe('必须填 timing')
    expect(
      constraintSummary({ ...base, type: 'coverage', relation: ['RESPONDS_WITH'] }),
    ).toBe('RESPONDS_WITH 要逐条覆盖原文条目')
  })
})
