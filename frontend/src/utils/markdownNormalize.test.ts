import { describe, expect, it } from 'vitest'
import { normalizeAnswerMarkdown } from './markdownNormalize'

describe('normalizeAnswerMarkdown', () => {
  it('区间符号 ~ 被转义，不会变成删除线定界符', () => {
    // 实测的原样：remark-gfm 把这两个 ~ 配成一对，渲染成
    // 10<del>20 年一遇降雨（24h 降雨量 155</del>170mm）
    const src = '10~20 年一遇降雨（24h 降雨量 155~170mm）'
    const out = normalizeAnswerMarkdown(src)
    expect(out).toBe('10\\~20 年一遇降雨（24h 降雨量 155\\~170mm）')
    // 不能再出现成对的裸 ~，否则还是会被当删除线
    expect(out.match(/[^\\]~/)).toBeNull()
  })

  it('单个 ~ 也转义（只有一对才划线，但统一处理更省心）', () => {
    expect(normalizeAnswerMarkdown('水位 338.5~343m')).toBe('水位 338.5\\~343m')
  })

  it('中文波浪线本来就不会被当语法，保持原样', () => {
    expect(normalizeAnswerMarkdown('10～20 年一遇')).toBe('10～20 年一遇')
  })

  it('LaTeX 定界符归一化成 remark-math 认识的写法', () => {
    expect(normalizeAnswerMarkdown('公式 \\(E=mc^2\\)')).toBe('公式 $E=mc^2$')
    expect(normalizeAnswerMarkdown('块级 \\[a+b\\]')).toBe('块级 $$a+b$$')
  })

  it('没内容的输入不炸', () => {
    expect(normalizeAnswerMarkdown('')).toBe('')
  })
})
