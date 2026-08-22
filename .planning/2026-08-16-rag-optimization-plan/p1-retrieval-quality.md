# P1 检索质量主线：IR 重构 → parent-child 切片 → 检索校准 详细实施计划（v2 细化）

> 制定日期：2026-08-22
> 前置：P0 全部完成（354 测试绿）
> 用户决策：检索质量主线；P1-2（外部引擎 bake-off）/P1-8（pgvector）裁剪延期
> 本文档 v2：按代码现状细化到文件/函数/字段/SQL/算法/测试粒度（从 rag.py / chunker.py / manager.py / embedding.py / vector_store.py / bm25.py / chat.py / qa/routes.py 逐行查证）

---

## 代码现状速查（已逐行核对）

| 文件 | 现状 | P1 改造点 |
|---|---|---|
| `rag.py` `retrieve()` | 6 阶段：向量→BM25→补召回→`0.7v+0.3bm25`融合→rerank→章节/枚举扩展；`candidates: dict[int,float]` 无序 | P1-9 改 `Candidate[]` 流水线 + trace |
| `rag.py` `_expand_*` | 章节扩展扫全表 `_under_component`；枚举扩展按 `_ENUMERATION_RE` 正则触发 | P1-9 改 coverage plan |
| `chunker.py` `StructureAwareChunker` | 内部 `heading_level(text)` 猜层级；`parser_mode` 靠 `has_heading` | P1-1 接入 IR elements；P1-6 parent-child |
| `manager.py` `_write_chunks` | 已写 `block_type/clause_no/formula_no`（P0-11）；无 parent | P1-6 加 `parent_chunk_id/parent_context` |
| `bm25.py` `search()` | 返回 `[(cid, score)]`，`rag.py` 内 `s/max_s` 归一化 | P1-9 lexical 阶段独立 |
| `embedding.py` | `_cached_query_vector(model,text)` LRU，无 profile | P1-7 加 fingerprint |
| `vector_store.query()` | metadata where + cosine 转换 | P1-7 写入前维度校验 |
| `chat.py` `build_prompt()` | `_format_citations` 注入，无 token 预算 | P1-10 packer 接管 |
| `qa/routes.py` `chat()` | SSE 流式；检索→生成在路由内 | P1-10 trace 落库 + 编排可回放 |
| `models.py` `Chunk` | 有 content/section/page/block_type/clause_no/formula_no | P1-6 加 parent 列 |
| `verify.py` | 166 行，回答校验现状 | P1-10 引用校验接入 |

---

## 单元划分（每单元完成后确认再下一个）

### 单元1：P1-11 评测门禁（先建护栏，1.5-2 人日）

**目标**：任何 parser/chunker/检索改动都有可重复评测基线。

**新增文件**：
```
backend/evaluation/
  __init__.py
  gold_data.py       # 20-30 问 Q→{kb, doc, 预期 chunk_id/条款号}，dict 字面量
  scorers.py         # recall_at_k / mrr / citation_hit / clause_acc（确定性，无 LLM）
  run_eval.py        # CLI: python -m evaluation.run_eval --report out.json
  fixtures/          # 3-5 个真实水利 PDF/md/xlsx（从 data/uploads 挑选脱敏）
```
- `gold_data.py` 每条：`{"q": "...", "kb": "防汛预案库", "doc": "重庆市防汛预案.pdf", "expect_clauses": ["5.1", "5.2"], "expect_chunk_keywords": [...]}`
- `run_eval.py`：对每条 gold，调 `rag.retrieve(db, q, kb_id)`，比对命中。
  - **但 retrieve 现在无 trace** → 单元1 先加**最小 trace**：`retrieve` 返回 `(cites, trace)` 改为可选参数 `return_trace: bool = False`（默认 False 不破坏现有调用）。
  - `trace = {"candidates": [{chunk_id, vector_score, bm25_score, fusion, rerank}], "rerank_ok": bool, "expanded": str|None}`

**改 `rag.py`**：
- `retrieve()` 加 `return_trace: bool = False` 参数；True 时返回 `RetrievedResult(cites=..., trace=...)`
- `RetrievedResult` dataclass（不破坏现有 `list[RetrievedChunk]` 调用）

