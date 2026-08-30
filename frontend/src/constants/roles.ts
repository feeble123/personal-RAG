// 三级角色显示元数据（单元 I 补充）：中文名 + Tag 颜色 + 下拉选项
import type { Role } from '@/api/types'

export const ROLE_META: Record<Role, { label: string; color: string }> = {
  superadmin: { label: '超管', color: 'red' },
  admin: { label: '库管', color: 'gold' },
  user: { label: '普通用户', color: 'blue' },
}

export const ROLE_OPTIONS: { value: Role; label: string }[] = [
  { value: 'user', label: '普通用户' },
  { value: 'admin', label: '库管' },
  { value: 'superadmin', label: '超管' },
]
