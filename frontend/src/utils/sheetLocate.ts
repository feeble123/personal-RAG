/**
 * Excel 表格里定位"切片说的是哪一行"。
 *
 * 踩过的坑：切片首行是**表头**（`序号 | 方案名称 | 审批时间 …`），而表头里那些词
 * （序号、备注、时间…）在每一行都能匹配到——按"命中词最多"选行，结果每张表都
 * 选中表头，界面上就是"每张表都有一排黄底"。这不是高亮，是误判。
 *
 * 现在的规则：
 * 1. 表头里出现过的词**不具区分度**，先从匹配词里剔除；
 * 2. 只在**数据行**里找，表头不参与；
 * 3. 剔除后没有可用的词（说明切片本身就是表头）→ 不高亮，宁可不标也不误导。
 */

export interface RowMatch {
  /** 行下标（0 起，含表头行） */
  row: number
  /** 命中的列下标 */
  cols: number[]
}

function tokens(text: string): string[] {
  return text
    .split(/[\s|｜]+/)
    .map((t) => t.trim())
    .filter((t) => t.length >= 2)
}

/**
 * 找出与切片最匹配的**数据行**。
 *
 * - 同一行包含全部可用词 → 直接命中（切片通常就是这一行拼出来的）；
 * - 否则取命中词最多的一行，但至少要中 2 个词；
 * - 都不满足 → null，界面不画高亮。
 */
export function findRowMatch(rows: string[][], quote: string | null | undefined): RowMatch | null {
  if (!rows.length || !quote) return null

  const header = rows[0] ?? []
  const headerText = header.join('\u0001')
  const headerWords = new Set(tokens(header.join(' | ')))

  // 表头里出现过的词，任何一行都能"命中"，对定位没有信息量
  const words = tokens(quote).filter(
    (w) => !headerWords.has(w) && !headerText.includes(w),
  )
  // 切片只有表头（或全都撞在表头词上）→ 无从定位，别乱标
  if (!words.length) return null

  let best: RowMatch | null = null
  let bestScore = 0

  // 从第 1 行开始：第 0 行是表头，不参与
  for (let r = 1; r < rows.length; r++) {
    const cols: number[] = []
    let score = 0
    for (const word of words) {
      const at = rows[r].findIndex((cell) => (cell ?? '').includes(word))
      if (at >= 0) {
        score += 1
        if (!cols.includes(at)) cols.push(at)
      }
    }
    if (score === words.length) return { row: r, cols }
    if (score > bestScore) {
      bestScore = score
      best = { row: r, cols }
    }
  }
  // 只命中一个词说明不了什么，宁可不标
  return bestScore >= 2 ? best : null
}