**测试**：
- `pytest backend/evaluation/`：gold 全跑，输出 Recall@5/10 + citation 命中率
- **基线存档** `backend/evaluation/baseline.json`（提交进 git，后续任何改动对比）

**验收**：`python -m evaluation.run_eval` 可跑；gold ≥20 问；基线 JSON 落盘。

---

### 单元2：P1-1 DocumentElement IR 重构（核心地基，3-4 人日）

**目标**：解析器输出统一可审计结构，chunker 不再从文本猜标题。

**新增 `app/services/parser/ir.py`**：
```python
class ElementType(str, Enum):
    TITLE, HEADING, PARAGRAPH, LIST_ITEM, TABLE, TABLE_ROW, FIGURE, CAPTION, FORMULA, HEADER, FOOTER = ...

@dataclass(frozen=True)
class DocumentElement:
    element_id: str          # f"{parser}-{idx}" 稳定
    type: ElementType
    text: str
    page_start: int | None
    page_end: int | None
    bbox: tuple[float,float,float,float] | None
    reading_order: int
    heading_level: int | None
    section_path: tuple[str, ...]
    parent_id: str | None
    table: dict | None       # {rows: [[str]], header_path: [str], row_indices: [int]}
    confidence: float | None
    source_ref: dict         # {parser, parser_version, block_index}
    flags: frozenset[str]    # {boilerplate_candidate, inferred_heading, ...}
```

**新增 `app/services/parser/ir_validation.py`**：
- `validate_elements(elements) -> list[str]`（错误列表）：
  - page_start ≤ page_end（都有值时）；bbox 4 元组非负
  - reading_order 严格递增（按 elements 顺序即序）
  - parent_id 引用存在（非 None 时）
  - heading_level ∈ 1..6（非 None 时）
  - table 行列一致（rows 非空时每行长度 = header_path 长度）

**改 `base.py`**：
- `ParsedBlock` 保留（旧链路兼容），加 `to_element(idx, parser, version) -> DocumentElement`
- `ParsedDocument` 加 `elements: list[DocumentElement] = field(default_factory=list)`

**改各 parser**（逐步启用，每个 parser 一个 PR 粒度）：
- `pdf.py`：`_parse_page_text` / `_blocks_from_ocr` 已产出 blocks → parse 末尾 `self.elements = [b.to_element(...) for ...]`；**PDF 无 bbox/reading_order 时标注 `inferred_heading` flag**（不假装精确）
- `docx_parser.py`：标题→HEADING（heading_level 从 style 名取），段落→PARAGRAPH，表格→TABLE+TABLE_ROW，段落带 bbox=None（docx 无页面概念）
- `text_parser.py`：md 标题→HEADING，段落→PARAGRAPH
- `excel_parser.py`：整 sheet → TABLE，每行 → TABLE_ROW（**header_path 从首行生成**，P1-4 落地）

**改 `factory.py`**：`get_parser` 不变；新增 `parse_to_elements(filename, path, chunk_strategy) -> list[DocumentElement]`（调 parser.parse 后取 .elements）

**关键约束**：
- header/footer/watermark **不删**，`flags={boilerplate_candidate}` 保留（P1-5 前提）
- chunker **仍走 blocks 路径**（不动现状），IR 是增量（P1-6 才消费）

**测试**（`tests/unit/test_ir.py`）：
- IR validator：非法 bbox / 乱序 reading_order / parent 悬空 / 表格行列不一致 → 拒绝
- pdf/docx/md/excel 各产 IR：100% 过 validator、section_path 非空
- 10 代表文档 IR JSON snapshot 固定（`tests/fixtures/ir_snapshots/`）
- `ParsedBlock.to_element` 往返：text/page/section 不丢

**验收**：4 类 parser 100% 元素过 validator；snapshot 固定；header/footer 只标记不删。

---

### 单元3：P1-7 embedding profile（向量指纹，1.5-2 人日）

**目标**：切块改版后，向量缓存/索引/查询永不因模型配置漂移而错配。

