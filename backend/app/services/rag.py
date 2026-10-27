"""RAG 检索：向量检索为主 + BM25 补充候选 + bge-reranker 重排。

v3 要点（2026-08-04 实测）：
- 候选池 = 向量 top50 ∪ 本库 BM25 top50，按 RRF 等权融合（先排序再按名次倒数累加）
- rerank 候选 100（BGE-M3 对抽象/长查询会漏召回，候选池小则 reranker 见不到正确答案）
- rerank 用「聚焦主题词」替代原查询：长查询会稀释关键项（如
  「水利技术标准编写规定中规定的引用标准是什么？」里文档标题喧宾夺主，
  reranker 反而偏好前言/总则）；聚焦「引用标准」后正确规则节得分 0.98+。
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.metrics import metrics
from app.db.models import Chunk, Document
from app.schemas import CitationOut
from app.services import bm25, vecmath, vector_store
from app.services.embedding import embed_query
from app.services.query_expand import expand_query

logger = logging.getLogger(__name__)

# G6-2：作用域/聚焦词/文档名/章节/扩展已拆到 retrieval/ 子包。
# 这里 re-export 保持既有调用方（含测试）的 import 路径不变。
from app.services.retrieval.doc_scope import (  # noqa: F401
    doc_name_candidates as _doc_name_candidates,
    match_document as _match_document,
    resolve_documents_by_title,
)
from app.services.retrieval.expand import (  # noqa: F401
    expand_chapter_sections as _expand_chapter_sections,
    expand_enumeration_sections as _expand_enumeration_sections,
)
from app.services.retrieval.focus import focus_rerank_query  # noqa: F401
from app.services.retrieval.rerank import close_rerank_client, rerank  # noqa: F401
from app.services.retrieval.scope import (  # noqa: F401
    active_collection_names as _active_collection_names,
    active_version_ids as _active_version_ids,
)
from app.services.retrieval.section import (  # noqa: F401
    ENUMERATION_RE as _ENUMERATION_RE,
    match_section_component as _match_section_component,
    section_no as _section_no,
    under_component as _under_component,
)


def judge_evidence_level(scores: list[float]) -> str:
    """依据检索重排后的相关分数判定证据等级（四级：sufficient/partial/weak/none）。

    - sufficient 充足：top1 高分 或 ≥2 块强相关（交叉印证）
    - partial 部分：top1 中等相关（有据可答但可能不完整）
    - weak 较弱：top1 弱相关（LLM 据实作答并提示资料有限）
    - none 不足：无高分块（系统直接拒答，不调 LLM —— 防幻觉 + 省 token）
    阈值见 settings.evidence_*，可在 .env 调优。
    """
    if not scores:
        return "none"
    top1 = max(0.0, float(scores[0]))
    strong = sum(1 for s in scores if float(s) >= settings.evidence_strong_threshold)
    if top1 >= settings.evidence_sufficient_threshold:
        return "sufficient"
    if strong >= 2:
        return "sufficient"
    if top1 >= settings.evidence_partial_threshold:
        return "partial"
    if top1 >= settings.evidence_weak_threshold:
        return "weak"
    return "none"


@dataclass
class RetrievedChunk:
    chunk_id: int
    kb_id: int
    doc_id: int
    source: str
    page: int | None = None
    section: str | None = None
    snippet: str = ""
    score: float = 0.0
    rank: int = 0
    # P0-11 检索出处元数据（未来 DSH 引用来源）：块类型 / 条款号 / 公式编号 / 文档类型
    block_type: str = "text"
    clause_no: str | None = None
    formula_no: str | None = None
    doc_type: str = "other"

    def to_citation(self) -> CitationOut:
        return CitationOut(
            chunk_id=self.chunk_id,
            kb_id=self.kb_id,
            doc_id=self.doc_id,
            source=self.source,
            page=self.page,
            section=self.section,
            snippet=self.snippet,
            score=self.score,
            rank=self.rank,
        )


@dataclass
class CandidateTrace:
    """P1-1 评测门禁：单个候选的阶段性分数（用于离线评测/可回放 trace）。

    记录从向量/BM25 到融合到 rerank 的完整路径，评测时按需求取对应分。
    """

    chunk_id: int
    vector_score: float | None = None
    bm25_score: float | None = None
    fusion_score: float | None = None
    rerank_score: float | None = None


@dataclass
class RetrievalTrace:
    """P1-1 评测门禁：一次 retrieve 的完整可回放 trace。"""

    query: str
    candidates: list[CandidateTrace]
    rerank_ok: bool = False
    rerank_status: str = "disabled"  # ok / failed / disabled
    expanded_type: str | None = None  # None / chapter / enumeration / coverage
    vector_hits: int = 0
    bm25_hits: int = 0


@dataclass
class RetrievedResult:
    """P1-1 评测门禁：retrieve 返回值（return_trace=True 时）。cites 兼容旧调用。"""

    cites: list[RetrievedChunk]
    trace: RetrievalTrace | None = None


def _table_citation(ans) -> RetrievedChunk | None:
    """把精确读表答案转成一条检索引用（snippet=格式化答案正文，带出处）。

    chunk_id 取答案来源表的首个切片 id（引用锚点），snippet 放完整答案文本；
    LLM 生成时据此引用，命中 expect_keywords 也靠 snippet 里的原文值（如「3」）。
    """
    from app.services.table_query import TableAnswer

    if not isinstance(ans, TableAnswer):
        return None
    chunk_id = ans.chunk_ids[0] if ans.chunk_ids else None
    return RetrievedChunk(
        chunk_id=chunk_id,
        kb_id=ans.kb_id or 0,
        doc_id=ans.doc_id or 0,
        source=ans.source or "",
        section=ans.section,
        snippet=ans.answer_text,
        score=1.0,
        rank=1,
        block_type="table",
    )


async def _try_table_channel(
    db: AsyncSession,
    query: str,
    kb_id: int | None,
    doc_ids: list[int] | None,
    return_trace: bool,
) -> list[RetrievedChunk] | RetrievedResult | None:
    """阶段0：精确读表通道（先于向量检索）。

    命中「计数/枚举/查值」且库里能精确定位到一张表、读出确切答案时，直接返回该表引用。
    任何一步不确定都返回 None → 回退向量检索（非表格问题零回归）。
    点名文档限定（doc_ids）时暂不走精确通道（语义不同：可能只要某文档里的表）。
    """
    if doc_ids:
        return None
    try:
        from app.services.table_query import query_table

        table_ans = await query_table(db, query, kb_id)
    except Exception:
        logger.warning("精确读表通道失败，回退向量检索", exc_info=True)
        table_ans = None
    if table_ans is None:
        return None
    cite = _table_citation(table_ans)
    if cite is None:
        return None
    if return_trace:
        trace = RetrievalTrace(
            query=query,
            candidates=[],
            rerank_ok=False,
            rerank_status="disabled",
            expanded_type="table",
            vector_hits=0,
            bm25_hits=0,
        )
        return RetrievedResult(cites=[cite], trace=trace)
    return [cite]


async def _vector_candidates(
    db: AsyncSession,
    rq: str,
    kb_id: int | None,
    doc_ids: list[int] | None,
    return_trace: bool,
    trace_cands: dict[int, CandidateTrace],
) -> tuple[list[float], list, dict[int, float], set[int], set[int]]:
    """阶段1：向量检索（真实余弦）；按 kb 路由到 active collection（软切换）。

    返回 (qvec, vec_hits, candidates, doc_chunk_ids, doc_kb_ids)。
    """
    qvec = await embed_query(rq)
    doc_chunk_ids: set[int] = set()
    doc_kb_ids: set[int] = set()
    if doc_ids:
        # P0-2 scope 隔离：点名文档也必须在当前库内（kb 非空时），杜绝 KB-B 文档混入
        # P0-8 active 过滤：DB 同时存 active+retired 版本，只取 active 版本切片
        active_ids = await _active_version_ids(db, kb_id=kb_id, doc_ids=doc_ids)
        stmt = select(Chunk.id, Chunk.kb_id).where(Chunk.doc_id.in_(doc_ids))
        if active_ids:
            stmt = stmt.where(Chunk.document_version_id.in_(active_ids))
        if kb_id is not None:
            stmt = stmt.where(Chunk.kb_id == kb_id)
        rows = (await db.execute(stmt)).all()
        doc_chunk_ids = {cid for cid, _ in rows}
        doc_kb_ids = {kid for _, kid in rows}
        # doc_ids 限定：按这些文档所属库的 collection 检索，where 只留 doc_id 过滤
        doc_collections = await _active_collection_names(db, kb_id=kb_id)
        if not doc_collections:
            # 兜底：kb_id 未给但 doc_ids 给了 → 跨全库的 collection 名
            doc_collections = await _active_collection_names(db)
        where = {"doc_id": doc_ids[0]} if len(doc_ids) == 1 else {"doc_id": {"$in": doc_ids}}
        vec_hits = await asyncio.to_thread(
            vector_store.query_many, doc_collections, qvec, where, settings.top_k_vector
        )
    elif kb_id is not None:
        names = await _active_collection_names(db, kb_id=kb_id)
        vec_hits = await asyncio.to_thread(
            vector_store.query_many, names, qvec, None, settings.top_k_vector
        )
    else:
        names = await _active_collection_names(db)
        vec_hits = await asyncio.to_thread(
            vector_store.query_many, names, qvec, None, settings.top_k_vector
        )
    candidates: dict[int, float] = {h.chunk_id: h.score for h in vec_hits}
    if return_trace:
        for h in vec_hits:
            trace_cands.setdefault(h.chunk_id, CandidateTrace(chunk_id=h.chunk_id)).vector_score = h.score
    return qvec, vec_hits, candidates, doc_chunk_ids, doc_kb_ids


async def _bm25_candidates(
    rq: str,
    kb_id: int | None,
    doc_ids: list[int] | None,
    doc_chunk_ids: set[int],
    doc_kb_ids: set[int],
    return_trace: bool,
    trace_cands: dict[int, CandidateTrace],
) -> dict[int, float]:
    """阶段2：BM25 检索 + 库内归一化（跨库按各库 top1 归一，无关库不产生噪声）。"""
    bm25_norm: dict[int, float] = {}
    if doc_ids:
        kb_ids: list[int] = list(doc_kb_ids)
        bm25_k = settings.top_k_bm25 * 5  # 限定文档时候选放大，防后过滤缩水
    else:
        kb_ids = [kb_id] if kb_id is not None else bm25.all_kb_ids()
        bm25_k = settings.top_k_bm25
    for kid in kb_ids:
        if not bm25.has_kb(kid):
            continue
        hits = await asyncio.to_thread(bm25.search, kid, rq, bm25_k)
        if doc_ids:
            hits = [(cid, s) for cid, s in hits if cid in doc_chunk_ids]
        if not hits:
            continue
        max_s = max(s for _, s in hits)
        if max_s <= 0:
            # 该库 BM25 全 0（查询匹配不到关键词，如问候语「你好」）→ 跳过，避免除零崩溃
            continue
        for cid, s in hits:
            bm25_norm[cid] = s / max_s  # 该库内归一化到 0~1
            if return_trace:
                trace_cands.setdefault(cid, CandidateTrace(chunk_id=cid)).bm25_score = s / max_s
    return bm25_norm


async def _backfill_bm25_vectors(
    db: AsyncSession,
    kb_id: int | None,
    qvec: list[float],
    candidates: dict[int, float],
    bm25_norm: dict[int, float],
    return_trace: bool,
    trace_cands: dict[int, CandidateTrace],
) -> None:
    """阶段3：BM25 独有候选补真实余弦（原地更新 candidates）。"""
    extra_ids = [cid for cid in bm25_norm if cid not in candidates]
    if not extra_ids:
        return
    # 软切换：按 active collection 取向量（跨库合并）
    all_names = await _active_collection_names(db, kb_id=kb_id) or await _active_collection_names(db)
    embeddings = await asyncio.to_thread(vector_store.get_embeddings_by_ids_many, all_names, extra_ids)
    # P1-3：补召回余弦是纯 CPU 密集活，放线程池批量算，不在事件循环里逐条跑。
    sims = await asyncio.to_thread(
        vecmath.cosine_batch, qvec, [embeddings[cid] for cid in extra_ids]
    )
    for cid, sim in zip(extra_ids, sims):
        candidates[cid] = sim
        if return_trace:
            trace_cands.setdefault(cid, CandidateTrace(chunk_id=cid)).vector_score = sim


def _fuse_candidates(
    candidates: dict[int, float],
    bm25_norm: dict[int, float],
    return_trace: bool,
    trace_cands: dict[int, CandidateTrace],
) -> dict[int, float]:
    """阶段4：RRF 等权融合（向量名次 + 本库 BM25 归一化名次）。"""
    from app.services.retrieval.fusion import rrf_fuse

    vec_sorted = sorted(candidates.items(), key=lambda x: x[1], reverse=True)
    bm25_sorted = sorted(bm25_norm.items(), key=lambda x: x[1], reverse=True)
    fused = rrf_fuse(vec_sorted, bm25_sorted)
    if return_trace:
        for cid, fs in fused.items():
            trace_cands.setdefault(cid, CandidateTrace(chunk_id=cid)).fusion_score = fs
    return fused


async def _select_and_rerank(
    db: AsyncSession,
    rq: str,
    candidates: dict[int, float],
    top_k: int,
    return_trace: bool,
    trace_cands: dict[int, CandidateTrace],
) -> tuple[list[tuple[int, float]], dict[int, str | None], bool, str]:
    """阶段5：加载候选内容 → 短块过滤/回补 → rerank 重排。

    返回 (cand_sorted, section_by_id, rerank_ok, rerank_status)。
    """
    cand_sorted = sorted(candidates.items(), key=lambda x: x[1], reverse=True)[
        : settings.rerank_candidates
    ]
    rerank_ok = False
    rerank_status = "disabled"
    if not settings.rerank_enabled:
        metrics["rerank_requests_total"].labels("disabled").inc()
    section_by_id: dict[int, str | None] = {}
    if not cand_sorted:
        return cand_sorted, section_by_id, rerank_ok, rerank_status

    # 预先取候选的章节信息：rerank 与两种扩展共用；rerank 关闭时扩展仍可用
    ids = [cid for cid, _ in cand_sorted]
    rows = (
        await db.execute(
            select(Chunk, Document)
            .join(Document, Chunk.doc_id == Document.id)
            .where(Chunk.id.in_(ids))
        )
    ).all()
    content_by_id = {c.id: c.content for c, _ in rows}
    section_by_id = {c.id: c.section for c, _ in rows}
    # P1-9：min_content_len 提前到候选池阶段过滤（短块不参与 rerank/扩展），
    # 被过滤的从候选池回补 top_k（防 final hydrate 缩水）。
    cand_sorted = [
        (cid, sc) for cid, sc in cand_sorted
        if len((content_by_id.get(cid) or "").strip()) >= settings.min_content_len
    ]
    if len(cand_sorted) < settings.top_k_final:
        # 回补：从原候选池按融合分补足 top_k_final
        for cid, sc in sorted(candidates.items(), key=lambda x: x[1], reverse=True):
            if len(cand_sorted) >= settings.top_k_final:
                break
            if cid not in {c for c, _ in cand_sorted} and cid not in content_by_id:
                # 未加载内容的候选也保留（防全滤空）
                cand_sorted.append((cid, sc))
    ids = [cid for cid, _ in cand_sorted]
    docs = [content_by_id.get(cid, "") for cid in ids]
    if settings.rerank_enabled:
        try:
            # 用聚焦主题词重排：长查询会稀释关键项（BGE 局限），
            # 聚焦「引用标准」后正确规则节 0.98+（原文案仅 0.81 被前言压过）
            _t0 = perf_counter()
            r_scores = await rerank(focus_rerank_query(rq), docs)
            metrics["chat_stage_seconds"].labels("rerank").observe(perf_counter() - _t0)
            cand_sorted = sorted(zip(ids, r_scores), key=lambda x: x[1], reverse=True)
            rerank_ok = True
            rerank_status = "ok"
            metrics["rerank_requests_total"].labels("ok").inc()
            if return_trace:
                for cid, rs in zip(ids, r_scores):
                    if cid in trace_cands:
                        trace_cands[cid].rerank_score = rs
        except Exception:
            logger.warning("rerank 失败，回退到混合排序", exc_info=True)
            rerank_status = "failed"
            metrics["rerank_requests_total"].labels("failed").inc()
            cand_sorted = cand_sorted[:top_k]
    return cand_sorted, section_by_id, rerank_ok, rerank_status


async def _expand_and_hydrate(
    db: AsyncSession,
    rq: str,
    cand_sorted: list[tuple[int, float]],
    section_by_id: dict[int, str | None],
    candidates: dict[int, float],
    top_k: int,
    kb_id: int | None,
    include_snippet: bool,
) -> tuple[list[RetrievedChunk], str | None]:
    """阶段6：章节/枚举扩展 → hydrate → scope 不变量校验。

    返回 (hydrated, expanded_type)。
    """
    ranked = cand_sorted[:top_k]
    expanded_type: str | None = None
    expanded = await _expand_chapter_sections(
        db, rq, cand_sorted, section_by_id, top_k, kb_id=kb_id
    )
    if expanded:
        expanded_type = "chapter"
    else:
        expanded = await _expand_enumeration_sections(
            db, rq, cand_sorted, section_by_id, top_k, kb_id=kb_id,
            candidates_full=candidates,
        )
        if expanded:
            expanded_type = "enumeration"
    if expanded:
        ranked = expanded
    hydrated = await _hydrate(db, ranked, include_snippet)
    # P0-2 scope 不变量：kb 非空时所有引用必须属于该库（纵深防御，防未来扩展路径再漏）
    if kb_id is not None:
        leaked = [c for c in hydrated if c.kb_id != kb_id]
        if leaked:
            logger.error(
                "scope 越界：检索返回其他库切片 kb=%s leaked_chunks=%s",
                kb_id, [c.chunk_id for c in leaked],
            )
            hydrated = [c for c in hydrated if c.kb_id == kb_id]
    return hydrated, expanded_type


async def retrieve(
    db: AsyncSession,
    query: str,
    kb_id: int | None = None,
    doc_ids: list[int] | None = None,
    top_k: int | None = None,
    include_snippet: bool = True,
    return_trace: bool = False,
) -> list[RetrievedChunk] | RetrievedResult:
    """检索主入口。kb_id=None 跨全库（按库归一化 BM25 加权）；doc_ids 限定只搜点名文档。

    final = RRF 等权融合（向量名次 + 本库 BM25 归一化名次）
    - 向量为主（真实余弦，区分度好）
    - BM25 精确关键词兜底：解决 BGE-M3 对「计算公式/推求步骤」等查询区分度不足，
      以及长问题下关键词被稀释的问题（如「请问…通信网络体系有什么要求呢？」）
    - 跨库时各库 BM25 按**该库自身 top1 归一化**，无关库（BM25 近 0）不会产生噪声
    - doc_ids 非空：只搜这些文档的切片（问题点名《书名》或「XXX中」时），BM25 按文档后过滤

    G6-2：本函数只做编排，各阶段见 _try_table_channel / _vector_candidates /
    _bm25_candidates / _backfill_bm25_vectors / _fuse_candidates /
    _select_and_rerank / _expand_and_hydrate。

    P1-1 评测门禁：return_trace=True 时返回 RetrievedResult（cites + trace）。
    默认 False 返回 list[RetrievedChunk]，零影响现有调用。
    """
    # P2 单元3：检索范围指标（scoped=点名文档限定，global=跨全库）
    metrics["retrieval_requests_total"].labels("scoped" if doc_ids else "global").inc()
    top_k = top_k or settings.top_k_final

    # 阶段0：读表问题走精确结构化通道；命中直接返回
    early = await _try_table_channel(db, query, kb_id, doc_ids, return_trace)
    if early is not None:
        return early

    # 单元 N 口语→术语 查询扩展：口语/宽泛问法追加规范术语（只加词不删词），
    # 仅作用于检索查询（向量/BM25/rerank），语义缓存/记忆库/用户可见提问仍用原 query。
    rq = expand_query(query)

    # P1-1 评测门禁：收集各阶段分数供 trace
    trace_cands: dict[int, CandidateTrace] = {}

    qvec, vec_hits, candidates, doc_chunk_ids, doc_kb_ids = await _vector_candidates(
        db, rq, kb_id, doc_ids, return_trace, trace_cands
    )
    bm25_norm = await _bm25_candidates(
        rq, kb_id, doc_ids, doc_chunk_ids, doc_kb_ids, return_trace, trace_cands
    )
    await _backfill_bm25_vectors(
        db, kb_id, qvec, candidates, bm25_norm, return_trace, trace_cands
    )
    candidates = _fuse_candidates(candidates, bm25_norm, return_trace, trace_cands)
    cand_sorted, section_by_id, rerank_ok, rerank_status = await _select_and_rerank(
        db, rq, candidates, top_k, return_trace, trace_cands
    )
    hydrated, expanded_type = await _expand_and_hydrate(
        db, rq, cand_sorted, section_by_id, candidates, top_k, kb_id, include_snippet
    )

    if return_trace:
        trace = RetrievalTrace(
            query=query,
            candidates=sorted(trace_cands.values(), key=lambda c: c.chunk_id),
            rerank_ok=rerank_ok,
            rerank_status=rerank_status,
            expanded_type=expanded_type,
            vector_hits=len(vec_hits),
            bm25_hits=len(bm25_norm),
        )
        return RetrievedResult(cites=hydrated, trace=trace)
    return hydrated


async def _hydrate(
    db: AsyncSession, ranked: list[tuple[int, float]], include_snippet: bool
) -> list[RetrievedChunk]:
    if not ranked:
        return []
    ids = [cid for cid, _ in ranked]
    rows = (
        await db.execute(
            select(Chunk, Document)
            .join(Document, Chunk.doc_id == Document.id)
            .where(Chunk.id.in_(ids))
        )
    ).all()
    by_id: dict[int, tuple[Chunk, Document]] = {}
    # P1-2 单元1（纵深防御）：只认 active 版本的 chunk 为证据。
    # 索引（Chroma/BM25）只该含 active，但历史 bug（如 BM25 预热灌全量）会把 retired
    # 版本的 chunk_id 顶进 ranked。此处按 doc.active_version_id 兜底拦截——旧版本内容
    # 绝不作为答案依据，防止「重灌后问答仍命中旧垃圾切片/空公式」这类问题再犯。
    active_version_ids = {
        doc.id: doc.active_version_id
        for chunk, doc in rows
        if doc.active_version_id is not None
    }
    for chunk, doc in rows:
        if chunk.document_version_id != active_version_ids.get(doc.id):
            continue
        by_id[chunk.id] = (chunk, doc)

    order = {cid: i for i, cid in enumerate(ids)}
    items: list[RetrievedChunk] = []
    for cid, score in ranked:
        pair = by_id.get(cid)
        if not pair:
            continue
        chunk, doc = pair
        # 过滤低信息量短块（封面/目录碎片，向量相似度虚高）。
        # P1-4：有父上下文的子块，用父块长度判断（父块够长即保留，内容短子块不误杀）。
        effective_len = len(chunk.parent_context or chunk.content)
        if effective_len < settings.min_content_len:
            continue
        # P1-4：命中子块（有父上下文）且内容偏短 → 注入父块全文（LLM 看到完整小节）。
        # 引用锚点仍是子块 chunk_id；父块本身是 block_type='parent' 不直接作为引用。
        snippet = chunk.content if include_snippet else chunk.content[:200]
        if (
            chunk.parent_context
            and chunk.parent_chunk_id is not None
            and len(chunk.content.strip()) < settings.min_content_len * 2
        ):
            snippet = chunk.parent_context if include_snippet else chunk.parent_context[:200]
        items.append(
            RetrievedChunk(
                chunk_id=chunk.id,
                kb_id=chunk.kb_id,
                doc_id=chunk.doc_id,
                source=doc.filename,
                page=chunk.page,
                section=chunk.section,
                snippet=snippet,
                score=round(score, 4),
                # P0-11 出处元数据：从 DB 落库字段带出（旧数据 NULL 回退默认）
                block_type=chunk.block_type or "text",
                clause_no=chunk.clause_no,
                formula_no=chunk.formula_no,
                doc_type=getattr(doc, "doc_type", None) or "other",
            )
        )
    items.sort(key=lambda x: order[x.chunk_id])
    for i, it in enumerate(items):
        it.rank = i + 1
    return items


async def retrieve_document_wide(
    db: AsyncSession,
    query: str,
    kb_id: int | None = None,
    top_k: int = 5,
    cap: int = 60,
    _cites: list[RetrievedChunk] | None = None,
) -> list[RetrievedChunk]:
    """补全重生成用：先普通检索定位问题所属文档，再返回该文档**全部切片**（上限 cap）。

    层2 完备性校验判定「枚举题遗漏」时，单章节扩展可能仍不全（答案横跨多个 sheet/章节），
    此时扩大到整份相关文档，保证模型看到完整数据。按 chunk.id 保持原文顺序。
    _cites：调用方已做普通检索（如 /optimize 需要同时拿证据等级分数）时传入，避免二次检索。
    """
    cites = _cites if _cites is not None else await retrieve(db, query, kb_id=kb_id, top_k=top_k)
    if not cites:
        return []
    doc_id = cites[0].doc_id
    # P0-2 scope 隔离：整文档补全只取该文档**且属于当前库**的切片；
    # kb_id 未指定时用命中文档自身所在库兜底（补全不跨库）。
    doc_kb_id = cites[0].kb_id if kb_id is None else kb_id
    # P0-8 active 过滤：只取 active 版本切片（retired 不可补全）
    active_ids = await _active_version_ids(db, kb_id=doc_kb_id, doc_ids=[doc_id])
    stmt = (
        select(Chunk, Document)
        .join(Document, Chunk.doc_id == Document.id)
        .where(Chunk.doc_id == doc_id, Chunk.kb_id == doc_kb_id)
    )
    if active_ids:
        stmt = stmt.where(Chunk.document_version_id.in_(active_ids))
    rows = (await db.execute(stmt)).all()
    items: list[RetrievedChunk] = []
    for chunk, doc in rows:
        if len(chunk.content.strip()) < settings.min_content_len:
            continue
        items.append(
            RetrievedChunk(
                chunk_id=chunk.id,
                kb_id=chunk.kb_id,
                doc_id=chunk.doc_id,
                source=doc.filename,
                page=chunk.page,
                section=chunk.section,
                snippet=chunk.content,
                score=0.0,
            )
        )
    items.sort(key=lambda x: x.chunk_id)
    items = items[:cap]
    for i, it in enumerate(items):
        it.rank = i + 1
    return items
