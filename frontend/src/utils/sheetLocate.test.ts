import { describe, expect, it } from 'vitest'
import { findRowMatch } from './sheetLocate'

const ROWS = [
  ['序号', '方案名称', '报送监理时间', '备注'],
  ['1', '质量保证体系', '2026-03-01', ''],
  ['2', '高支模专项施工方案', '2026-03-08', '已归档'],
  ['3', '临时用电方案', '2026-03-12', ''],
]

describe('findRowMatch', () => {
  it('定位到**数据行**，不是表头（每张表一排黄底就是这么来的）', () => {
    const hit = findRowMatch(ROWS, '序号 | 方案名称 | 报送监理时间\n2 | 高支模专项施工方案 | 2026-03-08 | 已归档')
    expect(hit?.row).toBe(2)
    expect(hit?.cols).toEqual([1, 2, 3])
  })

  it('切片只写了表头时不高亮（无信息量，宁可不动）', () => {
    expect(findRowMatch(ROWS, '序号 | 方案名称 | 报送监理时间 | 备注')).toBeNull()
  })

  it('只命中一个词时宁可不标', () => {
    expect(findRowMatch(ROWS, '凑巧出现的词')).toBeNull()
  })

  it('没有引用、没有行时返回 null', () => {
    expect(findRowMatch(ROWS, null)).toBeNull()
    expect(findRowMatch([], '质量保证体系')).toBeNull()
  })

  it('两个词分别落在不同行、各中一个时返回 null（不硬指）', () => {
    expect(findRowMatch(ROWS, '临时用电方案 | 已归档')).toBeNull()
  })

  it('表头词与数据值同名时，仍能选出真正的数据行', () => {
    const rows = [
      ['名称', '数量', '单位'],
      ['动力配电箱', '3', '台'],
      ['照明配电箱', '6', '台'],
    ]
    const hit = findRowMatch(rows, '名称 | 数量 | 单位\n照明配电箱 | 6 | 台')
    expect(hit?.row).toBe(2)
  })
})