**改 `app/services/embedding.py`**：
- 新增 `@dataclass(frozen=True) EmbeddingProfile`：
  ```python
  provider: str; model: str; base_url: str; dimension: int; normalize: bool
  query_instruction: str; doc_instruction: str; tokenizer: str
  ```
- `build_profile() -> EmbeddingProfile`（从 settings 读）
- `profile_fingerprint() -> str`：`sha256(json.dumps(profile))[:16]`
- `embed_query`/`embed_documents` 内部先 `_probe_profile()`（固定 probe 文本向量）：维度非零、有限值、len==dimension；不符抛 `BizError("向量维度异常")`

**改 `app/db/models.py`**：
- `EmbeddingCache` 加 `profile_fingerprint: str = ""`（现有行默认 ""，兼容旧缓存）
- 唯一约束 `(content_hash, model_version)` → `(content_hash, model_version, profile_fingerprint)`

**改 `alembic/versions/`**（新迁移 `f8a9b0c1d2e3_embedding_profile.py`）：
- `add_column EmbeddingCache.profile_fingerprint`
- 重建唯一索引 `uq_embedding_cache` → `(content_hash, model_version, profile_fingerprint)`

**改 `load_cache_vectors`/`store_cache_vectors`**：
- WHERE/INSERT 带 `profile_fingerprint`（旧行 "" 不匹配新 profile → 重新计算，安全）

**改 `vector_store.py` `add_vectors`/`build_shadow`**：
- 写入前校验：`len(embeddings[0])` 与 collection 已知维度一致（不一致抛错）

**测试**（`tests/unit/test_embedding_profile.py`）：
- 同 content_hash 不同 profile → 两条缓存记录
- 维度不匹配写入 → 抛错
- 旧缓存（profile=""）不被新 profile 命中 → 重新计算

**验收**：profile 指纹化；维度不匹配写入前失败；多 profile 共存。

---

### 单元4：P1-6 parent-child 切片（主线核心，3-4 人日）

**目标**：检索用精确小块，生成得完整父上下文。

**改 `app/db/models.py`**：
- `Chunk` 加：
  ```python
  parent_chunk_id: Mapped[int | None] = mapped_column(ForeignKey("chunks.id", ondelete="SET NULL"), nullable=True, index=True)
  parent_context: Mapped[str | None] = mapped_column(Text, nullable=True)  # 父块全文冗余（免 join）
  ```

**改 `alembic/versions/`**（`f9a0b1c2d3e4_parent_child.py`）：
- `add_column` 两列 + `ix_chunks_parent` index（SQLite 手动重建，参照 P0-8 chunks 重建模式）

**改 `chunker.py`**（新增 `parent_child.py`）：
```python
@dataclass
class ParentChildChunk:
    content: str            # 子块内容
    parent_content: str     # 父块全文（含 breadcrumb）
    section: str | None
    page: int | None
    content_hash: str       # 子块正文规范化哈希
    parent_hash: str        # 父块哈希
    block_type: str
```
- **token 计数**：`tiktoken.get_encoding("cl100k_base")`；`settings.fake_token` 时退回 `len(text)` 字符近似（标注清楚）
- **子块**：目标 350 tokens（范围 200-500）
- **父块**：目标 1000 tokens（范围 700-1600）
- **原子边界**：IR elements 的 PARAGRAPH/LIST_ITEM/TABLE_ROW/FORMULA；无 IR 时用 ParsedBlock 边界
- **section tree**：heading_level 1/2 为父块边界（PARAGRAPH 组内合并）
- **表格**：TABLE_ROW 10-30 行组子块，整表为父块；不按字符跨行切
- **算法**（`build_parent_child(elements, section_tree) -> list[ParentChildChunk]`）：
  1. 按 1/2 级 heading 建 section tree → 每小节一组原子
  2. 组内贪心合并 atoms 到子块上限；超限回退最近原子边界
  3. 子块 → 归入父块（小节边界），父块超 1600 再拆
  4. 超长单个 atom → 内部切分（表格行不切，正文按句切）
  5. 生成 `parent_hash`；父子都算 content_hash（正文规范化，去 breadcrumb）

