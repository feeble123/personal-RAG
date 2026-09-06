// rehype 插件：把 Markdown 正文里的引用标记 [n] 转成可点击角标 <cite class="cite-mark" data-cite="n">。
//
// 背景：后端 prompt 强制 LLM 在正文用 [1]、[2] 标注引用（[n] 精确对应第 n 条资料）。
// 本插件在 rehype 阶段（Markdown 已解析为 HAST）把合法的 [n] 文本节点拆成
// 「普通文本 + <cite> 角标节点」，其余结构（标题/列表/表格/公式）原样保留。
//
// 跳过三类节点，避免误伤：
//   1. code / pre —— 代码块里的 [1] 是代码，不是引用
//   2. katex / math —— 公式内容里的字面 [1]
//   3. 编号越界（n 不在 [1, count]）的 [n] 保持原文不动

interface HastText {
  type: 'text'
  value: string
}

interface HastElement {
  type: 'element'
  tagName: string
  properties: Record<string, unknown>
  children: HastNode[]
}

type HastNode = HastText | HastElement | { type: string; children?: HastNode[]; value?: string }

export interface RehypeInlineCitationsOptions {
  count: number // 合法引用数量（= citations.length），超出此范围的 [n] 不转
}

const IS_SKIP = new Set(['code', 'pre'])

function hasClass(node: HastNode, fragment: string): boolean {
  const el = node as HastElement
  if (!el.properties || !el.properties.className) return false
  const cls = el.properties.className
  const list = Array.isArray(cls) ? cls : [cls]
  return list.some((c) => typeof c === 'string' && c.includes(fragment))
}

// 把单个文本节点拆成「文本 + 角标」序列；无合法引用时返回 null（表示无需改动）
function splitText(value: string, count: number): HastNode[] | null {
  const re = /\[(\d+)\]/g
  const matches: RegExpExecArray[] = []
  let m: RegExpExecArray | null
  while ((m = re.exec(value)) !== null) matches.push(m)
  if (matches.length === 0) return null

  const out: HastNode[] = []
  let last = 0
  for (const mm of matches) {
    const n = Number(mm[1])
    const valid = n >= 1 && n <= count
    if (mm.index > last) out.push({ type: 'text', value: value.slice(last, mm.index) })
    if (valid) {
      out.push({
        type: 'element',
        tagName: 'cite',
        properties: { className: ['cite-mark'], 'data-cite': String(n) },
        children: [{ type: 'text', value: mm[1] }],
      } as HastElement)
    } else {
      // 越界编号保留原文字，不做角标（可能是 LLM 偶发的笔误，交给正文原样显示）
      out.push({ type: 'text', value: mm[0] })
    }
    last = mm.index + mm[0].length
  }
  if (last < value.length) out.push({ type: 'text', value: value.slice(last) })
  return out
}

function walk(node: HastNode, count: number): void {
  if (!node || typeof node !== 'object') return
  const el = node as HastElement
  // 跳过代码块 / 内联代码 / 公式（含 katex/math 类名）
  if (IS_SKIP.has(el.tagName)) return
  if (hasClass(node, 'katex') || hasClass(node, 'math')) return

  const children = (el as HastElement).children
  if (!Array.isArray(children)) return

  for (let i = 0; i < children.length; i++) {
    const child = children[i]
    if (child.type === 'text') {
      const replaced = splitText((child as HastText).value, count)
      if (replaced) {
        children.splice(i, 1, ...replaced)
        i += replaced.length - 1
      }
    } else if (child.type === 'element') {
      walk(child, count)
    }
  }
}

export default function rehypeInlineCitations(
  options: RehypeInlineCitationsOptions,
): (tree: HastNode) => void {
  const { count } = options
  return (tree) => walk(tree, count)
}
