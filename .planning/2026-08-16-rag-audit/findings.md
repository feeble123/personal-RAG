# Findings & Decisions: RAG 系统全面审计

## Requirements
- 全面阅读工作区文件并进行功能检查。
- 批判性评估文件处理与 OCR、切片策略、索引效果、程序架构、技术栈选型。
- 特别审查两类切片策略，以及 LLM 增强切片效果不佳的根因。
- 判断现有问答准确性是否可靠，识别上线后可能暴露的问题。
- 明确做得好、待优化、完全错误及未来功能方向。
- 输出能直接指导后续优化的完整中文报告。

## Research Findings
- 审计开始时工作树已有多处修改与新增文件，主要覆盖 parser、chunker、ingestion、knowledge API、数据库模型和相关测试；必须避免覆盖。
- 根目录包含 backend、frontend、deploy、docs、PDF test、README.md、evaluation-report.md 和 start.bat。
- Git 状态命令可用，但用户级全局 ignore 文件因权限产生警告；该警告本身不代表项目错误。
- 第一次递归枚举触及大量依赖/缓存文件并因旧版 .NET 缺少 `GetRelativePath` 失败；后续清单必须区分项目自有文件与 node_modules/venv/缓存/构建产物。
- 排除 `.git/.planning/node_modules/.venv/__pycache__/.pytest_cache/dist/build` 后仍有 232 个文件；其中包含源码、测试、配置，也包含 SQLite/Chroma 持久化文件及用户上传的 PDF/DOCX/XLSX。
- 依赖与运行产物规模显著：backend 共约 29,360 个文件/1.08 GB，frontend 共约 25,995 个文件/202 MB，主要来自 `.venv` 与 `node_modules`；这类第三方文件不应逐行作为项目源码审计，但其依赖声明、锁文件和是否被版本控制必须审查。
- 仓库中存在真实 `backend/.env`、`backend/data/app.db`、WAL/SHM、Chroma 索引与多份上传文档；需要检查 Git 跟踪/忽略状态及敏感数据泄露风险，读取 `.env` 时只检查键名与危险默认值，不回显秘密值。
- 项目自有审计文本文件共 134 个、约一万余行；体量适合逐组完整阅读。关键大文件包括 `qa/routes.py`、`rag.py`、PDF/outline/toc 解析、API 测试和知识库前端页面。
- `git ls-files -- backend/.env backend/data ...` 无输出，表明本次检查时真实环境文件与运行数据未被 Git 跟踪，这是正确做法。
- `.env` 仅启用了 JWT、LLM 和 embedding 相关键；其注释文本在当前 PowerShell 默认解码下出现乱码，需以显式 UTF-8 重读，不能据此断言仓库源文件本身损坏。
- 现有 `evaluation-report.md` 是一份先前的静态审查，已经指出弱默认凭证、代理限流、SQLite-only 迁移、配置漂移等问题；本次必须独立复核，且补上实际测试和最新未提交解析改动的评估。
- 已复核的生产阻断风险：`config.py` 内置可预测 JWT secret 与 `admin/123456`，`main.py` 首启自动创建该管理员；当前 `.env.example` 虽提醒修改，但 fail-open 默认值不适合公网部署。
- `db/session.py:init_db()` 在所有数据库方言上无条件执行 `PRAGMA table_info` 与 SQLite 风格 `ALTER TABLE`，所以“只改连接串即可迁移 MySQL/PostgreSQL”的注释与 README 声明不成立；需要 Alembic 和方言实测。
- `deploy/docker-compose.yml` 使用 `build: ..`，Docker 默认会在仓库根找 `Dockerfile`，但实际文件位于 `deploy/Dockerfile`；按当前 compose 直接构建预计失败。Dockerfile 还依赖预先存在的 `frontend/dist`，未做多阶段构建。
- `requirements.txt` 全部使用宽松下界、无上界/锁定哈希，未来重装不可复现；声明了 OCR 能力但未直接列出 RapidOCR/onnxruntime/PaddleOCR 依赖，需结合 `ocr.py` 复核是否会静默不可用。
- `/api/health` 只返回常量 `ok`，没有检查数据库、Chroma、模型服务或入库队列，不能作为生产 readiness；全局异常处理器也没有记录未捕获异常，可能把关键故障变成无日志 500。
- JWT 仅含 `sub/exp`，有效期 7 天，无 `iat/jti/iss/aud`、刷新/吊销机制；改密或退出后旧 token 仍继续有效。
- slowapi 的实际 key 函数使用 `get_remote_address(request)`，反向代理下默认会看到代理地址；另一个 `deps.get_client_ip()` 当前没有被限流器使用，存在重复且漂移的实现。
- `Chunk.content_hash` 被定义为全局唯一，是否会阻止不同文档/知识库出现相同条款取决于入库哈希是否混入 doc/KB 标识；这是后续必须核查的高风险数据建模点。
- 正向设计：集中配置、FastAPI 模块化、async SQLAlchemy、统一业务异常、会话 404 隔离、SQLite WAL/foreign_keys/busy_timeout、embedding/semantic cache 独立表、回答证据与引用持久化，都体现了比普通原型更完整的工程意识。
- 多格式解析接口抽象清晰，但实际保真度有限：Markdown 按单行处理，代码块/列表/表格/跨行段落结构会丢；DOCX 仅识别英文 `Heading N` 样式，中文“标题 1”、编号列表、页眉页脚、文本框、图片与脚注不处理；CSV 固定 UTF-8、无编码/分隔符探测；Excel 按行扁平化，合并单元格、多行表头、公式/图表/隐藏结构未保留。
- `ExcelParser.extensions` 宣称支持 `xls`，但上传白名单不允许 `xls`，且依赖中没有 `xlrd`；属于能力声明/配置不一致。
- PDF/OCR 做了逐页文本质量路由、CID 乱码启发式、表格检测、TOC、OCR 条带和断号恢复，思路有实际工程经验；但它仍是基于 PyMuPDF 内容流 + 正则的启发式解析，不是版面理解系统。
- PDF 文字层读取没有显式版面排序/栏识别；表格文本被从正文区域剔除后统一追加到页尾，破坏表格在页内的原始阅读顺序；块中心点判断表格相交也可能造成重复或漏掉混排文本。
- `_page_needs_ocr()` 对“有足够文本但没有中文”的页面会因常用汉字占比为 0 强制 OCR，英文页、纯数字表格或公式页可能把优质文本层降级成 OCR。
- OCR 仅按 y/x 排序，不理解多栏、表格、公式、图片标题与阅读顺序；默认初次 OCR 是 3 条带，断号修复则执行整页+3条带+6条带（每页约 10 次推理），质量换成本的幅度很大且没有吞吐/内存基准。
- `ocr_image_union()` 把所有合并行置信度硬写为 1.0，失去真实置信度；去重仅与上一行比较且依赖文本相同/包含，近似识别结果可能重复进入正文。进程内 OCR 进度不支持多 worker/重启恢复。
- PDF `quality["garble_ratio"]` 初始化后从未计算更新；解析发生异常时缺少 `try/finally`，PDF 句柄与 OCR 进度可能残留。断号修复不限定原 OCR 页，文本层 PDF 也可能因合法跳号触发昂贵重 OCR。
- 新切片策略采用“TOC 权威大纲 + 编号候选 + LLM 筛选 + 边界注入”，约束 LLM 不能新增编号是正确设计；但 LLM 失败时反而确认全部候选，可能把合法跳号/正文数字误当边界，且多层启发式相互依赖，缺少 A/B 质量指标证明收益。
- `StructureAwareChunker` 的 `chunk_size` 是字符数而非模型 token；允许单块到 1.5×阈值。`chunk_overlap` 只在超长字符串进入 Recursive splitter 时生效，正常的标题/page/缓冲 flush 基本没有 overlap，因此配置中的 50 并非真实的全局重叠策略。
- Chunker 强制按页 flush，保证单页引用但会切断跨页段落/条款/表格；`merge_tiny_chunks` 又允许同 section 跨页合并，并把页码记为后一块页码，反而造成引用页码不准确。尾部 tiny chunk 也不会被合并。
- Markdown/DOCX 若文档只有同级一级标题，由于标题块丢失显式 level 且 `parser_mode` 启发式失败，chunker 会把后续一级标题逐层嵌套，章节路径错误；解析数据模型应直接携带 `heading_level/path`，不要二次猜测。
- **严重数据正确性问题**：`Chunk.content_hash` 全局唯一，入库又按全库哈希去重，所以两份文档出现相同内容时，第二份文档的该片段直接不落库；文档 `chunk_count` 却仍按切片前数量填写，统计与真实 DB 不一致，来源/引用也丢失。哈希唯一性应至少是 `(doc_id, content_hash[, occurrence])`，embedding 缓存才适合全局按内容复用。
- **严重历史数据问题**：重解析先删除旧 Chunk 并提交，Chunk 删除通过外键级联删除历史 Citation；因此用户过去回答的引用记录会消失，即使 Citation 已冗余保存 snippet/source。引用历史不应依赖可重建 chunk 的级联生命周期。
- **严重一致性/并发问题**：入库先提交删除旧块，再做 embedding 和整库 Chroma 重建；中途失败会永久丢旧索引。`_write_lock` 在 Chroma 全量 rebuild 前释放，两个入库任务仍可同时 reset/add 同一集合，出现空窗、相互覆盖或索引损坏。DB 与 Chroma 没有版本/双写事务/原子切换。
- 每个文档入库都对全部知识库的所有 Chunk 重置并重建 Chroma，复杂度随文档累计接近 O(N²)，重建期间在线查询可能读到空/半成品索引；这是当前最主要的规模化瓶颈之一。
- 入库任务只存在于进程内 `asyncio.create_task`；服务重启、多 worker、崩溃后任务和进度丢失，`parsing/embedding/indexing` 状态可能永久卡住。无持久队列、租约、重试、幂等 job/version。
- 水印/广告 LLM 会根据文档内文本决定删除行，构成文档 prompt injection 与误删风险；即使输入只作为候选，也需要严格结构化输出、审计记录和默认“不删”策略。
- 正向设计：parser/chunker 分层、TOC 内容单独保留、LLM 候选白名单、OCR 修复内容量守卫、SQLAlchemy JSON 整体重赋、BM25 按 KB 重建，都是值得保留的思路。
- Embedding 抽象和批量/查询缓存是合理的，但 `embedding_cache` 以 `content_hash` 单列为主键、同时又按 `model_version` 查询；更换模型后相同内容无法写入新模型向量，导致永久 cache miss。应使用 `(model_version, content_hash)` 复合键，并把维度/provider/base URL/instruction 纳入索引版本。
- 查询向量 LRU 虽把 `settings.embedding_model` 作为 key，却不包含 provider/base URL/query instruction；热进程中切换配置可能复用错误向量。Embedding 调用也没有明确超时、速率/批次自适应、维度校验和成本指标。
- Chroma 封装只锁初始化，不锁 `reset/add/query`；结合入库并发重建会产生真实竞态。`reset_collection()` 吞掉任意删除异常继续创建，可能掩盖 I/O/损坏问题。
- BM25 按 KB 内存索引简单有效，但文档注释仍写 RRF、实际使用线性加权；没有停用词、领域词典、字段权重或持久化，进程启动需全量重建，数据规模增大后启动时间和内存会线性增长。
- **严重跨库污染/潜在数据泄露**：`resolve_documents_by_title()` 扫描全部知识库文档且不接受 kb_id；`retrieve()` 有 doc_ids 时完全忽略 kb_id；两类章节扩展又从全表读取 Chunk/Document，按同名 section 聚合且不限制 KB/doc。用户选中 A 库时，点名/枚举/整章问题可能把 B 库内容与引用混入回答。
- `_expand_enumeration_sections()` 的“章节相关性=候选分数总和”天然偏向切片多的大章节；全表 members 无确定 `order_by` 后先截前 2×cap，再 rerank，声称“全部”但可能丢后半段。流程先做 chapter 扩展（最多 15）再做 enumeration（最多 40），枚举题一旦前者命中就不会进入更完整的后者。
- Reranker 分数、向量/BM25 融合分数使用同一套 evidence 阈值，但两种分数不在同一标度；rerank 失败后证据等级不可比。阈值来自手工配置且无标注集校准，系统统计中的 sufficient/partial/weak/none 不能视作真实准确率。
- `min_content_len` 在最终 top-k hydrate 后才过滤且不回补，可能明明有足够候选却返回少于 k 条；章节/文档扩展没有模型上下文 token 预算，40–60 个块可能超限或把关键信息淹没。
- `retrieve_document_wide()` 只取 top1 所属文档并截前 60 块，对多文档对比、超长文档和答案跨文档场景不完整；optimize 因此不是可靠的“整文档补全”。
- 语义缓存按 KB/doc/style 隔离但**不按用户、会话历史或检索查询版本隔离**。当前问题向量来自原始短追问（如“还有呢”），检索却用“上一轮主题+追问”；缓存可能在另一用户/另一会话中重放带前文语境的答案，构成正确性和隐私风险。
- 语义缓存放在完成真实检索之后，只节省 LLM、不节省 embedding/向量/BM25/rerank 延迟；每次启动又全量清空，跨重启无收益。缓存缺少知识库版本号、prompt/model/version、TTL 与原子失效机制。
- Prompt 把历史既作为独立 chat messages，又重复写入当前 human 消息的【对话历史】，造成 token 双花和历史影响加倍；检索内容也无 token 预算/去冗余/相邻块合并策略。
- 系统并非严格 grounded RAG：证据为 none/weak 时，除少数实时问题外仍允许 LLM 用通用知识回答；“引用编号纪律”只靠 prompt，`verify_citations()` 在主问答路由中根本未调用。现有“引用准确率/幻觉控制”更多是设计意图而非已落地保证。
- 完备性校验只给 LLM 文件名/章节/页码，不给资料正文或 gold 条目，它无法可靠判断“是否全部覆盖”；主 chat 发生补全重生成后也不再次校验，却把 `answer_complete=False` 固定落库。该字段反映流程触发，不是最终答案真实完备度。
- QA 流的错误落库与 SSE 友好提示设计较好；但生成被 max_tokens 截断或空输出时仍把 Message `is_complete=True`，只另设 answer_complete=False，状态语义冲突。第二轮生成的 token usage/finish_reason 也未正确覆盖。
- 重解析/删除 Chunk 会让记忆和语义缓存保存的旧 chunk_id 失效；缓存/记忆命中后先向客户端流出答案，再尝试插入带旧 FK 的 Citation，可能在末尾失败并追加 error 事件，用户看到“答案+错误”且消息未持久化。
- 反馈取消只清空 Message.feedback，不撤销已沉淀的 good/bad QaMemory；UI 上“取消评价”不等于撤销模型行为，容易造成难以解释的持续复用/强制重检。
- 上传仅检查扩展名与总字节数，不校验 magic/MIME、压缩炸弹、PDF 对象复杂度、病毒或租户配额；普通 I/O 异常时部分文件未确保删除。200MB DOCX/XLSX/PDF 可造成解压、渲染或内存 DoS。
- 公共知识库列表使用 `status != empty`，会暴露/允许选择 `indexing` 中的库；删除文档与正在运行的入库 task 也无取消/互斥协议。
- 管理端导出用户问题/答案为 CSV 时未防 `=,+,-,@` 公式注入；管理后台打开恶意内容可能触发 Excel 公式执行。
- 正向设计：doc_scope/style 缓存隔离、用户级 memory、追问改写、SSE reset/done/error 协议、错误答案不进缓存、管理员检索预览、用户/会话权限检查，均是有价值的产品化能力。
- 前端采用 React 18 + TypeScript strict + Ant Design + TanStack Query + Zustand，技术组合成熟且适合该类管理/问答应用；路由同时设置登录与管理员守卫，但安全边界仍必须以后端 RBAC 为准（当前后端已有对应检查）。
- 前端包版本仍使用 `^` 范围，不过 `package-lock.json` 可提供实际安装锁定；相较后端裸 `requirements.txt` 可复现性更好。当前没有 lint、format、前端单测或 E2E 脚本，`noUnusedLocals/noUnusedParameters` 被关闭，质量门禁偏弱。
- `vite.config.js` 与 `vite.config.d.ts` 是 TypeScript 构建产生的同目录生成物，且项目另有 `vite.config.ts`；这类文件被纳入仓库会形成双源漂移风险，应该只保留 TS 源文件并把生成物放入单独输出目录或忽略。
- 前端把 `chunkSizeWarningLimit` 提高到 1500KB，只隐藏/放宽告警而非真正解决首屏体积；需要通过实际 build 产物确认 AntD、KaTeX、粒子背景和页面是否按路由懒加载。
- 认证 token 由 Zustand persist 长期保存在 `localStorage`；实现简单，但一旦将来引入 XSS/第三方脚本，token 可被直接读取。公网多用户系统更适合短时 access token + HttpOnly/Secure/SameSite refresh cookie、CSP 与服务端吊销/会话版本。
- SSE 客户端对 JSON 解析失败直接静默忽略，不处理流结束时剩余缓冲；fetch 路径也绕过 axios 的 401 清理和后端错误消息解析。网络代理分片、非 JSON 错误页或 token 过期时，用户可能只看到泛化错误或空回答，诊断困难。
- **前端状态竞态**：问答流式生成期间仍能切换、创建、删除当前会话；回调总是修改全局 `messages` 的最后一条，而不核验 `conversationId/requestId`。旧会话的后续 SSE 可能显示在新会话页面。切换/删除应先 abort，事件应以 requestId + conversationId 定位目标消息。
- 消息“停止”只 abort 浏览器请求；是否真正停止后端 LLM 消耗取决于服务端是否监测客户端断开（当前生成主链未建立可靠的 cooperative cancellation）。因此 UI 的“已停止”不等同于计算/计费已停止。
- ReactMarkdown 默认不启用原始 HTML，是一项正向的 XSS 防护；引用正文也以 React 文本节点显示。需要继续用 CSP 和依赖审计防御供应链/未来插件风险。
- 欢迎问题、新建会话、历史分页、引用详情、反馈与优化入口构成较完整的产品流程；但没有前端测试，无法自动覆盖快速切会话、SSE 半包/错误包、断网重连、移动端与无障碍行为。
- 管理端提供解析质量摘要、切片浏览、检索预览、OCR 进度和两种切片策略选择，这是很有价值的可解释/调试面；但所谓 `garble_ratio` 当前后端恒为 0，UI 展示会产生错误安全感，且“相关度/证据等级可作为论文反幻觉数据”的文案远超现有指标的统计效度。
- 前端允许 `multiple` 上传，但 mutation 只有一个全局 pending 状态、没有逐文件进度/失败重试/取消；回调还主动丢弃 `onUploadProgress`。真实批量大文件场景下管理员很难判断每个文件的状态。
- 管理列表固定只取前 100 份文档且关闭表格分页，超过 100 后文档会在 UI 中“消失”；知识库侧边栏显示的 doc_count 与表格 `docs.length` 可能不一致。
- 知识库管理页把每次键入的 `searchQ` 直接放入 query key 且 `enabled`，因此输入过程中会自动连续检索；显式“检索”按钮并没有真正控制请求。应分离 draft 与 submitted query，并加 debounce/cancel。
- 管理端重新入库/删除按钮对 `pending/parsing/embedding` 文档不禁用，也没有任务冲突提示；与后端缺少 job cancellation/互斥叠加后，会更容易触发索引竞态。
- CSS/视觉完成度良好，但大量 blur、三层 aurora 和持续 60fps 粒子在低端设备/远程桌面上可能显著耗 GPU，且没有 `prefers-reduced-motion` 降级；管理页布局也主要面向桌面宽屏。
- `.npmrc` 硬编码 `D:\Program Files\Git\usr\bin\bash.exe`，在 Linux、容器、CI 或 Git 安装路径不同的 Windows 上会使 npm scripts 无法运行；注释已说明旧路径问题已经消失，应删除机器专属配置。
- lockfile v3 记录约 380 个包，实际 TypeScript 已解析为 5.9.3，而 manifest 下界写 5.5.4；lockfile 能固定当前安装，但升级时仍需 CI build/test 与 Dependabot/Renovate 类审计。`tsbuildinfo` 与编译出的 vite config 不应作为源码提交。
- 真实 48 页样例 PDF 经独立解析确认 48/48 页无任何文字字符且每页恰有一张扫描图；这是必须依赖 OCR/版面恢复的困难样本，不应拿普通文本层 PDF 的成功率推断其质量。
- 真实样例第 48 页图像内容横置 90°，但 PDF `/Rotate` 为 0；当前解析器没有图像方向检测/纠偏，横向条带会把竖排后的表格切碎，OCR 行序与表格结构都难以恢复。
- 样例页包含目录/前言、密集规范编号、三级条款与复杂表格，恰好覆盖当前启发式的薄弱点。仅以“识别出多少字/多少页”判断成功会漏掉阅读顺序、编号、单元格关系和横置页方向等关键质量维度。
- 仓库 `.venv` 的 `pyvenv.cfg` 固定指向已不存在的用户 Python 3.10.11 路径；该虚拟环境不可迁移，当前机器无法直接启动其 Python/pytest。应使用可重建锁文件/容器而非携带本地 venv 作为运行依赖。
- 前端标准 `npm run build` 也会因机器级 PowerShell policy 首先失败；改用 `npm.cmd` 后又被项目 `.npmrc` 的硬编码 Git Bash 触发 `sed/dirname/uname` 缺失。绕过机器专属 shell 后，TypeScript 检查和 Vite production build 均通过。
- 前端 production build 共转换 3697 modules；主要 JS 产物约为 AntD 991.83KB、主包 541.84KB、React 162.42KB、Markdown 158.22KB（gzip 分别约 312.93/170.17/53.06/48.10KB）。没有路由级 lazy loading，管理员页面代码也进入主包；首屏性能仍有优化空间。
- 只读检查现有运行库得到 11 个 KB、12 个 ready 文档、1242 个 DB chunks、1242 个 Chroma embeddings；当前 DB/向量数量一致，说明当前快照并未处于半重建状态，这是正向结果。
- 现有数据已实证全局去重带来的计数错误：文档 3 记录 `chunk_count=100`，实际仅 99 行。当前库中没有重复 hash 组，正是全局唯一/去重约束的结果，不能据此证明不同文档没有相同内容。
- 真实扫描样例（文档 3，经典策略）99 个 chunk 中平均 502.8 字，但最短 11、最长 2402；21 个小于 100 字、15 个大于 900 字。分布极不稳定，已直接反证“目标约 500 字”的切片配置在实际 OCR 文档上可控。
- 真实扫描样例声称 OCR 平均置信度 0.951、乱码率 0；然而横置表格页实际生成多个 2000+ 字、高度重叠的 chunk，section 路径被数十个表格单元格文本不断拼接，并含 `数字李生` 等 OCR 错字。这证明当前置信度/乱码率不能代表结构与语义质量。
- 文档 3 的横置表格页 chunk 94–96 大量重复同一整页内容，且标题路径把表格每个短单元格当作层级标题；这是解析错误进入标题识别，再被 chunker 放大的完整故障链。继续调 LLM 断号无法修复这一层问题。
- 当前数据中只有 1 份文档使用 `new` 策略，而且它是 59 页、0 OCR 页的文本层 PDF；其余特别是 48 页纯扫描样例仍为 `old`。没有同一文档的 old/new 配对结果，因此现有数据无法支持两种策略的因果 A/B 结论。
- 新策略文档的存量数据为 87 chunks，平均 340.5、最短 36、最长 607、9 个小于 100；在这份容易得多的文本层 PDF 上长度更稳定，但不能外推到扫描件。应建立同文件、同 embedding/retrieval 配置的配对实验。
- `app.db` 已约 76.5MB，主要之一是 2971 条把向量存进 SQLite 的 embedding cache；相对只有 1242 个当前 chunks，历史/多模型缓存缺少 TTL/容量治理会继续膨胀。
- Chroma SQLite 当前只有 1 collection、2 active segments、1242 embeddings，维度 1024，与 DB 一致；但 `.chroma` 目录残留 25 个非当前 HNSW 目录，约 59.2MB。频繁 delete/reset collection 已造成明确的磁盘垃圾与多代索引痕迹。
- 运行时会话样本直接推翻“多数情况下精准即可推断可上线”：面对 Excel 台账，系统把同一行不同日期列错配为“专家论证时间”，用户明确纠错后才承认；根因是 Excel 行被扁平为一段文本，列名—单元格对应关系未以结构化形式保留，LLM只能猜列语义。
- “列出文件中全部方案/全部专家”这类集合完整性问题多轮反复漏项；在 evidence=`none`、top score 约 0.0004 时，系统仍生成看似完整的 22 行表格和具体字段，而引用与答案内容明显不相称。这是检索不全 + 允许弱证据生成 + 无逐声明引用校验叠加的真实幻觉案例。
- 同一问题重复询问会得到不同范围/不同解释，且“sufficient=0.99”仍可能是只命中某个台账行而不是任务所需的全部数据。现有 evidence 分数只代表 top hit 相关性，不代表字段映射正确、集合召回完整或回答事实正确。
- 当前 123 条 assistant 消息中仅 40 条带 evidence 等级；其中 21 条为 none、10 条 weak、1 条 partial、6 条 sufficient（另有早期无等级消息）。仅 10 个点赞、6 个点踩，覆盖太低且存在选择偏差，无法据此计算准确率或上线置信度。
- `conv_dump.txt/conv_compare.txt` 把真实问答、引用以及文档中个人信息明文导出到运行目录。虽然 `backend/data` 未被 Git 跟踪，但这类调试转储需要访问控制、脱敏、保留期和清理策略，不能随部署备份长期存在。
- `PROJECT_RECORD.md`/README 的“企业级性能优化、路由懒分包、可迁移 PostgreSQL、深浅主题、前端虚拟列表、测试全绿”等多项描述已与当前代码或实测环境不符；文档质量曾经很好，但现在成为高风险的过期事实来源。
- 既有 `evaluation-report.md` 的 7.2/10 主要来自静态设计印象，且当时未运行真实数据；它高估了 PDF/检索成熟度。当前审计基于存量切片和历史问答，可观察到结构破坏与真实幻觉，因此评分必须下调并区分“原型功能完成度”和“生产就绪度”。
- `OCR_ENGINE=paddle` 的文档声称会切到 PP-Structure（版面/表格/公式），但代码实际只实例化 `PaddleOCR` 行级 OCR；它不会自动补齐布局、阅读顺序、表格结构或公式解析。该选项属于能力说明错误，不能作为当前解析问题的现成开关。
- 测试源码现有约 239 个 test functions（48 个 API 集成 + 191 个单元），数量和回归覆盖比文档所写 54/11 更强；但项目文档数字已严重过期。无法在当前 `.venv` 执行并不等同于测试失败，报告必须标注“未运行”。
- 当前测试将“今天这个水库的水位是多少”断言为非实时问题；对于水利业务这是错误分类，弱证据时可能允许模型用通用知识编造实时水位，属于领域安全用例缺失。

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| 使用全仓清单驱动阅读 | 能证明覆盖范围并识别遗漏/生成物 |
| 以源码行号和测试输出作为主要证据 | 让报告可复核、可直接定位修复 |
| 对外部模型、向量库等不可用路径明确标注“未实测” | 防止过度声称功能已验证 |

