import { Navigate, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'
import { useAuthStore } from '@/stores/auth'
import { isAdminRole, isSuperadminRole } from '@/api/types'

// 登录守卫：未登录跳登录页
export function RequireAuth({ children }: { children: ReactNode }) {
  const token = useAuthStore((s) => s.token)
  const restored = useAuthStore((s) => s.restored)
  const location = useLocation()
  // P0-1：启动时正在用 refresh cookie 恢复登录态 → 先不跳转，等恢复完成
  if (!restored) return null
  if (!token) return <Navigate to="/login" state={{ from: location }} replace />
  return <>{children}</>
}

// 管理员守卫：库管(admin)与超管(superadmin)都可进入知识库/记忆库/审计/统计
export function RequireAdmin({ children }: { children: ReactNode }) {
  const role = useAuthStore((s) => s.user?.role)
  if (!isAdminRole(role)) return <Navigate to="/chat" replace />
  return <>{children}</>
}

// 超管守卫：仅 superadmin 可进入账号管理
export function RequireSuperAdmin({ children }: { children: ReactNode }) {
  const role = useAuthStore((s) => s.user?.role)
  if (!isSuperadminRole(role)) return <Navigate to="/chat" replace />
  return <>{children}</>
}
