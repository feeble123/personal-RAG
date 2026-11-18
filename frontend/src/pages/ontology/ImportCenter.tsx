/**
 * 导入中心：三种 Excel 导入收在一个页签里，先选"这张表是什么"，再上传。
 *
 * **合并的是入口，不是逻辑。** 三种表的「表 → 数据」映射规则完全不同：
 * - 实例：表头 = 属性名，一行一个实例，不建关系；
 * - 参数先验：带物理/通用范围、条件范围、敏感度、依据溯源；
 * - 流程链条：一行一个动作，程序推导出四类边。
 * 揉成一个"万能导入"会让用户先声明"我这是哪种表"、再面对一堆开关，
 * 而且报错也说不清。所以这里保留三套面板，只把入口收拢。
 */
import { useState } from 'react'
import { Alert, Card, Space, Tag, Typography } from 'antd'
import FlowImportPanel from './FlowImportPanel'
import InstanceImportPanel from './InstanceImportPanel'
import ParamImportPanel from './ParamImportPanel'

const { Text } = Typography

type TemplateKey = 'instance' | 'params' | 'flow'

interface Props {
  profileId: string
  entityTypes: { name: string; display_name?: string }[]
  libraryName: string
  /** 任一导入成功后：刷新实例/关系/图缓存 */
  onImported?: () => void
}

export default function ImportCenter({
  profileId,
  entityTypes,
  libraryName,
  onImported,
}: Props) {
  const hasParamType = entityTypes.some((t) => t.name === 'Parameter')
  const [template, setTemplate] = useState<TemplateKey>('instance')

  const cards: { key: TemplateKey; title: string; desc: string; need: string; ready: boolean }[] = [
    {
      key: 'instance',
      title: '实例数据（通用）',
      desc: '表头就是属性名，一行一个实例；只建实例，不建关系',
      need: '任何图谱库',
      ready: true,
    },
    {
      key: 'params',
      title: '参数先验表',
      desc: '物理/通用范围、条件范围、敏感度、依据溯源——参数推荐卡专用',
      need: '含「参数」类型的库（如调参本体）',
      ready: hasParamType,
    },
    {
      key: 'flow',
      title: '流程链条',
      desc: '阶段 → 动作 → 工具 / 数据，导入时自动连好四类关系',
      need: '定义了流程类型的库（如流程骨架本体）',
      ready: true,
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="先选这张表是什么，再上传"
        description="三种表的结构不一样，导进去的东西也不一样：实例表只建点，参数表带范围和依据，流程表会自动连边。选错了会直接报错并说明原因，不会写一半。"
      />

      <Space wrap size={10}>
        {cards.map((c) => (
          <Card
            key={c.key}
            size="small"
            hoverable
            onClick={() => setTemplate(c.key)}
            style={{
              width: 250,
              cursor: 'pointer',
              borderColor: template === c.key ? '#1677ff' : undefined,
              borderWidth: template === c.key ? 2 : 1,
            }}
          >
            <Space direction="vertical" size={4}>
              <Space size={6}>
                <Text strong>{c.title}</Text>
                {!c.ready && <Tag color="orange">当前库不适用</Tag>}
              </Space>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {c.desc}
              </Text>
              <Text type="secondary" style={{ fontSize: 11 }}>
                适用：{c.need}
              </Text>
            </Space>
          </Card>
        ))}
      </Space>

      <Card size="small">
        {template === 'instance' && (
          <InstanceImportPanel
            profileId={profileId}
            entityTypes={entityTypes}
            onImported={onImported}
          />
        )}
        {template === 'params' && (
          <ParamImportPanel
            profileId={profileId}
            hasParamType={hasParamType}
            libraryName={libraryName}
            onImported={onImported}
          />
        )}
        {template === 'flow' && (
          <FlowImportPanel profileId={profileId} onImported={onImported} />
        )}
      </Card>
    </Space>
  )
}
