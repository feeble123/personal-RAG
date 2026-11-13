/**
 * 图谱画布的视觉规则（颜色、半径、绘制）。
 *
 * 颜色一律取自 `index.css` 的设计 token，不在这里另起一套色值——
 * 这样全局换主题时图也跟着变，符合"勿散写硬编码色"的约定。
 */
import type { OntologyGraphNode } from '@/api/types'

export interface GraphNode extends OntologyGraphNode {
  /** force-graph 默认按 `id` 认节点；我们的主键叫 uid，必须显式给一份 */
  id: string
  x?: number
  y?: number
  vx?: number
  vy?: number
  fx?: number
  fy?: number
  /** 相连的边数，用来决定节点大小 */
  degree?: number
}

export interface GraphLink {
  id: string
  source: string | GraphNode
  target: string | GraphNode
  predicate: string
  status: string
  confidence: number
  evidence_text: string | null
}

function cssVar(name: string, fallback: string): string {
  if (typeof document === 'undefined') return fallback
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}

/**
 * 品牌色盘：按实体类型顺序取色。
 * 前 4 个取设计 token，后面几个是同色系的补充，保证 7~8 类实体也能区分开。
 */
export function palette(): string[] {
  return [
    cssVar('--brand', '#00c6ff'),
    cssVar('--brand-accent', '#7a6bff'),
    cssVar('--brand-success', '#2ee6b8'),
    cssVar('--brand-danger', '#ff5c7a'),
    '#ffb347',
    '#5ad1ff',
    '#c86bff',
    '#8ee36b',
  ]
}

/**
 * 类型 → 颜色。
 *
 * 注意：图里有 TBox 之外的实体类型（防洪链的 engineering_object / admin_region 等）。
 * 索引找不到时要给它**自己的**颜色，不能都退到同一个——否则整张图一个色，看不出分类。
 */
export function colorOf(entityType: string, typeOrder: string[]): string {
  const colors = palette()
  const idx = typeOrder.indexOf(entityType)
  if (idx >= 0) return colors[idx % colors.length]
  let hash = 0
  for (let i = 0; i < entityType.length; i += 1) hash = (hash * 31 + entityType.charCodeAt(i)) >>> 0
  return colors[hash % colors.length]
}

function hexToRgba(hex: string, alpha: number): string {
  const h = hex.replace('#', '')
  const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h
  const r = parseInt(full.slice(0, 2), 16)
  const g = parseInt(full.slice(2, 4), 16)
  const b = parseInt(full.slice(4, 6), 16)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

/**
 * 节点半径（图形单位，不是像素——屏幕上会随缩放变化）。
 * 参数节点大多没有边，靠度数区分意义不大，所以基础值小、上限也压住，
 * 否则挤在一起时整张图会糊成一团光斑。
 */
export function nodeRadius(node: GraphNode): number {
  return 4 + Math.min(4, Math.sqrt(node.degree ?? 0) * 1.4)
}

export interface PaintOptions {
  color: string
  dimmed: boolean
  selected: boolean
  hovered: boolean
  showLabel: boolean
}

/** 画一个节点：光晕 + 实心核 + 高亮环 + 标签。 */
export function paintNode(
  node: GraphNode,
  ctx: CanvasRenderingContext2D,
  globalScale: number,
  opts: PaintOptions,
): void {
  const x = node.x ?? 0
  const y = node.y ?? 0
  const base = nodeRadius(node)
  const radius = opts.hovered || opts.selected ? base * 1.35 : base
  const alpha = opts.dimmed ? 0.18 : 1

  // 外层光晕：越靠中心越亮，营造「粒子在发光」的感觉
  const glow = ctx.createRadialGradient(x, y, 0, x, y, radius * 1.9)
  glow.addColorStop(0, hexToRgba(opts.color, 0.5 * alpha))
  glow.addColorStop(0.5, hexToRgba(opts.color, 0.12 * alpha))
  glow.addColorStop(1, hexToRgba(opts.color, 0))
  ctx.beginPath()
  ctx.arc(x, y, radius * 1.9, 0, Math.PI * 2)
  ctx.fillStyle = glow
  ctx.fill()

  // 实心核
  ctx.beginPath()
  ctx.arc(x, y, radius, 0, Math.PI * 2)
  ctx.fillStyle = hexToRgba(opts.color, 0.85 * alpha)
  ctx.fill()

  // 选中/悬停：加一圈亮环
  if (opts.selected || opts.hovered) {
    ctx.beginPath()
    ctx.arc(x, y, radius + 3 / globalScale, 0, Math.PI * 2)
    ctx.strokeStyle = hexToRgba(opts.color, 0.9 * alpha)
    ctx.lineWidth = 1.4 / globalScale
    ctx.stroke()
  }

  if (!opts.showLabel || alpha < 0.5) return
  const label = node.display || node.name
  // 标签随缩放变大，但图上缩得很小时不画（否则密集区域糊成一团）
  // 屏幕上的字号保持 ~10px：画布坐标要除以缩放，否则缩得越小字越看不见
  const fontSize = Math.min(26, Math.max(10 / globalScale, 2.5))
  ctx.font = `${fontSize}px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif`
  ctx.textAlign = 'center'
  ctx.textBaseline = 'top'
  const textY = y + radius + 4 / globalScale

  // 深色药丸底：标签压在连线/光晕上也能看清
  const padding = 2.5 / globalScale
  const width = ctx.measureText(label).width + padding * 2
  const height = fontSize + padding * 2
  ctx.fillStyle = `rgba(6, 12, 26, ${0.62 * alpha})`
  ctx.beginPath()
  ctx.roundRect(x - width / 2, textY - padding, width, height, 3 / globalScale)
  ctx.fill()

  ctx.fillStyle = opts.selected ? 'rgba(235, 247, 255, 0.98)' : 'rgba(210, 232, 255, 0.8)'
  ctx.fillText(label, x, textY)
}

// ---------- 分组轮廓（把同类节点圈在一个区域，思路取自 G6 的 combo） ----------

type Point = { x: number; y: number }

/** Andrew 单调链求凸包 */
function convexHull(points: Point[]): Point[] {
  if (points.length < 3) return points
  const sorted = [...points].sort((a, b) => (a.x === b.x ? a.y - b.y : a.x - b.x))
  const cross = (o: Point, a: Point, b: Point) =>
    (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x)
  const lower: Point[] = []
  for (const p of sorted) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) {
      lower.pop()
    }
    lower.push(p)
  }
  const upper: Point[] = []
  for (const p of [...sorted].reverse()) {
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) {
      upper.pop()
    }
    upper.push(p)
  }
  return lower.slice(0, -1).concat(upper.slice(0, -1))
}

