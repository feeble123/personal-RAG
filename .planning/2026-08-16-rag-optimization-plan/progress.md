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
| P0-10 上传隔离 | ✅ 9dc984f | MIME/signature + zip bomb + quarantine + 二次验证（354 绿） |

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
- ✅ 计划已定：[p1-retrieval-quality.md](p1-retrieval-quality.md)（检索质量主线，用户确认裁剪决策）
- ✅ 单元1：P1-11 评测门禁（gold 集 + scorers + run_eval CLI + retrieve 最小 trace；基线 + 水力学主料后 R@5=65% R@10=75%，enumeration 偏弱 37.5% → P1-9 coverage plan 改进方向）
- ✅ 单元2：P1-1 DocumentElement IR 重构（ir.py + ir_validation.py + ParsedBlock.to_element adapter + 5 parser 产出 elements + snapshot 固定；381 绿）
  - 📥 主测试料：《水力学 上 第5版》(OCR) 547页入库（库12，1445 chunks），以后评测/验证用它
- ✅ 单元3：P1-7 embedding profile 指纹（EmbeddingProfile + fingerprint + EmbeddingCache 复合主键迁移 + 维度写入守卫；387 绿）
- ✅ 单元4：P1-6 parent-child 切片（ParentChildChunk + chunking/parent_child.py + 两遍插入父子同表 + 检索短子块注入父上下文 + 迁移 f9a0b1c2d3e4；392 绿）
- ✅ 单元5：P1-9 检索校准流水线（RRF 融合 + 枚举扩展排名加权；**评测 R@5 85%→95%、R@10 90%→100%**；392 绿）
- 📋 用户反馈后新规划：[p1-eval-rigor-and-answer-quality.md](p1-eval-rigor-and-answer-quality.md)（检索严谨化 + 回答质量评测；单元6 收尾延后）
  - ⬜ 工作包A：检索严谨化（扩大 gold 50+ 问 + 多轮方差 + 分层 + 严格判定 + 参数调优）
  - ⬜ 工作包B：回答质量评测（answer_eval.py 批量回答 + 引用/完备/事实指标）
  - ⬜ 之后：单元6 收尾
- ⬜ 单元4：P1-7 embedding profile（向量指纹）
- ⬜ 单元5：P1-9 检索校准流水线（主线核心）
- ⬜ 单元6：P1-3/4/5 最小集 + 收尾提交
- ⏸ 裁剪延期：P1-2（外部引擎 bake-off）/ P1-8（pgvector）→ 答辩后

## 用户约束
- 只保存到 feature/rag-optimization 分支，main 保持 aa372b6 不动
- 不 push 到远程
- 每个单元完成等用户确认再进下一个
