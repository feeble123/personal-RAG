# 项目技术档案 — 基于 LangChain 的 RAG 企业级知识库问答系统

> 更新日期：2026-08-23
> 定位：**真实落地、长期演进的产品项目**（非毕业设计演示）。面向**水利工程**领域，浏览器操作，回答自动引用知识库片段，支持多知识库、多会话、全格式文档入库。终极愿景：作为未来 AI Agent 的个性化长期知识库底座（见 `docs/agent-vision.md`）。
> 本档案记录：项目概况、技术路线、已完成/未完成内容、预留升级接口、未来改造方向与提升建议。
> ⚠️ 项目最高权威指引见根目录 `CLAUDE.md`；本文档与 CLAUDE.md 冲突处以 CLAUDE.md 为准。

---

## 一、项目概况

| 项 | 内容 |
|---|---|
| 项目名 | 基于 LangChain 的 RAG 企业级知识库问答系统 |
| 领域 | 水利工程（规范 / 工程报告 / 模型参数 / 教材等强结构文档为主） |
| 形态 | B/S 架构；后端 FastAPI 服务 + 前端 React 单页；单机部署（SQLite + Chroma 嵌入式） |
| 核心价值 | 上传文档 → 自动解析分块向量化 → 混合检索 + 重排 → LLM 生成带引用答案；全流程可溯源 |
| 默认账号 | 管理员 `admin` / `123456`（首启自动创建）；普通用户可注册 |
| 文档格式 | PDF（文字层/扫描 OCR 分层）/ Word(.docx) / Markdown / TXT / Excel(.xlsx/.csv) |
| 部署形态 | 单机：后端托管前端构建产物；升级路径见第六节 |

---

## 二、系统架构与技术路线

```
浏览器 (React 18 + AntD 5 + Vite) ──JWT──▶ FastAPI (async) ──▶ SQLite(WAL) · Chroma(.chroma/)
      │ 登录/注册/问答/会话            modules: auth/users/conversations/knowledge/qa/ingestion
      │ 上传文档/知识库管理            services: parser→chunker→embedding→vector_store→rag→chat
      ▼ SSE 流式 (POST+ReadableStream)   ──▶ DeepSeek API / 硅基流动 OpenAI 兼容 API
```

**文档入库流**：上传(≤200MB 流式写盘) → 后台任务（信号量限并发）→ 分层解析(文字层/OCR) → 结构感知分块(注入章节上下文) → embedding 缓存向量化 → Chroma + BM25 更新 → 语义缓存清空。

**问答流（SSE 流式）**：鉴权 → 存用户消息 → 混合检索（向量 + BM25 加权 → **bge-reranker 重排**）→ 语义缓存检查 → LCEL 组装 prompt（引用编号）→ DeepSeek 流式生成 → 引用落库 → 前端渲染引用卡片。

**核心设计取舍**：
1. **检索质量优先**：放弃 RRF 排名融合（分数窄带、跨库噪声），改为 0.7×向量相似度 + 0.3×BM25 归一化加权，再经 bge-reranker 交叉编码重排（详见第六节修复记录）。
2. **零嵌入成本**：BGE-M3 走硅基流动免费额度；测试用 FAKE 嵌入离线跑通。
3. **结构感知**：规范类强结构文档需要 1/2 级标题硬边界分块（进行中，见第六节），是目前与检索质量并列的关键改进方向。

---

## 三、技术栈明细（每层具体技术）

### 后端（Python 3.11+，FastAPI async）
| 层 | 具体技术 | 版本/说明 |
|---|---|---|
| Web 框架 | FastAPI + Uvicorn | fastapi>=0.115，uvicorn[standard]>=0.30，SSE 流式 |
| RAG 框架 | LangChain（LCEL）+ langchain-core | langchain>=0.3.14；`langchain_deepseek` 接 DeepSeek |
| LLM | `deepseek-chat`（ChatDeepSeek） | 温度 0.2，max_tokens 1500，超时 120s，重试 3 |
| Embedding | 硅基流动 `BAAI/bge-m3`（OpenAI 兼容 `/v1/embeddings`） | 免费；1024 维；`EMBEDDING_*` 可切换任何厂商 |
| 重排 | 硅基流动 `BAAI/bge-reranker-v2-m3`（`/v1/rerank`） | 候选 20 → top 5；`RERANK_*` 可开关/换模型 |
| 向量库 | Chroma 嵌入式（langchain-chroma / chromadb>=1.0） | HNSW：cosine、ef_construction=200、max_neighbors=32、ef_search=100 |
| 关系库 | SQLite + SQLAlchemy 2.0 async + aiosqlite | WAL + 连接池(10/20) + PRAGMA；可迁移 MySQL/PG |
| 文档解析 | PyMuPDF(fitz) 分层解析 + pdfplumber/pypdf 辅助 + RapidOCR(onnxruntime) | PDF 文字层/扫描 OCR 双路径；docx/xlsx/md/txt 各一解析器 |
| BM25 | rank-bm25 + jieba 分词 | 启动预热重建语料，单库/跨库按库归一化 |
| 语义缓存 | SQLite 表 + 余弦阈值 | 阈值 0.92，池 200，上限 500；启动/入库/删除自动清空 |
| 认证/安全 | PyJWT + bcrypt + slowapi | JWT 7 天；认证按 IP、问答按用户限流 |
| 网络 | httpx（异步）+ tenacity | OCR/LLM/embedding API 调用与重试 |