/** 用中点二次贝塞尔把折线磨圆，避免出现硬邦邦的多边形 */
function smoothPath(ctx: CanvasRenderingContext2D, hull: Point[]): void {
  if (hull.length < 3) return
  ctx.beginPath()
  const first = hull[0]
  const last = hull[hull.length - 1]
  ctx.moveTo((first.x + last.x) / 2, (first.y + last.y) / 2)
  for (let i = 0; i < hull.length; i += 1) {
    const current = hull[i]
    const next = hull[(i + 1) % hull.length]
    ctx.quadraticCurveTo(current.x, current.y, (current.x + next.x) / 2, (current.y + next.y) / 2)
  }
  ctx.closePath()
}

/**
 * 给每个实体类型画一层背景光圈 + 类型名。
 * 单点/两点的分组凸包退化，用圆代替，保证「每个类型都有自己的一块地」。
 */
export function paintGroupHulls(
  ctx: CanvasRenderingContext2D,
  nodes: GraphNode[],
  colorFor: (entityType: string) => string,
  globalScale: number,
  labelFor: (entityType: string) => string = (t) => t,
): void {
  const groups = new Map<string, GraphNode[]>()
  for (const node of nodes) {
    if (node.x === undefined || node.y === undefined) continue
    const list = groups.get(node.entity_type) ?? []
    list.push(node)
    groups.set(node.entity_type, list)
  }

  for (const [entityType, members] of groups) {
    const color = colorFor(entityType)
    const points = members.map((n) => ({ x: n.x as number, y: n.y as number }))
    const pad = 26 + (globalScale < 1 ? 0 : 6)
    const hull = convexHull(points)

    ctx.save()
    if (hull.length >= 3) {
      // 外扩一点，让轮廓包住节点而不是贴着
      const cx = points.reduce((s, p) => s + p.x, 0) / points.length
      const cy = points.reduce((s, p) => s + p.y, 0) / points.length
      const expanded = hull.map((p) => {
        const dx = p.x - cx
        const dy = p.y - cy
        const len = Math.hypot(dx, dy) || 1
        return { x: p.x + (dx / len) * pad, y: p.y + (dy / len) * pad }
      })
      smoothPath(ctx, expanded)
      ctx.fillStyle = `${color}0f` // 约 6% 透明度
      ctx.fill()
      ctx.strokeStyle = `${color}44`
      ctx.lineWidth = 1 / globalScale
      ctx.stroke()
    } else {
      const [only] = points
      ctx.beginPath()
      ctx.arc(only.x, only.y, pad + 8, 0, Math.PI * 2)
      ctx.fillStyle = `${color}0c`
      ctx.fill()
      ctx.strokeStyle = `${color}33`
      ctx.lineWidth = 1 / globalScale
      ctx.stroke()
    }

    // 类型名 + 数量，画在分组中心上方
    const cx = points.reduce((s, p) => s + p.x, 0) / points.length
    const topY = Math.min(...points.map((p) => p.y)) - pad - 10 / globalScale
    const fontSize = Math.max(10 / globalScale, 2.6)
    ctx.font = `600 ${fontSize}px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'bottom'
    ctx.fillStyle = `${color}cc`
    ctx.fillText(`${labelFor(entityType)} · ${members.length}`, cx, topY)
    ctx.restore()
  }
}

/** 悬停点选判定用（force-graph 需要一个矩形命中区域） */
export function paintPointerArea(
  node: GraphNode,
  color: string,
  ctx: CanvasRenderingContext2D,
): void {
  const r = nodeRadius(node) * 1.6
  ctx.fillStyle = color
  ctx.beginPath()
  ctx.arc(node.x ?? 0, node.y ?? 0, r, 0, Math.PI * 2)
  ctx.fill()
}

// ---------- 分层布局（自己算坐标，不用 d3-dag） ----------

/**
 * 为什么不用 force-graph 自带的 dagMode：
 * 它会给**每个孤立节点单独分配一层**，13 个还没连线的参数就被排成一竖列，
 * 把真正有结构的子图压成一条细带。这里自己分层：
 *   · 有连接的节点 → 按 BFS 深度分列（左→右），同层内上下排开
 *   · 孤立节点      → 单独放在最左一列（多列折行），不参与分层
 */
export function layoutLayered(nodes: GraphNode[], links: GraphLink[]): void {
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const outgoing = new Map<string, string[]>()
  const indegree = new Map<string, number>()
  const touched = new Set<string>()
  for (const n of nodes) indegree.set(n.id, 0)

  for (const link of links) {
    const s = typeof link.source === 'string' ? link.source : (link.source as GraphNode).id
    const t = typeof link.target === 'string' ? link.target : (link.target as GraphNode).id
    if (!byId.has(s) || !byId.has(t) || s === t) continue
    outgoing.set(s, [...(outgoing.get(s) ?? []), t])
    indegree.set(t, (indegree.get(t) ?? 0) + 1)
    touched.add(s)
    touched.add(t)
  }

  const connected = nodes.filter((n) => touched.has(n.id))
  const isolated = nodes.filter((n) => !touched.has(n.id))

  // BFS 分层：从入度为 0 的节点出发，环里的节点兜底按已访问处理
  const depth = new Map<string, number>()
  const queue: string[] = []
  for (const n of connected) {
    if ((indegree.get(n.id) ?? 0) === 0) {
      depth.set(n.id, 0)
      queue.push(n.id)
    }
  }
  if (queue.length === 0 && connected.length > 0) {
    depth.set(connected[0].id, 0)  // 全图成环时随便找个起点
    queue.push(connected[0].id)
  }
  let guard = 0
  while (queue.length && guard < nodes.length * 4) {
    guard += 1
    const id = queue.shift() as string
    const d = depth.get(id) ?? 0
    for (const next of outgoing.get(id) ?? []) {
      if (depth.has(next)) continue
      depth.set(next, d + 1)
      queue.push(next)
    }
  }
  // 环里没被 BFS 覆盖到的节点，挂到已访问邻居的下一层
  for (const n of connected) {
    if (!depth.has(n.id)) depth.set(n.id, 1)
  }

  const layers = new Map<number, GraphNode[]>()
  for (const n of connected) {
    const d = depth.get(n.id) ?? 0
    layers.set(d, [...(layers.get(d) ?? []), n])
  }

  const COL_GAP = 230
  const ROW_GAP = 86
  for (const [d, members] of layers) {
    members.forEach((node, index) => {
      node.fx = (d - 1) * COL_GAP
      node.fy = (index - (members.length - 1) / 2) * ROW_GAP
    })
  }

  // 孤立节点：最左一列（每列最多 7 个，超出折到更左）
  const PER_COLUMN = 7
  isolated.forEach((node, index) => {
    const column = Math.floor(index / PER_COLUMN)
    const row = index % PER_COLUMN
    const inColumn = Math.min(PER_COLUMN, isolated.length - column * PER_COLUMN)
    node.fx = -2 * COL_GAP - column * COL_GAP * 0.7
    node.fy = (row - (inColumn - 1) / 2) * ROW_GAP
  })
}
