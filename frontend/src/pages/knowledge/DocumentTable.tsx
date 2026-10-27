/**
 * 文档列表表格列定义（G6-1 从 KnowledgeBase.tsx 抽出）。
 *
 * 只负责「怎么展示一列」，不持有状态；操作回调由父组件传入，
 * 保持原来的重新入库 / 删除 / 打开详情行为不变。
 */
import { Button, Popconfirm, Progress, Space, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import dayjs from 'dayjs'
import { DeleteOutlined, FileOutlined, RedoOutlined } from '@ant-design/icons'
import type { DocumentItem } from '@/api/types'
import {
  STAGE_PCT,
  STATUS_META,
  formatBytes,
  parseLabel,
  parseSummary,
  qualityItems,
} from './documentMeta'

interface Props {
  onOpenDetail: (doc: DocumentItem) => void
  onReparse: (id: number) => void
  onDelete: (id: number) => void
}

export function buildDocumentColumns({ onOpenDetail, onReparse, onDelete }: Props): ColumnsType<DocumentItem> {
  return [
    {
      title: '文件',
      dataIndex: 'filename',
      key: 'filename',
      ellipsis: true,
      render: (v: string, r: DocumentItem) => (
        <Space>
          <FileOutlined style={{ color: '#1677ff' }} />
          <a onClick={() => onOpenDetail(r)} title="查看解析详情">
            {v}
          </a>
        </Space>
      ),
    },
    { title: '类型', dataIndex: 'file_type', key: 'file_type', width: 80 },
    {
      title: '大小',
      dataIndex: 'file_size',
      key: 'file_size',
      width: 90,
      render: (v: number) => formatBytes(v),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 140,
      render: (s: string, r: DocumentItem) => {
        const meta = STATUS_META[s] || { text: s, color: 'default' }
        const pct = STAGE_PCT[s]
        const ocr = r.progress
        const ocrPct = ocr && ocr.total ? (ocr.percent ?? 0) : null
        return (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'flex-start' }}>
            <Tag color={meta.color}>{meta.text}</Tag>
            {ocrPct != null ? (
              <>
                <Progress percent={ocrPct} size="small" strokeWidth={5} style={{ width: 90 }} />
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                  OCR {ocr?.done ?? 0}/{ocr?.total ?? 0} 页
                </Typography.Text>
              </>
            ) : pct != null ? (
              <Progress percent={pct} showInfo={false} status="active" strokeWidth={4} style={{ width: 72 }} />
            ) : null}
          </div>
        )
      },
    },
    {
      title: 'Chunks',
      dataIndex: 'chunk_count',
      key: 'chunk_count',
      width: 80,
      render: (v: number, r: DocumentItem) =>
        r.status === 'failed' ? (
          <Tooltip title={r.error_message}>
            <Typography.Text type="danger" style={{ cursor: 'help' }}>失败</Typography.Text>
          </Tooltip>
        ) : (
          v
        ),
    },
    {
      title: '解析模式',
      dataIndex: 'parse_mode',
      key: 'parse_mode',
      width: 90,
      render: (v: string) =>
        v === 'high' ? (
          <Tag color="purple">高精度</Tag>
        ) : (
          <Tag color="blue">快速</Tag>
        ),
    },
    {
      title: '解析质量',
      dataIndex: 'quality',
      key: 'quality',
      width: 210,
      render: (_: unknown, r: DocumentItem) => {
        if (r.status === 'failed') return <Tag color="error">解析失败</Tag>
        if (!r.quality) return <Typography.Text type="secondary">—</Typography.Text>
        const { label, color } = parseLabel(r)
        const lines = parseSummary(r)
        return (
          <Tooltip
            title={
              <div style={{ maxWidth: 260 }}>
                {qualityItems(r.quality).map((it) => (
                  <div key={it.label} style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
                    <span>{it.label}</span>
                    <span style={{ color: '#d9d9d9' }}>{it.value}</span>
                  </div>
                ))}
              </div>
            }
          >
            <div>
              <Tag color={color}>{label}</Tag>
              <Typography.Text style={{ fontSize: 12, color: '#666', display: 'block' }}>{lines.join(' · ')}</Typography.Text>
            </div>
          </Tooltip>
        )
      },
    },
    { title: '上传时间', dataIndex: 'created_at', key: 'created_at', width: 110, render: (v: string) => dayjs(v).format('MM-DD HH:mm') },
    {
      title: '操作',
      key: 'action',
      width: 110,
      render: (_: unknown, r: DocumentItem) => (
        <Space>
          <Tooltip title="重新入库">
            <Button size="small" icon={<RedoOutlined />} onClick={() => onReparse(r.id)} />
          </Tooltip>
          <Popconfirm title="删除该文档？" okText="删除" cancelText="取消" onConfirm={() => onDelete(r.id)}>
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]
}
