# Findings: RAG 系统分级优化实施方案

## Inherited Audit Baseline
- 原审计报告：`RAG-SYSTEM-AUDIT-REPORT.md`。
- 当前结论：强内部 Alpha，公网生产 No-Go；生产就绪度约 3.2/10。
- 核心根因：解析阶段先损失结构，切片器随后放大；LLM 切片无法修复版面/表格/阅读顺序缺失。
- 最高风险：跨知识库取数、非原子重解析、全局 chunk hash 去重、历史引用级联删除、弱证据仍生成、默认安全配置。

## Implementation Constraints
- 不修改业务源码，仅交付计划文档。
- 方案需覆盖数据迁移、灰度、回滚和验收，不能只描述最终架构。
- 现有 SQLite/Chroma 数据需要保留并允许重建验证。

## Dependency Order
1. 先建立基线、备份、评测样本和 feature flag，否则重构后无法证明提升或安全回退。
2. 数据隔离、安全默认值、弱证据拒答可以先独立止血。
3. 数据模型迁移必须先于版本化入库；版本化入库必须先于解析器大规模重建。
4. 统一 DocumentElement IR 必须先于 parent-child chunking；不能先继续微调现有字符切片。
5. 解析/切片 gold 与新索引版本稳定后，才校准检索和 evidence 阈值。
6. 检索可复现后，才上线逐声明引用验证、语义缓存和反馈记忆。
7. PostgreSQL/worker/可观测性可以为版本化入库提供生产底座，但迁移要双轨灰度，不能一次性切换。

## Existing Module Map
- 数据模型/迁移：`backend/app/db/models.py`、`backend/app/db/session.py`。
- 入库状态机：`backend/app/modules/ingestion/manager.py`。
- 解析与 OCR：`backend/app/services/parser/*`。
- 切片：`backend/app/services/chunker.py`。
- embedding/cache：`backend/app/services/embedding.py`。
- 向量/BM25/检索：`vector_store.py`、`bm25.py`、`rag.py`。
- 生成/校验/缓存：`qa/routes.py`、`chat.py`、`verify.py`、`semantic_cache.py`、`memory.py`。
- 安全与运行：`core/*`、`main.py`、`deploy/*`。
- 前端关键状态：`frontend/src/stores/chat.ts`、`api/modules.ts`、`pages/KnowledgeBase.tsx`。

## Plan Structure
- 总方案按“准备层 → P0 止血与数据正确性 → P1 解析/切片/检索 → P2 生产工程与前端 → P3 高阶能力”展开。
- 每个工作包固定包含：为什么、前置条件、目标文件/组件、数据/API 变更、逐步实施、测试、验收、灰度/回滚、工时与完成定义。

## External Research
- Alembic 官方 cookbook 支持增量 schema/data migration、事务连接共享、从空库构建后 stamp head，也提供 SQLite batch migration；适合替换当前运行时手写 PRAGMA/ALTER。
- pgvector 官方当前支持 HNSW/IVFFlat、精确搜索、PostgreSQL FTS 混合检索、cross-encoder/RRF，以及 0.8+ iterative scan；多租户强隔离可使用 list partitioning 或独立表。官方同时明确 ANN 加过滤可能少返回结果，需要 iterative scan、召回抽检和 exact baseline。
- Celery 5.6 是当前 stable 文档线；官方任务语义要求任务幂等，`acks_late` 才能在执行后确认，retry 与 worker crash 的恢复策略需分别设计。因此队列不能替代业务侧 job/version/idempotency 表。
- PP-StructureV3 官方管线覆盖文档方向、版面、表格、公式/图表与阅读顺序恢复，符合当前中文扫描规范的主要缺口；必须用本地 gold bake-off，不直接因官方能力宣称替换生产解析器。
- MinerU 官方定位是把复杂 PDF/Office 转为 LLM-ready Markdown/JSON，支持复杂版面、表格/公式与阅读顺序；适合作为 PP-StructureV3 的主对照方案。
- Docling 官方提供统一 Document 模型、OCR 引擎、表格导出、全页 OCR、confidence、序列化与 hybrid chunking；适合作为中间表示和跨格式能力的第三基线，但最终 IR 仍应由本项目控制。
- Ragas 官方当前提供 context precision/recall、noise sensitivity、response relevancy、faithfulness、factual correctness 及 SQL 等指标；可作为自动评测组件，但需要本项目 deterministic gold 与人工复核校准。
- OpenTelemetry Python 官方支持 traces/metrics/logs 与自动/代码插桩；适合贯穿 API → Celery job → parser → embedding → retrieval → LLM 的 trace_id。
- OWASP File Upload Cheat Sheet 要求扩展白名单、真实文件类型/签名校验、文件名重写、大小限制、授权、站外存储/隔离、恶意内容扫描和 CSRF 防护；当前系统只做扩展与字节数检查，不足。

