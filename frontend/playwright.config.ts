import { defineConfig } from '@playwright/test'

// E2E 测试（单元 G-③）：测前端路由守卫与页面渲染。
// 不依赖后端登录数据——未登录时 App 的 refresh 请求失败后走 setRestored()，
// 随后 RequireAuth 把 /chat 重定向到 /login。webServer 自动起 vite dev。
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: 'list',
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'on-first-retry',
  },
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:5173',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
})
