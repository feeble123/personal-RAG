/**
 * 在**已渲染的 DOM** 里把切片/引用那段话标出来。
 *
 * 为什么要跨节点匹配：Word 渲染出来是一堆 `<span>`，一句话常被 Word 的 run 边界
 * 拆成好几段，单看某个节点永远找不到整句。所以先把整棵树的文本节点摊平成一条
 * 字符流（记下每个字符属于哪个节点、第几个字），在字符流里找，再映射回去把
 * 命中的部分各自包成 `<mark>`。
 *
 * 三级策略，越往后越"退而求其次"：
 * 1. **整段**（忽略空白）——最忠实，标出来的就是切片原文；
 * 2. **切片里最长的一行/一句**——切片跨了多段（比如目录）时，至少标出最长的那段；
 * 3. 都不行就返回 null，让界面明说"没在原文里找到"，不做无意义的单字标记。
 */

/** 扁平字符流里的一段：属于哪个文本节点、在该节点里的起止下标 */
interface Piece {
  node: Text
  start: number
  end: number
}

function squash(text: string): string {
  return text.replace(/\s+/g, '')
}

/** 把 root 下所有文本节点摊平成 (归一化字符串, 每个字符的归属) */
function flatten(root: HTMLElement): { text: string; pieces: Piece[] } {
  const doc = root.ownerDocument
  const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const pieces: Piece[] = []
  const chars: string[] = []
  while (walker.nextNode()) {
    const node = walker.currentNode as Text
    const raw = node.nodeValue ?? ''
    let runStart = -1
    for (let i = 0; i < raw.length; i++) {
      if (/\s/.test(raw[i])) continue
      if (runStart < 0) runStart = i
      chars.push(raw[i])
      // 同一节点连续命中就并成一段，避免一句拆出几十个 piece
      const last = pieces[pieces.length - 1]
      if (last && last.node === node && last.end === i) {
        last.end = i + 1
      } else {
        pieces.push({ node, start: i, end: i + 1 })
      }
    }
    if (runStart < 0) continue
  }
  return { text: chars.join(''), pieces }
}

/** 把扁平下标还原成 (节点, 原始起点, 原始终点) */
function locate(pieces: Piece[], from: number, to: number): { node: Text; start: number; end: number }[] {
  const out: { node: Text; start: number; end: number }[] = []
  let cursor = 0
  for (const piece of pieces) {
    const len = piece.end - piece.start
    const pieceFrom = cursor
    const pieceTo = cursor + len
    cursor = pieceTo
    if (pieceTo <= from || pieceFrom >= to) continue
    const takeFrom = Math.max(from, pieceFrom) - pieceFrom
    const takeTo = Math.min(to, pieceTo) - pieceFrom
    const start = piece.start + takeFrom
    const end = piece.start + takeTo
    // 同一节点里被空白隔开的几段要**并成一段**再包，否则第二次包会用已经截短的
    // 节点内容，标出来的只剩前半截（"第7章 验收要求" 变成 "第7章"）
    const last = out[out.length - 1]
    const raw = piece.node.nodeValue ?? ''
    if (last && last.node === piece.node && /^\s*$/.test(raw.slice(last.end, start))) {
      last.end = end
      continue
    }
    out.push({ node: piece.node, start, end })
  }
  return out
}

//: 太短的片段（"技术措施"）在长文档里到处都是，标了反而误导
const MIN_SEGMENT = 6
//: 一次最多标多少段。原来是 12，结果一段 14 句的切片按长度排序后，
//: 短句排在后面被直接砍掉——用户看到的就是"中间那句没高亮"。
//: 这些段本来就是切片的内容，全部标出来才是对的，上限只防病态输入。
const MAX_MARKS = 60

const MARK_STYLE =
  'background:rgba(255,205,60,0.45);outline:1px solid rgba(230,140,0,0.9);border-radius:2px;padding:0 1px'

