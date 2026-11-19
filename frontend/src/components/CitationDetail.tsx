import { Suspense, lazy, useState } from 'react'
import { Button, Modal, Space, Tag, Typography } from 'antd'
import { FilePdfOutlined, FileTextOutlined } from '@ant-design/icons'
import type { Citation } from '@/api/types'
import ViewerBoundary from '@/components/origin/ViewerBoundary'

// pdf.js 有 1MB+，只有点了「看原文」才需要 —— 懒加载，免得拖慢问答首屏
const PdfOriginViewer = lazy(() => import('@/components/origin/PdfOriginViewer'))

// 引用详情弹窗：来源文件 / 页码 / 章节 / 相关度 / 排名 + 原文片段。
// 从原 CitationCard 提取，供「行内角标」与「底部来源清单」两处复用。
export default function CitationDetail({
  citation,
  open,
  onClose,
}: {
  citation: Citation | null
  open: boolean
  onClose: () => void
}) {
  // 看原文是**另一个弹窗**（要宽、要滚、要缩放），不塞进这个 640px 的详情里
  const [originOpen, setOriginOpen] = useState(false)
  const canOpenOrigin = Boolean(citation?.doc_id)

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={640}
      title={
        <Space>
          <FileTextOutlined />
          引用详情
        </Space>
      }
    >
      {citation && (
        <>
          <div style={{ marginBottom: 8 }}>
            <Space wrap>
              <Tag color="blue">{citation.source}</Tag>
              {/* 书内印刷页码优先；PDF 物理页同时标出来，方便在阅读器里跳 */}
              {(citation.printed_page ?? citation.page) && (
                <Tag className="font-mono">第 {citation.printed_page ?? citation.page} 页</Tag>
              )}
              {citation.printed_page && citation.page && citation.printed_page !== citation.page && (
                <Tag className="font-mono" color="default">
                  PDF 第 {citation.page} 页
                </Tag>
              )}
              {citation.score != null && (
                <Tag color="green" className="font-mono">
                  相关度 {citation.score.toFixed(3)}
                </Tag>
              )}
              <Tag className="font-mono">排名 #{citation.rank}</Tag>
            </Space>
          </div>
          {citation.section && (
            <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
              章节：{citation.section}
            </Typography.Paragraph>
          )}
          <div className="citation-snippet">{citation.snippet}</div>
          <Space style={{ marginTop: 12 }}>
            <Button
              icon={<FilePdfOutlined />}
              disabled={!canOpenOrigin}
              onClick={() => setOriginOpen(true)}
            >
              {citation.page != null ? `看原文（PDF 第 ${citation.page} 页）` : '看原文'}
            </Button>
            {!canOpenOrigin && <Tag>这条引用没有关联文档</Tag>}
          </Space>

          {originOpen && citation.doc_id != null && (
            <Suspense fallback={null}>
              <ViewerBoundary>
                <PdfOriginViewer
                  open={originOpen}
                  docId={citation.doc_id}
                  page={citation.page}
                  quote={citation.snippet}
                  onClose={() => setOriginOpen(false)}
                />
              </ViewerBoundary>
            </Suspense>
          )}
        </>
      )}
    </Modal>
  )
}
