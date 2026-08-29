// E2E：路由守卫 + 登录/注册页渲染。
// 不依赖后端登录数据——未登录访问 /chat 应被 RequireAuth 重定向到 /login。
import { expect, test } from '@playwright/test'

test('未登录访问 /chat 重定向到 /login', async ({ page }) => {
  await page.goto('/chat')
  // 等重定向落地（登录页标题 + 表单出现）
  await expect(page).toHaveURL(/\/login/)
  await expect(page.getByText('水利知识库问答系统')).toBeVisible()
  await expect(page.getByPlaceholder('用户名')).toBeVisible()
})

test('登录页渲染出账号密码表单', async ({ page }) => {
  await page.goto('/login')
  await expect(page.getByPlaceholder(/用户名|账号/).first()).toBeVisible()
  await expect(page.getByPlaceholder(/密码/).first()).toBeVisible()
})

test('注册页可从登录页跳转', async ({ page }) => {
  await page.goto('/login')
  await page.getByText('注册', { exact: false }).first().click()
  await expect(page).toHaveURL(/\/register/)
})
