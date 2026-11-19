import { describe, expect, it } from 'vitest'
import { isWholePageBox, scaleRects } from './pageRects'

describe('scaleRects', () => {
  it('按渲染宽度等比缩放 PDF 点坐标', () => {
    // 页面 464.6 点宽，渲染成 929px → 系数 2
    const boxes = scaleRects([[10, 20, 110, 40]], 464.6, 929.2)
    expect(boxes).toHaveLength(1)
    expect(boxes[0].left).toBeCloseTo(20, 5)
    expect(boxes[0].top).toBeCloseTo(40, 5)
    expect(boxes[0].width).toBeCloseTo(200, 5)
    expect(boxes[0].height).toBeCloseTo(40, 5)
  })

  it('退化的框至少留 1px，否则 CSS 里直接消失', () => {
    const [box] = scaleRects([[10, 20, 10, 20]], 464.6, 464.6)
    expect(box.width).toBe(1)
    expect(box.height).toBe(1)
  })

  it('空输入与非法尺寸返回空数组', () => {
    expect(scaleRects(null, 464, 900)).toEqual([])
    expect(scaleRects([], 464, 900)).toEqual([])
    expect(scaleRects([[1, 2, 3, 4]], 0, 900)).toEqual([])
    expect(scaleRects([[1, 2, 3, 4]], 464, 0)).toEqual([])
  })
})

describe('isWholePageBox', () => {
  it('整页大的框判为"别遮住"', () => {
    expect(isWholePageBox({ left: 0, top: 10, width: 900, height: 900 }, 1000)).toBe(true)
  })
  it('正常段落框保留', () => {
    expect(isWholePageBox({ left: 0, top: 10, width: 900, height: 120 }, 1000)).toBe(false)
  })
})