### 前端（TypeScript 5，React 18）
| 层 | 具体技术 | 说明 |
|---|---|---|
| 框架 | React 18.3 + Vite 5 | 路由懒分包、构建产物 gzip |
| UI | Ant Design 5 + @ant-design/icons | 深浅主题、中文文案 |
| 状态 | zustand（UI/流式）+ @tanstack/react-query（服务端缓存） | SSE 流式解析在 store 中完成 |
| Markdown | react-markdown 9 + remark-gfm | 引用卡片独立组件；数学公式渲染为待办（单元 E） |
| 虚拟列表 | react-window | 历史消息懒加载 |
| HTTP | axios + fetch(ReadableStream) | 上传进度、SSE 流式 |

### 测试
- pytest（pytest-asyncio）端到端 + 单元测试；conftest 用 FAKE embedding/LLM 离线、临时数据目录隔离。
- `.claude/skills/unit-test`：`/unit-test` 一条命令跑全部测试。

---

## 四、已完成内容

### 4.1 功能构建（M1–M6，全部完成并验证）
1. 多知识库管理（仅管理员）：增删改查、文档上传（≤200MB）、重解析、后台异步入库+进度、检索质量预览。
2. 知识库问答 + 引用：RAG 生成、引用卡片（来源文件/页码/章节/原文片段）、点击查看全文、跨库问答。
3. 多用户多会话：会话归属强隔离（他人访问 404）、标题自动生成、历史懒加载分页。
4. 历史持久化：消息与引用全部落库，登录可完整找回（含引用还原）。
5. 账号体系：注册/登录/改密；管理员/普通用户角色；限流。
6. 企业级性能优化：流式输出、并发入库、embedding 内容哈希缓存、HNSW 调优、混合检索+重排、SQLite WAL+池、游标分页、语义缓存、限流、启动预热、GZip、前端优化。

### 4.2 测试与质量
- 54 个单元测试（安全/分块/解析辅助/文本解析/PDF 解析/检索/语义缓存/嵌入）+ 端到端测试全绿。
- `/unit-test` skill 集成。

### 4.3 BUG 修复（当前批次）
- **单元 A（完成）**：检索链路重写——RRF→向量+BM25 加权→bge-reranker 重排；语义缓存自动清空（修复"修复后仍错"的根因：旧答案被缓存重放）；短块过滤（min_content_len=40）。真实 E2E：长问题命中 3.2.1 通信网络(0.9991)。
- **单元 B 第一部分（完成）**：UI 解析透明度——解析质量列（文本层/OCR/文本+OCR + 页数 + 置信度）、解析阶段进度条、文档解析详情弹窗、`parsed_at` 字段透出。前端 `tsc + vite build` 通过。

---

## 五、未完成内容（待办）

