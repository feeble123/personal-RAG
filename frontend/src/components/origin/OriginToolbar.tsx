/**
 * 原文阅读器的工具条：翻页 / 缩放 / 跳到高亮 / 重新加载。
 *
 * 抽成独立组件是因为 PDF、Word 各有各的"页"和"缩放"实现，但用户看到的
 * 应该是**同一套按钮**——第一次做的时候把 Word 那侧的工具栏隐藏了，
 * 用户马上问"为什么 docx 没有上一页下一页"，这属于自找的困惑。
 *
 * 能力缺失时不显示（而不是显示一个点不动的按钮）：Excel 没有页，就不出页码。
 */
import type { ReactNode } from 'react'
import { Button, InputNumber, Space, Tooltip, Typography } from 'antd'
import { AimOutlined, ReloadOutlined, ZoomInOutlined, ZoomOutOutlined } from '@ant-design/icons'

const { Text } = Typography

interface Props {
  /** 总页数；不给就不显示翻页 */
  pages?: number
  /**
   * 计程单位。PDF 是真正的「页」；Word 原文件没有分页信息（docx-preview 只按
   * 显式分页符断页），所以按**屏幕高度**翻，如实叫「屏」。
   */
  unit?: '页' | '屏'
  current?: number
  onGoto?: (page: number) => void
  /** 缩放；不给就不显示缩放按钮 */
  onZoom?: (delta: number) => void
  onJump?: () => void
  canJump?: boolean
  onReload?: () => void
  /** 右侧附加信息（如"扫描件：框出的是整块"） */
  extra?: ReactNode
}

export default function OriginToolbar({
  pages,
  unit = '页',
  current = 1,
  onGoto,
  onZoom,
  onJump,
  canJump = false,
  onReload,
  extra,
}: Props) {
  const hasPages = Boolean(pages && onGoto)
  return (
    <Space size={10} wrap style={{ marginBottom: 10 }}>
      {hasPages && (
        <>
          <Space.Compact>
            <Button onClick={() => onGoto?.((current ?? 1) - 1)} disabled={(current ?? 1) <= 1}>
              上一{unit}
            </Button>
            <InputNumber
              min={1}
              max={pages}
              value={current}
              onChange={(v) => v && onGoto?.(Number(v))}
              style={{ width: 76 }}
            />
            <Button onClick={() => onGoto?.((current ?? 1) + 1)} disabled={(current ?? 1) >= (pages ?? 1)}>
              下一{unit}
            </Button>
          </Space.Compact>
          <Text type="secondary" style={{ fontSize: 12 }}>
            / 共 {pages} {unit}
          </Text>
        </>
      )}
      {onZoom && (
        <>
          <Tooltip title="缩小">
            <Button icon={<ZoomOutOutlined />} onClick={() => onZoom(-1)} size="small" />
          </Tooltip>
          <Tooltip title="放大">
            <Button icon={<ZoomInOutlined />} onClick={() => onZoom(1)} size="small" />
          </Tooltip>
        </>
      )}
      <Tooltip title="回到高亮位置">
        <Button size="small" icon={<AimOutlined />} disabled={!canJump} onClick={onJump}>
          跳到高亮
        </Button>
      </Tooltip>
      {onReload && (
        <Button size="small" icon={<ReloadOutlined />} onClick={onReload}>
          重新加载
        </Button>
      )}
      {extra}
    </Space>
  )
}
