# RAG 优化实施进度（2026-08-22 更新）

## 已完成（提交到 feature/rag-optimization，未 push 未并 main）

| 工作包 | 状态 | 关键产出 |
|---|---|---|
| P0-2 scope 隔离 | ✅ | 跨库 0 泄露 |
| P0-3 缓存安全 | ✅ | 语义缓存失效安全 |
| P0-4 拒答策略 | ✅ | 实时/外部问题拒答 |
| P0-5 引用快照 | ✅ | citations 不可变 |
| P0-6 Alembic 基线 | ✅ | 迁移体系 |
| P0-7 chunk 身份 | ✅ | embedding cache 复用 |
| P0-8 版本化入库 | ✅ | 影子索引+原子发布 |
| P0-9 持久 job | ✅ c5be4c6 | job 表+worker+心跳/reaper/取消（315 绿） |
| P0-11 DSH 接口 | ✅ 936165a | 契约+出处元数据+README（291 绿） |
| P0-1 生产安全 | ✅ 21bb31a | APP_ENV fail-safe + session 认证加固（338 绿） |

## 未完成（按计划顺序）

### P0-1 生产安全止血（进行中）
- ✅ 单元1：APP_ENV production 校验（config.py model_validator，缺 secret/默认密码/空 key/DEBUG → 启动失败）+ 13 测试
- ✅ 单元2：认证加固（access 短期 15min + refresh 轮换 + auth_sessions 表 + session_version 改密/禁用立即失效 + 前端 token 改内存 + 401 自动 refresh）+ 10 测试
- ⬜ 单元3：全量回归 + 分支提交

### P0-10 上传隔离（进行中）
- ✅ 单元1：MIME/signature 校验（pdf %PDF / docx/xlsx PK zip）+ zip bomb 防护（压缩比 500 倍/解压 200MB）+ 文本二进制伪装拦截 + 13 测试
- ✅ 单元2：quarantine 隔离（上传先进 .quarantine → 校验通过移入 uploads）+ 解析前二次 verify（防 TOCTOU）+ 失败清理不留垃圾 + 3 测试
- ⬜ 单元3：收尾 + 全量回归 + 分支提交

### P1 系列（P0 全部完成后）
- P1-1 统一 DocumentElement IR（下一步核心重构）
- P1-2 parser bake-off 离线实验
- P1-3/4/5 PDF/Office 保真+安全清洗
- P1-6/7 parent-child chunk + embedding profile
- P1-8/9 pgvector 对照 + retrieval v2
- P1-10/11 回答编排 + 引用校验

## 用户约束
- 只保存到 feature/rag-optimization 分支，main 保持 aa372b6 不动
- 不 push 到远程
- 每个单元完成等用户确认再进下一个