## Official Sources
- https://alembic.sqlalchemy.org/en/latest/cookbook.html
- https://docs.celeryq.dev/en/stable/userguide/tasks.html
- https://github.com/pgvector/pgvector
- https://www.paddleocr.ai/main/en/version3.x/algorithm/PP-StructureV3/PP-StructureV3.html
- https://github.com/opendatalab/MinerU
- https://docling-project.github.io/docling/usage/
- https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/
- https://opentelemetry.io/docs/languages/python/instrumentation/
- https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html

## Current Schema/Dependency Facts
- `Document` 只有单一状态/chunk_count，没有 active_version；`Chunk.doc_id` 直连文档且 `content_hash` 全局唯一；`Citation.chunk_id` 非空并 cascade；`EmbeddingCache` 以 content_hash 单列主键。
- `manager.py` 用进程内 `create_task`，在 `_write_lock` 内先删旧 chunk 并 commit，锁外再全库 Chroma reset/rebuild；改造必须拆成 stage + verify + activate + GC。
- 后端依赖只有宽松下界，没有 Alembic、PostgreSQL driver、Celery/Redis、OCR 版面引擎、OpenTelemetry 或评测工具；方案需要独立 lock/dev/prod optional groups。
- 当前 Dockerfile 基于 Python 3.10，compose 构建上下文与 Dockerfile 路径不匹配，且镜像假设 frontend/dist 已存在；生产化必须多阶段构建。
- 前端 package 只有 dev/build/preview，无 lint/unit/e2e；实施计划需补 ESLint/Prettier、Vitest/RTL 和 Playwright。

## Concrete Target Model Changes
- 将 `Document` 作为稳定逻辑文件，新增 `DocumentVersion` 保存 source_hash/parser_profile/status/quality/element_count/chunk_count/created_at；`Document.active_version_id` 指向已验证版本。
- 新增 `DocumentElement` 作为可审计 IR：type/text/page_start/page_end/bbox/reading_order/heading_level/section_path/parent_id/table_json/confidence/source_ref。
- `Chunk` 改为从 `DocumentVersion` 派生，身份使用 `(document_version_id, chunk_index)`；新增 parent_chunk_id/page_start/page_end/token_count/metadata/index_version_id，content_hash 不再全局唯一。
- `Citation.chunk_id` 改可空 `SET NULL`，同时保存 document_id/document_version_id/content_hash/page range/bbox/section/snippet，形成不可变快照。
- `EmbeddingCache` 主键改 `(embedding_profile_id, content_hash)`；profile 包含 provider/base/model/dimension/normalize/query_instruction/doc_instruction。
- 新增 `IndexVersion` 与 `KnowledgeBase.active_index_version_id`；索引只查询 active version，新版完成计数/维度/抽样检索后一次性切换。
- 新增 `IngestionJob`：job_id/doc_id/target_version/stage/status/attempt/lease/heartbeat/progress/error/cancel_requested，Celery task 只引用 job_id。
- 新增 `RetrievalTrace`/`AnswerTrace` 或等价可观测存储，记录 scope、query rewrite、索引/模型版本、候选/重排/pack/验证和耗时。

## API/State Changes
- 检索入口统一接收不可变 `RetrievalScope(kb_id, allowed_doc_ids, user_id/tenant_id, index_version_id)`；任何 doc/title/chapter/enumeration/document-wide 分支不能自行丢弃 scope。
- `qa/routes.py` 拆成 orchestration service；知识问答明确 grounded policy，无证据/不受支持声明返回拒答，不用 prompt 软约束代替校验。
- SSE 事件增加 request_id/conversation_id；前端按 local_message_id 精确更新并在切会话/删除时 abort，禁止更新“最后一条消息”。
- parser 接口从 `ParsedBlock` 升级为统一 `DocumentElement`；chunker 不再猜 parser_mode/heading_level。

## Design Decisions
- 数据库主选 PostgreSQL + Alembic；SQLite 仅保留本地开发/单机模式。
- 向量层在迁移到 PostgreSQL 后主选 pgvector，先以 exact search 建 recall baseline，数据规模/延迟需要时再开 HNSW；不能一开始盲调 ANN。
- 持久任务主选 Celery + Redis，但业务真相保存在 `ingestion_jobs`/`document_versions` 表；Celery 只负责投递和执行。
- 复杂中文扫描解析先做 PP-StructureV3 vs MinerU bake-off，Docling 为第三基线；保留 PyMuPDF 快速通道。
- 切片重构使用自有 DocumentElement IR + tokenizer-aware parent-child，避免被特定解析器输出格式绑死。
