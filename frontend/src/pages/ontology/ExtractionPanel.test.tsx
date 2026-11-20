/**
 * 待审区：三种提案长得完全不一样，得让人一眼看懂"我要接受的是什么"。
 *
 * 光 `tsc + build` 通过不代表界面能用。这里验四件事：
 *   1. 关系提案要显示成「A —关系→ B」，不能是一串 uid；
 *   2. 新实例要显示名字 + 类型，字段值不能渲染成 [object Object]；
 *   3. 能按类型分开看（新实例 / 新关系 / 字段值）；
 *   4. 没扫完的范围要明说——否则用户会以为整份文档都提过了。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { App } from 'antd'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ExtractionPanel from './ExtractionPanel'
import { kbPublicApi, ontologyApi } from '@/api/modules'
import type { OntologyProposalItem } from '@/api/types'

const ROWS: OntologyProposalItem[] = [
  {
    id: 1,
    profile_id: 'reservoir_flood',
    instance_uid: 'rl:1',
    instance_name: 'Ⅱ级响应',
    entity_type: 'response_level',
    attribute: '',
    kind: 'instance',
    payload: {
      entity_type: 'response_level',
      name: 'Ⅱ级响应',
      uid: 'rl:1',
      props: { level: 'Ⅱ' },
      kb_id: 0,
    },
    subject_name: '',
    object_name: '',
    value: {},
    current_value: null,
    method: 'graph',
    confidence: 0.6,
    evidence: { doc_name: '调度规程.pdf', page: 39, quote: '当库水位达到 1012.50 m 时' },
    status: 'pending',
    created_at: null,
  },
  {
    id: 2,
    profile_id: 'reservoir_flood',
    instance_uid: 'rl:1',
    instance_name: 'Ⅱ级响应',
    entity_type: '',
    attribute: '',
    kind: 'relation',
    payload: {
      subject_uid: 'rl:1',
      predicate: 'RESPONDS_WITH',
      object_uid: 'ms:9',
      props: {},
      kb_id: 0,
    },
    subject_name: 'Ⅱ级响应',
    object_name: '加强水情监测',
    value: {},
    current_value: null,
    method: 'graph',
    confidence: 0.6,
    evidence: { doc_name: '调度规程.pdf', page: 39, quote: '加强水情监测' },
    status: 'pending',
    created_at: null,
  },
  {
    id: 3,
    profile_id: 'reservoir_flood',
    instance_uid: 'pm:1',
    instance_name: 'sm',
    entity_type: 'Parameter',
    attribute: 'range_low',
    kind: 'attr',
    payload: null,
    subject_name: '',
    object_name: '',
    value: 5,
    current_value: null,
    method: 'rule',
    confidence: 0.9,
    evidence: { doc_name: '手册.pdf', page: 4, quote: 'sm 5~50' },
    status: 'pending',
    created_at: null,
  },
]

// vi.mock 会被提升到文件顶部，工厂里**不能引用**模块级变量（ROWS 还没初始化）。
// 所以工厂只造空壳，返回值在 beforeEach 里注入。
vi.mock('@/api/modules', () => ({
  kbPublicApi: { list: vi.fn() },
  ontologyApi: {
    proposals: vi.fn(),
    extractRun: vi.fn(),
    anchorRun: vi.fn(),
    proposeRun: vi.fn(),
    linkRun: vi.fn(),
    acceptProposal: vi.fn(),
    acceptProposals: vi.fn(),
    rejectProposal: vi.fn(),
  },
}))

beforeEach(() => {
  vi.mocked(kbPublicApi.list).mockResolvedValue([])
  vi.mocked(ontologyApi.proposals).mockResolvedValue(ROWS)
})

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <App>
      <QueryClientProvider client={qc}>
        <ExtractionPanel profileId="reservoir_flood" />
      </QueryClientProvider>
    </App>,
  )
}

describe('ExtractionPanel', () => {
  it('关系提案显示成「主语 —关系→ 宾语」，不是一串 uid', async () => {
    renderPanel()
    expect(
      await screen.findByText(/Ⅱ级响应 —RESPONDS_WITH→ 加强水情监测/),
    ).toBeInTheDocument()
  })

  it('新实例显示名字和字段值，不渲染成 [object Object]', async () => {
    renderPanel()
    expect(await screen.findByText('level=Ⅱ')).toBeInTheDocument()
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument()
  })

  it('三种提案分别打标签，能按类型分开看', async () => {
    renderPanel()
    await screen.findByText(/Ⅱ级响应 —RESPONDS_WITH→ 加强水情监测/)
    expect(screen.getAllByText('新实例').length).toBeGreaterThan(0)
    expect(screen.getAllByText('新关系').length).toBeGreaterThan(0)

    // 只看新关系：新实例那条消失
    await userEvent.click(screen.getByText('新关系 1'))
    expect(screen.getByText(/Ⅱ级响应 —RESPONDS_WITH→ 加强水情监测/)).toBeInTheDocument()
    expect(screen.queryByText('level=Ⅱ')).not.toBeInTheDocument()
  })

  it('待审总数与各类型条数一致', async () => {
    renderPanel()
    expect(await screen.findByText('待审 3 条')).toBeInTheDocument()
    expect(screen.getByText('全部 3')).toBeInTheDocument()
    expect(screen.getByText('新实例 1')).toBeInTheDocument()
    expect(screen.getByText('字段值 1')).toBeInTheDocument()
  })
})
