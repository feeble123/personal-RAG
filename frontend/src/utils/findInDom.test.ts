import { describe, expect, it } from 'vitest'
import { markQuote, quoteSegments, sanitizeQuote } from './findInDom'

function host(html: string): HTMLElement {
  const div = document.createElement('div')
  div.innerHTML = html
  return div
}

describe('quoteSegments', () => {
  it('按换行/句号/竖线切段，长度倒序，丢掉太短的', () => {
    const segs = quoteSegments('第一段内容比较长\n第二段\n的\n第三段也有些内容')
    expect(segs[0].length).toBe(8)
    expect(segs).toContain('第一段内容比较长')
    expect(segs).toContain('第三段也有些内容')
    expect(segs.every((s) => s.length >= 4)).toBe(true)
  })
})

describe('markQuote', () => {
  it('整段命中：标出来的就是切片原文本身', () => {
    const root = host('<p>下面这段是原文：高支模专项施工方案的编制依据是什么？请核对。</p>')
    const hit = markQuote(root, '高支模专项施工方案的编制依据')
    expect(hit?.exact).toBe(true)
    expect(hit?.mark.textContent).toBe('高支模专项施工方案的编制依据')
  })

  it('跨多个 run（Word 常见）：整句仍能完整标出', () => {
    const root = host('<p><span>高支模专项</span><span>施工方案 </span><span>的编制依据</span></p>')
    const hit = markQuote(root, '高支模专项施工方案的编制依据')
    expect(hit?.exact).toBe(true)
    const marks = [...root.querySelectorAll('mark')].map((m) => m.textContent)
    expect(marks.join('').replace(/\s+/g, '')).toBe('高支模专项施工方案的编制依据')
  })

  it('同一节点里被空格隔开也能整段标出', () => {
    const root = host('<p>第7章 验收要求</p>')
    const hit = markQuote(root, '第7章 验收要求')
    expect(hit?.exact).toBe(true)
    expect(hit?.mark.textContent?.replace(/\s+/g, '')).toBe('第7章验收要求')
  })

  it('切片跨了多段（如目录）：退到最长的一段，并标记 exact=false', () => {
    const root = host(
      '<p>第1章 工程概况 1.1 工程概况 1.2 模板支撑体系工程概况</p><p>第7章 验收要求 7.1 验收标准</p>',
    )
    const hit = markQuote(root, '第7章 验收要求\n7.1 验收标准\n7.2 验收程序及人员')
    expect(hit?.exact).toBe(false)
    expect(hit?.mark.textContent?.replace(/\s+/g, '')).toBe('第7章验收要求')
  })

  it('找不到就不标，返回 null', () => {
    expect(markQuote(host('<p>完全无关</p>'), '高支模专项施工方案')).toBeNull()
    expect(markQuote(host('<p>x</p>'), null)).toBeNull()
  })
})

describe('sanitizeQuote（切片里的合成标题路径）', () => {
  it('去掉 markdown 标题记号', () => {
    expect(sanitizeQuote('## 施工保证措施/技术措施')).toBe('施工保证措施/技术措施')
  })
})

describe('markQuote 对带标题路径的切片', () => {
  it('原文里标题是分开的段落时，整段仍能标出（去 ## 后再去 /）', () => {
    const root = host(
      '<h2>施工保证措施</h2><h3>技术措施</h3><h4>冬季施工措施</h4>' +
        '<p>（三）各分项工程冬季施工技术要点</p>',
    )
    const hit = markQuote(
      root,
      '## 施工保证措施/技术措施/冬季施工措施\n（三）各分项工程冬季施工技术要点',
    )
    expect(hit?.exact).toBe(true)
    const all = [...root.querySelectorAll('mark')].map((m) => m.textContent).join('')
    expect(all.replace(/\s+/g, '')).toContain('冬季施工措施')
  })
})

describe('markQuote 退路：标出切片里所有能对上的段落', () => {
  it('多段切片时，每一段都标出来，而不是只标最长的一段', () => {
    const root = host(
      '<h2>施工保证措施</h2><h3>技术措施</h3><p>（三）各分项工程冬季施工技术要点</p>' +
        '<p>（1）土方与地基工程：基坑基底应覆盖保温材料。</p>',
    )
    const hit = markQuote(
      root,
      '## 施工保证措施/技术措施\n（三）各分项工程冬季施工技术要点\n（1）土方与地基工程：基坑基底应覆盖保温材料。',
    )
    // 这几段在原文里恰好首尾相接（标题→正文），所以整段是能对上的
    expect(hit).not.toBeNull()
    const marks = [...root.querySelectorAll('mark')].map((m) => m.textContent)
    // 三段都能在原文找到 → 三段都标
    expect(marks.length).toBeGreaterThanOrEqual(2)
    expect(marks.join('')).toContain('（三）各分项工程冬季施工技术要点')
    expect(marks.join('')).toContain('（1）土方与地基工程')
  })

  it('太短的片段不标（避免把到处都有的词刷黄）', () => {
    const root = host('<p>技术措施</p><p>技术措施</p>')
    // 整段对不上、只剩下一个 4 字片段 → 宁可不标
    expect(markQuote(root, '技术措施\n另一段原文里没有的内容')).toBeNull()
  })
})

describe('markQuote 段数上限', () => {
  it('十几段的切片不能漏标（曾经短句被上限砍掉）', () => {
    const lines = Array.from({ length: 14 }, (_, i) => `第${i + 1}条措施内容不少于六个字`)
    const root = host(`<div>${lines.map((l) => `<p>${l}。</p>`).join('')}</div>`)
    const hit = markQuote(root, lines.join('。'))
    expect(hit).not.toBeNull()
    const marks = [...root.querySelectorAll('mark')].map((m) => m.textContent)
    expect(marks.length).toBe(14)
    expect(marks.join('')).toContain('第13条措施内容')
  })
})
