# Progress Log: RAG 系统全面审计

## Session: 2026-08-16

### Phase 1: 仓库盘点与审计边界
- **Status:** complete
- **Started:** 2026-08-16
- Actions taken:
  - 读取 planning-with-files 技能完整说明与模板。
  - 检查现有规划文件、Git 工作树状态和根目录结构。
  - 建立独立审计计划与初始证据记录。
  - 完成项目文件与第三方依赖/运行数据的初步分层统计。
  - 建立 134 个项目自有文本文件的行数/大小清单，并核验敏感运行数据未被 Git 跟踪。
  - 阅读根文档、既有评估、部署配置、后端入口/配置/安全/数据库模型与迁移实现。

### Phase 2: 全量代码与配置静态审查
- **Status:** complete
- Actions taken:
  - 开始按模块逐文件、UTF-8 解码阅读源码并建立问题证据。
  - 完整阅读解析器、OCR/PDF、TOC/outline/gap、完整性守卫、chunker 与 ingestion manager。
  - 阅读 embedding、Chroma、BM25、RAG 检索/扩展、缓存/记忆、prompt/校验、QA SSE 与管理/API 路由。
  - 阅读仓库内 `.claude` 配置/本地技能和前端构建、路由、主题与入口配置，确认前端测试/质量脚本缺失及生成配置双源问题。
  - 阅读前端 API/SSE、认证与聊天状态、消息/引用/输入/侧栏组件、登录注册和管理页主体，定位跨会话流式状态竞态与 token/SSE 可靠性问题。
  - 完整补读知识库、记忆、账号管理页和全局样式；解析 lockfile 根依赖/包数并检查 npm/TypeScript 生成物。
  - 按 PDF 专项流程检查三份样例的元数据、文字层和页面图像，并渲染核验真实 48 页扫描规范的代表性页面。
  - 执行前端类型检查与生产构建：绕过损坏的 `.npmrc` shell 配置后成功；记录产物体积。
  - 发现后端 `.venv` 绑定缺失的本机 Python 3.10，无法直接运行 pytest；保留为运行环境结论并继续进行不依赖该 venv 的只读数据一致性检查。
  - 只读检查 SQLite/Chroma 运行快照、文档切片分布和索引目录；确认当前 DB/向量数量一致，同时实证 chunk 计数漂移、真实扫描件极端切片和 25 代 HNSW 残留。
  - 完整复核项目技术档案、README、既有评估和运行时问答案例；定位 Excel 字段错配、集合问题漏项与 evidence 极低时仍生成完整敏感表格的真实失败案例。
- Files created/modified:
  - 仅更新审计规划文件，未修改业务源码。
- Files created/modified:
  - `.planning/2026-08-16-rag-audit/task_plan.md`
  - `.planning/2026-08-16-rag-audit/findings.md`
  - `.planning/2026-08-16-rag-audit/progress.md`

## Test Results
| Test | Input | Expected | Actual | Status |
|------|-------|----------|--------|--------|
| Git 工作树检查 | `git status --short` | 能识别审计前变更 | 成功，伴随用户级 ignore 权限警告 | PASS |
| PDF 文字层检查 | 48 页真实扫描规范 | 识别扫描/文本页 | 48/48 页无文字层、每页一张图 | PASS |
| PDF 视觉抽查 | 页 1/2/3/10/20/40/48 | 核对版面与方向 | 含正文、编号、目录/表格；末页图像横置但 Rotate=0 | PASS |
| SQLite/Chroma 一致性 | 当前运行快照 | DB 与向量数一致 | 1242 chunks = 1242 embeddings；发现一处 doc chunk_count 漂移 | PARTIAL |
| 前端标准构建命令 | `npm run build` / `npm.cmd run build` | 正常构建 | PowerShell policy + 硬编码 bash 配置导致失败 | FAIL (ENV/CONFIG) |
| 前端绕过 shell 后构建 | 直接调用 tsc + Vite | 类型检查并构建 | 成功，3697 modules，32.25s | PASS |
| 后端 pytest | `.venv\\Scripts\\python.exe -m pytest` | 运行 239 个测试函数 | venv 指向已不存在 Python 3.10，无法启动 | BLOCKED (ENV) |

## Error Log
| Timestamp | Error | Attempt | Resolution |
|-----------|-------|---------|------------|
| 2026-08-16 | 无法读取 `C:\Users\feeble light\.config\git\ignore` | 1 | 记录为环境警告；不依赖全局 ignore 做文件清单 |
| 2026-08-16 | `[System.IO.Path]::GetRelativePath` 方法不存在，输出膨胀并截断 | 1 | 改用兼容当前 PowerShell 的相对路径方法，并排除第三方依赖/缓存 |
| 2026-08-16 | 首次大批读取输出截断，且 PowerShell 默认解码导致中文乱码 | 1 | 后续按模块分批、显式 `-Encoding UTF8` 重读所有文件 |
| 2026-08-16 | bundled Poppler wrapper 找不到内部路径 | 1 | 直接调用已定位的 Poppler 可执行文件完成元数据和渲染 |
| 2026-08-16 | 仓库 `.venv` Python launcher 指向已删除的 Python 3.10.11 | 1 | 不修改环境；记录为可复现性缺陷，使用独立 Python 做只读 SQLite/PDF 元数据检查 |
| 2026-08-16 | `npm` 被 PowerShell execution policy 阻止；`npm.cmd` 又受硬编码 Bash 影响 | 2 | 直接调用本地 TypeScript/Vite Node 入口完成等价类型检查与构建 |

## Final Delivery
- 已生成并复核 `RAG-SYSTEM-AUDIT-REPORT.md`，共 17 个章节、271 行。
- 报告覆盖功能链路、文件/OCR、两类切片、索引检索、生成与引用、缓存与会话、架构、技术栈、安全部署、前端、测试评测、风险分级和分阶段路线图。
- 结论已尽量绑定到源码行号、运行数据、真实历史问答或本机验证结果；未把未执行的后端测试描述为通过。
- 未修改任何业务源码；保留审计前已有的工作树变更。

## 5-Question Reboot Check
| Question | Answer |
|----------|--------|
| Where am I? | 审计与报告交付已完成 |
| Where am I going? | 等待用户依据报告选择首批修复项 |
| What's the goal? | 对整个 RAG 系统形成证据充分、可执行的全面评估报告 |
| What have I learned? | 见 findings.md |
| What have I done? | 已完成全仓审查、运行验证、风险分级、路线图与正式报告 |
