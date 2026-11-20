/**
 * 体检面板：真的渲染一遍，验证「缺什么」看得懂、「去处理」点得到。
 *
 * 光 `tsc + build` 通过不代表界面能用——这个面板的价值全在两条：
 *   1. 违规要说出**具体缺哪一条**，不能只说「少了 3 条」；
 *   2. 能直接跳到那个实例去补，而不是让人自己去列表里翻。
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import HealthPanel from './HealthPanel'
import type {
  OntologyConstraintReport,
  OntologyEntityType,
  OntologyInstance,
} from '@/api/types'

const ENTITY_TYPES = [
  { name: 'response_level', display_name: '响应等级' },
  { name: 'measure', display_name: '处置措施' },
] as OntologyEntityType[]

const INSTANCES = [
  { id: 1, uid: 'rl:2', entity_type: 'response_level', name: 'Ⅱ级响应', props: {}, status: 'active' },
] as unknown as OntologyInstance[]

function report(overrides: Partial<OntologyConstraintReport> = {}): OntologyConstraintReport {
  return {
    profile_id: 'reservoir_flood',
    constraints: [
      {
        id: 'response_level.measures_coverage',
        type: 'coverage',
        entity: 'response_level',
        severity: 'error',
        relation: ['RESPONDS_WITH'],
        target_type: 'measure',
        total_attr: 'expected_measures',
        item_attr: 'source_item',
        cq: 'CQ-10',
        message: '预案该级的处置措施没有逐条建全',
      },
      {
        id: 'measure.executor',
        type: 'min_count',
        entity: 'measure',
        severity: 'warning',
        relation: ['PERFORMED_BY'],
        min: 1,
        cq: 'CQ-11',
        message: '措施没有挂执行单位',
      },
    ],
    summary: { total: 2, errors: 1, warnings: 1, by_constraint: {} },
    violations: [
      {
        constraint_id: 'response_level.measures_coverage',
        type: 'coverage',
        severity: 'error',
        entity: 'response_level',
        subject: 'Ⅱ级响应',
        message: '预案该级的处置措施没有逐条建全',
        cq: 'CQ-10',
        detail: { missing_items: [1, 3, 6], covered_items: [2, 4, 5] },
      },
    ],
    ...overrides,
  }
}

const noop = () => {}

function renderPanel(props: Partial<Parameters<typeof HealthPanel>[0]> = {}) {
  const onLocate = vi.fn()
  const utils = render(
    <HealthPanel
      report={report()}
      loading={false}
      entityTypes={ENTITY_TYPES}
      instances={INSTANCES}
      onRefetch={noop}
      onLocate={onLocate}
      {...props}
    />,
  )
  return { ...utils, onLocate }
}

describe('HealthPanel', () => {
  it('违规要说出缺的是哪几条，而不是只报数量', () => {
    renderPanel()
    expect(screen.getByText(/缺第 \(1\)\(3\)\(6\) 条/)).toBeInTheDocument()
  })

  it('规矩用 TBox 里写的人话展示，不露内部标识', () => {
    renderPanel()
    // 表里显示的是人话；内部 id 只放在 tooltip 里
    expect(screen.getAllByText('预案该级的处置措施没有逐条建全').length).toBeGreaterThan(0)
  })

  it('对象显示实例名与中文类型名', () => {
    renderPanel()
    expect(screen.getByText('Ⅱ级响应')).toBeInTheDocument()
    expect(screen.getByText('响应等级')).toBeInTheDocument()
  })

  it('点「去处理」把对应的实例交给调用方打开', async () => {
    const { onLocate } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /去处理/ }))
    expect(onLocate).toHaveBeenCalledWith(INSTANCES[0])
  })

  it('实例不在当前库里时按钮不可点，而不是点了没反应', () => {
    renderPanel({ instances: [] })
    const button = screen.getByRole('button', { name: /去处理/ })
    expect(button).toBeDisabled()
  })

  it('零违规时明确说全部通过，而不是空表格', () => {
    renderPanel({
      report: report({
        summary: { total: 0, errors: 0, warnings: 0, by_constraint: {} },
        violations: [],
      }),
    })
    expect(screen.getByText(/全部通过/)).toBeInTheDocument()
  })

  it('规矩清单把每条约束的 cq 一并展示，能追溯为什么有这条规矩', () => {
    renderPanel()
    expect(screen.getAllByText('CQ-10').length).toBeGreaterThan(0)
    expect(screen.getAllByText('CQ-11').length).toBeGreaterThan(0)
  })
})
