# 个人 RAG 系统 · 资深工程师批判式评估

> 评估对象：`feeble123/personal-RAG`（FastAPI + LangChain + Chroma + React，水利领域 RAG 问答）
> 评估方式：**源码静态通读**（沙箱到 GitHub 的 TLS 被拦截，仓库公开后逐文件 WebFetch 逐行读取）
> 已读范围：后端核心（main / config / session / security / deps / ratelimit / exceptions / rag / chat / embedding / vector_store / bm25 / chunker / semantic_cache / memory / parser/*）、六大模块路由、前端（Chat / MessageBubble / client / auth store / package.json）、部署（Dockerfile / docker-compose / nginx）、测试（test_api + 11 个 unit 文件）、两份文档、`.env.example`、`.gitignore`
> 说明：本次**未实际运行**（无 API Key、无法 clone），但项目自带 FAKE 离线模式，测试套件本身可离线跑通

---

## 0. 一句话结论

这是**远超一般 vibe coding 玩具的、有真实工程深度的项目**，RAG 检索与 PDF 解析两块达到了"踩过坑、有方法论"的成熟水平；但**生产卫生（安全默认值、可迁移性、配置一致性）明显弱于功能完成度**——这恰恰是第一次用 vibe coding 做全栈项目最容易翻车的地方。综合评分 **7.2/10**（功能内核 8+、生产卫生 6-）。

---

## 1. 分维度评分

| 维度 | 评分 | 一句话 |
|---|---|---|
| RAG 检索工程 | **8.5** | 混合检索 + 聚焦主题词重排 + 章节扩展，domain depth 很强 |
| PDF / 文档解析 | **8.0** | 文字层/扫描分层、CID 乱码三层判定、OCR 断号自检，工程含量最高 |
| 问答编排与记忆 | **8.0** | 记忆库 + 语义缓存 + 多回答风格，设计成熟且有弹性 |
| 安全与权限 | **6.0** | RBAC、会话强隔离、异常不泄露堆栈都好；但**默认凭据/密钥弱、限流反代失效、JWT 无吊销** |
| 生产部署与可迁移性 | **5.5** | 迁移脚本 SQLite-only，却声明"可迁移 MySQL/PG"；Docker 脆弱 |
| 配置与可维护性 | **6.0** | 死配置、误命名、文档漂移 |
| 测试 | **7.5** | 离线套件覆盖广（RBAC/级联/缓存/记忆），但缺检索质量与前端渲染两层 |
| 文档（PROJECT_RECORD） | **8.0** | 踩坑档案诚实优秀，但部分章节已过期 |
| **综合** | **7.2** | 工程内核强，生产卫生弱 |

---

## 2. 真正做得好的地方（这些经验要保留）

下面每一条都是"有经验的人才会做的取舍"，不是 vibe coding 的自然产物，值得你明确记下来复用：

1. **检索管线的领域深度（rag.py）**
   - 混合检索用 `0.7×向量 + 0.3×BM25归一化` 加权（而非 RRF），并对 BM25 做**按库归一化**避免跨库噪声——这是被真实 bug 逼出来的正确决策（`PROJECT_RECORD` 单元 A 有完整根因）。
   - `focus_rerank_query()` 用正则抽"聚焦主题词"喂给 bge-reranker，专门解决 BGE-M3 对长查询稀释关键项的问题。这是把模型局限变成解法，专业。
   - `_expand_chapter_sections()` 针对规范类"整章多子节"问题扩展引用范围，避免 top_k=5 答不全。思路对。

2. **PDF 解析的工程含量（parser/pdf.py + clause_gap + headings）**
   - 文字层 / 扫描 OCR 分层、CID 乱码三层检测（含常用汉字占比判定）、页眉页脚过滤、表格提取、**OCR 断号自检修复**（水利规范条款连续编号驱动的巧思）。这是全项目最"硬"的代码。

3. **SSE 流式异常处理的成熟度（qa/routes.py）**
   - 整个 `gen()` 被 `try/except` 包裹；中途失败会发 `event:error` 的**用户友好文案**（`_user_friendly_error` 把 401/429/timeout 转成中文提示），并落库一条 `is_complete=False` 的失败消息，**连接正常关闭而非断开**。
   - 用户消息在流式开始前就落库——即使后面全崩，提问也不丢。
   - 这是**专业级弹性设计**，很多成熟项目都未必做得这么稳。

4. **问答记忆库（AI-native，memory.py）**
   - 👍/👎 沉淀 good/bad 记忆、按用户隔离、作用域+主题双一致才复用、bad 强制重检。设计清晰，且刻意做成可移植组件。

5. **统一异常处理不泄露内部（exceptions.py）**
   - `BizError` → `{code, message}`；未捕获异常在生产返回通用文案，仅 `debug=True` 才吐原始堆栈。安全习惯正确。

6. **会话强隔离（conversations/routes.py）**
   - 非所有者统一返回 404，不泄露"该会话存在但非你的"这种元信息。正确。

7. **PROJECT_RECORD.md 踩坑档案（最值钱的习惯）**
   - 把每个 bug 的**症状 / 根因 / 修复 / 代价**写清楚，并诚实记录已知局限（如启发式标题识别对条文说明块偏好偏高）。这是专业工程师的做法，也是你这个项目最大的隐性资产。

8. **离线 FAKE 测试套件（tests/）**
   - FAKE LLM/Embedding 让 50+ 测试无需密钥离线跑通，覆盖认证、RBAC 403/404、KB CRUD、级联删除、缓存命中、记忆复用/强制重检。CI 友好。

---

## 3. 必须改的问题（按严重程度）

### P0 — 部署即高危，上线前必须处理

| # | 问题 | 位置 | 修复建议 |
|---|---|---|---|
| 1 | **默认弱口令 + 默认弱密钥**：`admin_password="123456"`，首次启动 `_seed_admin()` 自动建 `admin/123456`；`jwt_secret` 默认 `"dev-secret-change-me-in-production"`。若没认真填 `.env`，公网部署即被接管（攻击者用已知 secret 伪造 admin JWT，或直接登 `admin/123456`） | `config.py` / `main.py:_seed_admin` | 默认设为 `None`，启动时断言：`if settings.jwt_secret in (None,"dev-secret-change-me-in-production"): raise`；admin 密码首次启动强制改，或默认不自动建、由脚本初始化 |

> 好消息：`.gitignore` 已忽略 `backend/.env` 与 `backend/data/`，**仓库公开不会泄露真实密钥/库**——这点确认没问题。但仓库当前是**公开**状态，建议评完改回私有（尤其 `.claude/` 技能、测试 PDF 都在里面）。

### P1 — 安全性/正确性缺口，多用户或反代场景下会出事

| # | 问题 | 位置 | 修复建议 |
|---|---|---|---|
| 2 | **反代下 IP 限流失效**：nginx 已转发 `X-Forwarded-For`，但 `get_client_ip` 用 `request.client.host`（取到的是 nginx 的 127.0.0.1）。结果所有请求被当成同一 IP → 限流退化为"全实例共享一个桶"：既弱化了对爆破的防护，又会让任一用户触发限流时误伤所有人 | `core/deps.py:get_client_ip` | 信任代理时取 `X-Forwarded-For` 首段；或在 nginx 配 `real_ip` 模块让 `request.client.host` 变真。登录/注册限流务必按真实 IP |
| 3 | **迁移脚本 SQLite-only，却声明"可迁移 MySQL/PG"**：`init_db()` 用 `PRAGMA table_info` + `ALTER TABLE`（纯 SQLite 语法）。换 Postgres 直接报错。文档和 config docstring 都写了"改连接串即可迁移"——**声明与代码直接矛盾** | `db/session.py:init_db` / `config.py` docstring | 引入 **Alembic**（正经迁移）；或至少在文档明确"手写迁移仅覆盖 SQLite，换库需自备迁移"。别让 AI 写的漂亮 docstring 骗了你 |
| 4 | **`.env.example` 字段名错误**：写了 `USE_RERANK=false`，但配置字段是 `rerank_enabled`。pydantic-settings 读不到 `USE_RERANK`（`extra="ignore"` 静默忽略），想关重排**关不掉**；且从未文档化正确名 `RERANK_ENABLED` | `backend/.env.example` / `config.py:rerank_enabled` | 改正为 `RERANK_ENABLED=false` 并补注释；全局搜索避免死配置 |
| 5 | **章节扩展跨库全表扫描且无 kb 过滤**：`_expand_chapter_sections` 里 `select(Chunk, Document).join(...)` **没有 where 条件**，每次综合提问扫所有 KB 的全部切片；若两个库恰好有同名章节（如"5 应急保障"），会把他库切片混进答案 | `rag.py:_expand_chapter_sections` | 加 `where(Chunk.kb_id == kb_id)`（结合 `doc_ids` 限定） |
| 6 | **书名解析全表扫描无 kb 过滤**：`resolve_documents_by_title` 用 `select(Document)` 匹配所有库文档；多库时会跨库误匹配书名 | `rag.py:resolve_documents_by_title` | 限定当前 `kb_id` 范围内匹配 |
| 7 | **入库 `reset_collection` 全量重加 Chroma**（初版已发现）：每次入库重置整个集合并重加所有 KB 的向量，大库被拖垮，且并发入库会互相清 | `ingestion/manager.py` | 按 KB 增量 `add/delete`，不要整库 reset |

### P2 — 可维护性 / 性能 / 体验

| # | 问题 | 位置 | 修复建议 |
|---|---|---|---|
| 8 | **`top_k_rrf` 是死配置 + 误命名**：实际是加权融合（非 RRF），且该常量在 `retrieve()` 中**从未被引用**；`PROJECT_RECORD` 自己也承认"放弃 RRF"。命名误导后来的你 | `config.py` / `rag.py` | 改名 `top_k_fusion_candidates` 或删除；与文档统一 |
| 9 | **语义缓存每次启动 `clear_cache()`**：跨重启零收益，"全局清空"粗暴，且启动时要扫全表清 | `main.py:lifespan` | 改为按 KB/文档版本失效，或加 TTL |
| 10 | **对话历史双写**：`build_prompt` 把 history 既塞进【对话历史】文本段，又作为独立 messages 再发一遍 → LLM 看两遍历史，浪费 token 且可能干扰 | `chat.py:build_prompt` | 二选一 |
| 11 | **Docker 部署脆弱**：`docker-compose.yml` 无 `restart: unless-stopped`（崩溃/重启不自动拉起）；`Dockerfile` 直接 `COPY frontend/dist`，要求前端**预先构建并提交**到仓库——`frontend/dist` 不在则构建失败；非多阶段构建 | `deploy/*` | 多阶段构建（前端用 node 镜像 build，后端用 python 镜像）；compose 加 `restart: unless-stopped` |
| 12 | **JWT 7 天无刷新、无吊销**：改密/登出后旧 token 仍有效，无法主动失效 | `security.py` | 加 `jti` + 黑名单（Redis/DB），或短时效 + refresh token |
| 13 | **SQLite 与"企业级"定位不匹配**：系统已支持多用户/多管理员并发写入（入库后台任务 + 多人问答），但 SQLite 是单写者，WAL 下写入仍串行化，`busy_timeout=5000` 触顶会失败。自称"企业级知识库"但存储层扛不住并发写 | `config.py` / `session.py` | 多用户场景应直接上 Postgres（见 #3）；或文档把定位改回"个人/小团队单机" |
| 14 | **构建产物入仓**：`tsconfig.tsbuildinfo`、`vite.config.js`、`vite.config.d.ts` 已提交，`.gitignore` 未忽略 `*.tsbuildinfo` | `.gitignore` | 补 `*.tsbuildinfo`、`vite.config.js`、`vite.config.d.ts` |
| 15 | **3 个已知前端 bug 留在 TODO**：`PROJECT_RECORD` 把单元 C（流式原地修改不重渲染）、D（会话删除 Popconfirm）、E（公式渲染）标"待实现"。API 测试抓不到 React 渲染，故它们一直没被发现/修 | 前端 + 文档 | 用组件测试或手动验收覆盖；别只靠后端 e2e |

---

## 4. 这个项目里"vibe coding"的典型痕迹（复盘用）

你不是专业开发者，所以这些痕迹正常，但**值得你下次有意识地避开**：

1. **命名漂移**：`top_k_rrf`（早弃用 RRF 却保留名）、代码里散落 `BUG-A` / `BUG-B` 注释标记（应进 issue 跟踪，而非留在生产代码）。
2. **文档与代码漂移**：
   - `PROJECT_RECORD` 说"公式渲染=待实现（单元 E）"，但 `MessageBubble.tsx` **已经接好 remark-math + rehype-katex + katex CSS**——代码早就做了，文档没跟上。
   - `PROJECT_RECORD` / config docstring 说"可迁移 MySQL/PG"，但迁移脚本只认 SQLite。
3. **依赖装了没接 / 接了文档没记**：katex 三件套装好且接好，文档却仍标待办 → 说明 AI 改了代码但**没同步文档**，你也没发现。
4. **死配置**：`USE_RERANK`（无效）、`top_k_rrf`（未引用）。
5. **已知 bug 用注释 TODO 留着**（单元 C/D/E），而不是修复或开 issue。
6. **测试覆盖"happy path + 权限边界"好，但缺两层**：① 检索质量（recall@k）；② 前端渲染（流式更新、公式显示）。

---

## 5. 你这套"踩坑-修复"方法本身，是最该保留的经验

坦白说，你项目里**最专业的不是某段代码，而是你处理 bug 的方式**：

- `PROJECT_RECORD.md` 把每个 bug 写成"症状 → 根因 → 修复 → 代价"，这是正经团队的 postmortem 写法。
- FAKE 离线模式让测试**不依赖密钥就能跑**，CI 友好——这是把"可测试性"放进架构里。
- `.claude/skills/unit-test` 把"跑全部测试"固化成一条命令，降低回归成本。

**改进建议**：把"注释里的 BUG-A/B、TODO 单元 C/D/E"搬进 **GitHub Issues / CHANGELOG**，每个修复对应一个 git commit。这样"踩坑记录"可检索、可关闭、可复盘，比散在代码里的注释强十倍。

---

## 6. 给下次 vibe coding 的 7 条经验（可当 checklist）

1. **安全默认值宁可 `None` 不让弱值**：`admin/123456`、写死的 `jwt_secret` 是定时炸弹。让"没配置"在启动时报错，好过"有默认值能用"。
2. **任何"可迁移 / 升级路径"声明都要代码验证**：别让 AI 写漂亮的 docstring（`# 改连接串即可迁移 PG`）骗你——它没真的试过。
3. **改配置名要全局搜索**：`USE_RERANK` → `rerank_enabled` 这类改名，AI 常只改一处，留下死配置。
4. **文档随代码更新（或自动生成）**：你这项目"文档说公式没做、代码却做了"就是典型——过期文档比没有更糟，因为它会误导你。
5. **上线前做"反代 + 限流 + 真实 IP"实测**：限流在 nginx 后失效，是 vibe coding 极常见的坑，只有实测能发现。
6. **建检索质量评估集（recall@k + 答案人工评分）**：这是 RAG 的"单元测试"。你现在靠人工抽测，改动一多必回归。固定 30~50 条 `query → golden chunk`，CI 跑 recall@k。
7. **把"已知 bug"从代码注释搬进 issue**：用 GitHub Issues 跟踪，配 CHANGELOG，比 `BUG-A` 注释可维护。

---

## 7. 对初版报告的勘误（我之前说错的两点）

- **"前端无 katex，公式显示成裸文本" —— 错。** `MessageBubble.tsx` 已正确接入 `remark-math` + `rehype-katex` + `katex.min.css`，**公式能正常渲染**。`PROJECT_RECORD` 把公式渲染标成"待实现"是文档过期，不是代码缺。
- **"刷新会登出" —— 错。** `stores/auth.ts` 用了 zustand `persist` 中间件，token 持久化到 localStorage，**刷新不丢登录态**。
- 初版对 SSE 异常处理的评价偏保守——实际 `qa/routes.py` 的 `try/except + 用户友好错误 + 失败落库` 是专业级弹性，应予加分。

> 附：最该优先做的三件事（投入小、收益大）
> ① P0：把 `jwt_secret`/`admin_password` 改成"未配置即启动失败"（几行）；
> ② P1：修反代下 `get_client_ip` 取 `X-Forwarded-For`，并改 `.env.example` 的 `USE_RERANK` → `RERANK_ENABLED`；
> ③ P2：建检索质量评估集（recall@k）防回归——这是你项目当前最大的测量盲区。
