import axios from 'axios'
import { message as antdMessage } from 'antd'
import { useAuthStore } from '@/stores/auth'

// axios 实例：注入 token、401 尝试 refresh、统一错误提示
export const api = axios.create({
  baseURL: '/api',
  timeout: 60_000,
})

// P0-1：access token 改内存后，401 先用 refresh cookie 续期（避免误登出）
let refreshPromise: Promise<boolean> | null = null

async function tryRefresh(): Promise<boolean> {
  if (refreshPromise) return refreshPromise
  refreshPromise = (async () => {
    try {
      const res = await axios.post<TokenOutLike>('/api/auth/refresh', undefined, {
        timeout: 15_000,
      })
      useAuthStore.getState().setAuth(res.data.access_token, res.data.user)
      return true
    } catch {
      useAuthStore.getState().logout()
      return false
    } finally {
      refreshPromise = null
    }
  })()
  return refreshPromise
}

// refresh 返回结构与登录一致
interface TokenOutLike {
  access_token: string
  user: { id: number; username: string; role: 'admin' | 'user'; nickname: string | null; is_active: boolean }
}

api.interceptors.request.use((config) => {
  const token = useAuthStore.getState().token
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

api.interceptors.response.use(
  (res) => res,
  async (error) => {
    const original = error.config
    const status = error.response?.status
    const msg = error.response?.data?.message || error.message || '网络错误'

    if (status === 401 && !original?._retried) {
      // access 失效：先试 refresh（refresh cookie 浏览器自动携带）
      const ok = await tryRefresh()
      if (ok) {
        original._retried = true
        original.headers.Authorization = `Bearer ${useAuthStore.getState().token}`
        return api(original) // 重放原请求
      }
      // refresh 也失败 → 登出跳登录
      useAuthStore.getState().logout()
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login'
      }
      return Promise.reject(error)
    } else if (status === 401) {
      // 重放后仍 401：真正过期，登出
      useAuthStore.getState().logout()
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login'
      }
    } else if (status === 403) {
      antdMessage.error(msg || '无权限访问')
    } else if (!error.response) {
      antdMessage.error('无法连接服务器，请确认后端已启动')
    } else {
      antdMessage.error(msg)
    }
    return Promise.reject(error)
  },
)

// 后端统一错误结构
export function errMsg(e: unknown): string {
  if (axios.isAxiosError(e)) {
    return e.response?.data?.message || e.message
  }
  return String(e)
}
