import { Navigate, Route, Routes } from 'react-router-dom'
import { RequireAdmin, RequireAuth } from '@/router'
import Login from '@/pages/Login'
import Register from '@/pages/Register'
import Chat from '@/pages/Chat'
import KnowledgeBase from '@/pages/KnowledgeBase'
import MemoryManager from '@/pages/MemoryManager'
import NotFound from '@/pages/NotFound'

export default function App() {
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
      <Route path="*" element={<NotFound />} />
    </Routes>
  )
}
