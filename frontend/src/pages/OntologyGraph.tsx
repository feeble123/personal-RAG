/**
 * 知识图谱总览页（F1）。
 *
 * 数据：节点来自本体实例（PG 真相源），边来自图库（关系暂只存在于图里）。
 * 手感：力导向 + 粒子发光 + 悬停放大 + 点击看证据，力求「活着」而不是一张静态图。
 *
 * 显示细节拆在同目录 graph/ 下：
 *   - canvasTheme  颜色（取 CSS token）、节点半径、画布绘制
 *   - NodeDrawer  右侧详情面板（属性 / 证据 / 邻居）
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert, App, Button, Empty, Layout, Select, Space, Spin, Tag, Tooltip, Typography } from 'antd'
import {
  ApartmentOutlined,
  ArrowLeftOutlined,
  NodeIndexOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import ForceGraph2D from 'react-force-graph-2d'
import type { ForceGraphMethods } from 'react-force-graph-2d'
import UserMenu from '@/components/UserMenu'
import OntologyLibrarySelect from '@/components/OntologyLibrarySelect'
import { errMsg } from '@/api/client'
import { ontologyApi } from '@/api/modules'
import { useOntologyLibrary } from '@/stores/ontologyLibrary'
import LinkModal from './graph/LinkModal'
import { colorOf, layoutLayered, paintGroupHulls, paintNode, paintPointerArea } from './graph/canvasTheme'
import type { GraphLink, GraphNode } from './graph/canvasTheme'
import NodeDrawer from './graph/NodeDrawer'

const { Header, Content } = Layout
const { Text } = Typography

export default function OntologyGraph() {
  const navigate = useNavigate()
  const wrapRef = useRef<HTMLDivElement>(null)
  const graphRef = useRef<ForceGraphMethods<GraphNode, GraphLink> | undefined>(undefined)
  const [size, setSize] = useState({ width: 0, height: 0 })
  const [entityTypes, setEntityTypes] = useState<string[]>([])
  const [selectedUid, setSelectedUid] = useState<string | null>(null)
  const [hoveredUid, setHoveredUid] = useState<string | null>(null)
  const lastPointer = useRef<{ x: number; y: number } | null>(null)
  const [layoutMode, setLayoutMode] = useState<'dag' | 'force'>('dag')
  const [linking, setLinking] = useState(false)
  const [linkPair, setLinkPair] = useState<{ subject: GraphNode; object: GraphNode } | null>(null)
  const [linkFrom, setLinkFrom] = useState<GraphNode | null>(null)
  const queryClient = useQueryClient()
  const { message } = App.useApp()

  const { profileId } = useOntologyLibrary()
  const typesQuery = useQuery({
    queryKey: ['ontology-types', profileId],
    queryFn: () => ontologyApi.types(profileId || undefined),
  })
  const graphQuery = useQuery({
    queryKey: ['ontology-graph', profileId, entityTypes],
    queryFn: () =>
      ontologyApi.graph({
        profile: profileId || undefined,
        entity_type: entityTypes.length ? entityTypes : undefined,
      }),
  })

  // 中文名优先取 TBox 声明（覆盖没有实例的类型），其次取图里出现过的类型
  const labelOfType = (type: string) =>
    (typesQuery.data?.entity_types ?? []).find((t) => t.name === type)?.display_name ||
    typeLabels[type] ||
    type

  // 图里出现过、但 TBox 没声明的类型（防洪链那些）。
  // 必须**累积记住**：筛选后响应里只剩被选中的类型，如果每次按当前数据显示，
  // 筛成「参数」之后其他类型就从下拉里消失了，再也没法选第二个（实测 bug）。
  const [extraTypes, setExtraTypes] = useState<string[]>([])
  // 每个类型的「总数量」：拿历次响应里的最大值。筛选后响应只剩部分类型，
  // 直接用当前数据算数量会让没被选中的类型显示 0，图例跟着缩水。
  const [typeCounts, setTypeCounts] = useState<Record<string, number>>({})
  // 类型中文名同样要累积：筛选后的响应只带被选中类型的中文名
  const [typeLabels, setTypeLabels] = useState<Record<string, string>>({})
  useEffect(() => {
    const incoming = [...new Set((graphQuery.data?.nodes ?? []).map((n) => n.entity_type))]
    setExtraTypes((prev) => {
      const merged = new Set([...prev, ...incoming])
      return merged.size === prev.length ? prev : [...merged]
    })
    const counts: Record<string, number> = {}
    for (const node of graphQuery.data?.nodes ?? []) {
      counts[node.entity_type] = (counts[node.entity_type] ?? 0) + 1
    }
    const labels = graphQuery.data?.type_labels ?? {}
    setTypeLabels((prev) => {
      const merged = { ...prev, ...labels }
      return Object.keys(merged).length === Object.keys(prev).length ? prev : merged
    })
    setTypeCounts((prev) => {
      const merged = { ...prev }
      let changed = false
      for (const [type, count] of Object.entries(counts)) {
        if ((merged[type] ?? 0) < count) {
          merged[type] = count
          changed = true
        }
      }
      return changed ? merged : prev
    })
  }, [graphQuery.data])

  // 换图谱库 = 换一整套类型：累积状态（类型清单/数量/中文名）和筛选选择都要清掉，
  // 否则上一个库的类型会残留在图例里，点进去是空的。
  useEffect(() => {
    setExtraTypes([])
    setTypeCounts({})
    setTypeLabels({})
    setEntityTypes([])
  }, [profileId])

  // 颜色顺序：TBox 里声明的类型优先，其后接图里见过的类型
  const typeOrder = useMemo(() => {
    const declared = (typesQuery.data?.entity_types ?? []).map((t) => t.name)
    const seen = new Set(declared)
    return [...declared, ...extraTypes.filter((t) => !seen.has(t)).sort()]
  }, [typesQuery.data, extraTypes])

  // 容器尺寸：力导向图必须在 canvas 有宽高时才能正确居中
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect
      setSize({ width: Math.floor(width), height: Math.floor(height) })
    })
    observer.observe(el)
    const track = (e: PointerEvent) => {
      lastPointer.current = { x: e.clientX, y: e.clientY }
    }
    el.addEventListener('pointermove', track)
    return () => {
      observer.disconnect()
      el.removeEventListener('pointermove', track)
    }
  }, [])

  // force-graph 会往节点对象上写 x/y，所以每次数据变化都构造新对象
  const graphData = useMemo(() => {
    const raw = graphQuery.data
    if (!raw) return { nodes: [] as GraphNode[], links: [] as GraphLink[] }
    // 必须带 id：force-graph 用 id 解析连线端点，只给 uid 会报 "node not found" 并让所有连线失效
    const nodes: GraphNode[] = raw.nodes.map((n) => ({ ...n, id: n.uid, degree: 0 }))
    const byUid = new Map(nodes.map((n) => [n.uid, n]))
    const links: GraphLink[] = []
    for (const edge of raw.edges) {
      const source = byUid.get(edge.source)
      const target = byUid.get(edge.target)
      if (!source || !target) continue // 边的一端被过滤掉了
      links.push({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        predicate: edge.predicate,
        status: edge.status,
        confidence: edge.confidence,
        evidence_text: edge.evidence_text,
      })
      source.degree = (source.degree ?? 0) + 1
      target.degree = (target.degree ?? 0) + 1
    }
    return { nodes, links }
  }, [graphQuery.data])

  // 选中某个节点时，只有它和它的邻居保持明亮
  const highlight = useMemo(() => {
    const focus = selectedUid ?? hoveredUid
    if (!focus) return null
    const keep = new Set<string>([focus])
    for (const link of graphData.links) {
      const s = typeof link.source === 'string' ? link.source : link.source.uid
      const t = typeof link.target === 'string' ? link.target : link.target.uid
      if (s === focus) keep.add(t)
      if (t === focus) keep.add(s)
    }
    return keep
  }, [selectedUid, hoveredUid, graphData.links])

  const selectedNode = useMemo(
    () => graphData.nodes.find((n) => n.uid === selectedUid) ?? null,
    [graphData.nodes, selectedUid],
  )

  const neighbors = useMemo(() => {
    if (!selectedUid) return []
    const byUid = new Map(graphData.nodes.map((n) => [n.uid, n]))
    const out: { node: GraphNode; link: GraphLink; direction: 'out' | 'in' }[] = []
    for (const link of graphData.links) {
      const s = typeof link.source === 'string' ? link.source : link.source.uid
      const t = typeof link.target === 'string' ? link.target : link.target.uid
      if (s === selectedUid && byUid.get(t)) out.push({ node: byUid.get(t)!, link, direction: 'out' })
      if (t === selectedUid && byUid.get(s)) out.push({ node: byUid.get(s)!, link, direction: 'in' })
    }
    return out
  }, [graphData, selectedUid])

  // 力参数 + 按类型分簇 + 自动取景。
  // 依赖里必须带 size.width：首屏 canvas 还没挂载时 ref 是空的，只靠 graphData 会漏掉这次配置。
  useEffect(() => {
    const fg = graphRef.current
    if (!fg || graphData.nodes.length === 0) return

    // 分层布局：坐标由我们自己算好并钉死（fx/fy），不再跑力模拟。
    // 用 force-graph 自带的 dagMode 会把每个孤立节点单独排一层，图会被拉变形。
    if (layoutMode === 'dag') {
      fg.d3Force('cluster', null as never)
      fg.d3Force('collide', null as never)
      layoutLayered(graphData.nodes, graphData.links)
      fg.d3ReheatSimulation()
      const t = setTimeout(() => fg.zoomToFit(800, 70), 300)
      return () => clearTimeout(t)
    }
    // 切回力导向：清掉钉死的坐标，否则节点动不了
    for (const node of graphData.nodes) {
      node.fx = undefined
      node.fy = undefined
    }

    const charge = fg.d3Force('charge') as unknown as { strength?: (v: number) => void } | undefined
    charge?.strength?.(-180)
    const link = fg.d3Force('link') as unknown as
      | { distance?: (v: number) => void; strength?: (v: number) => void }
      | undefined
    link?.distance?.(60)
    // 跨簇的边会把两个簇拽到一起，把连线拉力压低，让聚簇力占主导
    link?.strength?.(0.15)

    // 分簇：每种实体类型一个锚点。
    //
    // 关键：**簇的摆放顺序按连通性排**（BFS），不按类型名的字典序。
    // 按字典序排时，关系最密的两个簇可能正好摆在对角，连线横穿整张图，看着又长又乱。
    const byUid = new Map(graphData.nodes.map((n) => [n.uid, n]))
    const types = [...new Set(graphData.nodes.map((n) => n.entity_type))]
    const neighbours = new Map<string, Set<string>>()
    for (const type of types) neighbours.set(type, new Set())
    for (const link of graphData.links) {
      const s = typeof link.source === 'string' ? link.source : link.source.uid
      const t = typeof link.target === 'string' ? link.target : link.target.uid
      const st = byUid.get(s)?.entity_type
      const tt = byUid.get(t)?.entity_type
      if (!st || !tt || st === tt) continue
      neighbours.get(st)?.add(tt)
      neighbours.get(tt)?.add(st)
    }
    // 从邻居最多的簇出发做 BFS：相连的簇排在相邻位置
    const seed = [...types].sort((a, b) => (neighbours.get(b)?.size ?? 0) - (neighbours.get(a)?.size ?? 0))[0]
    const ordered: string[] = []
    const visited = new Set<string>()
    const queue = seed ? [seed] : []
    while (queue.length) {
      const type = queue.shift() as string
      if (visited.has(type)) continue
      visited.add(type)
      ordered.push(type)
      queue.push(...[...(neighbours.get(type) ?? [])].filter((t) => !visited.has(t)))
    }
    ordered.push(...types.filter((t) => !visited.has(t)))  // 孤立的簇排在后面

    // 两块地：
    // · 有关系的簇 → 沿右侧弧线排开（读起来像一条流程）
    // · 没关系的簇（比如还没连线的参数）→ 在左边单独列队，不占弧线的位置
    // 全塞进一个整圆时，孤立簇会白占两个角度，把主线挤成歪的。
    const connected = ordered.filter((t) => (neighbours.get(t)?.size ?? 0) > 0)
    const isolated = ordered.filter((t) => (neighbours.get(t)?.size ?? 0) === 0)

    const rx = Math.max(300, connected.length * 110)
    const ry = Math.max(170, connected.length * 55)
    const anchors = new Map<string, { x: number; y: number }>()

    // 弧线：从 -110° 扫到 +110°（右侧大半圈），相邻簇挨着
    const sweep = (220 * Math.PI) / 180
    connected.forEach((type, index) => {
      const ratio = connected.length === 1 ? 0.5 : index / (connected.length - 1)
      const angle = -sweep / 2 + ratio * sweep
      anchors.set(type, { x: Math.cos(angle) * rx, y: Math.sin(angle) * ry })
    })

    // 孤立簇：左侧一列，垂直居中排开
    const columnX = -rx - 260
    const gap = 220
    isolated.forEach((type, index) => {
      const offset = (index - (isolated.length - 1) / 2) * gap
      anchors.set(type, { x: columnX, y: offset })
    })
    let clusterNodes: GraphNode[] = []
    const clusterForce = ((alpha: number) => {
      for (const node of clusterNodes) {
        const anchor = anchors.get(node.entity_type)
        if (!anchor) continue
        // 0.45 是试出来的分水岭：再弱会被斥力和跨簇连线抹平，同类又混到一起
        node.vx = (node.vx ?? 0) + (anchor.x - (node.x ?? 0)) * 0.45 * alpha
        node.vy = (node.vy ?? 0) + (anchor.y - (node.y ?? 0)) * 0.45 * alpha
      }
    }) as unknown as ((alpha: number) => void) & { initialize?: (nodes: GraphNode[]) => void }
    clusterForce.initialize = (nodes: GraphNode[]) => {
      clusterNodes = nodes
    }
    fg.d3Force('cluster', clusterForce as never)

    // 簇内碰撞：没有它，同类节点会挤成一团、标签全糊在一起。
    // 节点只有几十个，O(n²) 完全够用，不必引 d3-force 的 collide。
    let collideNodes: GraphNode[] = []
    const MIN_GAP = 26
    const collideForce = ((alpha: number) => {
      for (let i = 0; i < collideNodes.length; i += 1) {
        for (let j = i + 1; j < collideNodes.length; j += 1) {
          const a = collideNodes[i]
          const b = collideNodes[j]
          const dx = (b.x ?? 0) - (a.x ?? 0)
          const dy = (b.y ?? 0) - (a.y ?? 0)
          const dist = Math.hypot(dx, dy) || 0.01
          if (dist >= MIN_GAP) continue
          const push = ((MIN_GAP - dist) / dist) * 0.5 * alpha
          a.vx = (a.vx ?? 0) - dx * push
          a.vy = (a.vy ?? 0) - dy * push
          b.vx = (b.vx ?? 0) + dx * push
          b.vy = (b.vy ?? 0) + dy * push
        }
      }
    }) as unknown as ((alpha: number) => void) & { initialize?: (nodes: GraphNode[]) => void }
    collideForce.initialize = (nodes: GraphNode[]) => {
      collideNodes = nodes
    }
    fg.d3Force('collide', collideForce as never)

    fg.d3ReheatSimulation()
    // 等布局散开后取景，否则会按还没散开的初始坐标缩放到一个小点
    const timer = setTimeout(() => fg.zoomToFit(900, 60), 1800)
    return () => clearTimeout(timer)
  }, [graphData, size.width, layoutMode])

  // 拖线建关系：拖完松手时找落点附近最近的另一个节点，命中就弹小窗
  const handleDragEnd = useCallback(
    (node: GraphNode) => {
      if (!linking) return
      // 用「松手时的鼠标位置」换算图形坐标找落点：比用被拖节点自身坐标准，
      // 实测两者会差 100 多个单位（松手瞬间节点还在回弹）。
      const fg = graphRef.current
      const rect = wrapRef.current?.getBoundingClientRect()
      const pointer = lastPointer.current
      let origin = { x: node.x ?? 0, y: node.y ?? 0 }
      if (fg && rect && pointer) {
        const p = fg.screen2GraphCoords(pointer.x - rect.left, pointer.y - rect.top)
        if (Number.isFinite(p.x) && Number.isFinite(p.y)) origin = p
      }
      let best: GraphNode | null = null
      let bestDist = Infinity
      for (const other of graphData.nodes) {
        if (other.uid === node.uid) continue
        const dist = Math.hypot((other.x ?? 0) - origin.x, (other.y ?? 0) - origin.y)
        if (dist < bestDist) {
          bestDist = dist
          best = other
        }
      }
      if (best && bestDist < 45) setLinkPair({ subject: node, object: best })
      setLinking(false)
    },
    [linking, graphData.nodes],
  )

  const createRelation = useMutation({
    mutationFn: async (values: { predicate: string; note?: string }) => {
      if (!linkPair) throw new Error('没有选中两端')
      return ontologyApi.createRelation({
        subject_uid: linkPair.subject.uid,
        predicate: values.predicate,
        object_uid: linkPair.object.uid,
        profile_id: profileId || undefined,
        evidence: values.note
          ? [{ type: '人工录入', note: values.note, traceable: false, resolved_by: 'none' }]
          : [],
      })
    },
    onSuccess: () => {
      message.success('关系已建立')
      setLinkPair(null)
      queryClient.invalidateQueries({ queryKey: ['ontology-graph'] })
    },
    onError: (e) => message.error(errMsg(e)),
  })

  const focusNode = useCallback((uid: string) => {
    const node = graphData.nodes.find((n) => n.uid === uid)
    if (!node) return
    setSelectedUid(uid)
    graphRef.current?.centerAt(node.x, node.y, 600)
    graphRef.current?.zoom(2.2, 600)
  }, [graphData.nodes])

  const drawNode = useCallback(
    (node: GraphNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      paintNode(node, ctx, globalScale, {
        color: colorOf(node.entity_type, typeOrder),
        dimmed: highlight ? !highlight.has(node.uid) : false,
        selected: node.uid === selectedUid,
        hovered: node.uid === hoveredUid,
        // 缩得很小时不画标签，否则密集区域糊成一片；悬停/选中的永远显示
        // 标签去重叠：缩放小的时候只给「有关系」的节点显示标签，孤立节点/密集处靠悬停看
        // 分层布局位置是排好的，标签不会互相压，全部显示；
        // 力导向下空间拥挤，只给有关系的节点显示（孤立点靠悬停看名字）
        showLabel:
          layoutMode === 'dag' ||
          globalScale > 1.25 ||
          (node.degree ?? 0) > 0 ||
          node.uid === selectedUid ||
          node.uid === hoveredUid,
      })
    },
    [typeOrder, highlight, selectedUid, hoveredUid, layoutMode],
  )

  const stats = graphQuery.data
  const typeOptions = typeOrder.map((name) => ({
    value: name,
    label: (
      <Space size={6}>
        <span
          style={{
            display: 'inline-block',
            width: 8,
            height: 8,
            borderRadius: '50%',
            background: colorOf(name, typeOrder),
            boxShadow: `0 0 8px ${colorOf(name, typeOrder)}`,
          }}
        />
        {labelOfType(name)}
      </Space>
    ),
  }))

  return (
    <Layout style={{ height: '100vh' }}>
      <Header
        className="tech-line"
        style={{
          background: 'rgba(10, 17, 34, 0.5)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          borderBottom: '1px solid rgba(122, 190, 255, 0.1)',
          paddingInline: 24,
        }}
      >
        <Space>
          <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate('/chat')}>
            返回问答
          </Button>
          <ApartmentOutlined style={{ color: '#00c6ff', fontSize: 18 }} />
          <Text strong>知识图谱</Text>
          <Tag color="cyan">{stats?.profile_id ?? 'tuning'}</Tag>
          {stats && (
            <Text type="secondary" className="font-mono" style={{ fontSize: 12 }}>
              {stats.nodes.length} 节点 · {stats.edges.length} 关系
              {stats.truncated ? ' · 已截断' : ''}
            </Text>
          )}
        </Space>
        <Space>
          <Select
            mode="multiple"
            allowClear
            placeholder="按实体类型筛选"
            style={{ minWidth: 240 }}
            value={entityTypes}
            options={typeOptions}
            onChange={setEntityTypes}
            maxTagCount="responsive"
          />
          <Tooltip title="打开后，从一个节点拖到另一个节点即可建关系">
            <Button
              type={linking ? 'primary' : 'default'}
              icon={<NodeIndexOutlined />}
              onClick={() => {
                setLinking((v) => !v)
                setLinkFrom(null)
              }}
            >
              {linking
                ? linkFrom
                  ? `已选：${linkFrom.display || linkFrom.name}，再点一个目标`
                  : '连线中…点起点或直接拖'
                : '连线'}
            </Button>
          </Tooltip>
          <OntologyLibrarySelect allowCreate />
          <Button onClick={() => navigate('/')}>去问答</Button>
          <Button onClick={() => navigate('/ontology')}>本体管理</Button>
          <Button icon={<ReloadOutlined />} onClick={() => graphQuery.refetch()}>
            刷新
          </Button>
          <UserMenu />
        </Space>
      </Header>

      <Content style={{ position: 'relative', overflow: 'hidden' }} ref={wrapRef}>
        {graphQuery.isLoading && (
          <div style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center' }}>
            <Spin size="large" tip="正在加载图谱…" />
          </div>
        )}

        {!graphQuery.isLoading && graphData.nodes.length === 0 && (
          <div style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center' }}>
            <Empty description="还没有本体实例。等 F2 的「本体管理」上线后就能在这里建。" />
          </div>
        )}

        {size.width > 0 && graphData.nodes.length > 0 && (
          <ForceGraph2D<GraphNode, GraphLink>
            ref={graphRef}
            width={size.width}
            height={size.height}
            graphData={graphData}
            backgroundColor="transparent"
            nodeCanvasObject={drawNode}
            nodePointerAreaPaint={paintPointerArea}
            nodeRelSize={1}
            // 分组背景轮廓画在最底层（同类节点被圈在一块区域里）
            onRenderFramePre={(ctx, globalScale) => {
              // 分层布局里同类节点会跨列分布，画分组光圈只会互相盖住，干脆不画
              if (layoutMode !== 'force') return
              paintGroupHulls(
                ctx,
                graphData.nodes,
                (t) => colorOf(t, typeOrder),
                globalScale,
                (t) => graphQuery.data?.type_labels?.[t] ?? t,
              )
            }}
            // 关系线：弯曲 + 箭头（看得出方向）+ 流动粒子，比直线更接近 Neo4j Bloom 的观感
            linkColor={() => 'rgba(140, 205, 255, 0.55)'}
            linkWidth={1.6}
            linkCurvature={0.06}
            linkDirectionalArrowLength={4}
            linkDirectionalArrowRelPos={0.88}
            linkDirectionalArrowColor={() => 'rgba(160, 220, 255, 0.75)'}
            linkDirectionalParticles={2}
            linkDirectionalParticleWidth={2.6}
            linkDirectionalParticleSpeed={0.005}
            linkDirectionalParticleColor={() => 'rgba(0, 198, 255, 0.95)'}
            onNodeHover={(node) => setHoveredUid(node ? node.uid : null)}
            onNodeClick={(node) => {
              // 连线模式下：点一下选起点，再点另一个节点直接弹窗（比拖拽稳，也更好发现）
              if (linking) {
                if (!linkFrom) {
                  setLinkFrom(node)
                  setSelectedUid(node.uid)
                } else if (linkFrom.uid !== node.uid) {
                  setLinkPair({ subject: linkFrom, object: node })
                  setLinkFrom(null)
                  setLinking(false)
                }
                return
              }
              setSelectedUid(node.uid)
            }}
            onNodeDragEnd={handleDragEnd}
            onBackgroundClick={() => setSelectedUid(null)}
            cooldownTicks={layoutMode === 'dag' ? 0 : 200}
            warmupTicks={40}
            enableNodeDrag
          />
        )}

        {/* 左下图例：点一下就能只看这一类的节点 */}
        {graphData.nodes.length > 0 && (
          <div
            className="graph-float-card"
            style={{ position: 'absolute', left: 20, bottom: 20, pointerEvents: 'auto' }}
          >
            <Space direction="vertical" size={4}>
              <Tooltip
                title={
                  <span>
                    这些类别来自**已经入库的本体实例**：<br />
                    · 参数（Parameter）来自参数表 Excel<br />
                    · 工程对象 / 河流河段 / 行政区 / 管理单位 / 工况 / 措施 来自防洪链台账 Excel
                  </span>
                }
              >
                <Text type="secondary" style={{ fontSize: 11, cursor: 'help' }}>
                  点击图例只看某一类 ⓘ
                </Text>
              </Tooltip>
              {typeOrder.map((name) => {
                const active = entityTypes.length === 0 || entityTypes.includes(name)
                return (
                  <div
                    key={name}
                    onClick={() =>
                      setEntityTypes((prev) =>
                        prev.includes(name) ? prev.filter((t) => t !== name) : [...prev, name],
                      )
                    }
                    style={{
                      cursor: 'pointer',
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      opacity: active ? 1 : 0.4,
                    }}
                  >
                    <span
                      style={{
                        display: 'inline-block',
                        width: 8,
                        height: 8,
                        borderRadius: '50%',
                        background: colorOf(name, typeOrder),
                        boxShadow: `0 0 10px ${colorOf(name, typeOrder)}`,
                      }}
                    />
                    <Text style={{ fontSize: 12 }}>
                      {labelOfType(name)}
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        {' '}
                        {typeCounts[name] ?? 0}
                      </Text>
                    </Text>
                  </div>
                )
              })}
            </Space>
          </div>
        )}

        {/* 图库没开时如实提示：节点仍可看，关系边没有 */}
        {stats && !stats.graph_available && (
          <Alert
            type="warning"
            showIcon
            style={{ position: 'absolute', top: 16, left: 20, right: 20 }}
            message="图库未启用，当前只显示本体实例（没有关系边）"
            description={stats.note}
          />
        )}
      </Content>

      <LinkModal
        pair={linkPair}
        types={typesQuery.data}
        submitting={createRelation.isPending}
        onCancel={() => setLinkPair(null)}
        onSubmit={(values) => createRelation.mutate(values)}
      />

      <NodeDrawer
        node={selectedNode}
        color={selectedNode ? colorOf(selectedNode.entity_type, typeOrder) : '#00c6ff'}
        relationLabels={graphQuery.data?.relation_labels ?? {}}
        typeLabels={graphQuery.data?.type_labels ?? {}}
        neighbors={neighbors}
        onClose={() => setSelectedUid(null)}
        onPick={focusNode}
      />
    </Layout>
  )
}
