# 项目全局指南 — 水利工程 RAG 知识库问答系统

> 本文件是项目的**最高权威指引**。Claude 做任何决策前必须先读本文件。
> 任何与本文件冲突的历史文档（如 PROJECT_RECORD.md 旧定位）以本文件为准。

## 一、项目定位（最重要）

**这是一个要真实落地、长期演进的产品项目，不是毕业设计演示，不是玩具 demo。**

- 真实场景：**水利工程领域**知识库问答，规范/教材/工程报告等强结构文档
- 终极愿景：RAG 是未来 **AI Agent 的个性化长期知识库**底座（见 `docs/agent-vision.md`），最终演进到数字孪生水利
- 因此每一个决策都要问：**「这对真实使用、长期演进、未来接 Agent 有没有价值？」**——而不是「能不能答辩演示」
- 质量要求：检索要"找得到"且"答得对"，评测要严谨（gold 集 + 多轮方差 + 分层报告），不能停留在"20 问抽查"
- 发现问题必须**真正优化**，不能只报告不解决

## 二、硬性约束（必须遵守）

1. **分支策略**：所有优化只保存到 `feature/rag-optimization` 分支；`main` 保持不动；**不 push 到远程**
2. **先计划后执行**：每个阶段先写详细计划，用户确认后才执行；每个单元完成后**停下等用户确认**再进下一个单元
3. **API 密钥保密**：密钥在 `backend/.env`（gitignored），绝不暴露、不写进文档/代码/提交
4. **pip 用阿里云镜像**：`-i https://mirrors.aliyun.com/pypi/simple/ -q`（清华 403 不可用）
5. **知识域约束**：系统只针对**水利工程领域**知识，不做其他领域
6. **不替用户做主**：方向性决策（迁移、技术选型、范围裁剪）必须摆选项让用户拍板

## 三、用户工作方式（沟通风格）

用户是**水利领域专家，不是程序员**（vibe coding）。必须：
- 每个改动/每个计划用**大白话**解释清楚——说人话，说清"为什么"和"好处"
- 不使用术语堆砌；技术名词第一次出现时用一句人话解释
- 进度汇报要清晰：做了什么、结果指标如何（用表格/数字）、下一步是什么
- 测试/评测结果用**具体数字**说话（如"Recall@5 85%→95%"），而不是"有提升"
- 用户批评过"不对啊，你说效果不好为什么不优化好"——**必须主动优化，不能停在报告问题**
- 用户批评过评测不严谨——**评测必须多轮、分层、可复现**

## 四、当前架构与技术栈（决策上下文）

- **后端**：FastAPI + async SQLAlchemy 2 + SQLite(WAL) + Alembic + Chroma(嵌入式) + BGE-M3 + BM25(jieba) + bge-reranker + DeepSeek(中转站)
- **前端**：React 18 + AntD 5 + Vite + zustand + react-query
- **检索流**：向量 + BM25 混合 → RRF 融合 → rerank → LLM 带引用生成
- **入库流**：上传隔离 → 分层解析(PDF 文字层/OCR) → DocumentElement IR → 结构感知分块(parent-child) → embedding 缓存 → Chroma + BM25 → 原子发布
- **所有同步阻塞**（Chroma/BM25/解析/embedding）走 `asyncio.to_thread`，rerank 是唯一真 async IO
- **可迁移性**：SQLAlchemy 全部方言通用类型；config 已预留 MySQL/PG 切换；未来可接 pgvector/Milvus/Qdrant
- 数据：`backend/data/`（app.db 222MB + .chroma 115MB + uploads 131MB）——**这些是真实用户数据，不可随意删除**

## 五、评测基线（已建立的严谨体系）

- **gold 集**：`backend/evaluation/gold_data.py` 60 问覆盖 12 库、7 种意图、expect_clauses + answer_hint
- **评分**：`backend/evaluation/scorers.py` 分层（by_intent/by_kb）、严格条款判定
- **评测入口**：`backend/evaluation/run_eval.py`（多轮方差、baseline 对比）
- **回答质量**：`backend/evaluation/answer_eval.py`（引用校验 + 完备率 + 事实核对）
- **生产配置基准**：评测必须反映生产真实配置（含 rerank 开关）
- 历史指标（P1 收官时）：Recall@5 91.7%、严格条款@5 84.6%(生产 rerank)、引用 100%、完备 91.7%、事实 93.3%

## 六、质量与代码规范

- **不可变性**：创建新对象，绝不修改现有对象；`update()` 返回新副本
- **文件组织**：多小文件 > 少大文件；典型 200-400 行，上限 800 行
- **错误处理**：每层显式处理；UI 友好提示、服务端详细日志；**绝不静默吞错**
- **输入验证**：所有系统边界验证输入；快速失败 + 清晰错误消息
- 质量清单：命名良好 / 函数 < 50 行 / 文件 < 800 行 / 嵌套 ≤ 4 层 / 无硬编码（用常量/配置）

## 七、文档体系

- **README.md**：快速上手（用户可读）
- **docs/PROJECT_RECORD.md**：技术档案（当前定位描述）
- **docs/agent-vision.md**：Agent 愿景（未来方向）
- **.planning/**：各阶段计划 + 进度（计划驱动的开发记录）
- 历史基线：P0 安全/隔离/入库、P1 检索质量/评测严谨化已收官，详见 `.planning/2026-08-16-rag-optimization-plan/progress.md`

## 八、默认状态确认

- 当前分支 `feature/rag-optimization`；main 保持 aa372b6 不动；无远程 push
- 生产启动走 `APP_ENV=production` fail-safe（缺密钥/DEBUG 直接拒绝启动）
- 测试全离线（fake embedding/LLM、临时数据目录），pytest 当前 400+ 绿
