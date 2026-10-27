/**
 * 文档解析详情弹窗内容（G6-1 从 KnowledgeBase.tsx 抽出）。
 *
 * 展示：基础信息 + 解析质量键值对 + 版本历史（重灌审计，含切片数增减）。
 */
import { Descriptions, Space, Table, Tag, Typography } from 'antd'
import dayjs from 'dayjs'
import type { DocumentItem, DocumentVersionItem } from '@/api/types'
import {
  STATUS_META,
  VERSION_STATUS_META,
  formatBytes,
  qualityItems,
} from '@/pages/knowledge/documentMeta'

export default function DocDetail({ doc }: { doc: DocumentItem }) {
  const quality = doc.quality
  // 版本历史：按创建时间倒序（最新在前），active 高亮，chunk 数与上一版对比
  const versions = [...(doc.versions ?? [])].sort(
    (a, b) => +dayjs(b.created_at) - +dayjs(a.created_at),
  )
  return (
    <div>
      <Descriptions column={1} size="small" bordered>
        <Descriptions.Item label="文件名">{doc.filename}</Descriptions.Item>
        <Descriptions.Item label="类型">{doc.file_type}</Descriptions.Item>
        <Descriptions.Item label="解析模式">
          {doc.parse_mode === 'high' ? '高精度（hybrid-engine）' : '快速（pipeline）'}
        </Descriptions.Item>
        <Descriptions.Item label="大小">{formatBytes(doc.file_size)}</Descriptions.Item>
        <Descriptions.Item label="状态">{STATUS_META[doc.status]?.text ?? doc.status}</Descriptions.Item>
        <Descriptions.Item label="总页数">{doc.page_count ?? '—'}</Descriptions.Item>
        <Descriptions.Item label="内容片段">{doc.chunk_count}</Descriptions.Item>
        <Descriptions.Item label="入库时间">{dayjs(doc.created_at).format('YYYY-MM-DD HH:mm:ss')}</Descriptions.Item>
        {doc.parsed_at && (
          <Descriptions.Item label="解析完成">{dayjs(doc.parsed_at).format('YYYY-MM-DD HH:mm:ss')}</Descriptions.Item>
        )}
        {quality &&
          qualityItems(quality).map((it) => (
            <Descriptions.Item key={it.label} label={it.label}>
              {it.value}
            </Descriptions.Item>
          ))}
        {doc.error_message && <Descriptions.Item label="错误信息">{doc.error_message}</Descriptions.Item>}
      </Descriptions>

      {/* 版本历史（重灌审计）：chunk 数变化 + 状态时间线 */}
      {versions.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <Typography.Text strong style={{ display: 'block', marginBottom: 8 }}>
            版本历史（{versions.length}）
          </Typography.Text>
          <Table
            rowKey="id"
            size="small"
            pagination={false}
            dataSource={versions}
            columns={[
              {
                title: '状态',
                dataIndex: 'status',
                key: 'status',
                width: 90,
                render: (s: string) => {
                  const m = VERSION_STATUS_META[s] || { text: s, color: 'default' }
                  return <Tag color={m.color}>{m.text}</Tag>
                },
              },
              {
                title: '切片数',
                dataIndex: 'chunk_count',
                key: 'chunk_count',
                width: 100,
                render: (v: number, r: DocumentVersionItem, i: number) => {
                  // 与下一版（更旧）对比切片数增减
                  const prev = versions[i + 1]
                  const delta = prev ? v - prev.chunk_count : null
                  return (
                    <Space size={4}>
                      <span>{v}</span>
                      {delta !== null && delta !== 0 && (
                        <Typography.Text type={delta > 0 ? 'success' : 'danger'} style={{ fontSize: 11 }}>
                          {delta > 0 ? '+' : ''}{delta}
                        </Typography.Text>
                      )}
                    </Space>
                  )
                },
              },
              {
                title: '创建时间',
                dataIndex: 'created_at',
                key: 'created_at',
                width: 150,
                render: (v: string) => dayjs(v).format('MM-DD HH:mm'),
              },
              {
                title: '生效时间',
                dataIndex: 'activated_at',
                key: 'activated_at',
                width: 150,
                render: (v: string | null) => (v ? dayjs(v).format('MM-DD HH:mm') : '—'),
              },
              {
                title: '退役时间',
                dataIndex: 'retired_at',
                key: 'retired_at',
                width: 150,
                render: (v: string | null) => (v ? dayjs(v).format('MM-DD HH:mm') : '—'),
              },
            ]}
          />
        </div>
      )}
    </div>
  )
}
