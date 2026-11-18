/**
 * 在原文切片里定位「引用那句话」。
 *
 * 先按原文精确找；找不到就**忽略空白**再找一遍。
 * 为什么要有第二遍：PDF 抽出来的换行/空格位置很不稳定，
 * 后端校验引用时也是这么比的（`anchoring._normalize` 去掉所有空白），
 * 前端要是只做精确匹配，就会出现「有的高亮、有的不高亮」——
 * 明明后端已经验证过这句话在原文里。
 *
 * 找不到就返回 null，界面原样显示（不假装高亮成功）。
 */
export function findQuoteRange(
  content: string,
  quote?: string | null,
): [number, number] | null {
  const needle = (quote ?? '').trim()
  if (!needle || !content) return null

  const exact = content.indexOf(needle)
  if (exact >= 0) return [exact, exact + needle.length]

  // 去空白匹配：记录归一化后每个字符在原文里的下标
  const map: number[] = []
  let normalized = ''
  for (let i = 0; i < content.length; i += 1) {
    const ch = content[i]
    if (!/\s/.test(ch)) {
      normalized += ch
      map.push(i)
    }
  }
  const needleNormalized = needle.replace(/\s+/g, '')
  if (!needleNormalized) return null

  const pos = normalized.indexOf(needleNormalized)
  if (pos < 0) return null
  const start = map[pos]
  const end = map[pos + needleNormalized.length - 1] + 1
  return [start, end]
}
