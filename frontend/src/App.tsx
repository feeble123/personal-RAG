import { useEffect } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { RequireAdmin, RequireAuth, RequireSuperAdmin } from '@/router'
import Login from '@/pages/Login'
import Register from '@/pages/Register'
import Chat from '@/pages/Chat'
import KnowledgeBase from '@/pages/KnowledgeBase'
import MemoryManager from '@/pages/MemoryManager'
import UserManager from '@/pages/UserManager'
import AuditLogs from '@/pages/AuditLogs'
import NotFound from '@/pages/NotFound'
import { useAuthStore } from '@/stores/auth'

export default function App() {
  // P0-1：启动时用 HttpOnly refresh cookie 恢复登录态（access 已改内存存储）
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch('/api/auth/refresh', { method: 'POST' })
        if (res.ok) {
          const data = (await res.json()) as {
            access_token: string
            user: { id: number; username: string; role: 'superadmin' | 'admin' | 'user'; nickname: string | null; is_active: boolean }
          }
          if (!cancelled) useAuthStore.getState().setAuth(data.access_token, data.user)
        } else {
          if (!cancelled) useAuthStore.getState().setRestored()
        }
      } catch {
        if (!cancelled) useAuthStore.getState().setRestored()
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/register" element={<Register />} />
      <Route path="/" element={<Navigate to="/chat" replace />} />
      <Route
        path="/chat"
        element={
          <RequireAuth>
            <Chat />
          </RequireAuth>
        }
      />
      <Route
        path="/knowledge"
        element={
          <RequireAuth>
            <RequireAdmin>
              <KnowledgeBase />
            </RequireAdmin>
          </RequireAuth>
        }
      />
      <Route
        path="/memories"
        element={
          <RequireAuth>
            <RequireAdmin>
              <MemoryManager />
            </RequireAdmin>
          </RequireAuth>
        }
      />
      <Route
        path="/users"
        element={
          <RequireAuth>
            <RequireSuperAdmin>
              <UserManager />
            </RequireSuperAdmin>
          </RequireAuth>
        }
      />
      <Route
        path="/audit"
        element={
          <RequireAuth>
            <RequireAdmin>
              <AuditLogs />
            </RequireAdmin>
          </RequireAuth>
        }
      />
      <Route path="*" element={<NotFound />} />
    </Routes>
  )
}