## Issues Encountered
| Issue | Resolution |
|-------|------------|
| 仓库非干净工作树 | 只新增审计规划/报告文件，不编辑业务源码 |
| 全量文件枚举输出被依赖目录放大并截断 | 使用 Git/rg 清单和显式排除规则，单独统计被排除目录 |
| 排除目录定位仍输出大量嵌套缓存路径并截断 | 后续不再递归打印依赖内部目录，仅统计顶层依赖目录及项目文件 |
| 大批量 `Get-Content` 输出超过上限且默认字符集显示中文乱码 | 改为按模块小批读取并显式指定 UTF-8；对缺失片段重新读取 |

## Resources
- `E:\GPT-Codex\LangChainRAG`
- `E:\GPT-Codex\LangChainRAG\.planning\2026-08-16-rag-audit`
- [MinerU 官方仓库](https://github.com/opendatalab/MinerU)
- [Docling 官方文档](https://docling-project.github.io/docling/)
- [PaddleOCR PP-StructureV3 官方文档](https://www.paddleocr.ai/main/en/version3.x/algorithm/PP-StructureV3/PP-StructureV3.html)
- [Unstructured PDF partition 官方文档](https://docs.unstructured.io/open-source/core-functionality/partitioning)
- [Ragas 官方指标文档](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/)
- [pgvector 官方仓库](https://github.com/pgvector/pgvector)

## Visual/Browser Findings
- 尚未检查视觉资料或浏览器页面。
