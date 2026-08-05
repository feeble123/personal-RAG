import axios from 'axios'
import { message as antdMessage } from 'antd'
import { useAuthStore } from '@/stores/auth'

// axios 实例：注入 token、401 跳登录、统一错误提示
export const api = axios.create({
  baseURL: '/api',
  timeout: 60_000,
})

api.interceptors.request.use((config) => {
  const token = useAuthStore.getState().token
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

api.interceptors.response.use(
  (res) => res,
  (error) => {
    const status = error.response?.status
    const msg = error.response?.data?.message || error.message || '网络错误'

    if (status === 401) {
      // token 失效：清理并跳登录（避免刷屏）
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