| 单元 | 内容 | 状态 |
|---|---|---|
| 单元 B 收尾① | **统一标题识别模块**（`headings.py`：编号模式 + OCR bbox 行高字号 + 文本层字号；3 级条款排除） | ✅ 实现 |
| 单元 B 收尾② | **分块器重构**（1/2 级硬边界 + 层级前缀 `## 3 正文部分 / 3.2 引用标准` + 3/4 级按长度切） | ✅ 实现 |
| 单元 B 收尾③ | 全量重灌脚本 `scripts/reingest_all.py` + 重测「引用标准」题 | ✅ 完成（doc3 失败已修复重灌中） |
| 单元 B 收尾④ | OCR 实时进度（`ocr_progress.py` tracker + 文档列表内联 + 前端真实进度条） | ✅ 实现 |
| 单元 B 收尾⑤ | 文档切片浏览界面（知识库页第三标签 + doc 筛选） | ✅ 实现 |
| 检索增强 | **rerank_candidates 20→100**：修复 BGE-M3 对抽象查询漏召回（正确切片向量仅 0.22 → 候选池扩大后 reranker 0.86 → top5 → 答案与原文逐条匹配） | ✅ 完成 |
| 单元 C | `chat.ts` SSE 处理器改不可变更新（`last.content += text` 原地修改 + memo 不重渲染 → 流式不更新） | 待实现 |
| 单元 D | `SessionSidebar.tsx` 删除会话改 `modal.confirm`（Popconfirm 嵌 Dropdown label 点击即卸载） | 待实现 |
| 单元 E | 安装 remark-math + rehype-katex 渲染 LaTeX 公式 | 待实现 |
| 单元 F | 重写 `SYSTEM_PROMPT`（有层次/先结论后依据/引用自然融入）+ MessageBubble 补 markdown 排版样式（用户层次感要求） | 待实现 |
| 验证 9 | 修复后用实际问答复测 | 待实现 |

---

## 六、BUG 修复记录（关键根因与方案）

### 单元 A：检索融合（已完成）
- **症状**：引用来源混乱、跨库污染、检索预览与回答不符。
- **根因**：`_rrf` 融合只认排名，分数压成 0.023~0.031 窄带，跨库时无关 KB 的 BM25 噪声被抬进 top5；BGE-M3 对部分查询区分度不足；语义缓存残留旧错误答案被重放。
- **修复**：跨库纯向量 + 单库 BM25 加权（0.7/0.3，按库归一化）→ bge-reranker-v2-m3 重排（候选 20）→ 短块过滤；启动/入库/删除时 `semantic_cache.clear_cache()`。

### 单元 B：UI 解析透明度 + 结构识别 + 检索召回（已完成）
- **结构根因**（2026-08-04 纯扫描 PDF 实测）：**OCR 路径从不识别节标题**——`_blocks_from_ocr` 把 OCR 段落一律标成 `paragraph`，`section` 全为 `None`；「3.2 引用标准」被拦腰切进两个切片（idx=6 尾部被总则淹没 / idx=7 头部），检索 top5 无正确切片，LLM 无原文可依 → 编造。
- **修复①结构**：`headings.py` 统一标题识别（编号模式 `第X章/X.Y/附录X/条文说明` + OCR bbox 行高字号推断 + 文本层字号；3 级及以上条款 `_CLAUSE_RE` 排除）；`chunker.py` 1/2 级硬边界 + 层级前缀 `## 3 正文部分 / 3.2 引用标准` + 3/4 级按长度切。扫描件切片 41→170，section 全部填充。
- **修复②检索召回**：实测 BGE-M3 给「3.2 引用标准」正确切片仅 **0.22 向量分**（抽象/长问题区分度不足，已知模型局限），0.7 向量权重下 hybrid 排 76 名，`rerank_candidates=20` 时 reranker 见不到它 → 答案编造。**`rerank_candidates` 20→100** 后 reranker 给正确切片 0.86 → 进入 top5 → 答案与原文逐条匹配。代价：问答检索 ~3.3s（可配置）。
- **修复③工程**：OCR 实时进度（`ocr_progress.py` + 文档列表内联 `progress` + 前端真实进度条）；文档切片浏览界面（第三标签 + doc 筛选）；并发重灌 content_hash 去重竞态加 `_write_lock` 串行化。
- **已知局限**：封面/目录/条文说明块在启发式标题识别下会产生部分噪声 section（如 `SL / 中华人民共和国水利行业标准 / …` 深层路径），rerank 对条文说明块偏好偏高——**升级路径 = 版面分析模型（PP-Structure / MinerU），见第七节预留接口**。

---

## 七、预留升级接口（可插拔设计）

> 设计原则：**任何第三方服务都通过配置/接口替换，改一层不伤其余**。以下接口当前已预留，未来改造时上层代码零改动。