**改 `manager.py` `_write_chunks`**：
- 输入 `chunks: list[ParentChildChunk]`，写 DB 时：
  - 先插父块？**不**——父子同表，需先插子块拿 id 再回填父 id。
  - **两遍**：先插全部子块（parent_chunk_id=NULL）→ flush 拿 ids → 建 `parent_hash → parent_chunk_id` 映射（父块也作为一行插入，block_type 标记）→ 第二遍 update 子块 parent_chunk_id。
  - 简化：**父块也作为独立 Chunk 行插入**（content=parent_content，block_type="parent"），子块 parent_chunk_id 指向它。

**改 `rag.py` `retrieve`**：
- 命中子块时，按查询意图决定：
  - 精确条款 → 只用子块
  - 综合/概述 → 注入父块（`parent_context` 或 join 取 parent content）
- `_hydrate` 里加：`if chunk.parent_context and 需要父上下文: snippet = parent_context`

**测试**（`tests/unit/test_parent_child.py`）：
- 100% 非排除 element 属 1 子块 + 1 父块
- 子块 <100 tokens <5%；>600 tokens = 0（表格单独统计）
- 同文档高相似重复 <5%
- gold 事实在 child 或 parent 完整覆盖 ≥98%
- **改版后 `pytest evaluation/` Recall@10 不下降**（对照单元1 baseline.json）

**验收**：覆盖/分布/重复/Recall 四项达标；父子同表可追溯。

---

### 单元5：P1-9 检索校准流水线（主线核心，3-4 人日）

**目标**：检索各阶段统一候选结构、稳定分数语义、可回放 trace。

**新增 `retrieval/` 包**：
```
backend/app/services/retrieval/
  __init__.py
  candidates.py    # Candidate dataclass
  scope.py         # 作用域过滤（kb/doc/active version）
  fusion.py        # RRF 融合
  expand.py        # 章节/枚举扩展（coverage plan）
  pack.py          # token 预算分配 + 相邻去重
  diagnostics.py   # RetrievalDiagnostics
```

**`candidates.py`**：
```python
@dataclass
class Candidate:
    chunk_id: int
    vector_rank: int | None; vector_score: float | None
    lexical_rank: int | None; lexical_score: float | None
    fusion_score: float | None
    rerank_score: float | None
    final_rank: int | None
```

**改 `rag.py`**：
- 替换 `retrieve()` 内部 6 阶段为流水线调用：
  ```python
  cands = await scope.filter(db, kb_id, doc_ids)          # 先定作用域
  vec = await vector.search(qvec, scope, top_k_vector)     # 向量阶段
  lex = await lexical.search(query, scope, top_k_bm25)     # BM25 阶段
  fused = fusion.rrf(vec, lex)                             # RRF 融合（k=60）
  reranked = await rerank(focus, fused[:rerank_candidates])
  expanded = await expand.plan(...)                        # 章节/枚举/表格
  packed = pack.assign(fused, reranked, expanded, top_k)   # token 预算
  ```
- `_expand_*` 改造：`_expand_chapter_sections` 只扩 top hit 所属 version+scope（现全表扫 `_under_component`）；`_expand_enumeration_sections` 改 coverage plan（识别表/整节 → 返回全部 row IDs 按 token 分页）
- `min_content_len` 在**候选池阶段**过滤 + 回补 top-k（现在 final hydrate 后缩水）
- rerank 失败 → `score_type=fusion`（evidence policy 用单独阈值）
- trace 全量落库（见单元1 扩展）

**`diagnostics.py`**：
```python
@dataclass
class RetrievalDiagnostics:
    score_type: str            # "vector"|"lexical"|"fusion"|"rerank"
    top_scores: list[float]
    coverage: dict             # {章节命中数, 枚举row数}
    scope: dict                # {kb_id, doc_ids, active_version_ids}
    query_intent: str          # precise/general/named_doc/enumeration/comparison/followup/table/no_answer
    index_version: int | None
    rerank_status: str         # ok/failed/disabled
```

**改 `qa/routes.py`**：`chat()` 调用 `retrieve(..., return_trace=True)`，trace 落 `retrieval_trace` 表（新表，见下）或 JSON 存 Message。

