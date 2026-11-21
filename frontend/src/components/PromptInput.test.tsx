/**
 * 输入框：前置条件没满足时不能发出去。
 *
 * 为什么值得单测：本体模式下没选知识库时，后端只能退回默认本体，工况一条都
 * 匹配不上——答案看着"答偏了"却不报错。与其让用户拿到似是而非的结果，
 * 不如在发送这一步就拦住（实测：问泄水设施答成了报告流程）。
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import PromptInput from './PromptInput'

function setup(props: Partial<React.ComponentProps<typeof PromptInput>> = {}) {
  const onSend = vi.fn()
  render(
    <PromptInput onSend={onSend} onStop={vi.fn()} streaming={false} {...props} />,
  )
  const input = screen.getByRole('textbox')
  return { onSend, input }
}

describe('PromptInput', () => {
  it('正常状态下发送触发 onSend', async () => {
    const { onSend, input } = setup()
    await userEvent.click(input)
    await userEvent.keyboard('你好')
    await userEvent.keyboard('{Enter}')
    expect(onSend).toHaveBeenCalledWith('你好')
  })

  it('disabled 时按 Enter 也不发送', async () => {
    const { onSend, input } = setup({ disabled: true })
    await userEvent.click(input)
    await userEvent.keyboard('你好')
    await userEvent.keyboard('{Enter}')
    expect(onSend).not.toHaveBeenCalled()
  })

  it('空内容不发送', async () => {
    const { onSend, input } = setup()
    await userEvent.click(input)
    await userEvent.keyboard('{Enter}')
    expect(onSend).not.toHaveBeenCalled()
  })
})