| 层 | 预留接口 | 升级为 | 预期效果 |
|---|---|---|---|
| LLM | `LLM_PROVIDER` + `build_chat_model()` 工厂 + `reset_chat_model()` | OpenAI / Qwen / 本地 Ollama | 换模型改 `.env`，代码零改动 |
| Embedding | `EMBEDDING_PROVIDER` / `EMBEDDING_*` 配置 | 任意 OpenAI 兼容厂商 / 本地模型 | 换向量模型改配置 |
| 重排 | `RERANK_ENABLED / RERANK_MODEL / RERANK_CANDIDATES` | 换 rerank 模型 / 本地 reranker | 检索精度可调 |
| 向量库 | `services/vector_store.py`（query/delete/get_embeddings_by_ids/add 封装） | Milvus / Qdrant / pgvector | 百万级向量规模，只改一个文件 |
| 关系库 | `DATABASE_URL` 连接串 | MySQL / PostgreSQL | 高并发/多租户，改连接串 |
| RAG 编排 | `rag.retrieve()` + LCEL 阶段函数 | LangGraph 图节点 | 多步 Agent / 工具调用 / 自反思 |
| 文档解析 | `DocumentParser` 抽象 + `factory.get_parser()` 注册表 | 新格式 / 新引擎 | 新解析器注册即用 |
| **OCR（本批新增）** | `OCR_ENGINE`(rapid/paddle) + **`OCRResult` 扩展携带 bbox** + **统一标题识别模块（策略化）** | PaddleOCR PP-Structure / MinerU / Marker / Docling 版面分析 | 扫描件/报告标题、表格、公式识别升级；版面模型输出可直接喂给现有标题识别，无需改分块与检索 |
| 语义缓存 | `SEMANTIC_CACHE_ENABLED` + `clear_cache()` | 精细化（按 KB/文档维度、TTL） | 缓存命中更准，减少误重放 |
| 部署 | `deploy/`（Nginx 反代 / Docker 示例） | 多机 / 容器编排 | 生产部署 |
| 前端 | api 模块化 + 组件化 + theme | 公式渲染、虚拟化、多语言 | 交互与可访问性提升 |

---

## 八、未来改造方向与预期效果

1. **版面分析升级（高优先级）**：接入 PP-StructureV3 或 MinerU/Marker，对扫描版规范/工程报告做布局级标题、表格、公式识别 → 结构识别准确率从启发式提升到模型级，知识库可装更杂乱的真实报告。
2. **LangGraph 多步智能问答**：从"单轮 RAG"升级为"检索-反思-再检索"Agent，支持多文档对比、追问澄清、工具调用 → 复杂水利问题（跨规范条款对照）可直接问。
3. **向量库扩容**：Chroma 嵌入式 → Milvus/Qdrant → 支撑千万级片段、多副本、过滤检索（按时间/文号/机构）。
4. **本地化部署**：Ollama 跑本地 LLM + bge-m3 + reranker → 数据不出内网，满足涉密/合规场景。
5. **知识图谱增强**：规范间的引用关系（本标准引用了 GB/T ×××）、术语词典 → 回答"某规范引用哪些标准"类问题直达来源。
6. **幻觉控制与可解释**：强制引用校验（回答中的引用编号必须出自检索片段）、无引用拒答、置信度展示。
7. **评估体系**：建立检索质量评估集（query + 相关片段 id），量化 recall@k 与答案人工评分，回归防退化。

---

## 九、提升建议（按优先级）

1. **检索质量评估集**：当前检索质量靠人工抽测，建议固化 30~50 条 query+golden chunks，CI 跑 recall@k，防止未来改动回归。
2. **结构感知分块 + 标题识别**（本批在做）：是规范类文档问答质量的关键杠杆，优先完成并全量重灌。
3. **幻觉控制**：SYSTEM_PROMPT 强化"只依据引用回答 + 无引用明说未覆盖"（单元 F），并加引用编号校验。
4. **单元 C/D/E 前端缺陷**：流式更新、会话删除、公式渲染，直接影响体验，尽快修。
5. **语义缓存精细度**：目前全局清空较粗暴，升级为按 KB/文档版本失效。
6. **入库监控**：并发队列长度、OCR 进度（本批在做）、失败重试的可视化。
7. **测试覆盖**：补检索回归测试 + OCR/分块单元测试；扫描件用固定样本做快照对比。
8. **文档**：本档案随版本更新；答辩时可展示升级路径（第六~八节）体现工程性。

---

## 十、验收与测试

- 命令：`cd backend && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`（FAKE 离线模式）
- 前端：`cd frontend && npm run build`（tsc 类型检查 + vite 构建）
- 人工验收脚本：管理员建库→上传（文字层/扫描件各一）→检索预览→问答（长问题验证引用定位）→历史还原。
