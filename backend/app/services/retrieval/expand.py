"""证据扩展：章节扩展 + 枚举扩展（G6-2 从 rag.py 抽出）。

- 章节扩展：综合型问题命中某章多个子节时，把整章子节纳入引用（防 top_k 截断漏内容）
- 枚举扩展：枚举/清单类问题把「投票最高的章节单元」全部切片纳入（防名单漏成员）
两者都只在本库内、只拉 active 版本切片（P0-2 scope / P0-8 active 过滤）。
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Chunk, Document
from app.services.retrieval.focus import focus_rerank_query
from app.services.retrieval.rerank import rerank
from app.services.retrieval.scope import active_version_ids
from app.services.retrieval.section import (
    ENUMERATION_RE,
    match_section_component,
    under_component,
)

logger = logging.getLogger(__name__)


async def expand_enumeration_sections(
    db: AsyncSession,
    query: str,
    cand_sorted: list[tuple[int, float]],
    section_by_id: dict[int, str | None],
    top_k: int,
    kb_id: int | None = None,
    candidates_full: dict[int, float] | None = None,
) -> list[tuple[int, float]] | None:
    """枚举/概述类问题的证据完备检索：纳入查询最可能指向的章节单元的**全部切片**。

    取代按内容关键词（台账/名单）猜列表的正则补丁，纯结构判定，对任何知识库通用：
    - 看 top-K 候选切片归属哪些「章节单元」（完整章节路径），取出现最多者
      ——「这个问题最关心哪个章节」由检索结果投票，不依赖章节叫什么名；
    - 平局时选库里切片数多的章节（更可能是清单/台账类大单元）。
    再从 DB 拉该章节全部切片（直接归属、排除嵌套附件），保证枚举类回答不遗漏任何成员。
    扩展上限 complete_expansion_cap（默认 40）。
    """
    if not cand_sorted:
        return None
    if not ENUMERATION_RE.search(query):
        return None
    # P0-2 scope 隔离：扩展只在本库内拉取（kb_id=None 跨全部库时不过滤）
    kb_cond = Chunk.kb_id == kb_id if kb_id is not None else None
    # P0-8 active 过滤：DB 同时存 active+retired 版本，扩展只拉 active 版本切片
    active_ids = await active_version_ids(db, kb_id=kb_id)
    # 取候选集：优先 rerank 排序后的候选（cand_sorted 已是 [(cid, rerank_score)] 降序）——
    # rerank 认得正确答案，比融合分投票准。融合分对「数据底板/预警分级/技术保障」等词
    # 区分度差，会把隔壁章（如「5 系统体系架构」「1 总则」）误选成 best_unit。
    # 仅当主流程没产出 rerank 排序结果时才回退融合分（candidates_full）。
    if cand_sorted:
        ranked_pool = list(cand_sorted)[:200]
    elif candidates_full:
        ranked_pool = sorted(candidates_full.items(), key=lambda x: x[1], reverse=True)[:200]
    else:
        return None
    if not ranked_pool:
        return None
    # 补齐章节信息（候选可能超出 top-100 的 section_by_id 范围）
    sec_map: dict[int, str | None] = dict(section_by_id)
    missing = [cid for cid, _ in ranked_pool if cid not in sec_map]
    if missing:
        stmt = select(Chunk.id, Chunk.section).where(Chunk.id.in_(missing))
        if kb_cond is not None:
            stmt = stmt.where(kb_cond)
        if active_ids:
            stmt = stmt.where(Chunk.document_version_id.in_(active_ids))
        rows = (await db.execute(stmt)).all()
        sec_map.update({cid: sec for cid, sec in rows})
    # 全量章节-切片结构表（统计章节大小 + 拉取全量用）；只含 active 版本
    all_stmt = select(Chunk, Document).join(Document, Chunk.doc_id == Document.id)
    if kb_cond is not None:
        all_stmt = all_stmt.where(kb_cond)
    if active_ids:
        all_stmt = all_stmt.where(Chunk.document_version_id.in_(active_ids))
    all_chunks = (await db.execute(all_stmt)).all()
    # 章节单元：按「章前缀」（section 首段）聚合——OCR 噪声子节分散不影响判定。
    # 例：`4 流动阻力与水头损失 / 将J=气代入上式` → `4 流动阻力与水头损失`。
    def _chapter_of(sec: str) -> str:
        parts = [p.strip() for p in sec.split("/") if p.strip()]
        return parts[0] if parts else sec

    size_by_sec: dict[str, int] = {}
    chapter_sizes: dict[str, int] = {}
    for c, _ in all_chunks:
        if not c.section:
            continue
        ch = _chapter_of(c.section)
        chapter_sizes[ch] = chapter_sizes.get(ch, 0) + 1
    # 候选归属的章节，取其章前缀的大小（容忍子节分散）
    for sec in {s for s in sec_map.values() if s}:
        size_by_sec[sec] = chapter_sizes.get(_chapter_of(sec), 0)
    # P1-9 章节相关性：RRF 融合后分数尺度接近，纯计数投票会让「块多但无关」的章节
    # （如预案附则下大量子节）喧宾夺主。改为**排名加权投票**——候选排名越靠前，
    # 对该章节的投票权重越高（top1 权重 1.0，top10 权重 ~0.1）：
    #   score(章节) = Σ 1/(1+rank)  仅候选排名，不依赖分数尺度。
    vote_by_sec: dict[str, float] = {}
    for rank, (cid, _sc) in enumerate(ranked_pool):
        sec = sec_map.get(cid)
        if not sec:
            continue
        unit = _chapter_of(sec)  # 章前缀（容 OCR 噪声子节分散）
        vote_by_sec[unit] = vote_by_sec.get(unit, 0.0) + 1.0 / (1.0 + rank)
    if not vote_by_sec:
        return None
    # 选「加权投票最高」的单元；平局取块数多者
    best_unit = max(vote_by_sec, key=lambda s: (vote_by_sec[s], chapter_sizes.get(s, 0)))
    if chapter_sizes.get(best_unit, 0) < 4:
        logger.debug("枚举扩展：单元 %s 块数 %s < 4，不扩展", best_unit, chapter_sizes.get(best_unit, 0))
        return None  # 单元块太少（≤3）不扩展，top_k 已覆盖
    # 拉取该单元下全部切片（含子节）。枚举扩展要全量列表（含「附件N」子节），
    # 不用 under_component 排除附件（那是章节扩展的防混入逻辑，枚举需完整）。
    members = [
        (c, d)
        for c, d in all_chunks
        if c.section and _chapter_of(c.section) == best_unit
    ]
    members = members[: settings.complete_expansion_cap * 2]
    ids = [c.id for c, _ in members]
    if settings.rerank_enabled:
        docs = [c.content for c, _ in members]
        try:
            scores = await rerank(focus_rerank_query(query), docs)
            ranked = sorted(zip(ids, scores), key=lambda x: x[1], reverse=True)
        except Exception:
            logger.warning("枚举扩展 rerank 失败，按原文顺序", exc_info=True)
            ranked = sorted(zip(ids, [0.0] * len(ids)), key=lambda x: x[0])
    else:
        # 离线/降级：按原文顺序（chunk.id），保证列表不乱序
        ranked = sorted(zip(ids, [0.0] * len(ids)), key=lambda x: x[0])
    return ranked[: settings.complete_expansion_cap]


async def expand_chapter_sections(
    db: AsyncSession,
    query: str,
    cand_sorted: list[tuple[int, float]],
    section_by_id: dict[int, str | None],
    top_k: int,
    kb_id: int | None = None,
) -> list[tuple[int, float]] | None:
    """综合型问题：主题词命中章节的多个子节时，把整章子节纳入最终引用。

    规范类章节横跨多子节多页（如重庆预案「5 应急保障」5.1~5.12），top_k=5 只覆盖
    一小部分 → LLM 必然答不完整。按「与聚焦主题词匹配的章节组件」扩展：
    - 命中整章组件（「5 应急保障」）→ 覆盖该章全部子节；
    - 命中窄组件（「3.2 引用标准」）→ 只扩该组件，不扩大章节；
    - 命中子节数不足 top_k → 维持原 top_k。
    直接从 DB 取该组件下全部切片再 rerank 排序（不受 rerank 候选池限制，
    跨库检索时也不会因候选被挤占而丢子节）。P0-2 scope 隔离：只在本库内扩展。
    返回最多 15 条。
    """
    if not cand_sorted:
        return None
    if not settings.rerank_enabled:
        return None  # 章节扩展依赖 rerank 排序；离线/未开启时跳过，避免无意义网络调用
    top1 = cand_sorted[0][0]
    comp = match_section_component(focus_rerank_query(query), section_by_id.get(top1))
    if not comp:
        return None
    stmt = select(Chunk, Document).join(Document, Chunk.doc_id == Document.id)
    if kb_id is not None:
        stmt = stmt.where(Chunk.kb_id == kb_id)
    # P0-8 active 过滤：扩展只拉 active 版本切片
    active_ids = await active_version_ids(db, kb_id=kb_id)
    if active_ids:
        stmt = stmt.where(Chunk.document_version_id.in_(active_ids))
    rows = (await db.execute(stmt)).all()
    members = [(c, d) for c, d in rows if under_component(c.section, comp)]
    if len(members) < 4:
        return None  # 章节块太少（≤3）不扩展；≥4 则全部纳入，防 top_k 截断漏块（如名单章节 5 块只取 5 块=top_k 也会被旧守卫拦掉）
    ids = [c.id for c, _ in members]
    docs = [c.content for c, _ in members]
    try:
        scores = await rerank(focus_rerank_query(query), docs)
    except Exception:
        logger.warning("章节扩展 rerank 失败，维持原 top_k", exc_info=True)
        return None
    return sorted(zip(ids, scores), key=lambda x: x[1], reverse=True)[:15]