/** 在一段文本节点里把 [start,end) 包成 mark，返回生成的 mark */
function wrap(node: Text, start: number, end: number): HTMLElement {
  const doc = node.ownerDocument
  const raw = node.nodeValue ?? ''
  const mark = doc.createElement('mark')
  mark.style.cssText = MARK_STYLE
  mark.textContent = raw.slice(start, end)
  const rest = doc.createTextNode(raw.slice(end))
  node.nodeValue = raw.slice(0, start)
  node.parentNode?.insertBefore(mark, node.nextSibling)
  mark.parentNode?.insertBefore(rest, mark.nextSibling)
  return mark
}

/**
 * 切片开头的标题路径是**程序合成**的：`## 施工保证措施/技术措施/冬季施工措施`。
 * Word 原文里既没有 `##`，也没有把层级用 `/` 串起来——照原样去匹配永远对不上，
 * 于是只能退化成"标最长的一句"。这里把它还原成原文可能出现的样子。
 */
export function sanitizeQuote(quote: string): string {
  return quote
    .split(/\n/)
    .map((line) => line.replace(/^#+\s*/, ''))
    .join('\n')
}

/** 把引用切成"能在原文里独立成段"的候选：按行、句号、分号、竖线切 */
export function quoteSegments(quote: string | null | undefined): string[] {
  if (!quote) return []
  return quote
    .split(/[\n|｜。;；]+/)
    .map((s) => s.trim())
    .filter((s) => s.length >= 4)
    .sort((a, b) => b.length - a.length)
}

/**
 * 在已渲染的文档里标出引用，返回第一个 `<mark>`（供 scrollIntoView）。
 *
 * 返回值额外带 `exact`：整段命中为 true；退到"最长片段"时为 false，
 * 界面据此提示"这段话在原文里被分成多段，只标出了最长的一段"。
 */
export function markQuote(
  root: HTMLElement,
  quote: string | null | undefined,
): { mark: HTMLElement; exact: boolean } | null {
  if (!root || !quote) return null
  const { text, pieces } = flatten(root)
  if (!text) return null

  // 三个变体依次试：原样 → 去掉 markdown 标题记号 → 再把合成路径里的 `/` 去掉
  const cleaned = sanitizeQuote(quote)
  const variants = [
    quote,
    cleaned,
    cleaned.replace(/\s*\/\s*/g, ''),
  ].map(squash).filter((v, i, all) => v && all.indexOf(v) === i)

  for (const needle of variants) {
    const at = text.indexOf(needle)
    if (at < 0) continue
    const spans = locate(pieces, at, at + needle.length)
    // 倒序包：前面的节点先改，后面节点的下标就不受影响了
    const marks = [...spans].reverse().map((s) => wrap(s.node, s.start, s.end))
    return { mark: marks[marks.length - 1], exact: true }
  }

  // 退路：把切片里**每一段能在原文找到的**都标出来（而不是只标最长的那一段）。
  // 切片常带合成的标题路径（`## A/B/C`），而原文里这些标题是带编号的独立段落，
  // 整段永远拼不起来；只标一句会让用户觉得"高亮的不是切片内容"。
  const hits: { at: number; len: number }[] = []
  for (const segment of quoteSegments(cleaned)) {
    if (segment.length < MIN_SEGMENT) continue
    const needle = squash(segment)
    const at = text.indexOf(needle)
    if (at < 0) continue
    // 与已选区间重叠就跳过（防止某段是另一段的子串而重复包）
    if (hits.some((h) => at < h.at + h.len && h.at < at + needle.length)) continue
    hits.push({ at, len: needle.length })
    if (hits.length >= MAX_MARKS) break
  }
  if (!hits.length) return null

  // 从后往前包：包完后面的，前面节点的字符下标仍然有效
  hits.sort((a, b) => b.at - a.at)
  let earliest: HTMLElement | undefined
  for (const hit of hits) {
    const spans = locate(pieces, hit.at, hit.at + hit.len)
    const marks = [...spans].reverse().map((s) => wrap(s.node, s.start, s.end))
    earliest = marks[marks.length - 1]
  }
  return earliest ? { mark: earliest, exact: false } : null
}
