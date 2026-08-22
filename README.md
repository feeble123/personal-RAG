# 基于 LangChain 的 RAG 企业级知识库问答系统

> 面向**水利工程基础知识**的企业级 RAG 知识库问答系统。用户通过浏览器完成知识库管理与知识库问答，回答自动引用知识库片段。

## ✨ 功能特性

| 功能 | 说明 |
|---|---|
| **知识库管理**（仅管理员） | 多知识库管理、PDF/Word/Markdown/TXT/Excel 上传（≤200MB）、后台异步入库 + 进度、文档重解析、检索质量预览 |
| **知识库问答 + 引用** | 基于 RAG 生成回答，前端显示引用卡片（来源文件 / 页码 / 章节 / 原文片段），点击可查看全文 |
| **文档类型标注**（P0-11） | 上传时选择「教材 / 规范 / 手册 / 其他」，随每个知识片段保留，供未来 AI 判断引用来源可信度 |
| **对外检索服务**（P0-11） | 独立的 `retriever.py` 检索服务，输出稳定契约（片段 + 出处元数据），未来可套一层 HTTP 供外部 AI 调用 |
| **多用户多会话** | 每个用户独立的会话列表，会话归属强隔离（他人访问返回 404） |
| **历史持久化** | 消息与引用全部落库，任意时间登录可完整找回历史对话（含引用还原） |
| **账号体系** | 注册 / 登录 / 修改密码；管理员 `admin` / `123456`（首启自动创建） |
| **企业级性能优化** | 见下文「性能优化」章节 |
| **增值功能** | 语义缓存秒回、会话标题自动生成、系统统计、多格式解析、扫描 PDF 自动 OCR、深浅主题等 |

## 🧱 技术栈

| 层 | 技术 | 说明 |
|---|---|---|
| 后端框架 | **FastAPI** + uvicorn | 异步、自带 Swagger 文档（`/api/docs`） |
| RAG 框架 | **LangChain**（LCEL）| 必选框架；管线按阶段函数组织，预留 LangGraph 升级路径 |
| LLM | **DeepSeek**（`deepseek-chat`）| `langchain_deepseek.ChatDeepSeek`，流式输出 |
| Embedding | **OpenAI 兼容 API**（硅基流动免费 `BAAI/bge-m3`）| 零嵌入成本；`EMBEDDING_*` 配置可切换任何厂商 |
| 向量库 | **Chroma** 嵌入式 | 持久化到本地，HNSW 参数调优，metadata 过滤 |
| 关系数据库 | **SQLite**（WAL + 连接池）| SQLAlchemy 2.0 async；可迁移 MySQL/PostgreSQL |
| 文档解析 | PyMuPDF + pdfplumber + RapidOCR | PDF 分层解析（文字层 / 扫描 OCR）；docx / xlsx / md / txt |
| 前端 | **React 18 + Ant Design 5 + Vite** | zustand（UI/流式）+ react-query（服务端缓存） |
| 认证 | JWT + bcrypt | 按用户/IP 限流（slowapi） |

## 🚀 快速开始

### 0. 准备 API Key