**新增表 `retrieval_trace`**（Alembic）：
```python
id, message_id(FK), query, query_intent, score_type,
candidates_json(JSON), rerank_status, expanded_type,
created_at
```

**测试**（`tests/unit/test_retrieval_pipeline.py`）：
- RRF 融合：跨库越界 0（scope 过滤）
- rerank 故障降级 → `score_type=fusion`，evidence 用 fusion 阈值
- 集合题 coverage plan → 返回全部 row IDs（不 top-k 猜）
- `pytest evaluation/` Recall@10 ≥90%、跨库越界 0

**验收**：Recall@10 ≥90%；跨库越界 0；trace 可回放；rerank 降级不伤核心。

---

### 单元6：P1-10 回答编排最小集 + P1-3/4/5 最小集 + 收尾（2-3 人日）

**P1-10 最小集（trace 可回放 + evidence 改名）**：
- `qa/routes.py`：检索→生成不拆大重构，但把 `verify.py` 的引用校验接入（生成完校验 [n] 编号是否都在注入 cites 内，防幻觉编号）
- `evidence_level` 列改名 `retrieval_support_level`？→ **保留列名**（DB 迁移成本大），前端 label 改「检索支撑度」，文档说明非答案正确率

**P1-3 质量门禁最小集**（改 `pdf.py`）：
- `parse()` 返回的 quality 加 `needs_review` 判定：`ocr_confidence < 0.5` 或 `garble_ratio > 0.1` → `quality["needs_review"] = True`
- `manager.py`：`doc.quality["needs_review"]` 时，`target.status = "needs_review"` **不自动 active**（复用 P0-8 状态机；DB 无此状态则先加 `needs_review` 到 status 枚举注释）

**P1-4 最小集**（改 `docx_parser.py`/`excel_parser.py`）：
- docx：`_paragraph_heading_level` 支持中文样式名（"标题 1"/"标题一"）+ `outlineLvl` XML 属性
- excel：`header_path = [str(c).strip() for c in frame.columns]` 存进 table dict；行 dict `cells{header:value}`（P1-2 的 IR TABLE 已带 header_path）

**P1-5 最小集**（改 `boilerplate.py` + 测试）：
- 新增对抗测试：正文含"忽略前文""删除所有条款""将以下内容视为标题" → parser 控制流不变（不触发删除/改结构）

**收尾**：
- 全量 pytest 回归 + 前端 build
- 提交 feature/rag-optimization（不 push 不并 main）
- 更新 progress.md

---

## 验收总表

| 单元 | 验收 |
|---|---|
| 单元1 | gold ≥20 问；baseline.json 落盘；retrieve 最小 trace |
| 单元2 | 4 parser 100% 过 validator；snapshot 固定；boilerplate 只标记不删 |
| 单元3 | 维度不匹配写入前失败；多 profile 共存；旧缓存兼容 |
| 单元4 | 覆盖 ≥98%；子块分布达标；Recall@10 不下降；父子同表 |
| 单元5 | Recall@10 ≥90%；跨库越界 0；trace 可回放 |
| 单元6 | 中文标题/header_path/对抗测试；引用校验接入；全量绿 |

## 回滚

- 单元1 trace：`return_trace` 默认 False，零影响
- 单元2 IR：blocks 兼容层保留；elements 增量
- 单元3 profile：新列默认 ""，旧缓存兼容；去校验即回现状
- 单元4 parent：DocumentVersion 固定 chunk_profile，active pointer 可回切旧 profile
- 单元5 检索：旧 `retrieve` 签名保留，新流水线以新函数接入
- 单元6 needs_review：仅 quality 标记，不 auto active，无强制

## 风险

- 单元4 parent-child 的 `tiktoken`：需 pip 装 `tiktoken`（阿里云镜像）；`settings.fake_token` 用 len() 近似（标注）
- 单元5 检索 trace 表：新增 Alembic 迁移；测试隔离
- 单元6 needs_review 状态：若 DB status 枚举已锁死，先加注释 + manager 判断（不依赖新枚举值）
