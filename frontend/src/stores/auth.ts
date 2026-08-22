import { create } from 'zustand'
import type { User } from '@/api/types'

interface AuthState {
  // P0-1：access token 只存内存（不 persist 到 localStorage）。
  // 刷新页面后凭 HttpOnly refresh cookie 调 /auth/refresh 恢复登录态。
  token: string | null
  user: User | null
  // 启动时是否已尝试过 /auth/refresh 恢复（未完成前守卫不跳登录页，避免闪跳）
  restored: boolean
  setAuth: (token: string, user: User) => void
  setUser: (user: User) => void
  setRestored: () => void
  logout: () => void
}

export const useAuthStore = create<AuthState>()((set) => ({
  token: null,
  user: null,
  restored: false,
  setAuth: (token, user) => set({ token, user, restored: true }),
  setUser: (user) => set({ user }),
  setRestored: () => set({ restored: true }),
  logout: () => set({ token: null, user: null, restored: true }),
}))

// 主动登出：吊销服务端 session（清 refresh cookie）+ 清本地内存态。
// 被动登出（401 时）只调 store.logout()，不再发请求，避免循环。
export async function logoutAndRevoke(): Promise<void> {
  try {
    await fetch('/api/auth/logout', { method: 'POST', keepalive: true })
  } catch {
    /* 后端不可达也照常清本地 */
  }
  useAuthStore.getState().logout()
  window.location.href = '/login'
}
