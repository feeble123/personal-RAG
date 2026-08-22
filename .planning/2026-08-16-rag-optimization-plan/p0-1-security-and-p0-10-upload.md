# P0-1 生产安全止血 + P0-10 上传隔离 详细实施计划

> 制定日期：2026-08-22
> 前置：P0-2~P0-9、P0-11 已完成（feature/rag-optimization 分支，315 测试绿）
> 目标：堵住「可预测管理员/JWT」与「上传不校验内容」两个上线阻断级风险

---

## 当前现状（已查证）

### P0-1 现状问题
| 问题 | 位置 | 风险 |
|---|---|---|
| `jwt_secret = "dev-secret-change-me-in-production"` 默认值 | [config.py:39](backend/app/core/config.py#L39) | 攻击者知道默认密钥 → 可伪造任意用户/admin 的 JWT |
| `admin_password = "123456"` 默认值 | [config.py:43](backend/app/core/config.py#L43) | 弱口令直接接管 admin |
| **全代码库无 `APP_ENV`/production 判断** | 全局 | 生产部署用默认密钥/密码也能启动，无任何拦截 |
| 前端 access_token 存 localStorage | [auth.ts:13](frontend/src/stores/auth.ts#L13) | XSS 可窃取 token 冒充用户（计划 P0-1 要求改内存） |

### P0-10 现状问题
| 问题 | 位置 | 风险 |
|---|---|---|
| 上传只校验扩展名 + 大小 | [routes.py:115-139](backend/app/modules/knowledge/routes.py#L115-L139) | 伪造扩展名（改后缀为 pdf 的恶意文件）会被当 PDF 解析 |
| 文件名服务端 UUID 化 ✅ | routes.py | 已做，无路径穿越风险 |
| 无 MIME/signature 校验 | routes.py | 内容与扩展名不符的文件混入知识库 |
| 无 quarantine（隔离暂存） | — | 未知内容直接交给解析器/OCR |

---

## P0-1 生产安全止血（先做，上线阻断）

### 单元1：APP_ENV 生产模式校验（config.py）

**目标**：`APP_ENV=production` 时，缺必需安全配置 → 启动直接失败（fail-safe）。

**改 `app/core/config.py`**：
1. 新增字段：
   ```python
   app_env: str = "development"   # development / test / production
   # 生产必需（默认值在 production 下视为「未配置」）
   jwt_secret: str = ""            # 默认空 → development 用随机 fallback，production 必须显式配置
   admin_password: str = ""
   ```
2. 加 `model_validator(mode="after")`：
   - `app_env == "production"` 时：
     - `jwt_secret` 为空 → 抛 `ValidationError("production 必须配置 JWT_SECRET")`
     - `admin_password` 为空 或 等于 `"123456"` → 抛错
     - `embedding_api_key` 为空 → 抛错（无法入库）
     - `deepseek_api_key` 为空 → 抛错（无法问答）
   - 启动时 log 打印 `APP_ENV`（不打印 secret）

**测试**（`tests/unit/test_security.py` 扩展 + `tests/test_config_production.py`）：
- production 缺 JWT_SECRET → Settings() 抛错
- production 默认 admin_password → 抛错
- production 空 embedding/deepseek key → 抛错
- development（默认）不受影响，仍可用 dev 默认值跑测试
- `get_settings()` 缓存按 APP_ENV 隔离（测试间不串）

### 单元2：认证加固（session 化 + token 轮换）

**目标**：access 短期 + refresh 随机化，改密/禁用立即失效。

**计划要求**（P0-1 原文）：短时 access JWT + 随机 refresh token + 服务端 session 表。

**改数据模型**：`users` 加 `session_version`（改密/禁用时 +1，旧 token 失效）
**新增 `auth_sessions` 表**：id, user_id, refresh_hash(sha256), expires_at, revoked_at, last_used_at
**改 auth/routes.py**：
- 登录：签发短 access(15min) + 随机 refresh（DB 存 hash，HttpOnly cookie 返回）
- `/auth/refresh`：refresh 轮换（旧作废 + 发新），DB 检测重放
- `/auth/logout`：吊销当前 session
- 改密：`session_version` +1 → 所有旧 token 失效
- access token decode 时校验 `session_version`

**前端改 auth.ts + client.ts**：
- access token 只存内存（zustand 不 persist）
- refresh cookie 浏览器自动携带；刷新页面 → 调 `/auth/refresh` 恢复登录态
- 401 → 尝试 refresh → 失败才登出

**测试**：
- 登录 → 改密 → 旧 access/refresh 均不可用
- 禁用账号 → 已发 token 立即失效
- refresh 重放 → 拒绝并吊销该 session
- 刷新页面（模拟）→ /auth/refresh 恢复登录态

### 单元3：P0-1 收尾 + 分支提交
- 全量 pytest 回归 + 前端 build 验证
- 提交到 feature/rag-optimization（不 push、不并 main）

---

## P0-10 上传隔离（P0-1 之后）

### 单元1：MIME/signature 校验 + 内容安全
**目标**：不信任客户端 Content-Type，用实际解析器安全打开验证。

**改 `app/modules/knowledge/routes.py`**：
- 上传时对文件做 **magic number 校验**（pdf `%PDF`、docx/xlsx zip `PK`、txt/md/csv 文本）：
  ```python
  _SIGNATURES = {
      "pdf": b"%PDF",
      "docx": b"PK\x03\x04",  # 也接受 docx/xlsx (zip)
      "xlsx": b"PK\x03\x04",
      "md": None,  # 文本，允许任意
      ...
  }
  ```
- 扩展名 + signature 不匹配 → 拒绝（BizError 400）
- **分格式大小上限**：PDF 页数/对象数上限（解析前读取）、docx/xlsx 解压后大小/压缩比上限

**测试**：
- 改后缀的假 PDF（内容不是 %PDF）→ 拒绝
- 真 PDF/真 docx → 通过
- 超大解压比 docx（zip bomb）→ 拒绝
- CSV 公式注入（`=cmd`, `+`, `-`, `@` 开头单元格）→ 清洗或拒绝

### 单元2：quarantine 隔离 + 解析前验证
**目标**：上传先进隔离区，验证通过才交解析器。

**改 routes.py + manager.py**：
- 上传 → 写入 `uploads/.quarantine/` → 校验通过 → move 到正式 uploads
- 校验失败 → 隔离区清理 + 拒绝
- 解析前在 `_process_document` 里二次 verify（打开文件确认可解析）

**测试**：
- 恶意文件在 quarantine 被拦截，不进入解析
- 解析失败文件清理 quarantine，不留垃圾

### 单元3：P0-10 收尾 + 全量回归 + 分支提交

---

## 验收标准
| P0-1 | P0-10 |
|---|---|
| production 缺 secret/默认密码 → 启动失败 | 伪造扩展名/zip bomb 全部被阻断 |
| 改密后旧 token 失效 | MIME/signature 校验生效 |
| 禁用账号立即失效 | 解析 worker 超限只杀当前 job，不拖垮 API |
| refresh 重放被拒 | quarantine 不留垃圾 |

## 回滚
- P0-1：config 校验是纯新增，去掉 APP_ENV 校验即回开发模式；auth_sessions 是新表（Alembic），回滚 = 删表 + 前端改回 persist
- P0-10：校验是纯新增逻辑，去掉即回现状；quarantine 目录删除即回现状

## 工作量估计
- P0-1：3-4 人日（含前端改造）
- P0-10：2-3 人日

## 风险
- 前端 token 改内存后刷新丢失登录态 → 必须实现 /auth/refresh 才能切换（单元2 顺序：先做后端 refresh，再做前端切内存）
- session 表引入 refresh 轮换 → 需保证 refresh 与 access 的过期窗口不冲突（refresh 过期 > access）
