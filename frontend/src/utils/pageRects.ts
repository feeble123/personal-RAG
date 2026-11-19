import type { SourceRect } from '@/api/types'

/** 画在页面上的一个高亮块（CSS 像素，相对页面左上角）。 */
export interface OverlayBox {
  left: number
  top: number
  width: number
  height: number
}

/**
 * 把 PDF 点坐标的矩形换算成页面上的 CSS 像素。
 *
 * 后端返回的 rects 用的是 PDF 页面坐标系（单位点，1/72 英寸），前端按
 * `渲染宽度 / 页面宽度` 等比缩放。缩放系数只取宽度方向——PDF 页面等比
 * 渲染，高度自然一致；分开算会在浮点误差下把框画歪半像素。
 */
export function scaleRects(
  rects: SourceRect[] | null | undefined,
  pageWidthInPoints: number,
  renderedWidth: number,
): OverlayBox[] {
  if (!rects?.length || !(pageWidthInPoints > 0) || !(renderedWidth > 0)) return []
  const k = renderedWidth / pageWidthInPoints
  return rects.map(([x0, y0, x1, y1]) => ({
    left: x0 * k,
    top: y0 * k,
    width: Math.max(1, (x1 - x0) * k),
    height: Math.max(1, (y1 - y0) * k),
  }))
}

/**
 * 高亮块是否"值得画"：整页那么大的框等于没框，反而挡住正文。
 *
 * 扫描件退回块级 bbox 时会出现接近整页的框（一张跨页大表），
 * 这时前端改成"只提示不遮盖"更合适。
 */
export function isWholePageBox(box: OverlayBox, pageHeightPx: number): boolean {
  return pageHeightPx > 0 && box.height / pageHeightPx > 0.85
}