| Key | 获取方式 | 用途 |
|---|---|---|
| `DEEPSEEK_API_KEY` | [platform.deepseek.com](https://platform.deepseek.com) 充值 | LLM 问答 |
| `EMBEDDING_API_KEY` | [siliconflow.cn](https://siliconflow.cn) **免费注册**（无需绑卡）| 嵌入向量（`BAAI/bge-m3` 免费）|

### 1. 后端

```bash
cd backend
python -m venv .venv
# 国内网络建议用阿里云镜像安装依赖（快且稳定）：
.venv/Scripts/pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
copy .env.example .env        # 编辑填入两个 API Key
.venv/Scripts/python scripts/seed_demo_data.py   # （可选）导入演示数据
start.bat                      # 启动后端 http://localhost:8000
```

> **离线演示**：不填 Key 时可用 `EMBEDDING_PROVIDER=fake` + `LLM_PROVIDER=fake` 启动，回答为模拟内容，便于先跑通全流程（检索质量下降）。

### 2. 前端

```bash
cd frontend
npm install
npm run dev                    # 开发模式 http://localhost:5173
# 或构建产物（单机演示：由后端托管静态文件）
npm run build
```

### 3. 使用

1. 浏览器打开 http://localhost:5173（开发）或 http://localhost:8000（单机托管）
2. **管理员** `admin / 123456` 登录 → 进入「知识库管理」上传文档
3. 任意用户注册登录 → 新建会话 → 选择知识库 → 提问
4. 回答下方显示**引用卡片**，点击查看引用原文

## 📁 项目结构

```
├── backend/
│   ├── app/
│   │   ├── core/           # 配置 / 安全 / 依赖注入 / 异常 / 限流
│   │   ├── db/             # SQLAlchemy 模型 + async session
│   │   ├── modules/        # auth / users / conversations / knowledge / qa / ingestion
│   │   └── services/       # parser / chunker / embedding / vector_store / bm25 / rag / chat / semantic_cache / retriever(P0-11)
│   ├── scripts/            # 演示数据种子
│   ├── tests/              # pytest 端到端测试（离线 FAKE 模式）
│   └── data/               # 数据库 / 上传文件 / Chroma（运行时生成）
├── frontend/
│   └── src/                # React + AntD 前端
└── deploy/                 # Nginx 反代 / Docker（升级路径）
```

## 🔍 系统架构

```
浏览器 (React + AntD) ──JWT──▶ FastAPI (async) ──▶ SQLite(WAL) · Chroma(.chroma/)
      │ 登录/注册/问答/会话        modules: auth/users/conversations/knowledge/qa/ingestion
      │ 上传文档/知识库管理        services: parser→chunker→embedding→vector_store→rag→chat
      ▼ SSE 流式 (POST+ReadableStream)   ──▶ DeepSeek API / OpenAI兼容Embedding API
```

**文档入库流**：上传(≤200MB 流式写盘) → 后台任务（信号量限并发）→ 分层解析(文字层/OCR) → 结构感知分块（注入章节上下文）→ embedding 缓存向量化 → Chroma + BM25 更新。

**问答流（SSE）**：鉴权 → 存用户消息 → 混合检索（向量 + BM25 加权 → **bge-reranker 重排**）→ 语义缓存检查 → LCEL 组装 prompt（引用编号）→ DeepSeek 流式生成 → 引用落库 → 前端渲染引用卡片。

## 🔌 对外检索服务（P0-11，为 DSH 预留）

本项目检索逻辑已抽成独立服务 `backend/app/services/retriever.py`，与页面代码解耦。未来本地 AI 智能体底座（DeepSeek Harness，DSH）可通过 HTTP 调用它获取「相关片段 + 出处」，再综合成带引用的回答。**当前只做了服务层，未实现 HTTP 端点**（下一阶段在 `retriever.py` 外套一层只读 HTTP router 即可）。

**稳定契约（字段名已冻结，不再更改）**：

```json
// 输入（未来 HTTP body）
{ "query": "明渠均匀流形成条件", "top_k": 5, "kb_id": 3 }

// 输出
{ "results": [ {
    "text": "明渠均匀流的形成条件包括：……",
    "score": 0.93,
    "source": {
      "document_name": "水力学.pdf",
      "document_type": "textbook",   // textbook 教材 / standard 规范 / manual 手册 / other 其他
      "section": "7.4 明渠均匀流",
      "page": 215,
      "clause_no": null,
      "formula_no": null,
      "block_type": "text",          // text / table / formula / figure
      "doc_id": 5,
      "chunk_id": 1882
    }
} ] }
```

每个知识片段在入库时保留出处元数据：来源文档名、文档类型（上传时选择）、章节、页码、条款号、公式编号、块类型。未来 AI 引用答案时靠这些信息注明出处。

## ⚡ 性能优化（企业级）

1. **流式输出**：SSE 逐 token，首字即时
2. **并发入库**：信号量限并发 + 解析/OCR 走线程池，不阻塞事件循环
3. **Embedding 缓存**：内容哈希去重 + DB 向量缓存 + 查询 LRU，重入库秒回、省 API 调用
4. **HNSW 调优**：cosine / ef_construction=200 / max_neighbors=32 / ef_search=100
5. **混合检索 + 重排**：向量 top50 + BM25 top50 加权融合 → **bge-reranker-v2-m3 交叉编码重排**（纠正向量模型对部分查询的区分度不足，检索质量关键）
6. **SQLite WAL + 连接池 + PRAGMA**：多读并发、写锁规避
7. **消息游标分页**：历史懒加载
8. **语义缓存**：相似提问（余弦>0.92）直接回缓存答案+引用
9. **限流**：认证按 IP、问答按用户（slowapi）
10. **启动预热**：重启即重建 BM25 语料，首问不慢
11. **HTTP 压缩**：GZipMiddleware
12. **前端优化**：路由懒分包、组件 memo、构建产物 gzip

## 🔄 升级路径（可插拔设计）

| 层 | 当前 | 升级为 | 改动 |
|---|---|---|---|
| LLM | DeepSeek | OpenAI / Qwen / 本地 | `LLM_PROVIDER` + `.env`，代码零改动 |
| Embedding | 硅基流动 bge-m3 | 任意 OpenAI 兼容 / 本地模型 | `EMBEDDING_*` 配置 |
| 向量库 | Chroma | Milvus / Qdrant / pgvector | 替换 `services/vector_store.py` |
| 关系库 | SQLite | MySQL / PostgreSQL | 改 `DATABASE_URL` 连接串 |
| RAG 编排 | LCEL | LangGraph | `services/pipeline/` 阶段函数直接映射图节点 |
| OCR | RapidOCR | PaddleOCR PP-Structure | `OCR_ENGINE=paddle` |
| 部署 | 单机 | Docker / Nginx / 服务器 | `deploy/` 已提供示例 |

## 🧪 测试

```bash
cd backend
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q
# 全量 ~291 个测试：认证 / 权限 / 知识库 / 入库 / 检索 / 会话隔离 / SSE 问答 / 引用还原 /
# 语义缓存 / 统计 / 版本化入库 / 检索契约 / 出处元数据 等（离线 FAKE 模式，无需真实 API Key）
```

## 📚 常见问题

- **上传文件被拒**：仅支持 `pdf / docx / md / markdown / txt / xlsx / csv`；`.doc` 旧格式请另存为 `.docx`
- **扫描版 PDF 怎么处理**：系统自动检测无文本层并走 OCR（RapidOCR）；需要更高精度可配置 PaddleOCR
- **扫描版 PDF 入库慢**：48 页扫描 PDF 在 CPU 上约需 2~3 分钟（OCR 推理），页面会显示「解析中」进度。超大扫描文档建议用独立进程入库：`scripts/ingest_real_pdf.py "<pdf路径>"`（更稳定）
- **首问很慢**：启动已预热 BM25；首次 embedding 调用有网络延迟，之后有查询向量缓存
- **修改端口 / 上传大小**：编辑 `.env` 中 `PORT` / `MAX_UPLOAD_SIZE`
