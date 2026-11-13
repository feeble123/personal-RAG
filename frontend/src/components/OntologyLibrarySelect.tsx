/**
 * 图谱库选择器：在不同「本体项目」之间切换，像切知识库一样。
 *
 * 一个库 = 一份 TBox（YAML）+ 属于它的实例与关系；库里有多少实例/关系直接标在下拉里，
 * 免得用户切进去发现是空的还以为丢数据了。
 */
import { useEffect, useState } from 'react'
import { Alert, Form, Input, Modal, Select, Tooltip } from 'antd'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { errMsg } from '@/api/client'
import { ontologyApi } from '@/api/modules'
import type { OntologyLibrary } from '@/api/types'
import { useOntologyLibrary } from '@/stores/ontologyLibrary'

interface Props {
  /** 允许新建库（图谱页/管理页都给 true；只读场景可关） */
  allowCreate?: boolean
}

export default function OntologyLibrarySelect({ allowCreate = false }: Props) {
  const { profileId, setProfileId } = useOntologyLibrary()
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()

  const { data: libraries = [] } = useQuery<OntologyLibrary[]>({
    queryKey: ['ontology-libraries'],
    queryFn: ontologyApi.libraries,
    staleTime: 30_000,
  })

  // 没选过 / 选中的库被删了 → 落到服务端默认库，避免带着一个不存在的 profile 去请求
  const fallback = libraries.find((l) => l.is_default)?.profile_id ?? libraries[0]?.profile_id
  const effective = profileId || fallback || ''

  useEffect(() => {
    if (!libraries.length) return
    if (!profileId && fallback) {
      setProfileId(fallback)
      return
    }
    if (profileId && !libraries.some((l) => l.profile_id === profileId) && fallback) {
      setProfileId(fallback)
    }
  }, [libraries, profileId, fallback, setProfileId])

  const create = useMutation({
    mutationFn: (values: { profile_id: string; display_name: string; description?: string; template: string }) =>
      ontologyApi.createLibrary(values),
    onSuccess: async (lib) => {
      await qc.invalidateQueries({ queryKey: ['ontology-libraries'] })
      setProfileId(lib.profile_id)
      setOpen(false)
      form.resetFields()
    },
  })

  const options = libraries.map((lib) => ({
    value: lib.profile_id,
    label: lib.error
      ? `${lib.display_name}（解析失败）`
      : `${lib.display_name}・${lib.node_count} 节点`,
    disabled: Boolean(lib.error),
  }))

  const current = libraries.find((l) => l.profile_id === effective)

  return (
    <>
      <Tooltip title={current?.error || current?.description || '选择图谱库（不同项目互不影响）'}>
        <Select
          value={effective || undefined}
          onChange={setProfileId}
          options={options}
          style={{ minWidth: 190 }}
          size="small"
          placeholder="选择图谱库"
          disabled={!libraries.length}
          popupRender={(menu) =>
            allowCreate ? (
              <div>
                {menu}
                <div style={{ padding: 6, borderTop: '1px solid rgba(122,190,255,0.16)' }}>
                  <a onClick={() => setOpen(true)}>+ 新建图谱库</a>
                </div>
              </div>
            ) : (
              menu
            )
          }
        />
      </Tooltip>

      <Modal
        open={open}
        title="新建图谱库"
        okText="创建"
        cancelText="取消"
        confirmLoading={create.isPending}
        onCancel={() => setOpen(false)}
        onOk={async () => {
          const values = await form.validateFields()
          create.mutate(values)
        }}
        destroyOnClose
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="每个图谱库是一套独立的规则"
          description="不同项目（不同流域、不同模型）各用各的库：实体、关系、参数表都互不影响，切换时在左上角下拉里选。"
        />
        {create.isError && (
          <Alert type="error" showIcon style={{ marginBottom: 12 }} message={errMsg(create.error)} />
        )}
        <Form form={form} layout="vertical" initialValues={{ template: 'tuning' }}>
          <Form.Item
            name="display_name"
            label="库名称"
            rules={[{ required: true, message: '给这个库起个名字' }]}
          >
            <Input placeholder="如：防洪调度本体" maxLength={60} />
          </Form.Item>
          <Form.Item
            name="profile_id"
            label="库标识"
            tooltip="英文小写，用于文件名和接口参数，创建后不可改"
            rules={[
              { required: true, message: '填写库标识' },
              { pattern: /^[a-z][a-z0-9_]{1,39}$/, message: '小写字母开头，只能用 a-z、0-9、下划线，2~40 位' },
            ]}
          >
            <Input placeholder="如：flood_ctrl" maxLength={40} />
          </Form.Item>
          <Form.Item name="description" label="说明（可选）">
            <Input.TextArea rows={2} maxLength={300} placeholder="这个库用来描述什么" />
          </Form.Item>
          <Form.Item
            name="template"
            label="起始内容"
            tooltip="从现有库复制，能直接复用它的实体/关系类型；空白库只有最基本的参数类型"
          >
            <Select
              options={[
                { value: 'tuning', label: '复制「调参本体」的类型定义（推荐）' },
                { value: 'blank', label: '空白库（只有参数类型）' },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
